# Go runtime

[中文页面](go-runtime.zh-CN.md)

The Go `backtest-job` CLI owns SQLite state, worker supervision, scheduling and final result publication. Native v5/v6 jobs call the independently installed Rust engine without Python. Deployment configuration selects the active implementation.

## Build and validate

Use Go 1.27.3 and the checked-in module lockfiles. Keep build outputs outside the repository.

```sh
go build -o /data/runtime-build/backtest-job ./cmd/backtest-job
go vet ./...
go test -race ./...
```

The integration suite uses `BACKTEST_GO_EXECUTABLE` for the absolute Go CLI path and `BACKTEST_ENGINE_TEST_EXECUTABLE` for the absolute engine path. Run it in the locked legacy Python development environment to exercise both implementations.

```sh
BACKTEST_GO_EXECUTABLE=/data/runtime-build/backtest-job \
BACKTEST_ENGINE_TEST_EXECUTABLE=/releases/engine/quant-backtest-engine \
uv run --locked --extra legacy pytest -q tests/test_go_runtime.py
```

CI also runs native protocol tests against its pinned engine source revision. These checks provide fixture-specific evidence. They do not establish compatibility with every historical job or deployment.

For an immutable local release, use `scripts/release.py stage COMMIT --implementation go` with the documented `--repo` and `--production` paths. The commit must already belong to `origin/main`. The helper builds `bin/backtest-job`, records the source revision in `version`, and writes the readiness marker after a successful build. Staging does not switch the active release. The helper retains Python as its default implementation for existing deployments.

## Check a staged release

Run `scripts/smoke_native.py` with Python 3.12 or newer. It uses only the standard library and needs no installed Python runtime package. Supply a synthetic fixture from the reviewed engine release and a new output directory outside the source tree.

```sh
python3 /releases/runtime/COMMIT/scripts/smoke_native.py \
  --executable /releases/runtime/COMMIT/bin/backtest-job \
  --expected-runtime-revision COMMIT \
  --engine-executable /releases/engine/quant-backtest-engine \
  --engine-request /releases/engine/fixtures/sessions.json \
  --root /data/runtime-checks/unique-check
```

Replace `COMMIT` with the full merged runtime revision. The check compares the build identity before creating state, then submits a plan-bound queued job. It verifies duplicate submission and receipt lookup, cancels a second queued job, drains the queue, and asks the runtime to verify the published result. It leaves `receipt.json`, the synthetic database and artifacts for inspection. The receipt records executable digests and the result hash. Existing output directories are rejected, including empty ones. A passing check covers the supplied fixture. Review its financial expectations separately.

## Limit a scheduler service

On a host with a working systemd user manager and memory/CPU controllers, a transient service can constrain the scheduler and its descendants together. Run the native check or an isolated queue within this service before using the settings for real jobs.

```sh
systemd-run --user --wait --pipe --collect \
  --unit=runtime-native-check-UNIQUE \
  -p MemoryMax=2G -p MemorySwapMax=0 -p CPUQuota=200% \
  python3 /releases/runtime/COMMIT/scripts/smoke_native.py \
  --executable /releases/runtime/COMMIT/bin/backtest-job \
  --expected-runtime-revision COMMIT \
  --engine-executable /releases/engine/quant-backtest-engine \
  --engine-request /releases/engine/fixtures/sessions.json \
  --root /data/runtime-checks/unique-cgroup-check
```

These values are an example for a synthetic check. Choose production limits from measured workload peaks and leave room for the scheduler. Verify the effective `memory.max`, `memory.swap.max` and `cpu.max` in the service's cgroup. A separate controlled allocation probe should verify that a child exceeding the memory boundary is killed. Keep its inputs and outputs isolated from real tasks.

For a persistent queue, launch `backtest-job ... serve --concurrency N` as the service command. Submit only queued jobs from outside that service. Workers launched by the scheduler inherit its cgroup. Immediate submissions from elsewhere do not. The service memory ceiling covers the whole queue process tree. Individual jobs retain their separate child address-space budgets. An out-of-memory event can interrupt several jobs, so inspect their processes and use `recover` after restarting. For per-job aggregate limits, enable the delegated mode below.

## Submit a native job

Pass the installed engine's absolute path through `--engine-executable`. The job's `config.executable_sha256` pins its bytes. The runtime hashes an open executable and executes that held descriptor, so replacing the path cannot select a different inode. Deploy immutable binaries and replace them atomically.

```sh
/data/runtime-build/backtest-job \
  --database /data/runtime/jobs.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  submit /data/runtime/job-v6.json
```

Ordinary `submit` starts a detached worker immediately and returns its job ID. This retains the existing immediate-submission workflow. It provides no shared concurrency limit across submissions. `status`, `inspect`, `lookup`, `result`, `cancel` and `recover` operate on the selected database and artifact roots.

## Run a persistent queue

Use a database dedicated to queue submissions. Submit each job with `submit --queue`, then run one scheduler with an explicit concurrency limit.

