# Opt-in Rust engine integration

The owner delegated continued implementation of the independent-engine architecture.
Provider: quant-backtest-engine, engine_request.v1 / engine_result.v1, upstream 0.66.0.

Use an additive v5 job request with backend nautilus.quote_market_orders and diagnostic
result evidence only. Inputs contain one immutable engine_request_ref; config pins the
selected executable SHA-256; execution is empty. CLI/worker executable selection is an
operator argument, never an arbitrary path embedded in a submitted request.

The existing Python worker remains the only job-state and publication writer. It
supervises the Rust subprocess, kills/reaps it on cancellation or timeout and installs
Linux parent-death SIGKILL before exec so hard worker recovery does not leave an orphan.
The child inherits the worker address-space limit. No new sandbox guarantees are claimed.
Keep v1-v4 behavior, normalization and fingerprints unchanged.

Results use quant.engine_job_result.v1 with one engine_result.json inventory entry,
SHA-256/size checks, request and executable identity. Verify normalized financial
invariants before publication and on read; do not pretend a final snapshot is an
execution-aware daily ledger. Pin provider revision in integration CI, run the real
binary against synthetic fixtures, and run all repository-required checks.

Tasks: [ ] v5 contract tests then validation; [ ] runner/protocol/lifecycle tests then
implementation; [ ] durable publication/read verification; [ ] real engine CI and
English/Chinese docs; [ ] full checks and independent review; [ ] PR and merge after provider.


Local validation: full suite 67 passed with real engine, branch+statement coverage 80%,
Ruff lint/format and ty passed, legacy synthetic CLI passed, pip-audit reported no known
vulnerabilities (local/Git packages not in PyPI were skipped), strict docs and locale
navigation passed. Independent review findings on executable replacement and nested
result validation were reproduced before fixes and covered by regression tests.
