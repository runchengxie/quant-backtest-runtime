package app

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
)

func regularBytes(path string, limit int64) ([]byte, error) {
	info, e := os.Lstat(path)
	if e != nil {
		return nil, e
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, failure("INVALID_REQUEST", "expected a regular file within size limit")
	}
	f, e := os.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, e
	}
	defer f.Close()
	b, e := io.ReadAll(io.LimitReader(f, limit+1))
	if e != nil {
		return nil, e
	}
	if int64(len(b)) > limit {
		return nil, failure("INVALID_REQUEST", "input exceeded size limit")
	}
	return b, nil
}
func resolveArtifact(root, ref string) (string, error) {
	const prefix = "artifact://sha256/"
	if !strings.HasPrefix(ref, prefix) || len(ref) != len(prefix)+64 {
		return "", failure("INVALID_REQUEST", "invalid artifact reference")
	}
	digest := strings.TrimPrefix(ref, prefix)
	if strings.Trim(digest, "0123456789abcdef") != "" {
		return "", failure("INVALID_REQUEST", "invalid artifact digest")
	}
	path := filepath.Join(root, "sha256", digest)
	info, e := os.Lstat(path)
	if e != nil {
		return "", e
	}
	if !info.Mode().IsRegular() {
		return "", failure("INTEGRITY_FAILURE", "artifact is not a regular file")
	}
	f, e := os.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if e != nil {
		return "", e
	}
	defer f.Close()
	h := sha256.New()
	if _, e = io.Copy(h, f); e != nil {
		return "", e
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != digest {
		return "", failure("INTEGRITY_FAILURE", "artifact digest mismatch")
	}
	return path, nil
}
func legacyCall(ctx context.Context, o Options, operation string, request []byte, extra ...string) (map[string]any, error) {
	if o.PythonExecutable == "" || !filepath.IsAbs(o.PythonExecutable) {
		return nil, failure("LEGACY_DEPENDENCY_UNAVAILABLE", "configure --python-executable with an installed quant-backtest-runtime[legacy] environment")
	}
	file, e := os.OpenFile(o.PythonExecutable, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, e
	}
	info, e := file.Stat()
	if e != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0111 == 0 {
		file.Close()
		return nil, failure("LEGACY_DEPENDENCY_UNAVAILABLE", "Python interpreter must be an executable regular file")
	}
	binary, e := io.ReadAll(file)
	file.Close()
	if e != nil {
		return nil, e
	}
	tmp, e := os.CreateTemp("", "backtest-go-request-*.json")
	if e != nil {
		return nil, e
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, e = tmp.Write(request); e != nil {
		tmp.Close()
		return nil, e
	}
	if e = tmp.Close(); e != nil {
		return nil, e
	}
	args := []string{"-m", "backtest_runtime.go_adapter", operation, "--request", name, "--artifact-root", o.ArtifactRoot, "--result-root", o.ResultRoot}
	if o.EngineExecutable != "" {
		args = append(args, "--engine-executable", o.EngineExecutable)
	}
	args = append(args, extra...)
	timeout := time.Minute
	memory := 0
	if operation == "stage" {
		var parsed struct {
			Budgets protocol.Budgets `json:"budgets"`
		}
		if e = json.Unmarshal(request, &parsed); e != nil {
			return nil, e
		}
		timeout = time.Duration(parsed.Budgets.WallSeconds) * time.Second
		memory = parsed.Budgets.MemoryMB
	}
	output, e := supervisor.Run(ctx, supervisor.Config{Executable: o.PythonExecutable, SHA256: hash(binary), Args: args, MemoryMB: memory, Timeout: timeout, StdoutLimit: 1 << 20, StderrLimit: 8192})
	if e != nil {
		if receipt, decodeErr := protocol.DecodeObject(output.Stderr); decodeErr == nil {
			if code, ok := receipt["code"].(string); ok {
				message, _ := receipt["message"].(string)
				return nil, failure(code, message)
			}
		}
		return nil, failure("LEGACY_DEPENDENCY_UNAVAILABLE", fmt.Sprintf("legacy adapter %s failed: %s: %s", operation, e, output.Stderr))
	}
	var m map[string]any
	if e = json.Unmarshal(output.Stdout, &m); e != nil {
		return nil, e
	}
	return m, nil
}
func loadRequest(ctx context.Context, o Options, path string) (*protocol.Request, error) {
	b, e := regularBytes(path, 65536)
	if e != nil {
		return nil, e
	}
	if _, e = protocol.DecodeObject(b); e != nil {
		return nil, failure("INVALID_REQUEST", e.Error())
	}
	var marker struct {
		SchemaVersion int `json:"schema_version"`
		Manifest      any `json:"quant_run_manifest_ref"`
	}
	if e = json.Unmarshal(b, &marker); e != nil {
		return nil, failure("INVALID_REQUEST", e.Error())
	}
	if marker.SchemaVersion >= 5 && marker.SchemaVersion <= 6 {
		r, e := protocol.Decode(b)
		if e != nil {
			return nil, failure("INVALID_REQUEST", e.Error())
		}
		if marker.Manifest != nil && o.PythonExecutable == "" {
			return nil, failure("LEGACY_DEPENDENCY_UNAVAILABLE", "root manifests require the optional Python contract adapter")
		}
		return r, nil
	}
	if marker.SchemaVersion < 1 || marker.SchemaVersion > 4 {
		return nil, failure("INVALID_REQUEST", "unsupported request schema")
	}
	receipt, e := legacyCall(ctx, o, "normalize", b)
	if e != nil {
		return nil, e
	}
	canonical, ok := receipt["request_json"].(string)
	digest, ok2 := receipt["request_sha256"].(string)
	if !ok || !ok2 {
		return nil, failure("INVALID_REQUEST", "invalid compatibility receipt")
	}
	return protocol.ImportCanonical(canonical, digest)
}

func buildPlan(ctx context.Context, o Options, r *protocol.Request) (map[string]any, error) {
	if _, has := r.Mapping["quant_run_manifest_ref"]; has || r.SchemaVersion < 5 {
		return legacyCall(ctx, o, "plan", []byte(r.CanonicalJSON))
	}
	inputs := map[string]any{}
	for key, ref := range r.Inputs {
		if _, e := resolveArtifact(o.ArtifactRoot, ref); e != nil {
			return nil, e
		}
		inputs[key] = strings.TrimPrefix(ref, "artifact://sha256/")
	}
	inputPath, e := resolveArtifact(o.ArtifactRoot, r.Inputs["engine_request_ref"])
	if e != nil {
		return nil, e
	}
	limit := int64(64 << 20)
	if r.SchemaVersion == 5 {
		limit = 1 << 20
	}
	input, e := regularBytes(inputPath, limit)
	if e != nil {
		return nil, failure("ENGINE_REQUEST_INVALID", e.Error())
	}
	if hash(input) != inputs["engine_request_ref"] {
		return nil, failure("INTEGRITY_FAILURE", "engine input changed during preflight")
	}
	if _, e = protocol.DecodeObject(input); e != nil {
		return nil, failure("ENGINE_REQUEST_INVALID", e.Error())
	}
	digest, _ := r.Config["executable_sha256"].(string)
	if !filepath.IsAbs(o.EngineExecutable) {
		return nil, failure("INVALID_REQUEST", "absolute engine executable is required")
	}
	f, e := os.OpenFile(o.EngineExecutable, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, e
	}
	info, statErr := f.Stat()
	if statErr != nil || !info.Mode().IsRegular() || info.Mode().Perm()&0111 == 0 {
		f.Close()
		return nil, failure("ENGINE_UNAVAILABLE", "engine must be an executable regular file")
	}
	h := sha256.New()
	_, e = io.Copy(h, f)
	f.Close()
	if e != nil {
		return nil, e
	}
	if fmt.Sprintf("%x", h.Sum(nil)) != digest {
		return nil, failure("INTEGRITY_FAILURE", "engine executable digest mismatch")
	}
	var validation map[string]any
	if r.SchemaVersion == 5 {
		deadline, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		cfg := supervisor.Config{Executable: o.EngineExecutable, SHA256: digest, Args: []string{"capabilities"}, Timeout: 10 * time.Second, StdoutLimit: 8192, StderrLimit: 8192}
		cap, e := supervisor.Run(deadline, cfg)
		if e != nil {
			return nil, failure("ENGINE_VALIDATION_UNAVAILABLE", e.Error())
		}
		capabilities, parseErr := protocol.DecodeObject(cap.Stdout)
		if parseErr != nil || capabilities["schema_version"] != "engine_capabilities.v1" || capabilities["request_schema"] != "engine_request.v1" || capabilities["validator_version"] != json.Number("1") {
			return nil, failure("ENGINE_VALIDATION_UNAVAILABLE", "static validator version 1 required")
		}
		cfg.Args = []string{"validate", "-"}
		cfg.Input = input
		receipt, e := supervisor.Run(deadline, cfg)
		if e != nil {
			code := "ENGINE_VALIDATION_UNAVAILABLE"
			if receipt.ExitCode == 2 {
				code = "ENGINE_REQUEST_INVALID"
			}
			return nil, failure(code, "engine static validation failed")
		}
		validation, e = protocol.DecodeObject(receipt.Stdout)
		if e != nil || len(validation) != 5 || validation["schema_version"] != "engine_validation.v1" || validation["validator_version"] != json.Number("1") || validation["request_schema"] != "engine_request.v1" || validation["valid"] != true || validation["input_sha256"] != hash(input) {
			return nil, failure("ENGINE_VALIDATION_UNAVAILABLE", "invalid static validation receipt")
		}
	}
	semantics := "not_checked"
	if validation != nil {
		semantics = "passed"
	}
	plan := map[string]any{"schema": "quant.backtest_plan.v1", "request": r.Mapping, "request_sha256": r.SHA256, "backend": r.Backend, "inputs": inputs, "budgets": r.Budgets, "executable_sha256": digest, "effects": []string{"job_insert", "worker_execution", "result_publication"}, "checks": map[string]any{"request_structure": "passed", "input_hashes": "passed", "root_manifest": "not_applicable", "executable_hash": "passed", "engine_request_semantics": semantics, "parquet_rows": "not_checked", "clock_table_consistency": "not_checked", "resource_enforcement": "not_checked", "historical_pit": "not_checked", "economic_validity": "not_checked", "promotion": "not_checked"}}
	if validation != nil {
		plan["engine_validation"] = validation
	}
	encoded, e := protocol.Canonical(plan)
	if e != nil {
		return nil, e
	}
	plan["plan_sha256"] = hash(encoded)
	return plan, nil
}
