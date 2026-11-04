# quant-backtest-runtime

[中文说明](README.zh-CN.md) · [Documentation](https://runchengxie.github.io/quant-backtest-runtime/)

Submit a backtest, follow its progress and retrieve checked results.
This runtime keeps job state in SQLite, runs separate worker processes, supports
queues and cancellation, and checks results before saving them for later use.

Use it to manage multiple backtest jobs and retain their results. The Go CLI manages jobs. The separately
installed [quant-backtest-engine](https://github.com/runchengxie/quant-backtest-engine)
simulates trades. Strategies and research decisions belong to callers.
[quant-platform](https://github.com/runchengxie/quant-platform) supplies the optional
Python backends for older jobs. See [repository ownership](docs/ownership.md).

## Run a synthetic job

Use a source checkout on Linux, Go 1.27.3 and Python 3.12+ for the check script
(standard library only). Native engine jobs do not need Python installed to run.
First install a reviewed engine executable using its
[release guide](https://runchengxie.github.io/quant-backtest-engine/releases/).
The documented alpha.2 supports the validator needed by current v5 jobs.

From this repository's root, replace the three paths below. Keep builds and
results outside the source tree. `check_root` must be a new directory each time.

```sh
runtime_build=/absolute/external/runtime-build
engine=/absolute/path/to/quant-backtest-engine
check_root=/absolute/external/new-synthetic-check
mkdir -p "$runtime_build"
revision=$(git rev-parse HEAD)
go build -trimpath \
  -ldflags "-X github.com/runchengxie/quant-backtest-runtime/internal/app.SourceRevision=$revision" \
  -o "$runtime_build/backtest-job" ./cmd/backtest-job
python3 scripts/smoke_native.py \
  --executable "$runtime_build/backtest-job" \
  --expected-runtime-revision "$revision" \
  --engine-executable "$engine" \
  --engine-request examples/engine-request-v2.json \
  --root "$check_root"
```

The script creates synthetic inputs, queues a job, checks duplicate submission,
cancels a second job, drains the queue and verifies the saved result.
Success prints JSON with `status: "SUCCEEDED"`, `cancel_status: "CANCELLED"` and
`summary.equity: "10099"`. It retains a database, artifacts, results and
`receipt.json` under `check_root`. No private data or production database is needed.

## Current scope

Native v5 jobs return a final account snapshot. v6 jobs return a daily session
ledger. Both use the external Rust engine and remain diagnostic evidence.
Supported trading rules come from that engine. Successful publication does not
mean every requested order filled or that a strategy is ready for production.
Raw partition transport v7 is a design proposal, not an active job protocol.

Older v1-v4 jobs use an explicitly configured Python compatibility adapter.
The [installation options](docs/development.md#installation-variants) and
[Python example](docs/index.md#run-the-synthetic-example) cover that path.
Go and Python job managers must never manage the same database concurrently.
Production cutover requires stopping the previous owner and backing up its state.
See the [Go guide](docs/go-runtime.md#cutover-and-rollback).

## Next steps

- [Go runtime guide](docs/go-runtime.md). Submit jobs, run a persistent queue and set limits
- [Jobs and results](docs/jobs.md). Requests, status and result validation
- [Development](docs/development.md). Dependencies and required checks
- [Operations](docs/operations.md). Releases, rollback and troubleshooting
