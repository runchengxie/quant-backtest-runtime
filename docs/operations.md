# Operations and troubleshooting

[中文页面](operations.zh-CN.md)

## Directory layout

Using `/srv/quant-backtest-runtime` as an example, `releases/<sha>` contains source and the virtual environment, `current` points to the active release, and `previous` is the rollback target. Runtime data stays outside the release tree in `data/jobs.sqlite`, `data/artifacts`, `data/results`, and `data/results/.logs`.

The runtime accepts only its own SQLite database. A database from the legacy research repository has a different schema and cannot be used directly.

## Release

The commands below install the Python implementation. For a Go release, use `stage COMMIT --implementation go`, validate with the [native release check](go-runtime.md#check-a-staged-release), and update callers to `current/bin/backtest-job`. Go staging does not create `current/.venv/bin/backtest-job` or install the optional legacy adapter. Inventory callers before switching between implementations.

After merging a PR, record the full commit SHA and stage it with a dry run first.

```bash
git fetch origin
release_sha=$(git rev-parse origin/main)
production_root=/srv/quant-backtest-runtime
python scripts/release.py --repo . --production "$production_root" --dry-run stage "$release_sha"
python scripts/release.py --repo . --production "$production_root" stage "$release_sha"
```

`stage` accepts only commits already present in `origin/main`. It installs locked dependencies in a versioned directory and writes `.release-ready` after success. Existing release directories are never overwritten.

An installation failure can leave a directory without `.release-ready`. `switch` rejects it and another `stage` does not overwrite it. Preserve and inspect the failed directory before any task-specific cleanup or retry.

Run the synthetic data check from the staged release before switching.

```bash
"$production_root/releases/$release_sha/.venv/bin/python" \
  "$production_root/releases/$release_sha/scripts/smoke_backtest.py" \
  --executable "$production_root/releases/$release_sha/.venv/bin/backtest-job"
python scripts/release.py --repo . --production "$production_root" --dry-run switch "$release_sha"
python scripts/release.py --repo . --production "$production_root" switch "$release_sha"
```

Rollback can also be previewed before execution.

```bash
python scripts/release.py --repo . --production "$production_root" --dry-run rollback
python scripts/release.py --repo . --production "$production_root" rollback
```

When `previous` is a symlink, rollback switches to that release. On a first installation without a previous release, rollback removes only the `current` pointer and leaves runtime data in place.

Rollback preserves the exact resolved previous path, including a release under a shared generation directory. It validates that path's readiness marker and implementation-specific executable before changing either pointer. Keep the generation and its Python environment intact until rollback is no longer needed. Coordinate updates with the owner of shared generation pointers.

Production calls use `current/.venv/bin/backtest-job` and pass the three data paths explicitly. Confirm that no worker still uses an old interpreter before removing a release.

## Troubleshooting

| Symptom or code | Check |
| --- | --- |
| `WORKER_START_FAILED` | Interpreter, permissions, and log directory |
| `WORKER_START_EXPIRED` | Worker stderr in `.logs` after startup |
| `JOB_TIMEOUT` | Input size and time budget. Resubmit with a new idempotency key |
| `RESOURCE_LIMIT_UNAVAILABLE` | Linux resource-limit support and permissions |
| `INPUT_ARTIFACT_MISSING` | Input directory and hash references |
| `BACKTEST_REJECTED` | Parameters, input data, and research-clock range |
| `WORKER_LEASE_EXPIRED` | Process exit and the recovered terminal state |
| `RUNNING` after cancellation | Query status, run `recover` if needed, and inspect `/proc` visibility |
| Result hash mismatch | Preserve the file and logs, then regenerate the input or job |

Back up SQLite using its consistency-safe backup method and preserve the input and result directories together. Backup and cleanup scheduling belongs to the deployment environment. This repository does not provide a resident backup service.

## Publication fault recovery

Result-directory publication and SQLite success are separate boundaries. A renamed
directory is not a successful result until the job state commits. Lease expiry
treats unfinished jobs conservatively. It never adopts a directory as success.
Repeated recovery also retries cleanup for expired failed jobs, including after a
cleanup interruption. Unrelated directories and processes remain outside cleanup.

## Offline backup and restore

Pause submissions and scheduling, then wait for workers to exit or explicitly cancel
them and verify exit. Do not restart the source while using a restored copy: two
runtimes must never own the same task state. This procedure preserves queued work
and completed results; it does not resume a running engine account.

Create a new, empty backup directory outside releases. Use Python's
`sqlite3.Connection.backup` (or SQLite's `.backup`) to write `jobs.sqlite` there;
do not copy only the main SQLite file, because committed data may still be in WAL.
While writers remain stopped, copy the complete artifact and result trees, including
logs, plus externally retained request, plan and submission receipts. For v7 also
copy the partition tree with its relative paths intact. Preserve permissions and
reject or explicitly inventory symlinks rather than silently following them.

Record a relative-path, byte-size and SHA-256 inventory for every backup file, plus
the runtime source revision, executable hashes, provider revision and launch options.
Keep the inventory outside the inventoried tree. Verify the inventory after copying
to a new, empty restore root, and run `PRAGMA integrity_check` on its SQLite database.
A self-created inventory detects transfer damage; it is not independent proof of
provenance. Keep the original and backup until acceptance is complete.

Before enabling submissions, pass the restored database, artifact, result and
partition roots explicitly to the same compatible runtime. Retain the exact pinned
engine bytes and required Python adapter environment. Read every successful job
through `result` to verify its manifest and payload hashes. Compare job IDs, states,
request fingerprints and saved receipts. Resubmit an unchanged request to verify
that its idempotency key resolves to the existing job. Only after these checks,
start the scheduler for pending `SUBMITTED` jobs. Repeated `recover` must leave
completed jobs intact. A pre-backup `RUNNING` record requires separate expired-lease
recovery after confirming its old process is gone; it cannot restore engine memory.

`tests/test_go_runtime.py::test_offline_backup_restores_results_queue_and_idempotency`
checks this boundary with a real Go CLI and pinned Rust engine: it moves the source
root away, restores SQLite plus immutable inputs/results and receipts, reads the
completed result, continues queued work and verifies idempotency and repeated recovery.
The test covers synthetic v6 jobs. It does not certify a deployment's storage,
external receipt retention, v7 partition relocation or backup scheduling.

## Restricted scheduled snapshots

For an exclusively queue-managed native v5/v6 deployment, run the dependency-free
utility from a stable release path with Python 3.12 or newer:

```sh
python3 /srv/quant-backtest-runtime/current/scripts/backup_runtime.py --config /private/runtime.toml
python3 /srv/quant-backtest-runtime/current/scripts/backup_runtime.py --config /private/runtime.toml --status
```

Use the same `runtime` table as the scheduler and add a `backup` table. All paths
must be absolute. The backup root must be owned by the service user with mode
`0700`, outside and disjoint from the configured source roots. Files are private.

```toml
[runtime]
queue = true
database = "/data/runtime/jobs.sqlite"
artifact_root = "/data/runtime/artifacts"
result_root = "/data/runtime/results"

[backup]
root = "/backups/runtime"
max_seconds = 30
max_age_seconds = 7200

[backup.metadata]
runtime_revision = "<full merged source revision>"
engine_revision = "<full pinned provider revision>"
engine_sha256 = "<retained engine executable SHA-256>"
```

Schedule hourly as an operator-owned oneshot service, with an independent hard
service timeout (for example 40 seconds). Run `--status` after creation and in
regular monitoring. A nonzero exit, including an idle check, is a visible failed
attempt: investigate it rather than treating it as a fresh backup. Status verifies
**the newest** published snapshot's complete file inventory, SQLite integrity and
age (default two hours); it never falls back silently to an older valid snapshot.
No backups are automatically deleted, and there is no mutable `latest` pointer.
Monitor capacity and arrange a separate reviewed off-host copy. A copy on the same
filesystem does not protect against loss of that filesystem. A self-created hash
inventory cannot establish independent provenance or resist an attacker who can
rewrite both data and manifest.

The utility takes a nonblocking destination lock and a SQLite `BEGIN IMMEDIATE`
writer reservation with zero wait. It rejects running jobs, submitted worker
PIDs/leases, unsupported request schemas, changed fingerprints, missing or altered
inputs/results, overlapping roots, symlinks and hardlinks. The writer reservation
closes the idle-check/admission race while a separate read-only SQLite connection
uses the backup API. New job transitions may wait during this interval; the
configured userspace deadline cannot exceed 30 seconds. Normal I/O failures or
timeouts leave previous snapshots intact. Hard interruption may leave a hidden
`.incomplete-*` directory, which status ignores; inspect it before manual cleanup.

A completed `snapshot-*` directory contains `manifest.json` and `data/`. Its scope
is exactly SQLite job state, request JSON and fingerprints, referenced engine-input
CAS files under `data/artifacts/sha256/`, and verified successful result directories
under `data/results/`. Files and directories are synced before an atomic rename;
the parent is then synced. Queued jobs and idempotency survive restoration.
Unpublished/orphan result directories, logs, staging, unreferenced artifacts,
external request/plan/receipt files, runtime/engine executables and environments
are omitted. Retain pinned releases and externally retained receipts separately.
The optional metadata is operator-supplied context, not an attestation of binaries.
Legacy v1-v4 and partition-backed v7 jobs fail the **whole** attempt; they are never
silently omitted. Only exclusively queued submissions are supported; `queue=true`
is an operator assertion, not enforcement of other clients' behavior.

This restricted snapshot does not replace the full offline backup procedure above.
To restore, stop all source submissions, schedulers and workers first. Verify the
snapshot with `--status`, copy `data/` to a new empty restore root, verify its hashes
against `manifest.json` and SQLite integrity, and supply the restored database,
artifact and result paths explicitly. Absolute stored log paths are historical and
refer to omitted files. Before enabling scheduling, use the retained compatible
runtime/engine bytes to read every successful `result`, compare IDs/fingerprints,
and resubmit unchanged saved requests with `--queue` to verify idempotency. Then
continue pending jobs and check repeated recovery. Never run both source and
restored owners. The synthetic test
`test_scheduled_snapshot_restores_native_results_and_pending_queue` exercises this
sequence with the real Go CLI and pinned Rust engine after moving the source away;
it does not validate a deployment's disk, external retention or monitoring.
