# Financial checkpoint sessions (Go v8)

[中文页面](financial-sessions.zh-CN.md)

Use the explicit `platform.minute_session_resume` backend when a bounded Platform
minute simulation needs financial checkpoint recovery. Existing v1–v7 jobs keep
terminal failure and stage cleanup; v7 replay journals are not financial checkpoints.
This mode is diagnostic synthetic execution, not broker or exchange recovery.

## Supported inputs and policy

Install the locked `legacy` extra with Platform revision
`7a618bddd8fdeee5dbe37fe5b54a9698e0de5355`. Pass the installed environment's absolute
interpreter path as `--python-executable`. Go always launches a fixed adapter module;
requests cannot choose commands, imports, executables, policies or deserializers.

```json
{
  "schema_version": 8,
  "idempotency_key": "synthetic-minute-1",
  "backend": "platform.minute_session_resume",
  "evidence_tier": "diagnostic",
  "inputs": {"minute_input_ref": "artifact://sha256/<input-sha256>"},
  "config": {
    "provider_revision": "7a618bddd8fdeee5dbe37fe5b54a9698e0de5355",
    "checkpoint_format": "minute-session.v2",
    "policy": "target-weights.v1"
  },
  "execution": {},
  "budgets": {"wall_seconds": 120, "memory_mb": 2048}
}
```

The single immutable UTF-8 JSON artifact has exactly these fields:

| Field | Contract |
| --- | --- |
| `schema` | `runtime-minute-input.v1` |
| `timezone` | `Asia/Shanghai`; this adapter currently supports only this market calendar |
| `targets` | 1–4096 owner target rows: aware `decision_time`, aware `entry_time`, `symbol`, `weight` |
| `bars` | 1–4096 owner raw-price rows: aware `time`/`interval_start`, symbol, OHLC, volume, amount, up/down limits, tradable |
| `config` | `MinuteExecutionConfig` fields, including initial cash and explicit buy/sell fees |
| `report_delay_seconds` | Finite nonnegative owner report delay |
| `impact` | `null`, or exact `LinearImpact` parameters: transient/permanent price units per signed share and decay seconds |
| `quantity_locks` | Exact `QuantityLock` fields; times are integer epoch nanoseconds, symbols an array |
| `corporate_actions` | Exact `CorporateAction` fields with calendar dates |
| `valuation_events` | Exact `ValuationEvent` fields with aware availability/effective clocks |
| `capital_flows` | Exact `CapitalFlow` fields with aware clocks; deposits/withdrawals remain separate from PnL |

Every tape is required, including empty arrays. Input size is at most 1 MiB.
Aware bar/target clocks convert to the declared market calendar before owner execution;
UTC-stamped capital flows retain their actual instant and local owner date. Unknown
wealth/return is JSON `null`, never fabricated from stale or missing marks. The fixed
policy uses supplied target weights only when no orders/reports are pending and stores
its entire observation history. Learned policies, arbitrary models, other market
calendars, and unsupported action constructors are rejected. Impact is an uncalibrated
stress model. Matching, entitlement accounting, costs and capital-flow returns remain
entirely in Platform.

`plan` checks hashes, the exact installed provider and owner constructor semantics,
without simulating events or creating the database. It does not establish economic
validity or historical point-in-time evidence. It binds artifact bytes, complete input
configuration, policy and loaded Runtime/Platform source bytes, dependency versions,
and the Go build's `SourceRevision`. Build immutable releases with the correct revision.
Use `--expected-plan-sha256` on submit to reject a changed preflight.

## Explicit recovery

After recovery marks a dead v8 worker `FAILED`, submit a new idempotency key and the
same input/configuration, with `execution: {"resume_from": "<failed-job-id>"}`.
Run `recover` after the recorded lease expires. Recovery kills/reaps a matching old
worker before terminalizing it. A cancelled or successful source, no admitted progress,
or changed source/provider/configuration fails closed. A failed predecessor stays failed;
the successor's persisted request retains the link. Repeating the same submission key
returns the existing successor. There is no automatic retry or same-job reclaim.

Go creates a random ownership token after claiming each job. For each event, the fixed
Python child restores the last admitted owner checkpoint, advances one owner step, and
stages opaque JSON. Go verifies the receipt and consecutive cursor, writes a private
file, fsyncs it, renames without replacement, fsyncs its directory, then atomically admits
the digest/cursor/binding/token in SQLite while the job still owns `RUNNING` and is not
cancelled. The next step receives only that admitted checkpoint. Restore passes all
external expected tapes, including empties, to `MinuteSession.restore`; it does not
re-execute the committed financial prefix.

Checkpoints live at `RESULT_ROOT/.checkpoints/<job>/<token>/<cursor>.json`, outside the
private final-result stage. A renamed file with no DB admission is an orphan and is
never selected for recovery. A death after DB admission but before acknowledgement
uses that admitted cursor. Old tokens and terminal jobs cannot admit or commit success.
Cancellation and result publication remain Go-owned. Checkpoints are retained for
inspection; there is no automatic checkpoint garbage collection in this version.
Do not promote orphan files or edit SQLite to make them resumable.

Final `quant.minute_job_result.v1` manifests bind the request and financial payload
hash/size, committed cursor and initial resumed cursor. `financial.json` contains all
owner daily/order/fill/holding/valuation/capital-flow tables, summary, and the complete
last pre-finalization owner checkpoint (account, receivables, action/impact/policy state,
delivered and pending reports, locks and trace). `result` verifies publication integrity.
Final result publication is separate from checkpoint admission. Attempt metadata may
differ; canonical financial payloads must be identical for uninterrupted and resumed runs.

## Diagnostic crash acceptance

The operator-only `__worker JOB --financial-fault PHASE:CURSOR` control pauses at an
exact cursor for external SIGKILL. Allowed phases are `before_write`, `after_fsync`,
`after_rename` (before DB admission), and `after_admission` (before acknowledgement).
The worker writes `.checkpoints/<job>/fault.json` with its PID/phase/cursor and waits
until its normal deadline or cancellation. The option is rejected on public submit and
accepts no executable code. It is a synthetic fault probe, not a production retry policy.

`tests/test_go_financial_sessions.py` exercises the real Go CLI and fixed Python child
at all four boundaries; `tests/test_financial_sessions.py` compares every owner boundary
and includes independent local-midnight split/valuation/capital expectations. Run with
`BACKTEST_GO_EXECUTABLE` and the installed locked environment. Existing engine/queued
session tests remain required compatibility checks. See [development](development.md).
