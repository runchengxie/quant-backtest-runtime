package app

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"golang.org/x/sys/unix"
)

func engineFinancialPlan(ctx context.Context, o Options, r *protocol.Request) (map[string]any, error) {
	plan, e := legacyCall(ctx, o, "plan", []byte(r.CanonicalJSON))
	if e != nil {
		return nil, e
	}
	binding, ok := plan["financial_binding"].(string)
	if !ok || len(binding) != 64 {
		return nil, failure("INTEGRITY_FAILURE", "invalid financial plan")
	}
	if predecessor, ok := r.Execution["resume_from"].(string); ok {
		st, e := store.Open(o.Database, true)
		if e != nil {
			return nil, failure("INVALID_REQUEST", "resume requires existing predecessor database")
		}
		defer st.Close()
		_, e = engineFinancialResume(o, st, predecessor, binding)
		if e != nil {
			return nil, e
		}
	}
	return plan, nil
}

func engineCheckpointBytes(o Options, c store.Checkpoint) ([]byte, error) {
	if !jobIDPattern.MatchString(c.JobID) || !regexp.MustCompile(`^[a-f0-9]{32}$`).MatchString(c.Token) {
		return nil, failure("INTEGRITY_FAILURE", "invalid checkpoint ownership")
	}
	for _, dir := range []string{filepath.Join(o.ResultRoot, ".checkpoints"), filepath.Join(o.ResultRoot, ".checkpoints", c.JobID), filepath.Dir(checkpointPath(o, c))} {
		info, err := os.Lstat(dir)
		if err != nil {
			return nil, err
		}
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, failure("INTEGRITY_FAILURE", "unsafe checkpoint namespace")
		}
	}
	raw, e := regularBytes(checkpointPath(o, c), 64<<20)
	if e != nil {
		return nil, e
	}
	m, e := protocol.DecodeObject(raw)
	if e != nil || hash(raw) != c.Digest || m["schema"] != "runtime-engine-checkpoint.v1" || m["job_id"] != c.JobID || m["token"] != c.Token || m["binding"] != c.Binding || m["cursor"] != json.Number(strconv.Itoa(c.Cursor)) {
		return nil, failure("INTEGRITY_FAILURE", "admitted checkpoint identity mismatch")
	}
	if e = protocol.ValidateEngineCheckpoint(m); e != nil {
		return nil, failure("INTEGRITY_FAILURE", e.Error())
	}
	return raw, nil
}

func engineFinancialResume(o Options, st *store.Store, id, binding string) (store.Checkpoint, error) {
	j, e := st.Get(id)
	if e != nil {
		return store.Checkpoint{}, e
	}
	r, e := persistedEnvelope(j)
	if e != nil || r.SchemaVersion != 9 {
		return store.Checkpoint{}, failure("INVALID_REQUEST", "only v9 failed jobs can resume")
	}
	c, e := st.ResumeCheckpoint(id, binding)
	if e != nil {
		return c, failure("INVALID_REQUEST", e.Error())
	}
	_, e = engineCheckpointBytes(o, c)
	return c, e
}

func admitEngineFinancial(ctx context.Context, o Options, st *store.Store, c store.Checkpoint, raw []byte) error {
	if e := financialFault(ctx, o, c.JobID, "before_write", c.Cursor); e != nil {
		return e
	}
	root := filepath.Dir(checkpointPath(o, c))
	tmp, e := os.CreateTemp(root, ".candidate-")
	if e != nil {
		return e
	}
	defer os.Remove(tmp.Name())
	if _, e = tmp.Write(raw); e == nil {
		e = tmp.Sync()
	}
	closeErr := tmp.Close()
	if e == nil {
		e = closeErr
	}
	if e != nil {
		return e
	}
	if e = financialFault(ctx, o, c.JobID, "after_fsync", c.Cursor); e != nil {
		return e
	}
	if e = unix.Renameat2(unix.AT_FDCWD, tmp.Name(), unix.AT_FDCWD, checkpointPath(o, c), unix.RENAME_NOREPLACE); e != nil {
		return e
	}
	if e = syncDirectory(root); e != nil {
		return e
	}
	if e = financialFault(ctx, o, c.JobID, "after_rename", c.Cursor); e != nil {
		return e
	}
	if _, e = engineCheckpointBytes(o, c); e != nil {
		return e
	}
	ok, e := st.AdmitCheckpoint(c)
	if e != nil {
		return e
	}
	if !ok {
		return failure("INTEGRITY_FAILURE", "checkpoint attempt no longer owns job")
	}
	return financialFault(ctx, o, c.JobID, "after_admission", c.Cursor)
}

