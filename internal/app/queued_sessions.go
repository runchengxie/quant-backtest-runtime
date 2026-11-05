package app

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
)

// queuedInventory rejects links/special files, unexpected paths and unbounded output.
func queuedInventory(ctx context.Context, root string) ([]any, error) {
	inventory := []any{}
	var total int64
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if stopped := ctx.Err(); stopped != nil {
			return stopped
		}
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if d.IsDir() {
			if rel != "." && rel != "stream" {
				return fmt.Errorf("unexpected staged directory")
			}
			return nil
		}
		if rel == "manifest.json" {
			return nil
		}
		name := filepath.Base(path)
		if d.Type()&os.ModeSymlink != 0 || !d.Type().IsRegular() || filepath.Dir(rel) != "stream" || !(name == "journal.json" || name == ".lock" || strings.HasPrefix(name, "report-") || strings.HasPrefix(name, "result-")) {
			return fmt.Errorf("unexpected staged file")
		}
		if len(inventory) >= 4099 {
			return fmt.Errorf("too many staged files")
		}
		raw, err := regularBytes(path, 64<<20)
		if err != nil {
			return err
		}
		total += int64(len(raw))
		if total > 256<<20 {
			return fmt.Errorf("staged output exceeds 256 MiB")
		}
		inventory = append(inventory, map[string]any{"path": filepath.ToSlash(rel), "sha256": hash(raw), "size_bytes": len(raw)})
		return nil
	})
	return inventory, err
}

func queuedVerification(ctx context.Context, o Options, r *protocol.Request, stage string) (map[string]any, error) {
	return legacyCall(ctx, o, "verify", []byte(r.CanonicalJSON), "--staging-path", stage)
}

func executeQueuedSessions(ctx context.Context, o Options, id string, r *protocol.Request) (string, string, error) {
	stage, err := os.MkdirTemp(o.ResultRoot, "."+id+".tmp-")
	if err != nil {
		return "", "", err
	}
	success := false
	defer func() {
		if !success {
			_ = os.RemoveAll(stage)
		}
	}()
	_, err = legacyCall(ctx, o, "stage", []byte(r.CanonicalJSON), "--staging-path", stage)
	if err != nil {
		return "", "", err
	}
	receipt, err := queuedVerification(ctx, o, r, stage)
	if err != nil {
		return "", "", failure("INTEGRITY_FAILURE", err.Error())
	}
	inventory, err := queuedInventory(ctx, stage)
	if err != nil {
		return "", "", failure("INTEGRITY_FAILURE", err.Error())
	}
	manifest := map[string]any{"schema_version": "quant.engine_job_result.v3", "job_id": id, "request_sha256": r.SHA256, "backend": r.Backend, "executable_sha256": r.Config["executable_sha256"], "session_receipt": receipt, "inventory": inventory}
	// Receipts contain a stage-local absolute path. Persist a portable relative name.
	resultPath, ok := receipt["result_path"].(string)
	if !ok || filepath.Dir(resultPath) != filepath.Join(stage, "stream") {
		return "", "", failure("INTEGRITY_FAILURE", "invalid result receipt path")
	}
	receipt["result_path"] = "stream/" + filepath.Base(resultPath)
	raw, err := protocol.Canonical(manifest)
	if err != nil {
		return "", "", err
	}
	raw = append(raw, '\n')
	if err = os.WriteFile(filepath.Join(stage, "manifest.json"), raw, 0600); err != nil {
		return "", "", err
	}
	success = true
	return stage, hash(raw), nil
}

func readQueuedSessions(ctx context.Context, o Options, j store.Job, r *protocol.Request) (map[string]any, error) {
	ctx, cancel := context.WithTimeout(ctx, time.Duration(r.Budgets.WallSeconds)*time.Second)
	defer cancel()
	root := filepath.Join(o.ResultRoot, j.JobID)
	info, err := os.Lstat(root)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, failure("INTEGRITY_FAILURE", "unsafe result root")
	}
	raw, err := regularBytes(filepath.Join(root, "manifest.json"), 2<<20)
	if err != nil {
		return nil, err
	}
	if hash(raw) != *j.ResultSHA256 {
		return nil, failure("INTEGRITY_FAILURE", "manifest digest mismatch")
	}
	manifest, err := protocol.DecodeObject(raw)
	if err != nil {
		return nil, err
	}
	if len(manifest) != 7 || manifest["schema_version"] != "quant.engine_job_result.v3" || manifest["job_id"] != j.JobID || manifest["request_sha256"] != r.SHA256 || manifest["backend"] != r.Backend || manifest["executable_sha256"] != r.Config["executable_sha256"] {
		return nil, failure("INTEGRITY_FAILURE", "result identity mismatch")
	}
	inventory, err := queuedInventory(ctx, root)
	if err != nil {
		return nil, failure("INTEGRITY_FAILURE", err.Error())
	}
	expected, err := protocol.Canonical(inventory)
	if err != nil {
		return nil, err
	}
	actual, err := protocol.Canonical(manifest["inventory"])
	if err != nil || string(expected) != string(actual) {
		return nil, failure("INTEGRITY_FAILURE", "result inventory mismatch")
	}
	receipt, err := queuedVerification(ctx, o, r, root)
	if err != nil {
		return nil, failure("INTEGRITY_FAILURE", err.Error())
	}
	path, ok := receipt["result_path"].(string)
	if !ok || filepath.Dir(path) != filepath.Join(root, "stream") {
		return nil, failure("INTEGRITY_FAILURE", "invalid result receipt path")
	}
	receipt["result_path"] = "stream/" + filepath.Base(path)
	// Decode both mappings uniformly so numeric tokens retain their canonical identity.
	check, _ := protocol.Canonical(receipt)
	decoded, _ := protocol.DecodeObject(check)
	if !reflect.DeepEqual(decoded, manifest["session_receipt"]) {
		return nil, failure("INTEGRITY_FAILURE", "financial receipt mismatch")
	}
	return manifest, nil
}
