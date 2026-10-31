package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"golang.org/x/sys/unix"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"github.com/runchengxie/quant-backtest-runtime/internal/supervisor"
)

var jobIDPattern = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)

func workerArgs(o Options, id string) []string {
	args := []string{"__worker", id, "--database", o.Database, "--artifact-root", o.ArtifactRoot, "--result-root", o.ResultRoot}
	if o.EngineExecutable != "" {
		args = append(args, "--engine-executable", o.EngineExecutable)
	}
	if o.PythonExecutable != "" {
		args = append(args, "--python-executable", o.PythonExecutable)
	}
	return args
}
func startWorker(o Options, st *store.Store, id string) (*exec.Cmd, error) {
	logs := filepath.Join(o.ResultRoot, ".logs")
	if e := os.MkdirAll(logs, 0700); e != nil {
		return nil, e
	}
	stdout := filepath.Join(logs, id+".stdout.log")
	stderr := filepath.Join(logs, id+".stderr.log")
	accepted, e := st.SetLogs(id, stdout, stderr)
	if e != nil || !accepted {
		return nil, e
	}
	out, e := os.OpenFile(stdout, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if e != nil {
		return nil, e
	}
	defer out.Close()
	errout, e := os.OpenFile(stderr, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if e != nil {
		return nil, e
	}
	defer errout.Close()
	self, e := os.Executable()
	if e != nil {
		return nil, e
	}
	cmd := exec.Command(self, workerArgs(o, id)...)
	cmd.Stdout = out
	cmd.Stderr = errout
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LANG=C.UTF-8", "LC_ALL=C.UTF-8"}
	for _, key := range []string{"TMPDIR", "LD_LIBRARY_PATH"} {
		if v, ok := os.LookupEnv(key); ok {
			cmd.Env = append(cmd.Env, key+"="+v)
		}
	}
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	// Scheduler death recovery uses persisted leases. Detached workers survive the short CLI parent.
	// A running scheduler always waits for every child, including graceful shutdown.
	if e = cmd.Start(); e != nil {
		return nil, e
	}
	return cmd, nil
}

func persistedEnvelope(j store.Job) (*protocol.Request, error) {
	if hash([]byte(j.RequestJSON)) != j.RequestSHA256 {
		return nil, failure("REQUEST_HASH_MISMATCH", "persisted request digest mismatch")
	}
	r, e := protocol.ImportCanonical(j.RequestJSON, j.RequestSHA256)
	if e != nil {
		return nil, e
	}
	if r.SchemaVersion >= 5 {
		native, e := protocol.Decode([]byte(j.RequestJSON))
		if e != nil {
			return nil, e
		}
		if native.SHA256 != r.SHA256 {
			return nil, failure("REQUEST_HASH_MISMATCH", "persisted native canonical hash changed")
		}
		return native, nil
	}
	return r, nil
}

func requestForJob(ctx context.Context, o Options, j store.Job) (*protocol.Request, error) {
	r, e := persistedEnvelope(j)
	if e != nil {
		return nil, e
	}
	if r.SchemaVersion >= 5 {
		return r, nil
	}
	receipt, e := legacyCall(ctx, o, "normalize", []byte(j.RequestJSON))
	if e != nil {
		return nil, e
	}
	if receipt["request_sha256"] != j.RequestSHA256 {
		return nil, failure("REQUEST_HASH_MISMATCH", "legacy normalization changed")
	}
	return r, nil
}

func worker(parent context.Context, o Options) error {
	if !jobIDPattern.MatchString(o.Argument) {
		return failure("INVALID_REQUEST", "invalid worker job ID")
	}
	st, e := store.Open(o.Database, false)
	if e != nil {
		return e
	}
	defer st.Close()
	job, e := st.Get(o.Argument)
	if e != nil {
		return e
	}
	if job.Status != "SUBMITTED" {
		return nil
	}
	r, e := persistedEnvelope(job)
	if e != nil {
		_, _ = st.Finish(job.JobID, "FAILED", "", "", "INVALID_PERSISTED_REQUEST", e.Error())
		return e
	}
	claimed, e := st.Claim(job.JobID, os.Getpid(), float64(r.Budgets.WallSeconds+30))
	if e != nil || !claimed {
		return e
	}
	ctx, cancel := context.WithTimeout(parent, time.Duration(r.Budgets.WallSeconds)*time.Second)
	defer cancel()
	stopped := make(chan struct{})
	defer close(stopped)
	go func() {
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-stopped:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				j, e := st.Get(job.JobID)
				if e != nil || j.CancelRequestedAt != nil {
					cancel()
					return
				}
			}
		}
	}()
	// Claim before loading optional Python contracts so startup is cancellable
	// and visible to a restarted scheduler's resource accounting.
	var stage, digest string
	r, e = requestForJob(ctx, o, job)
	startupFailure := e != nil
	if e == nil {
		if _, has := r.Mapping["quant_run_manifest_ref"]; has {
			_, e = buildPlan(ctx, o, r)
		}
	}
	if e == nil {
		stage, digest, e = execute(ctx, o, job.JobID, r)
	}
	current, readErr := st.Get(job.JobID)
	if readErr != nil {
		return readErr
	}
	if current.CancelRequestedAt != nil {
		removeOutput(o, job.JobID)
		_, e = st.Finish(job.JobID, "CANCELLED", "", "", "", "")
		return e
	}
	if e != nil {
		removeOutput(o, job.JobID)
		code := workerFailureCode(e)
		if startupFailure && code != "LEGACY_DEPENDENCY_UNAVAILABLE" && code != "REQUEST_HASH_MISMATCH" {
			code = "INVALID_PERSISTED_REQUEST"
		}
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			code = "JOB_TIMEOUT"
		}
		_, finishErr := st.Finish(job.JobID, "FAILED", "", "", code, e.Error())
		if finishErr != nil {
			return finishErr
		}
		return e
	}
	return publishResult(ctx, o, st, job.JobID, stage, digest, syncTree, syncDirectory)
}

