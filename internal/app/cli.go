// Package app composes protocol, SQLite, and Linux process ownership.
package app

import (
	"context"
	"crypto/sha256"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/runchengxie/quant-backtest-runtime/internal/protocol"
	"github.com/runchengxie/quant-backtest-runtime/internal/store"
	"modernc.org/sqlite"
)

type Options struct {
	Database, ArtifactRoot, ResultRoot, EngineExecutable, PythonExecutable string
	Command, Argument, Key, ExpectedPlan                                   string
	JSONErrors, Queue, Once                                                bool
	Concurrency                                                            int
}
type Error struct{ Code, Message string }

// SourceRevision is set by the immutable release builder.
var SourceRevision = "uncommitted"

func (e *Error) Error() string           { return e.Message }
func failure(code, message string) error { return &Error{code, message} }

//go:embed capabilities.json
var capabilityBytes []byte

func parse(args []string) (Options, error) {
	o := Options{Concurrency: 1}
	values := map[string]*string{"--database": &o.Database, "--artifact-root": &o.ArtifactRoot, "--result-root": &o.ResultRoot, "--engine-executable": &o.EngineExecutable, "--python-executable": &o.PythonExecutable, "--idempotency-key": &o.Key, "--expected-plan-sha256": &o.ExpectedPlan}
	for i := 0; i < len(args); i++ {
		arg := args[i]
		v := ""
		has := false
		if a, b, ok := strings.Cut(arg, "="); ok {
			arg, v, has = a, b, true
		}
		if p, ok := values[arg]; ok {
			if !has {
				i++
				if i >= len(args) {
					return o, failure("INVALID_ARGUMENTS", "missing option value")
				}
				v = args[i]
			}
			*p = v
			continue
		}
		switch arg {
		case "--error-format", "--concurrency":
			if !has {
				i++
				if i >= len(args) {
					return o, failure("INVALID_ARGUMENTS", "missing option value")
				}
				v = args[i]
			}
			if arg == "--error-format" {
				if v != "json" && v != "text" {
					return o, failure("INVALID_ARGUMENTS", "invalid error format")
				}
				o.JSONErrors = v == "json"
			} else {
				n, e := strconv.Atoi(v)
				if e != nil || n < 1 || n > 256 {
					return o, failure("INVALID_ARGUMENTS", "concurrency must be 1-256")
				}
				o.Concurrency = n
			}
		case "--queue":
			o.Queue = true
		case "--once":
			o.Once = true
		case "--help", "-h":
			o.Command = "help"
			return o, nil
		default:
			if strings.HasPrefix(arg, "-") {
				return o, failure("INVALID_ARGUMENTS", "unknown option "+arg)
			}
			if o.Command == "" {
				o.Command = arg
			} else if o.Argument == "" {
				o.Argument = arg
			} else {
				return o, failure("INVALID_ARGUMENTS", "unexpected argument")
			}
		}
	}
	switch o.Command {
	case "help", "capabilities", "version":
		return o, nil
	case "submit", "plan", "status", "inspect", "lookup", "result", "cancel", "recover", "serve", "__worker":
	default:
		return o, failure("INVALID_ARGUMENTS", "unknown command")
	}
	if o.Database == "" || o.ArtifactRoot == "" || o.ResultRoot == "" {
		return o, failure("INVALID_ARGUMENTS", "database, artifact-root and result-root are required")
	}
	for _, p := range []*string{&o.Database, &o.ArtifactRoot, &o.ResultRoot} {
		v, e := filepath.Abs(*p)
		if e != nil {
			return o, e
		}
		*p = v
	}
	if (o.Command == "submit" || o.Command == "plan" || o.Command == "status" || o.Command == "inspect" || o.Command == "result" || o.Command == "cancel" || o.Command == "__worker") && o.Argument == "" {
		return o, failure("INVALID_ARGUMENTS", "command argument is required")
	}
	if o.Command == "lookup" && o.Key == "" {
		return o, failure("INVALID_ARGUMENTS", "idempotency-key is required")
	}
	return o, nil
}

func Run(args []string, out, errout io.Writer) int {
	o, e := parse(args)
	// A malformed option must still preserve an explicit JSON error request.
	for i, a := range args {
		if a == "--error-format=json" || (a == "--error-format" && i+1 < len(args) && args[i+1] == "json") {
			o.JSONErrors = true
		}
	}
	if e == nil && o.Command == "help" {
		fmt.Fprintln(out, "backtest-job --database DB --artifact-root INPUTS --result-root RESULTS [--engine-executable ENGINE] [--python-executable PYTHON] COMMAND\nCommands: capabilities plan submit inspect lookup status result cancel recover serve\nUse submit --queue with serve --concurrency N for scheduler-managed databases.")
		return 0
	}
	var result any
	if e == nil {
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		result, e = dispatch(ctx, o)
	}
	if e != nil {
		code := "INTERNAL_ERROR"
		var a *Error
		var sqlError *sqlite.Error
		var pathError *os.PathError
		switch {
		case errors.As(e, &a):
			code = a.Code
		case errors.Is(e, store.ErrNotFound):
			code = "JOB_NOT_FOUND"
		case errors.Is(e, store.ErrConflict):
			code = "IDEMPOTENCY_CONFLICT"
		case errors.Is(e, os.ErrNotExist):
			code = "INPUT_UNAVAILABLE"
		case errors.As(e, &sqlError), errors.As(e, &pathError):
			code = "STORAGE_FAILURE"
		}
		if o.JSONErrors {
			action := "inspect_inputs_and_state"
			if o.Command == "submit" {
				action = "lookup_before_resubmit"
			}
			_ = json.NewEncoder(errout).Encode(map[string]any{"schema": "quant.agent_error.v1", "code": code, "message": e.Error(), "retryable": false, "suggested_action": action, "job_id": nil, "idempotency_key": nil})
		} else {
			fmt.Fprintln(errout, e)
		}
		if code == "INVALID_ARGUMENTS" {
			return 2
		}
		return 1
	}
	if result != nil {
		if e = json.NewEncoder(out).Encode(result); e != nil {
			fmt.Fprintln(errout, e)
			return 1
		}
	}
	return 0
}