```sh
/data/runtime-build/backtest-job \
  --database /data/runtime/queue.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  submit --queue /data/runtime/job-v6.json

/data/runtime-build/backtest-job \
  --database /data/runtime/queue.sqlite \
  --artifact-root /data/runtime/artifacts \
  --result-root /data/runtime/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  serve --concurrency 4
```

The scheduler discovers persisted queued jobs after restart. A slot remains occupied until its worker has been reaped. Use `serve --concurrency 4 --once` to drain the queue and exit during validation. Keep one scheduler per database and do not mix immediate submissions with scheduler-managed work.

## Limit each queued job

Run `serve --cgroup-root auto` under a systemd service with `Delegate=memory` and `DelegateSubgroup=supervisor`. The scheduler must start in the delegated `supervisor` subgroup. Alternatively pass an absolute, empty, delegated cgroup v2 parent through `--cgroup-root`. Ordinary directories and missing delegation fail with `RESOURCE_LIMIT_UNAVAILABLE`. No weaker fallback is used. This option is available only to `serve`.

The scheduler creates a database-specific namespace and a fresh group for each job. Linux clones the worker directly into that group before Go initialization. `budgets.memory_mb` sets its `memory.max` in MiB, with swap disabled and group OOM termination enabled. The group includes the worker, engine and optional Python descendants. The scheduler remains outside. The existing child address-space limit also applies. Physical memory accounting differs from virtual address-space accounting, so this adds an aggregate boundary without claiming equivalence to the old Python limit.

An OOM-killed active worker becomes `FAILED` with `JOB_MEMORY_EXCEEDED`. Cancellation takes precedence when already requested. The scheduler continues with later queued jobs. It records memory peak and OOM counters in `.logs/<job-id>.resources.json`, synchronizes a snapshot before removing the group, and marks final cleanup in the receipt. Receipt storage failure stops scheduling and retains the group/counters when removal has not happened. Published financial result manifests keep their existing contract.

Restart reconciliation retains live claimed workers and counts their slots. A worker interrupted between spawn and claim is conservatively failed and its group cleared. Completed or abandoned groups are killed and removed before reuse. Existing successful state is preserved. These controls supervise trusted code. A process with permission to edit the delegated hierarchy can alter its own controls.

Use [the TOML example](https://github.com/runchengxie/quant-backtest-runtime/blob/main/configs/runtime.example.toml) and [the user service template](https://github.com/runchengxie/quant-backtest-runtime/blob/main/configs/quant-backtest-runtime.service). Keep actual paths in private deployment configuration. `scripts/serve_config.py --config /configured/runtime.toml --dry-run` prints the command without opening the task database. Without `--dry-run`, the standard-library launcher replaces itself with Go. The same `[runtime]` table can configure research clients. Set `queue=true`, pin the engine and optional adapter interpreter, and tune `[scheduler].concurrency` to measured capacity. Install the unit in the user's systemd directory, reload it, and validate with a separate synthetic task database before assigning real work.

Kernel memory controller behavior is documented in [cgroup v2](https://docs.kernel.org/admin-guide/cgroup-v2.html). Delegation and subgroup settings are described in [systemd resource control](https://www.freedesktop.org/software/systemd/man/latest/systemd.resource-control.html).

## Optional Python compatibility adapter

For legacy v1-v4 requests and operations that need legacy contract validation, install `quant-backtest-runtime[legacy]` into a Python environment and pass that interpreter's absolute path with `--python-executable`. From the repository, `uv sync --locked --extra legacy --group dev` provides the locked development environment.

Go invokes the stateless adapter through that interpreter. Python normalizes the existing financial contract and writes staged computation artifacts. Go owns SQLite transitions, result verification and final publication. The adapter does not open the job database or publish the final result directory. The existing Python CLI remains available for compatibility and rollback.

## Cutover and rollback

`budgets.memory_mb` applies a hard address-space limit to the engine or legacy computation child. The Go job-management process is outside this limit. Whole-job memory parity with the former Python worker has not been established. Enable and validate the delegated per-job mode for deployments requiring an aggregate memory budget. Queue concurrency limits remain independent of this memory boundary.

Before a production cutover, stop new Python submissions, quiesce its workers and scheduler, and back up the database and associated artifacts. Validate Go with a separate synthetic database first. Record the runtime revision, engine revision and executable digest, Python environment when used, and validation results before assigning Go sole ownership of the production database.

Never run Python and Go lifecycle management against the same database concurrently. For rollback, stop Go submissions and scheduling, wait for or explicitly cancel active Go workers, verify their processes have exited, and only then restore Python ownership. Preserve the backup and result inventory. This change does not migrate production data or alter scheduled jobs.

Linux memory limits, deadlines, bounded logs, parent-death signals and process identity checks supervise trusted workloads. They do not provide a sandbox for untrusted strategies or executables. See [the language decision](runtime-language.md) for ownership and remaining evaluation work.
