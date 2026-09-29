# Development and checks

[中文页面](development.zh-CN.md)

Run the locked local checks from an isolated task worktree:

```bash
uv sync --locked --group dev --python 3.13
uv run --locked ruff check src scripts tests
uv run --locked ruff format --check src scripts tests
uv run --locked ty check src scripts
uv run --locked coverage erase
uv run --locked coverage run -m pytest -q
uv run --locked coverage combine
uv run --locked coverage report
uv run --locked python scripts/smoke_backtest.py
uv run --locked pip-audit --local
uv run --locked --group docs mkdocs build --strict
git diff --check
```

CI runs the static checks, tests, and synthetic CLI example on Linux with Python 3.12 and 3.13. The documentation workflow builds and publishes MkDocs in strict mode, so broken internal links fail the build.

## Coverage and test boundaries

Coverage includes statements and branches and combines data from Python subprocesses. The current combined threshold is 75%. Raising it requires additional behavior tests; excluding code or deleting assertions is not an acceptable shortcut.

Tests cover request fingerprints and idempotency, competing state updates, cancellation, timeouts, startup failures, lease recovery, result tampering, and v1/v2 subprocess replays. `scripts/smoke_backtest.py` verifies that the installed CLI, independent worker, and synthetic example work together.

## Dependency audit

`pip-audit --local` audits known vulnerabilities in the installed environment. Local source or Git dependencies may be skipped by the vulnerability database; fixed commits, code review, and project tests remain part of their security boundary.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Request fields, input references, research clocks, and fingerprints |
| `jobs.py` | Job service, cancellation, lease recovery, and input parsing |
| `store.py` | SQLite persistence and conditional state updates |
| `worker.py` | Resource limits, backend calls, and result publication |
| `results.py` | v1–v4 result integrity validation |
| `cli.py` | Argument parsing and worker startup |

`jobs.BacktestJobRequest` remains import-compatible; new code may import it directly from `contracts`.