func dispatch(ctx context.Context, o Options) (any, error) {
	if o.Command == "version" {
		return map[string]any{"schema": "quant.runtime_build.v1", "implementation": "go", "source_revision": SourceRevision, "go_version": runtime.Version()}, nil
	}
	if o.Command == "capabilities" {
		var m map[string]any
		if e := json.Unmarshal(capabilityBytes, &m); e != nil {
			return nil, e
		}
		m["implementation"] = "go"
		m["source_revision"] = SourceRevision
		m["resource_limits"] = map[string]any{"memory_mb_scope": "backend_child_address_space", "go_orchestration_memory_included": false, "whole_job_memory_parity": "not_established", "queue_concurrency_scope": "scheduler_managed_jobs"}
		operations, _ := m["operations"].([]any)
		m["operations"] = append(operations, queueCapability())
		return m, nil
	}
	if o.Command == "plan" {
		r, e := loadRequest(ctx, o, o.Argument)
		if e != nil {
			return nil, e
		}
		return buildPlan(ctx, o, r)
	}
	var request *protocol.Request
	if o.Command == "submit" {
		var e error
		request, e = loadRequest(ctx, o, o.Argument)
		if e != nil {
			return nil, e
		}
		p, e := buildPlan(ctx, o, request)
		if e != nil {
			return nil, e
		}
		if o.ExpectedPlan != "" && p["plan_sha256"] != o.ExpectedPlan {
			return nil, failure("PLAN_MISMATCH", "execution plan changed")
		}
	}
	if o.Command == "serve" {
		e := serve(ctx, o)
		return map[string]any{"status": "STOPPED"}, e
	}
	if o.Command == "__worker" {
		return nil, worker(ctx, o)
	}
	readOnly := o.Command == "inspect" || o.Command == "lookup" || o.Command == "result"
	if o.Command == "inspect" || o.Command == "lookup" {
		if info, e := os.Stat(o.Database); e != nil || !info.Mode().IsRegular() {
			return nil, failure("DATABASE_UNAVAILABLE", "existing job database is required")
		}
	}
	st, e := store.Open(o.Database, readOnly)
	if e != nil {
		return nil, e
	}
	defer st.Close()
	switch o.Command {
	case "submit":
		if !o.Queue {
			lock, err := schedulerLock(o.Database, false)
			if err != nil {
				return nil, err
			}
			defer lock.Close()
		}
		if _, e = recoverJobs(st, o); e != nil {
			return nil, e
		}
		job, created, e := st.Submit(request)
		if e != nil {
			return nil, e
		}
		if job.Status == "SUBMITTED" && !o.Queue {
			command, err := startWorker(o, st, job.JobID)
			if err != nil {
				_, _ = st.Finish(job.JobID, "FAILED", "", "", "WORKER_START_FAILED", err.Error())
				return nil, err
			}
			if command != nil {
				_ = command.Process.Release()
			}
		}
		return map[string]any{"job_id": job.JobID, "status": job.Status, "created": created}, nil
	case "recover":
		n, e := recoverJobs(st, o)
		return map[string]any{"recovered": n}, e
	case "status":
		if _, e = recoverJobs(st, o); e != nil {
			return nil, e
		}
		fallthrough
	case "inspect":
		j, e := st.Get(o.Argument)
		return status(j), e
	case "lookup":
		j, e := st.Lookup(o.Key)
		return status(j), e
	case "cancel":
		j, e := st.Cancel(o.Argument)
		return status(j), e
	case "result":
		j, e := st.Get(o.Argument)
		if e != nil {
			return nil, e
		}
		if j.Status != "SUCCEEDED" || j.ResultSHA256 == nil {
			return nil, failure("RESULT_UNAVAILABLE", "job has no successful result")
		}
		return readResult(ctx, o, j)
	}
	return nil, failure("INVALID_ARGUMENTS", "unknown command")
}

func status(j store.Job) map[string]any {
	return map[string]any{"job_id": j.JobID, "status": j.Status, "created_at": j.CreatedAt, "updated_at": j.UpdatedAt, "cancel_requested_at": j.CancelRequestedAt, "result_ref": j.ResultRef, "result_sha256": j.ResultSHA256, "error_code": j.ErrorCode, "error_message": j.ErrorMessage, "worker_pid": j.WorkerPID}
}
func hash(b []byte) string { return fmt.Sprintf("%x", sha256.Sum256(b)) }
func seconds() float64     { return float64(time.Now().UnixNano()) / 1e9 }
