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