// Files are durable before the success CAS. A failed or uncertain CAS must be
// reconciled with persisted state before removing any published output.
func publishResult(ctx context.Context, o Options, st *store.Store, id, stage, digest string, syncFiles, syncDir func(string) error) error {
	final := filepath.Join(o.ResultRoot, id)
	if _, e := os.Lstat(final); !os.IsNotExist(e) {
		_ = os.RemoveAll(stage)
		_, _ = st.Finish(id, "FAILED", "", "", "PUBLICATION_FAILED", "final directory already exists")
		return failure("PUBLICATION_FAILED", "final directory already exists")
	}
	attempted := false
	e := ctx.Err()
	if e == nil {
		e = syncFiles(stage)
	}
	if e == nil {
		e = ctx.Err()
	}
	if e == nil {
		e = unix.Renameat2(unix.AT_FDCWD, stage, unix.AT_FDCWD, final, unix.RENAME_NOREPLACE)
	}
	if e == nil {
		e = syncDir(o.ResultRoot)
	}
	if e == nil {
		e = ctx.Err()
	}
	if e == nil {
		attempted = true
		var completed bool
		completed, e = st.FinishContext(ctx, id, "SUCCEEDED", "backtest-job://"+id, digest, "", "")
		if completed && e == nil {
			return nil
		}
	}
	current, readErr := st.Get(id)
	if readErr != nil {
		// The commit outcome is unknown; recovery must decide whether files are live.
		return errors.Join(e, readErr)
	}
	if current.Status == "SUCCEEDED" {
		return nil
	}
	if current.Status == "FAILED" || current.Status == "CANCELLED" {
		_ = os.RemoveAll(stage)
		_ = os.RemoveAll(final)
		_ = syncDir(o.ResultRoot)
		return e
	}
	// Preserve output on storage errors unless cancellation/deadline establishes
	// a terminal outcome that can be committed before cleanup.
	status, code, message := "FAILED", "PUBLICATION_FAILED", "publication did not commit"
	if e != nil {
		message = e.Error()
	}
	if current.CancelRequestedAt != nil {
		status, code = "CANCELLED", ""
	} else if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		code = "JOB_TIMEOUT"
	} else if ctx.Err() != nil {
		code = "WORKER_INTERRUPTED"
	} else if e != nil && attempted {
		return e
	}
	done, finishErr := st.Finish(id, status, "", "", code, message)
	if finishErr != nil {
		return errors.Join(e, finishErr)
	}
	if done {
		_ = os.RemoveAll(stage)
		_ = os.RemoveAll(final)
		_ = syncDir(o.ResultRoot)
	}
	return e
}

