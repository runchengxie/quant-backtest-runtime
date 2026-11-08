# Development and checks

[中文页面](development.zh-CN.md)

Run the locked local checks from an isolated task worktree.

```bash
uv sync --locked --extra legacy --group dev --python 3.13
uv run --locked --extra legacy ruff check src scripts tests
uv run --locked --extra legacy ruff format --check src scripts tests
uv run --locked --extra legacy ty check src scripts
uv run --locked --extra legacy coverage erase
uv run --locked --extra legacy coverage run -m pytest -q
uv run --locked --extra legacy coverage combine
uv run --locked --extra legacy coverage report
uv run --locked --extra legacy python scripts/smoke_backtest.py
uv run --locked --extra legacy pip-audit --local
uv run --locked --extra legacy --group docs mkdocs build --strict
uv run --locked --extra legacy --group docs python project_tools/check_locale_navigation.py
git diff --check
```

CI runs the static checks, tests, and synthetic CLI example on Linux with Python 3.12 and 3.13. Dependency auditing runs on Python 3.13. The documentation workflow builds and publishes MkDocs in strict mode, so broken internal links fail the build.

Pull requests also build the docs and check English/Chinese sidebar, HTML language and theme-toggle labels. Locale changes affect presentation, not request fields or result data. The page hook preserves theme schemes and icons and uses English for document paths outside the `zh-CN` suffix. These PR checks do not publish the site.

Ruff checks imports, common errors, unsafe exception handling, obsolete syntax, and complexity, with a cyclomatic complexity limit of 10. ty checks runtime code and maintenance scripts. Request validation and tests enforce the content of dynamic JSON fields typed as `Any`.

## Coverage and test boundaries

Go checks run with Go 1.27.3 using the committed `go.mod` and `go.sum`. Run `gofmt`, `go vet ./...` and `go test -race ./...`, then build the CLI outside the source tree and run `tests/test_go_runtime.py` with both executable environment variables shown in the [Go guide](go-runtime.md). The real-engine CI job builds a CLI with race detection enabled. Native protocols and legacy compatibility are both exercised against the existing Python contracts.

Coverage includes statements and branches and combines data from Python subprocesses. The CLI restricts worker environment variables, so some indirectly launched workers do not inherit coverage settings. Direct worker integration tests retain subprocess coverage. Forcibly terminated processes may not write coverage data. The current combined threshold is 75%. Raising it requires additional behavior tests. Excluding code or deleting assertions is not an acceptable shortcut.

Tests cover request fingerprints and idempotency, competing state updates, cancellation, timeouts, startup failures, lease recovery, result tampering, and v1-v4 subprocess replays, including sequenced execution and trade-accounting result checks. `scripts/smoke_backtest.py` verifies that the installed CLI, independent worker, and synthetic example work together.

The v1–v4 protocol golden tests use immutable synthetic inputs in `tests/fixtures/legacy-golden`, captured from baseline revision `ab0de9e` with pandas 2.3.3 and PyArrow 25.0.1. Artifact filenames are their SHA-256 digests; `provenance.json` records the decoded Parquet schema and rows. Tests verify all bytes, schemas and values while preserving the original request and plan digests. Do not regenerate these fixtures when serialization libraries change. Other replay tests continue to write and read with the current libraries.

## Dependency audit

The legacy extra keeps quant-platform at reviewed compatibility baseline `7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`. Upgrade that provider only after its own dependency refresh is reviewed, merged and validated against these legacy protocols; package upgrades here do not select an arbitrary provider head.

`pip-audit --local` audits known vulnerabilities in the installed environment. Local source or Git dependencies may be skipped by the vulnerability database. Fixed commits, code review, and project tests remain part of their security boundary.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Request fields, input references, research clocks, and fingerprints |
| `jobs.py` | Job service, cancellation, lease recovery, and input parsing |
| `store.py` | SQLite persistence and conditional state updates |
| `worker.py` | Resource limits, backend calls, and result publication |
| `results.py` | v1-v4 result integrity validation |
| `cli.py` | Argument parsing and worker startup |

`jobs.BacktestJobRequest` remains import-compatible. New code may import it directly from `contracts`.

