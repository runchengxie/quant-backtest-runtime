# Session process channel and replay recovery

[中文页面](session-stream.zh-CN.md)

Use `backtest_runtime.session_stream.run_partition_sessions` to replay verified
partition rows through one persistent Rust engine process. This opt-in Python API
requires Linux and an engine release that provides `session-stream`. For Go queue
integration, use the separately defined [v7 mode](queued-sessions.md). Existing
queued v1-v6 jobs, fingerprints, budgets, Go controls and gateways retain their
current behavior. The provider pinned for their CI remains unchanged.

Each input row's payload has exactly two fields.

```json
{"schema_version":"engine_session_payload.v1","session":{"session_date":"2025-01-02","open_ts_ns":1735781400000000000,"valuation_ts_ns":1735781401000000000,"quotes":[],"orders":[]}}
```

The example shows the envelope shape; supply valid synthetic quotes and orders
according to the engine's `MarketSession` contract. The envelope's `event_time_ns`
must equal the session valuation timestamp. Event ID, event time and availability
time remain journal metadata. The engine receives only the session, so envelope
availability time does not become a strategy visibility rule.

`config` contains the required `engine_request.v2` header fields, excluding
`sessions`: `schema_version`, `currency`, `price_basis`, `initial_cash`, `fee_bps`,
`enforce_t1`, `instruments`, and `required_capabilities`. It is copied through
strict canonical JSON before use. The engine owns input and market semantics.

```python
from pathlib import Path
from backtest_runtime.partition_inputs import PartitionManifest, VerifiedPartitionReader
from backtest_runtime.session_stream import StreamLimits, run_partition_sessions

# manifest_mapping and config are operator-provided, public synthetic inputs.
manifest = PartitionManifest.from_dict(manifest_mapping)
reader = VerifiedPartitionReader(
    manifest, input_root=Path("/data/synthetic/inputs"),
    task_root=Path("/data/synthetic/spool"),
)
receipt = run_partition_sessions(
    reader, config,
    executable=Path("/releases/engine/quant-backtest-engine"),
    executable_sha256=operator_verified_binary_sha256,
    output_root=Path("/data/synthetic/session-run"),
    limits=StreamLimits(wall_seconds=300, max_sessions=4096),
    resume=False,
)
# Only a successful return identifies a reconciled final result.
print(receipt["result_path"], receipt["result_sha256"])
```

The caller creates input/spool directories and owns a private absolute output
root. Keep all of them outside source and production/shared authoritative paths.
Do not share this root with a job database or a `current`/`latest` publisher.

The client sends a canonical start frame, waits for ready, then sends one session
and reads its report before requesting another verified row. It sends finish and
closes stdin before accepting final success. Header SHA-256 binds the exact start
JSON bytes excluding newline; the prefix chain hashes the previous raw 32-byte
hash plus each exact session/finish JSON frame, excluding newline.

`SessionStreamClient` also exposes this sequence directly through `start`,
`session` and `finish` within a context manager. Its reports and final result are
transport receipts; callers needing financial verification and publication should
use `run_partition_sessions`.

Session reports and `report-*.json` files are provisional. The runner binds each
report's orders, fills, valuation marks and clocks while that input remains
available. It accumulates output records and independently reconciles the full
cash, positions, fees, NAV and T+1 ledger using the existing result validator. The
finished result must exactly equal the accumulated report result, including decimal
strings. Only then can it publish `result-*.json` and complete the journal.

## Limits and process lifetime

Default policy permits 4096 sessions, 300 wall seconds, 64 MiB per frame including
newline, 8 KiB cumulative stderr and an 8 MiB journal. Smaller frame limits are
supported; larger wire frames are rejected. Strict UTF-8 JSON rejects duplicate
keys, nonfinite numbers, unexpected fields, wrong receipt sequences/hashes,
truncation and extra stdout. The verified reader independently enforces its
partition/row/line limits.

Input retention consists of a verified disk spool for one partition and the
current whole session. This interface cannot stream part of a session. Output
records, engine history, aggregate result and journal grow with session count;
this is not a constant-memory financial account API. A final result exceeding
the frame limit fails even if individual reports fit.

Nonblocking selectors drain stdout/stderr while writing stdin. During pipe waits,
deadline and cancellation are checked at most every 50 ms. Checks also occur at
row delivery; partition verification, filesystem fsync and Python validation are
synchronous work. These operations cannot be interrupted on the same 50 ms bound.
A cancellation callable returns `True` to abort.

The executable is checked as a regular file and SHA-256 verified through a held
descriptor, then executed through `/proc/self/fd`. Publish immutable binary inodes
and immutable runtime releases; do not edit either in place during execution.
The small fresh Python helper sets Linux parent-death SIGKILL and checks its
expected parent before exec. It avoids `preexec_fn` in an embedding host.
The runner terminates its owned process group before reaping the leader. Later
context cleanup does not signal an already reaped group identifier. Exception
paths perform the same group cleanup.
Abrupt parent death kills the engine. The trusted engine must not detach children
or alter its process/death-signal controls. This API does not sandbox executables.

## Journal and restart

The output root holds an exclusive nonblocking `flock`. Starting with
`resume=False` rejects an existing journal/publications instead of replacing them.
With `resume=True`, the journal must already exist. It binds the manifest digest,
canonical config, binary bytes, relevant runtime module sources and stream limits.
Changing any binding requires a separate output root.

Each acknowledgement records the next `InputCursor`, sequence, exact input prefix
hash, report digest, deterministic publication ID and row clocks. The journal is
bounded, strictly parsed and protected by a content integrity digest. That digest
detects accidental edits; it does not authenticate the operator or prevent someone
with write access from resealing a modified file. Replay still compares recorded
reports, prefixes and cursors with actual engine output.

Publications are content addressed by run binding, sequence and content digest.
Files are fsynced, linked without overwriting an existing publication, and their
directory is fsynced before an atomic fsynced journal replacement acknowledges
them. An identical existing publication is verified and deduplicated. Missing or
corrupt acknowledged files reject recovery. A crash after publication and before
journal acknowledgement replays and deduplicates the same bytes. Interrupted file
writes may leave `.pending-*` files; these are never acknowledgements or results.

Every restart launches a new engine and replays `reader.iter_rows()` from row
zero, including all acknowledged sessions. It compares each saved report,
prefix and cursor before proceeding. The input cursor alone cannot rebuild
cash, positions, orders, fills or fee state. No financial state snapshot, pickle
or volatile account restoration is provided. The publication store supports
idempotent local replay; external callbacks and other stores have no exactly-once
guarantee. Reports do not establish successful completion without a validated
finished result and complete journal.

For real-provider tests, set `BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE` to the new
reviewed binary and run `tests/test_session_stream.py`. Keep the existing
`BACKTEST_ENGINE_TEST_EXECUTABLE` on its legacy provider pin when running the full
suite. Record both source revisions and the stream binary SHA-256 in validation
receipts.
