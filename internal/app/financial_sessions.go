package app

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"golang.org/x/sys/unix"
)

var financialFaultPattern = regexp.MustCompile(`^(before_write|after_fsync|after_rename|after_admission):[1-9][0-9]{0,4}$`)

func financialPlan(ctx context.Context, o Options, r *protocol.Request) (map[string]any, error) {
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
		_, e = financialResume(o, st, predecessor, binding)
		if e != nil {
			return nil, e
		}
	}
	return plan, nil
}

func checkpointPath(o Options, c store.Checkpoint) string {
	return filepath.Join(o.ResultRoot, ".checkpoints", c.JobID, c.Token, strconv.Itoa(c.Cursor)+".json")
}

func checkpointBytes(o Options, c store.Checkpoint) ([]byte, error) {
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
	if e != nil || hash(raw) != c.Digest || m["schema"] != "runtime-minute-checkpoint.v1" || m["job_id"] != c.JobID || m["token"] != c.Token || m["binding"] != c.Binding || m["cursor"] != json.Number(strconv.Itoa(c.Cursor)) {
		return nil, failure("INTEGRITY_FAILURE", "admitted checkpoint identity mismatch")
	}
	inner, ok := m["checkpoint"].(map[string]any)
	if !ok || inner["schema"] != "minute-session.v2" || inner["cursor"] != m["cursor"] {
		return nil, failure("INTEGRITY_FAILURE", "unsupported owner checkpoint")
	}
	return raw, nil
}

func financialResume(o Options, st *store.Store, id, binding string) (store.Checkpoint, error) {
	j, e := st.Get(id)
	if e != nil {
		return store.Checkpoint{}, e
	}
	r, e := persistedEnvelope(j)
	if e != nil || r.SchemaVersion != 8 {
		return store.Checkpoint{}, failure("INVALID_REQUEST", "only v8 failed jobs can resume")
	}
	c, e := st.ResumeCheckpoint(id, binding)
	if e != nil {
		return c, failure("INVALID_REQUEST", e.Error())
	}
	_, e = checkpointBytes(o, c)
	return c, e
}

func privateFinancialDir(path string) error {
	if e := os.Mkdir(path, 0700); e != nil && !os.IsExist(e) {
		return e
	}
	info, e := os.Lstat(path)
	if e != nil {
		return e
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return failure("INTEGRITY_FAILURE", "unsafe checkpoint directory")
	}
	return syncDirectory(filepath.Dir(path))
}

// A bounded operator-only pause makes crash boundaries observable without running injected code.
func financialFault(ctx context.Context, o Options, id, phase string, cursor int) error {
	if o.FinancialFault != phase+":"+strconv.Itoa(cursor) {
		return ctx.Err()
	}
	raw, _ := json.Marshal(map[string]any{"phase": phase, "cursor": cursor, "pid": os.Getpid()})
	if e := os.WriteFile(filepath.Join(o.ResultRoot, ".checkpoints", id, "fault.json"), raw, 0600); e != nil {
		return e
	}
	<-ctx.Done()
	return ctx.Err()
}

func admitFinancial(ctx context.Context, o Options, st *store.Store, c store.Checkpoint, raw []byte) error {
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
	if _, e = checkpointBytes(o, c); e != nil {
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

func executeFinancialSession(ctx context.Context, o Options, id string, r *protocol.Request) (string, string, error) {
	plan, e := financialPlan(ctx, o, r)
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
		prior, e = financialResume(o, st, predecessor, binding)
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
		if e = financialFault(ctx, o, id, "before_write", prior.Cursor+1); e != nil {
			return "", "", e
		}
		args := []string{"--staging-path", stage, "--job-id", id, "--token", o.attemptToken, "--binding", binding}
		if prior.Cursor > 0 {
			if _, e = checkpointBytes(o, prior); e != nil {
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
			manifest := map[string]any{"schema_version": "quant.minute_job_result.v1", "job_id": id, "request_sha256": r.SHA256, "backend": r.Backend, "financial_binding": binding, "resumed_cursor": resumed, "committed_cursor": int(cursor), "financial_sha256": hash(raw), "size_bytes": len(raw)}
			encoded, e := protocol.Canonical(manifest)
			if e != nil {
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
		if e = admitFinancial(ctx, o, st, c, raw); e != nil {
			return "", "", e
		}
		prior = c
	}
}

func readFinancialSession(ctx context.Context, o Options, j store.Job, r *protocol.Request) (map[string]any, error) {
	root := filepath.Join(o.ResultRoot, j.JobID)
	raw, e := regularBytes(filepath.Join(root, "manifest.json"), 1<<20)
	if e != nil {
		return nil, e
	}
	m, e := protocol.DecodeObject(raw)
	if e != nil || hash(raw) != *j.ResultSHA256 || m["schema_version"] != "quant.minute_job_result.v1" || m["job_id"] != j.JobID || m["request_sha256"] != r.SHA256 || m["backend"] != r.Backend {
		return nil, failure("INTEGRITY_FAILURE", "minute manifest identity mismatch")
	}
	payload, e := regularBytes(filepath.Join(root, "financial.json"), 64<<20)
	if e != nil {
		return nil, e
	}
	if hash(payload) != m["financial_sha256"] || m["size_bytes"] != json.Number(fmt.Sprint(len(payload))) {
		return nil, failure("INTEGRITY_FAILURE", "minute payload changed")
	}
	return m, ctx.Err()
}