func executeEngineFinancialSession(ctx context.Context, o Options, id string, r *protocol.Request) (string, string, error) {
	plan, e := engineFinancialPlan(ctx, o, r)
	if e != nil {
		return "", "", e
	}
	binding := plan["financial_binding"].(string)
	st, e := store.Open(o.Database, false)
	if e != nil {
		return "", "", e
	}
	defer st.Close()
	var prior store.Checkpoint
	if predecessor, ok := r.Execution["resume_from"].(string); ok {
		prior, e = engineFinancialResume(o, st, predecessor, binding)
		if e != nil {
			return "", "", e
		}
	}
	resumed := prior.Cursor
	for _, dir := range []string{filepath.Join(o.ResultRoot, ".checkpoints"), filepath.Join(o.ResultRoot, ".checkpoints", id), filepath.Join(o.ResultRoot, ".checkpoints", id, o.attemptToken)} {
		if e = privateFinancialDir(dir); e != nil {
			return "", "", e
		}
	}
	stage, e := os.MkdirTemp(o.ResultRoot, "."+id+".tmp-")
	if e != nil {
		return "", "", e
	}
	success := false
	defer func() {
		if !success {
			_ = os.RemoveAll(stage)
		}
	}()
	for {

		args := []string{"--staging-path", stage, "--job-id", id, "--token", o.attemptToken, "--binding", binding}
		if prior.Cursor > 0 {
			if _, e = engineCheckpointBytes(o, prior); e != nil {
				return "", "", e
			}
			args = append(args, "--checkpoint", checkpointPath(o, prior), "--checkpoint-sha256", prior.Digest)
		}
		receipt, e := legacyCall(ctx, o, "step", []byte(r.CanonicalJSON), args...)
		if e != nil {
			return "", "", e
		}
		cursor, ok := receipt["cursor"].(float64)
		done, valid := receipt["done"].(bool)
		if !ok || !valid || cursor < 1 || cursor > 65536 || cursor != float64(int(cursor)) || receipt["binding"] != binding || receipt["start_cursor"] != float64(prior.Cursor) {
			return "", "", failure("INTEGRITY_FAILURE", "invalid financial receipt")
		}
		if done {
			if int(cursor) != prior.Cursor {
				return "", "", failure("INTEGRITY_FAILURE", "uncommitted final cursor")
			}
			raw, e := regularBytes(filepath.Join(stage, "financial.json"), 64<<20)
			if e != nil {
				return "", "", e
			}
			if hash(raw) != receipt["sha256"] {
				return "", "", failure("INTEGRITY_FAILURE", "financial result digest mismatch")
			}
			_ = os.Remove(filepath.Join(stage, "candidate.json"))
			manifest := map[string]any{"schema_version": "quant.engine_financial_job_result.v1", "job_id": id, "request_sha256": r.SHA256, "backend": r.Backend, "financial_binding": binding, "resumed_cursor": resumed, "committed_cursor": int(cursor), "financial_sha256": hash(raw), "size_bytes": len(raw)}
			inventory, err := engineInventory(stage)
			if err != nil {
				return "", "", err
			}
			manifest["artifacts"] = inventory
			manifest["product_result"] = receipt["product_result"]
			manifest["product_manifest_sha256"] = plan["product_manifest_sha256"]
			manifest["input_manifest_sha256"] = strings.TrimPrefix(r.Inputs["product_manifest_ref"], "artifact://sha256/")
			encoded, e := protocol.Canonical(manifest)
			if e != nil {
				return "", "", e
			}
			if e = verifyEngineInventory(stage, manifest); e != nil {
				return "", "", e
			}
			if e = os.WriteFile(filepath.Join(stage, "manifest.json"), encoded, 0600); e != nil {
				return "", "", e
			}
			success = true
			return stage, hash(encoded), nil
		}
		if int(cursor) != prior.Cursor+1 {
			return "", "", failure("INTEGRITY_FAILURE", "nonconsecutive financial cursor")
		}
		raw, e := regularBytes(filepath.Join(stage, "candidate.json"), 64<<20)
		if e != nil {
			return "", "", e
		}
		if hash(raw) != receipt["sha256"] {
			return "", "", failure("INTEGRITY_FAILURE", "candidate digest mismatch")
		}
		c := store.Checkpoint{JobID: id, Token: o.attemptToken, Binding: binding, Cursor: int(cursor), Digest: hash(raw)}
		if e = admitEngineFinancial(ctx, o, st, c, raw); e != nil {
			return "", "", e
		}
		prior = c
	}
}

