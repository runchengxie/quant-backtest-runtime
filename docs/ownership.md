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

Architecture plans in the platform repository are migration history. Current runtime behavior is documented here. Cross-repository callers should link to these contracts instead of maintaining duplicate task and deployment procedures.

The runtime uses the base dependency set from `quant-platform`. Model training, cross-validation, and technical indicators are optional platform capabilities and are not installed by this runtime.
