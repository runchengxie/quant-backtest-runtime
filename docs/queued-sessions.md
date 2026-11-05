# Queued partition sessions (v7)

[中文页面](queued-sessions.zh-CN.md)

Use the Go CLI's explicit v7 mode to run verified partition sessions under the
existing job queue, cancellation, deadlines and resource controls. Install the
base Python runtime package and a reviewed Rust engine with `session-stream`.
The legacy Python extra is unnecessary for v7. Go remains the sole SQLite owner
and final result publisher. v1-v6 requests and fingerprints retain their contracts.

## Request and operator roots

Store an `engine_request.v2` config without `sessions` and a
[`partition_manifest.v1`](partition-inputs-v1.md) as immutable artifacts under
`ARTIFACTS/sha256/<exact-file-sha256>`. Every row contains the
[`engine_session_payload.v1`](session-stream.md) session envelope. The manifest
binds partition bytes and row ranges. The request uses these exact fields:

```json
{
  "schema_version": 7,
  "idempotency_key": "synthetic-partition-sessions",
  "backend": "nautilus.partition_session_replay",
  "evidence_tier": "diagnostic",
  "inputs": {
    "engine_config_ref": "artifact://sha256/<config-file-sha256>",
    "partition_manifest_ref": "artifact://sha256/<manifest-file-sha256>"
  },
  "config": {"executable_sha256": "<engine-binary-sha256>"},
  "execution": {},
  "budgets": {"wall_seconds": 300, "memory_mb": 4096}
}
```

Supply absolute operator-selected paths for `--engine-executable`,
`--python-executable` and `--partition-root`. The request supplies no filesystem
roots or commands. Partition paths are relative and reject traversal, symlinks and
special files through held directory descriptors. Keep input, database, result
and temporary storage outside source trees and production/shared data.

```sh
backtest-job --database /data/synthetic/jobs.sqlite \
  --artifact-root /data/synthetic/artifacts --result-root /data/synthetic/results \
  --engine-executable /releases/engine/quant-backtest-engine \
  --python-executable /releases/runtime/venv/bin/python \
  --partition-root /data/synthetic/inputs \
  submit /data/synthetic/job-v7.json --queue
```

Run `serve --concurrency N` with the same roots and executables to drain jobs.
`plan` accepts the same options and supports `--expected-plan-sha256` on submit.
Go discovery advertises this mode under `queued_session`. Shared Python discovery
and request parsing continue to support v1-v6.

## Verification and bounds

Before creating the database, preflight checks strict request structure, both
artifact hashes and bounded manifest metadata. It requires an exact start/ready
engine handshake under a 10-second, 256-MiB child limit. Ready proves transport
support and config decoding. Financial/config semantics and partition bodies
remain explicitly unchecked by preflight. No sessions are simulated by planning.
The plan does not scan large input bodies on each invocation.

The worker verifies and spools each whole partition before delivery, submits one
session and receives its provisional report, then reconciles the final account
with the existing financial validator. A separate verification pass rereads inputs
and reports, checking their cursors, metadata, exact prefix hashes and final result.
Go independently inventories every staged file, fsyncs and renames the stage, then
commits success through the existing SQLite compare-and-set. Python writes only
inside the supplied private stage and temporary spool. It creates no database and
never publishes the final job directory.

Policy limits are 1 MiB for each config/manifest artifact, 4096 sessions and
partitions, 256 MiB per partition, 16 GiB total declared input, 1 MiB per NDJSON
line, 64 MiB per wire frame/report/result including newline, 8 MiB per journal and
256 MiB total retained reports/result/journal. Reports and result together have a
248-MiB cap, reserving 8 MiB for the journal. Inputs must fit these bounds as whole
sessions. Large sessions require a separately reviewed input protocol.

Output history, account state and result validation grow with session count.
The default child address-space budget applies to the Python adapter and is
inherited by its engine. It does not cap their combined physical memory. Use the
[delegated per-job cgroup](go-runtime.md) to include the worker and descendants in
one memory boundary. The Go wall deadline covers computation and verification.
Result retrieval repeats bounded input/report reconciliation under the job's
wall/memory policy and rejects missing inputs or any inventory/content mismatch.
It executes no financial engine.

## Restart and evidence

A scheduler restart discovers live workers and counts their slots. Worker death
or lease expiry follows existing terminal failure/cancellation and cleanup rules.
Queued v7 does not restore accounts or automatically replay a failed job. Use a
new idempotency key for an explicitly requested new execution. The separate
[standalone API](session-stream.md) retains its explicit full-prefix replay mode.
Provisional reports and a complete private stream journal do not establish queued
job success without Go publication and the committed success state.

Synthetic acceptance tests cover real engine execution, invalid preflight without
database creation, malformed payloads, partition digest/symlink failures,
cancellation, deadline, hard worker death, scheduler restart and result tampering.
Run `tests/test_go_queued_sessions.py` with `BACKTEST_GO_EXECUTABLE` and
`BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE`. Record both exact source revisions and
binary hashes. The broader partition transport proposal remains a separate record.
