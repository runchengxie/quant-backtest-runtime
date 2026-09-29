# Operations and troubleshooting

[中文页面](operations.zh-CN.md)

## Directory layout

Using `/srv/quant-backtest-runtime` as an example, `releases/<sha>` contains source and the virtual environment, `current` points to the active release, and `previous` is the rollback target. Runtime data stays outside the release tree in `data/jobs.sqlite`, `data/artifacts`, `data/results`, and `data/results/.logs`.

The runtime accepts only its own SQLite database. A database from the legacy research repository has a different schema and cannot be used directly.

## Release

After merging a PR, record the full commit SHA and stage it with a dry run first:

```bash
git fetch origin
release_sha=$(git rev-parse origin/main)
production_root=/srv/quant-backtest-runtime
python scripts/release.py --repo . --production "$production_root" --dry-run stage "$release_sha"
python scripts/release.py --repo . --production "$production_root" stage "$release_sha"
```

`stage` accepts only commits already present in `origin/main`. It installs locked dependencies in a versioned directory and writes `.release-ready` after success. Existing release directories are never overwritten.

Run the synthetic data check from the staged release before switching:

```bash
"$production_root/releases/$release_sha/.venv/bin/python" \
  "$production_root/releases/$release_sha/scripts/smoke_backtest.py" \
  --executable "$production_root/releases/$release_sha/.venv/bin/backtest-job"
python scripts/release.py --repo . --production "$production_root" --dry-run switch "$release_sha"
python scripts/release.py --repo . --production "$production_root" switch "$release_sha"
```

Rollback can also be previewed before execution:

```bash
python scripts/release.py --repo . --production "$production_root" --dry-run rollback
python scripts/release.py --repo . --production "$production_root" rollback
```

Production calls use `current/.venv/bin/backtest-job` and pass the three data paths explicitly. Confirm that no worker still uses an old interpreter before removing a release.

## Troubleshooting

| Symptom or code | Check |
| --- | --- |
| `WORKER_START_FAILED` | Interpreter, permissions, and log directory |
| `WORKER_START_EXPIRED` | Worker stderr in `.logs` after startup |
| `JOB_TIMEOUT` | Input size and time budget; resubmit with a new idempotency key |
| `RESOURCE_LIMIT_UNAVAILABLE` | Linux resource-limit support and permissions |
| `INPUT_ARTIFACT_MISSING` | Input directory and hash references |
| `BACKTEST_REJECTED` | Parameters, input data, and research-clock range |
| `WORKER_LEASE_EXPIRED` | Process exit and the recovered terminal state |
| `RUNNING` after cancellation | Query status, run `recover` if needed, and inspect `/proc` visibility |
| Result hash mismatch | Preserve the file and logs, then regenerate the input or job |

Back up SQLite using its consistency-safe backup method and preserve the input and result directories together. Backup and cleanup scheduling belongs to the deployment environment; this repository does not provide a resident backup service.