## Independent Rust engine jobs (v5)

The opt-in `nautilus.quote_market_orders` backend launches
[quant-backtest-engine](https://github.com/runchengxie/quant-backtest-engine).
It does not change v1-v4 normalization or fingerprints. It currently provides a
diagnostic final snapshot, not the execution-aware daily ledger of v2.

Install/build the engine from a reviewed, merged revision outside task worktrees.
Dedicated CI pins provider `c65548a4ee1e938125cce1edbcc282c760c4c8f2` and tests the real binary with this
consumer revision. Local validation uses that provider's identical tested sources.
Pass the absolute executable using the operator CLI option `--engine-executable`.
The submitted request cannot select an arbitrary executable path or shell command.
It pins the executable bytes with `config.executable_sha256` instead.

```json
{
  "schema_version": 5,
  "idempotency_key": "synthetic-engine-1",
  "backend": "nautilus.quote_market_orders",
  "evidence_tier": "diagnostic",
  "inputs": {"engine_request_ref": "artifact://sha256/<request-file-sha256>"},
  "config": {"executable_sha256": "<installed-executable-sha256>"},
  "execution": {},
  "budgets": {"wall_seconds": 30, "memory_mb": 4096}
}
```

Store the engine JSON request as an immutable artifact under the existing SHA-256
artifact layout. Its schema is `engine_request.v1` (see the engine repository fixture).
A whole job request fingerprint therefore binds both input bytes and the selected
executable. Hashes identify exact build bytes, not source revisions or signed provenance.
Record the source revision and build toolchain in deployment records as well.
The runner executes a held, verified inode through `/proc/self/fd`, so atomic path
replacement cannot change the selected executable. Publish immutable binaries using
atomic replacement. Do not modify an executable inode in place during a job.

```sh
backtest-job --database /data/jobs.sqlite --artifact-root /data/artifacts   --result-root /data/results --engine-executable /releases/engine/quant-backtest-engine   submit /data/job-v5.json
```

Agent `plan` and plan-bound `submit` perform owner-authoritative static v5
validation before opening the job database. The runtime reads at most 1 MiB plus
one byte, verifies the input digest, and sends those exact bytes to the pinned
engine's `validate -` command through a held executable descriptor. First it
checks `capabilities` through that same descriptor and requires validator version 1.
The two child operations share one 10-second deadline, use a minimal environment
and each have independently enforced 8-KiB
stdout/stderr limits. Overflow or timeout kills and reaps it. This operation creates
no job state or temporary output files and never runs a simulation.

A successful v5 plan marks `engine_request_semantics` as `passed` and binds the
strict `engine_validation.v1` receipt (validator version 1 and exact input SHA-256)
into `plan_sha256`. Invalid requests produce `ENGINE_REQUEST_INVALID`. An older or
unsupported validator, malformed receipt, process failure or resource limit gives
`ENGINE_VALIDATION_UNAVAILABLE`. Install a compatible immutable engine release.
There is no fallback to unchecked v5 plans. v1-v4 plan digests and all request
fingerprints keep their existing contracts. The worker still verifies the original
binary/input digests and the engine validates again at execution time.

The Python worker remains the sole SQLite writer and result publisher. The Rust child
inherits its address-space limit. Timeout/cancellation kills and reaps the child.
Linux parent-death SIGKILL handles hard worker death and expired-worker recovery.
The runner is intended only for the existing single-threaded Linux worker, not a
multithreaded embedding host. The engine is a trusted, pinned binary. This is not a
sandbox for untrusted executables or strategies. Temporary request/output files use
the worker's temporary directory. No private data belongs in source repositories.

Published `quant.engine_job_result.v1` manifests contain one `engine_result.json`
entry, size/SHA-256 inventory, executable digest and final balance summary. Reads
verify integrity and financial summary consistency. Orders may be rejected even when
the job succeeds. Inspect the normalized engine order statuses. Market rules and
information-time limitations remain those explicitly documented by the engine.

To run the real integration locally (build outputs stay outside the source tree).

```sh
BACKTEST_ENGINE_TEST_EXECUTABLE=/absolute/path/to/quant-backtest-engine   uv run --locked --extra legacy pytest -q tests/test_rust_engine_jobs.py
```

Without that variable the real-engine test is skipped. Dedicated `rust-engine` CI
builds the pinned provider and must pass it. Other lifecycle tests use controlled
fixture processes to verify failure, cancellation, hard parent death and timeout.

## Session ledger jobs (v6)

v6 selects `nautilus.session_replay` and consumes an immutable `engine_request.v2`
artifact. As in v5, provide an executable SHA-256 config, an empty execution
mapping and explicit wall/memory budgets. v1-v5 normalization and fingerprints remain
unchanged. Discovery and side-effect-free preflight include v6 and verify its binary.

The worker supervises the same held-inode child boundary and publishes
`quant.engine_job_result.v2` with one `engine_result.json`. Verification checks strict
nested fields, USD/CNY identity, raw-price basis, active T+1 flag, ISO session order,
valuation timezone, order/fill quantities, rejection reasons, daily cash/positions/NAV,
per-fill currency rounding and commission. It reconstructs reported cash and shares
from fills to reconcile the ledger, without deciding trades or matching orders.
T+1 assertions are independently checked against prior-session holdings. Before
publication the result is bound to input currency, initial cash, active rules, session
coverage, requested orders and their information/valuation clocks.

Example. Change the v5 job wrapper to `schema_version: 6` and
`backend: nautilus.session_replay`, and point `engine_request_ref` at the engine's
[session fixture](https://github.com/runchengxie/quant-backtest-engine/blob/c65548a4ee1e938125cce1edbcc282c760c4c8f2/fixtures/sessions.json).
The synthetic result has first-day cash 8999/NAV 10099, a same-day T+1 denial, and
final cash/NAV 10197.8. The runtime summary retains the common cash/positions/equity/
commission keys, while the inventory JSON retains the complete session ledger.

A session ledger remains diagnostic evidence unless a separate consumer supplies the
full research clock, producer and canonical execution-aware mapping. Corporate actions,
receivables, delisting policies, daily-volume participation and exchange-specific tax
schedules are not inferred. Keep the existing Python backend for unsupported scenarios.
Dedicated CI tests both v5 and v6 against merged provider
`c65548a4ee1e938125cce1edbcc282c760c4c8f2`.

## Installation variants

The base package has no Python runtime dependencies. It supports v5/v6 Rust jobs,
SQLite lifecycle management, engine result verification, and agent discovery and
preflight. Install a reviewed engine executable separately and pass its absolute
path with `--engine-executable`.

```sh
pip install quant-backtest-runtime
# From this repository, with locked development tools but no legacy stack:
uv sync --locked --group dev
```

For v1-v4 execution, legacy result reads, or `quant_run_manifest_ref` validation,
install the explicit compatibility extra.

```sh
pip install 'quant-backtest-runtime[legacy]'
# Locked repository workflow:
uv sync --locked --extra legacy --group dev
```

The extra pins quant-platform to a reviewed Git revision and requires Git access.
The `dev` group does not enable it automatically. Missing packages produce
`LEGACY_DEPENDENCY_UNAVAILABLE` with installation guidance. CLI plan and submit
reject the request before creating state. Dependency preflight checks package
availability only, without importing pandas or portfolio code. It does not prove
backend compatibility or request semantics. Existing v1-v6 request fingerprints,
plan payloads and persisted state contracts remain unchanged. v6 plans still mark
engine semantics `not_checked`. v5 uses the engine's static validator.

This is an installation boundary, not migration of Python algorithms to Rust.
Keep the legacy extra until callers and unsupported scenarios have migrated.
Runtime continues to own process supervision, SQLite state and result publication
verification through its engine protocol. It does not use the provider SDK as a
subprocess supervisor.

Go v8 [financial checkpoint sessions](financial-sessions.md) use the same locked provider. Run `tests/test_go_financial_sessions.py` with the race-built Go executable; all four actual SIGKILL boundaries are required. This provider upgrade retains immutable v1–v4 golden fixtures and all v1–v7 regression gates.