func workerFailureCode(err error) string {
	var typed *Error
	if errors.As(err, &typed) {
		switch typed.Code {
		case "LEGACY_DEPENDENCY_UNAVAILABLE", "CAPABILITY_MISMATCH", "RESOURCE_LIMIT_UNAVAILABLE", "REQUEST_HASH_MISMATCH", "INPUT_ARTIFACT_MISSING", "BACKTEST_REJECTED", "BACKEND_EXECUTION_FAILED":
			return typed.Code
		case "INPUT_UNAVAILABLE":
			return "INPUT_ARTIFACT_MISSING"
		case "INVALID_REQUEST", "ENGINE_REQUEST_INVALID", "INTEGRITY_FAILURE":
			return "BACKTEST_REJECTED"
		}
	}
	if errors.Is(err, os.ErrNotExist) {
		return "INPUT_ARTIFACT_MISSING"
	}
	return "BACKEND_EXECUTION_FAILED"
}

func execute(ctx context.Context, o Options, id string, r *protocol.Request) (string, string, error) {
	if e := os.MkdirAll(o.ResultRoot, 0700); e != nil {
		return "", "", e
	}
	if r.SchemaVersion < 5 {
		receipt, e := legacyCall(ctx, o, "stage", []byte(r.CanonicalJSON), "--job-id", id)
		if e != nil {
			return "", "", e
		}
		path, ok := receipt["staging_path"].(string)
		digest, ok2 := receipt["manifest_sha256"].(string)
		if !ok || !ok2 || filepath.Dir(path) != o.ResultRoot || !strings.HasPrefix(filepath.Base(path), "."+id+".tmp-") {
			return "", "", failure("INTEGRITY_FAILURE", "invalid adapter staging receipt")
		}
		if _, e = legacyCall(ctx, o, "verify", []byte(r.CanonicalJSON), "--job-id", id, "--manifest-sha256", digest, "--staging-path", path); e != nil {
			_ = os.RemoveAll(path)
			return "", "", e
		}
		return path, digest, nil
	}
	path, e := resolveArtifact(o.ArtifactRoot, r.Inputs["engine_request_ref"])
	if e != nil {
		return "", "", e
	}
	input, e := regularBytes(path, 64<<20)
	if e != nil {
		return "", "", e
	}
	if _, e = protocol.DecodeObject(input); e != nil {
		return "", "", failure("INVALID_REQUEST", e.Error())
	}
	if hash(input) != strings.TrimPrefix(r.Inputs["engine_request_ref"], "artifact://sha256/") {
		return "", "", failure("INTEGRITY_FAILURE", "input changed before execution")
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
	requestFile := filepath.Join(stage, ".input.json")
	if e = os.WriteFile(requestFile, input, 0600); e != nil {
		return "", "", e
	}
	digest, _ := r.Config["executable_sha256"].(string)
	output, e := supervisor.Run(ctx, supervisor.Config{Executable: o.EngineExecutable, SHA256: digest, Args: []string{"run", requestFile}, MemoryMB: r.Budgets.MemoryMB, Timeout: time.Duration(r.Budgets.WallSeconds) * time.Second, StdoutLimit: 64 << 20, StderrLimit: 8192})
	_ = os.Remove(requestFile)
	if e != nil {
		return "", "", fmt.Errorf("engine process failed: %w: %s", e, output.Stderr)
	}
	result, e := protocol.ValidateResult(r, input, output.Stdout)
	if e != nil {
		return "", "", failure("INTEGRITY_FAILURE", e.Error())
	}
	resultPath := filepath.Join(stage, "engine_result.json")
	if e = os.WriteFile(resultPath, output.Stdout, 0600); e != nil {
		return "", "", e
	}
	schema := "quant.engine_job_result.v1"
	if r.SchemaVersion == 6 {
		schema = "quant.engine_job_result.v2"
	}
	manifest := map[string]any{"schema_version": schema, "job_id": id, "request_sha256": r.SHA256, "backend": r.Backend, "executable_sha256": digest, "summary": protocol.Summary(r, result), "inventory": []any{map[string]any{"path": "engine_result.json", "sha256": hash(output.Stdout), "size_bytes": len(output.Stdout)}}}
	encoded, e := protocol.Canonical(manifest)
	if e != nil {
		return "", "", e
	}
	encoded = append(encoded, '\n')
	if e = os.WriteFile(filepath.Join(stage, "manifest.json"), encoded, 0600); e != nil {
		return "", "", e
	}
	success = true
	return stage, hash(encoded), nil
}

func syncDirectory(path string) error {
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer f.Close()
	return f.Sync()
}
func syncTree(root string) error {
	dirs := []string{}
	e := filepath.WalkDir(root, func(path string, d os.DirEntry, e error) error {
		if e != nil {
			return e
		}
		if d.Type()&os.ModeSymlink != 0 {
			return failure("INTEGRITY_FAILURE", "result symlinks are forbidden")
		}
		if d.IsDir() {
			dirs = append(dirs, path)
			return nil
		}
		if !d.Type().IsRegular() {
			return failure("INTEGRITY_FAILURE", "result must contain regular files")
		}
		f, e := os.Open(path)
		if e != nil {
			return e
		}
		defer f.Close()
		return f.Sync()
	})
	if e != nil {
		return e
	}
	for i := len(dirs) - 1; i >= 0; i-- {
		if e = syncDirectory(dirs[i]); e != nil {
			return e
		}
	}
	return nil
}

func readResult(ctx context.Context, o Options, j store.Job) (map[string]any, error) {
	if !jobIDPattern.MatchString(j.JobID) || j.ResultSHA256 == nil {
		return nil, failure("INTEGRITY_FAILURE", "invalid result identity")
	}
	r, e := requestForJob(ctx, o, j)
	if e != nil {
		return nil, e
	}
	if r.SchemaVersion < 5 {
		return legacyCall(ctx, o, "verify", []byte(r.CanonicalJSON), "--job-id", j.JobID, "--manifest-sha256", *j.ResultSHA256)
	}
	root := filepath.Join(o.ResultRoot, j.JobID)
	info, e := os.Lstat(root)
	if e != nil {
		return nil, e
	}
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, failure("INTEGRITY_FAILURE", "unsafe result directory")
	}
	b, e := regularBytes(filepath.Join(root, "manifest.json"), 1<<20)
	if e != nil {
		return nil, e
	}
	if hash(b) != *j.ResultSHA256 {
		return nil, failure("INTEGRITY_FAILURE", "manifest digest mismatch")
	}
	manifest, e := protocol.DecodeObject(b)
	if e != nil {
		return nil, e
	}
	schema := "quant.engine_job_result.v1"
	if r.SchemaVersion == 6 {
		schema = "quant.engine_job_result.v2"
	}
	if manifest["schema_version"] != schema || manifest["job_id"] != j.JobID || manifest["request_sha256"] != j.RequestSHA256 || manifest["backend"] != r.Backend || manifest["executable_sha256"] != r.Config["executable_sha256"] {
		return nil, failure("INTEGRITY_FAILURE", "result identity mismatch")
	}
	inventory, ok := manifest["inventory"].([]any)
	if !ok || len(inventory) != 1 {
		return nil, failure("INTEGRITY_FAILURE", "invalid result inventory")
	}
	item, ok := inventory[0].(map[string]any)
	if !ok || item["path"] != "engine_result.json" {
		return nil, failure("INTEGRITY_FAILURE", "invalid result path")
	}
	raw, e := regularBytes(filepath.Join(root, "engine_result.json"), 64<<20)
	if e != nil {
		return nil, e
	}
	size, ok := item["size_bytes"].(json.Number)
	if !ok || string(size) != fmt.Sprint(len(raw)) || item["sha256"] != hash(raw) {
		return nil, failure("INTEGRITY_FAILURE", "result content digest mismatch")
	}
	inputPath, e := resolveArtifact(o.ArtifactRoot, r.Inputs["engine_request_ref"])
	if e != nil {
		return nil, e
	}
	input, e := regularBytes(inputPath, 64<<20)
	if e != nil {
		return nil, e
	}
	result, e := protocol.ValidateResult(r, input, raw)
	if e != nil {
		return nil, failure("INTEGRITY_FAILURE", e.Error())
	}
	expected, e := protocol.Canonical(protocol.Summary(r, result))
	if e != nil {
		return nil, e
	}
	actual, e := protocol.Canonical(manifest["summary"])
	if e != nil {
		return nil, e
	}
	if string(expected) != string(actual) {
		return nil, failure("INTEGRITY_FAILURE", "summary differs from financial result")
	}
	return manifest, nil
}

