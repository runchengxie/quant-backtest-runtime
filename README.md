# quant-backtest-runtime

[中文 README](README.zh-CN.md)

The Go implementation owns job state, worker supervision and durable publication. Native v5/v6 Rust engine jobs run without Python. Legacy v1–v4 financial computation uses an explicitly configured, stateless Python adapter.

Build on Linux with Go 1.27.3. Choose an external output directory.

```sh
go build -trimpath -o /absolute/external/runtime/bin/backtest-job ./cmd/backtest-job
```

The [Go guide](docs/go-runtime.md) covers immediate submission, the bounded persistent queue and the optional legacy interpreter. Production cutover requires stopping the previous lifecycle owner and backing up its database. This migration does not switch production services or schedules.

`quant-backtest-runtime` accepts submitted backtest jobs, validates requests, manages job state in SQLite, runs workers, and validates and publishes results. It does not run LLM agents.

Within the Quant Research project family, [`quant-platform`](https://github.com/runchengxie/quant-platform) provides reusable backtest capabilities, the research layer owns strategies and promotion, and this repository owns generic job execution. See [Repository ownership](docs/ownership.md) for the boundaries.

## Python compatibility example

Linux and `uv` are required. Package metadata requires Python 3.12 or newer; CI validates 3.12 and 3.13. Other Python versions are not covered by these jobs:

```bash
uv sync --locked --extra legacy --group dev --python 3.13
uv run --locked --extra legacy python scripts/smoke_backtest.py
```

The example uses synthetic data in a temporary directory. It submits a job and validates the result without requiring private research data.

## Reading order

1. [Backtest jobs and results](docs/jobs.md): request versions, input references, job state, and result validation.
2. [Development and checks](docs/development.md): local setup, quality gates, and test scope.
3. [Operations and troubleshooting](docs/operations.md): release directories, rollback, and common failures.
4. [Repository ownership](docs/ownership.md): interfaces with the research and platform repositories.

## Python compatibility installation

The base package has no Python runtime dependencies. It supports v5/v6 Rust jobs,
SQLite lifecycle management, engine result verification, and agent discovery and
preflight. Install a reviewed engine executable separately and pass its absolute
path with `--engine-executable`:

```sh
pip install quant-backtest-runtime
# From this repository, with locked development tools but no legacy stack:
uv sync --locked --group dev
```

For v1-v4 execution, legacy result reads, or `quant_run_manifest_ref` validation,
install the explicit compatibility extra:

```sh
pip install 'quant-backtest-runtime[legacy]'
# Locked repository workflow:
uv sync --locked --extra legacy --group dev
```

The extra pins quant-platform to a reviewed Git revision and requires Git access.
The `dev` group does not enable it automatically. Missing packages produce
`LEGACY_DEPENDENCY_UNAVAILABLE` with installation guidance; CLI plan and submit
reject the request before creating state. Dependency preflight checks package
availability only, without importing pandas or portfolio code; it does not prove
backend compatibility or request semantics. Existing v1-v6 request fingerprints,
plan payloads and persisted state contracts remain unchanged. v6 plans still mark
engine semantics `not_checked`; v5 uses the engine's static validator.

This is an installation boundary, not migration of Python algorithms to Rust.
Keep the legacy extra until callers and unsupported scenarios have migrated.
Runtime continues to own process supervision, SQLite state and result publication
verification through its engine protocol; it does not use the provider SDK as a
subprocess supervisor.
