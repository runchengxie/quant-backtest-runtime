# Verified partition inputs v1

[中文页面](partition-inputs-v1.zh-CN.md)

`backtest_runtime.partition_inputs` provides an opt-in Python input reader. It verifies
one complete immutable NDJSON partition before yielding any row from that partition.
The reader has no job submission, strategy, matching, portfolio or publication API.
It is not connected to the active engine transport. Existing v1-v6 job requests,
engine requests, fingerprints, limits and runtime pins remain unchanged.

## Manifest and row schema

Construct an immutable `PartitionManifest` with `PartitionManifest.from_dict(mapping,
limits=PartitionLimits(...))`. Unknown fields, policies and schema versions fail.
The caller supplies a mapping already parsed under its own manifest transport size
limit. Construction copies primitive fields into frozen records and tuples. Subsequent
mutations of the source mapping cannot change the manifest or its identity.

```json
{
  "schema_version": "partition_manifest.v1",
  "row_schema": "event_envelope.v1",
  "order": "event_time_ns,event_id",
  "duplicate_policy": "reject_event_id",
  "global_order": "strict",
  "partitions": [
    {
      "path": "synthetic/0001.ndjson",
      "sha256": "<64 lowercase hexadecimal characters>",
      "bytes": 123,
      "rows": 1,
      "first": [100, 1],
      "last": [100, 1]
    }
  ]
}
```

The digest above is a placeholder. `bytes` counts exact file bytes including each
required final newline. Each nonempty partition declares its positive row count and
inclusive first/last `[event_time_ns,event_id]`. Empty manifests and partitions are
rejected. Paths are unique relative POSIX paths. Absolute paths, empty components,
`.`/`..`, backslashes, NULs and symlinks at any traversed component are rejected.
The reader walks held directory descriptors with `O_NOFOLLOW` and only opens regular
files. The caller explicitly authorizes the existing absolute `input_root`. This
API does not discover or broaden an allowlist.

Each UTF-8 NDJSON line has exactly this envelope.

```json
{"event_id":1,"event_time_ns":100,"available_time_ns":150,"payload":{"symbol":"SYNTH"}}
```

IDs and clocks are nonnegative signed-64-bit integers (booleans are rejected).
`event_time_ns` is the occurrence time. `available_time_ns >= event_time_ns` is the
information availability time. Within and across partitions, occurrence time cannot
decrease and integer event IDs must strictly increase. IDs are stable replay ordinals,
not symbol IDs. This rejects duplicate IDs without retaining a dataset-sized set.
There is no sorting, deduplication or silent repair. `available_time_ns` may decrease.
An early fill with a delayed report may precede a later immediately visible quote.
A consumer must separately enforce information visibility. The reader yields truth
in occurrence order and does not decide what a strategy may see.

`payload` is a JSON object interpreted by the consumer. Duplicate JSON keys,
non-finite numbers (including exponent overflow), malformed JSON, blank lines and
truncated final lines fail. The envelope is not a financial observation schema.

## Resource and verification boundary

The configurable default `PartitionLimits` are 4096 partitions, 256 MiB per
partition, 16 GiB total, 1,000,000 rows per partition, 100,000,000 rows total and
1 MiB per line including its newline. Every limit must be a positive bounded integer.
Declared partition and total sizes/counts are checked before input files are opened.
Actual bytes and counts cannot exceed their declarations. These are reader resource
policies, not changes to any engine's 64 MiB transport limit.

For each partition, the reader reads at most one bounded line at a time, validates
all rows, ordering, exact byte/row counts, ranges and SHA-256, and spools exact bytes
into a private temporary file under the caller's explicit existing absolute
`task_root`. Only after verification succeeds does it replay those spooled bytes.
Source replacement or mutation after verification cannot change delivered rows.
A later corrupt partition can fail after earlier verified partitions were consumed.
There is no whole-dataset atomic delivery guarantee. The temporary file is removed
on partition completion, failure or iterator close. Call `close()` on an abandoned
iterator to release its descriptor promptly. Disk usage is bounded by one declared
partition. Row iteration memory is bounded by the line limit. Batched iteration also
retains at most the requested batch size, which must be positive and no greater than
the per-partition row limit. Choose smaller caller limits to fit available resources.
Input/task roots and spool storage must be operator-controlled. This is an integrity
reader, not an operating-system sandbox against a hostile same-user process.

## Cursors and consumer integration

```python
from pathlib import Path
from backtest_runtime.partition_inputs import PartitionManifest, VerifiedPartitionReader

manifest = PartitionManifest.from_dict(manifest_mapping)
reader = VerifiedPartitionReader(
    manifest, input_root=Path("/data/approved-inputs"), task_root=Path("/data/tasks/run-1")
)
for row, next_cursor in reader.iter_rows():
    consume(row)  # Consumer owns financial validation and information visibility.
    save_together(consumer_state(), next_cursor.to_dict())
```

`InputCursor.to_dict()` has exactly `schema_version: partition_input_cursor.v1`,
`manifest_sha256`, `partition` and `row`. Positions are zero-based and identify the
next row. The end of a partition normalizes to the next partition, row zero.
Terminal position is `(len(partitions),0)`. `InputCursor.from_dict()` and the reader
reject unknown schemas/fields, wrong types, negative/out-of-range positions and
manifest mismatches. In-range positions are seek instructions, not authenticated
proof that preceding financial work happened. Resume re-verifies the entire
selected partition before skipping preceding rows. It does not read earlier
partitions or guarantee their continued existence. Their identity remains manifest-bound.

The manifest digest is SHA-256 of its normalized JSON with sorted keys, ASCII
escaping and compact separators. It includes partition order, paths, digest, bytes,
rows, ranges and all schema/policy fields. Resource limits are caller policy and
are excluded. Each emitted cursor binds this identity. `iter_batches(batch_size,
cursor=...)` groups the same `(row,next_cursor)` pairs. Batch size does not affect
event order or cursor identity.

An input cursor is not an engine checkpoint. The consumer must atomically bind
its own versioned financial state, configuration and source revision to this cursor,
validate the envelope payload and reject incompatible checkpoints. The reader does
not claim that re-reading alone restores cash, orders, fees or pending information.
See the separate [v7 transport proposal](plans/partition-transport-v7.md) for the
unactivated integration boundary.