func recoverJobs(st *store.Store, o Options) (int, error) {
	now := seconds()
	n, e := st.ExpireUnclaimed(now)
	if e != nil {
		return n, e
	}
	jobs, e := st.Expired(now)
	if e != nil {
		return n, e
	}
	for _, j := range jobs {
		if j.WorkerPID != nil {
			alive, e := supervisor.WorkerAlive(*j.WorkerPID, j.JobID)
			if errors.Is(e, supervisor.ErrIdentityMismatch) {
				alive = false
				e = nil
			}
			if e != nil {
				continue
			}
			if alive {
				if e = supervisor.SignalWorker(*j.WorkerPID, j.JobID, syscall.SIGKILL); e != nil {
					continue
				}
				deadline := time.Now().Add(2 * time.Second)
				for alive && time.Now().Before(deadline) {
					time.Sleep(10 * time.Millisecond)
					alive, e = supervisor.WorkerAlive(*j.WorkerPID, j.JobID)
					if errors.Is(e, supervisor.ErrIdentityMismatch) {
						alive = false
						e = nil
					}
					if e != nil {
						break
					}
				}
				if e != nil || alive {
					continue
				}
			}
		}
		state := "FAILED"
		code := "WORKER_LEASE_EXPIRED"
		if j.CancelRequestedAt != nil {
			state = "CANCELLED"
			code = ""
		}
		done, e := st.Finish(j.JobID, state, "", "", code, "worker lease expired")
		if e != nil {
			return n, e
		}
		if done {
			n++
			removeOutput(o, j.JobID)
		}
	}
	cleanup, e := st.Unpublished()
	if e != nil {
		return n, e
	}
	for _, j := range cleanup {
		removeOutput(o, j.JobID)
	}
	return n, nil
}
func removeOutput(o Options, id string) {
	if !jobIDPattern.MatchString(id) {
		return
	}
	_ = os.RemoveAll(filepath.Join(o.ResultRoot, id))
	paths, _ := filepath.Glob(filepath.Join(o.ResultRoot, "."+id+".tmp-*"))
	for _, path := range paths {
		if info, e := os.Lstat(path); e == nil && info.IsDir() && info.Mode()&os.ModeSymlink == 0 {
			_ = os.RemoveAll(path)
		}
	}
}
