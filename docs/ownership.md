# Repository ownership and migration

[中文页面](ownership.zh-CN.md)

After the runtime was split from the research repository, generic task-operation instructions, examples, and operations scripts are maintained here.

| Content | Maintained by |
| --- | --- |
| Job requests, SQLite state, worker, cancellation, and recovery | `quant-backtest-runtime` |
| Generic CLI examples, synthetic checks, release, and rollback | This repository's `scripts/`, `examples/`, and `docs/` |
| Strategy inputs, research clients, provenance, and promotion | `quant-research` |
| Read-only adapters for legacy research jobs | `quant-research` |
| Backtest algorithms, execution simulation, and standard result packages | `quant-platform` |

The research-side `scripts/dev/check_legacy_backtest_jobs.py` checks whether the legacy `ExperimentRegistry` still has unfinished jobs. It remains in the research repository while the historical adapter is in use.

Plans for extracting this runtime from the platform repository are migration history. Current runtime behavior is documented here. Cross-repository callers should link to these contracts instead of maintaining duplicate task and deployment procedures.

The base runtime requires Python but no third-party Python packages. Install the explicit `legacy` extra for the older quant-platform backends. Model training remains a caller responsibility.

The [runtime language record](runtime-language.md) describes the proposed Go evaluation and the compatibility checks required before a migration.

## Cross-repository workflow coordination

[quant-task-orchestrator](https://github.com/runchengxie/quant-task-orchestrator)
maintains workflow DAGs, dispatch intent, operator approvals, pinned Skill metadata
and unified TUI/API projections. It consumes Runtime's existing plan-bound CLI;
it neither opens this owner's database directly nor starts or replaces its queue
scheduler. `lookup` establishes the original job, exact idempotent submission
checks the request fingerprint, and `result` verifies published content before a
dependent workflow step can advance. Missing receipts stay uncertain.

Research admission, historical PIT, economic conclusions and promotion remain
with Research. A coordinator workflow or completed Runtime job cannot grant those
permissions. The [public demo](https://runchengxie.github.io/quant-task-orchestrator/)
uses synthetic browser-local fixtures. Register real work only after isolated
owner acceptance; this integration does not migrate production timers or services.
