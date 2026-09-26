# quant-backtest-runtime

Independent local Job runtime for versioned backtests. `quant-platform` owns the backend and official execution-aware bundle. This repository owns request validation, SQLite state transitions, worker limits, artifact resolution, result verification, and the CLI. It does not contain strategy code or market data connectors.

## Job contracts

Diagnostic v1 requests retain the historical `ticknet.backtest_job_result.v1` frame format. Execution-aware v2 requests require `execution.ledger=true`, a complete `research.clock.v1`, producer identity, and hash-addressed Parquet inputs. The worker writes `quant.backtest_job_result.v2` with an official `portfolio_backtester.backtest_result.v1` bundle. `result` verifies both manifest hashes and every bundle file before returning metadata. An execution-aware Job alone does not authorize research promotion; research binds it to a separate root run manifest.

The runtime checks order entry and fill dates against the caller's execution window, and daily NAV dates against valuation. Native replay currently records simulated fill times at date precision; the clock does not prove intraday fill timing. Callers must supply a window covering the entire replay.

## CLI

Use a release-installed executable and paths outside the release directory:

```sh
backtest-job --database /srv/quant-backtest-runtime/data/jobs.sqlite \
  --artifact-root /srv/quant-backtest-runtime/data/artifacts \
  --result-root /srv/quant-backtest-runtime/data/results submit request.json
```

The same global options support `status <job-id>`, `cancel <job-id>`, `result <job-id>`, and `recover`. Submit starts a detached one-shot worker with a wall-time and memory budget. `recover` handles expired leases. The request manifest must be a regular JSON file no larger than 64 KiB. Inputs live at `<artifact-root>/sha256/<digest>` and are verified before reading.

## Release

After merging a runtime PR, stage a commit reachable from `origin/main` with `python scripts/release.py --repo <checkout> --production /home/richard/code/production/quant-backtest-runtime stage <sha>`. The script installs locked dependencies into an immutable `releases/<sha>` directory. Run `switch <sha>` only after a dry run and a synthetic CLI check against external data paths. `rollback` restores the prior `current` symlink via `previous`. Keep the database, input artifacts, results, logs, and any credentials outside releases. Scheduled callers must reference `current/.venv/bin/backtest-job`, never a task worktree.
