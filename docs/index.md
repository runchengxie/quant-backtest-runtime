# quant-backtest-runtime

[中文页面](index.zh-CN.md)

`quant-backtest-runtime` accepts submitted backtest jobs, validates requests, manages job state in SQLite, runs workers, and validates and publishes results. It does not run LLM agents.

Within the Quant Research project family, [`quant-platform`](https://github.com/runchengxie/quant-platform) provides reusable backtest capabilities, the research layer owns strategies and promotion, and this repository owns generic job execution. See [Repository ownership](ownership.md) for the boundaries.

## Run the synthetic example

Linux, Python 3.12 or 3.13, and `uv` are required:

```bash
uv sync --locked --group dev --python 3.13
uv run --locked python scripts/smoke_backtest.py
```

The example uses synthetic data in a temporary directory. It submits a job and validates the result without requiring private research data.

## Reading order

1. [Backtest jobs and results](jobs.md): request versions, input references, job state, and result validation.
2. [Development and checks](development.md): local setup, quality gates, and test scope.
3. [Operations and troubleshooting](operations.md): release directories, rollback, and common failures.
4. [Repository ownership](ownership.md): interfaces with the research and platform repositories.
