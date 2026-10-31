# Go runtime migration

Go owns CLI operations, SQLite state, worker supervision and final result publication. Existing v1–v6 job requests and result manifests remain readable. Python remains an optional stateless adapter for legacy financial computation and its contracts. Rust v5/v6 jobs run without Python.

## Decisions

The native Go implementation accepts v5/v6 requests and validates engine results using exact decimal arithmetic. Legacy requests use an explicitly configured Python interpreter to normalize the existing financial contract and generate staged result files. That adapter never opens the job database or publishes a final directory. Existing Python runtime commands remain available for compatibility and rollback, and must not manage a database concurrently with Go.

Preserve the existing SQLite table and conditional transitions. Do not switch production databases or schedules in this task. A cutover requires quiescing the previous runtime and a backup. Add a persistent single-machine scheduler with an explicit concurrency limit. Keep ordinary submit compatible with immediate detached execution, and expose queue-only submission explicitly for scheduler-managed databases. Do not claim a global limit for immediate mode or permit mixing submission modes during scheduled operation.

Open and hash executable bytes, then execute through the held descriptor. Apply memory limits in a re-exec helper immediately before exec. Use Linux parent-death signals and explicit kill/reap logic. Cancellation and recovery use pidfd identity checks. Fail closed when identity cannot be established. Resource limits cover the trusted worker workload; they are not an untrusted-code sandbox.

Publish through a unique staging directory, fsync files and directories, atomically rename and fsync the parent before committing success. If cancellation wins the database transition, remove unpublished output. Read results by manifest identity and inventory hashes, then validate financial evidence. Reconcile engine input and output clocks, cash, quantities and marks.

## Package interfaces

`internal/protocol` exports Request, Budgets, Decode, ImportCanonical, Canonical, ValidateResult and Summary. Request fields are SchemaVersion, IdempotencyKey, Backend, EvidenceTier, Inputs, Config, Execution, Budgets, Mapping, CanonicalJSON and SHA256. Budgets holds integer WallSeconds and MemoryMB. Decode natively handles v5/v6; ImportCanonical accepts a legacy adapter receipt and verifies its raw canonical bytes against the supplied hash. ValidateResult(request, inputBytes, resultBytes) returns a validated JSON object. Summary(request, result) returns the common financial summary.

`internal/store` exports Open(path, readOnly), Close, Submit(request), Get(id), Lookup(key), Claim(id,pid,leaseSeconds), Finish(id,status,resultRef,digest,code,message), Cancel(id), SetLogs(id,stdout,stderr), Queued(), Expired(now) and ExpireUnclaimed(now). Job uses the existing database field names in JSON and nullable pointers. Submit returns (Job, created, error), Claim/Finish/SetLogs return (bool,error), Cancel returns (Job,error). No financial algorithms belong in this package.

`internal/supervisor` exports Run(context, Config), HandleExec(args), SignalWorker(pid,jobID,signal) and WorkerAlive(pid,jobID). Config holds Executable, SHA256, Args, Input, MemoryMB, Timeout, StdoutLimit and StderrLimit. Run returns Outcome with Stdout, Stderr and ExitCode. Run owns all child handles. HandleExec handles the private CLI re-exec command and applies limits before replacing the process image. SignalWorker uses a pidfd plus the exact worker marker and job ID. Only task-owned processes are signalled in tests.

Root integration implements `cmd/backtest-job`, `internal/app`, the stateless Python compatibility adapter, integration tests, packaging/CI and bilingual documentation.

## Acceptance

Test canonical request hashes against the Python owner, including Unicode and legacy floating-point configuration. Exercise competing claims and cancellation racing completion. Test timeout, bounded output, parent death and recovery with real processes. Verify v5/v6 against the immutable alpha.2 executable and verify legacy fixture execution via the stateless adapter. Queue restart must discover persisted jobs, and a concurrency slot remains occupied until the child has been reaped. Run race detection and existing Python checks. Report remaining incompatibilities explicitly and retain Python deployment until the cutover gates pass.