func readEngineFinancialSession(ctx context.Context, o Options, j store.Job, r *protocol.Request) (map[string]any, error) {
	root := filepath.Join(o.ResultRoot, j.JobID)
	raw, e := regularBytes(filepath.Join(root, "manifest.json"), 1<<20)
	if e != nil {
		return nil, e
	}
	m, e := protocol.DecodeObject(raw)
	if e != nil || hash(raw) != *j.ResultSHA256 || m["schema_version"] != "quant.engine_financial_job_result.v1" || m["job_id"] != j.JobID || m["request_sha256"] != r.SHA256 || m["backend"] != r.Backend {
		return nil, failure("INTEGRITY_FAILURE", "minute manifest identity mismatch")
	}
	payload, e := regularBytes(filepath.Join(root, "financial.json"), 64<<20)
	if e != nil {
		return nil, e
	}
	if hash(payload) != m["financial_sha256"] || m["size_bytes"] != json.Number(fmt.Sprint(len(payload))) {
		return nil, failure("INTEGRITY_FAILURE", "minute payload changed")
	}
	if e := verifyEngineInventory(root, m); e != nil {
		return nil, e
	}
	return m, ctx.Err()
}

func engineInventory(root string) ([]map[string]any, error) {
	inventory := []map[string]any{}
	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if entry.IsDir() {
			return nil
		}
		name, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		if name == "candidate.json" || name == "manifest.json" {
			return nil
		}
		raw, err := regularBytes(path, 64<<20)
		if err != nil {
			return err
		}
		inventory = append(inventory, map[string]any{"path": filepath.ToSlash(name), "sha256": hash(raw), "size_bytes": len(raw)})
		return nil
	})
	return inventory, err
}

func verifyEngineInventory(root string, manifest map[string]any) error {
	actual, err := engineInventory(root)
	if err != nil {
		return err
	}
	want, err := protocol.Canonical(manifest["artifacts"])
	if err != nil {
		return err
	}
	got, err := protocol.Canonical(actual)
	if err != nil {
		return err
	}
	if string(want) != string(got) {
		return failure("INTEGRITY_FAILURE", "Engine artifact inventory changed")
	}
	product, ok := manifest["product_result"].(string)
	if !ok || product == "" || filepath.IsAbs(product) || filepath.Clean(product) != product || strings.HasPrefix(product, "..") {
		return failure("INTEGRITY_FAILURE", "invalid product result reference")
	}
	raw, err := regularBytes(filepath.Join(root, product), 64<<20)
	if err != nil {
		return err
	}
	payload, err := regularBytes(filepath.Join(root, "financial.json"), 64<<20)
	if err != nil || string(raw) != string(payload) {
		return failure("INTEGRITY_FAILURE", "product result payload changed")
	}
	value, err := protocol.DecodeObject(raw)
	if err != nil || value["schema"] != "backtest-product-result.v1" || value["status"] != "completed" || value["manifest_sha256"] != manifest["product_manifest_sha256"] {
		return failure("INTEGRITY_FAILURE", "invalid Engine product result")
	}
	provider, ok := value["provider"].(map[string]any)
	if !ok || provider["backend_id"] != "python.minute" || provider["distribution"] != "quant-backtest-core" || provider["version"] != "0.3.0" {
		return failure("INTEGRITY_FAILURE", "Engine product producer mismatch")
	}
	reference, ok := value["raw_result"].(map[string]any)
	if !ok || reference["uri"] != product+".native.json" {
		return failure("INTEGRITY_FAILURE", "Engine original ledger reference mismatch")
	}
	original, err := regularBytes(filepath.Join(root, product+".native.json"), 64<<20)
	if err != nil || hash(original) != reference["sha256"] {
		return failure("INTEGRITY_FAILURE", "Engine original ledger digest mismatch")
	}
	return nil
}
