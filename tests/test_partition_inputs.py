"""Input verification must precede delivery and resume must preserve event order."""

import hashlib
import json
from dataclasses import replace

import pytest

from backtest_runtime.partition_inputs import (
    InputCursor,
    PartitionLimits,
    PartitionManifest,
    VerifiedPartitionReader,
)


def event(event_id, clock=None):
    return {
        "event_id": event_id,
        "event_time_ns": event_id,
        "available_time_ns": event_id if clock is None else clock,
        "payload": {"symbol": "SYNTH"},
    }


def fixture(tmp_path, groups=None, raw=None):
    root = tmp_path / "inputs"
    root.mkdir(exist_ok=True)
    task = tmp_path / "task"
    task.mkdir(exist_ok=True)
    refs = []
    for i, rows in enumerate(groups or [[event(1), event(2)], [event(3)]]):
        data = (
            raw
            if raw is not None
            else b"".join(
                (json.dumps(row, separators=(",", ":")) + "\n").encode() for row in rows
            )
        )
        (root / f"{i}.ndjson").write_bytes(data)
        refs.append(
            {
                "path": f"{i}.ndjson",
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "rows": len(rows),
                "first": [rows[0]["event_time_ns"], rows[0]["event_id"]],
                "last": [rows[-1]["event_time_ns"], rows[-1]["event_id"]],
            }
        )
    mapping = {
        "schema_version": "partition_manifest.v1",
        "row_schema": "event_envelope.v1",
        "order": "event_time_ns,event_id",
        "duplicate_policy": "reject_event_id",
        "global_order": "strict",
        "partitions": refs,
    }
    return root, task, mapping


def reader(tmp_path, groups=None, raw=None, limits=None):
    root, task, mapping = fixture(tmp_path, groups, raw)
    manifest = PartitionManifest.from_dict(mapping, limits=limits or PartitionLimits())
    return VerifiedPartitionReader(manifest, input_root=root, task_root=task), mapping


def test_batches_resume_and_source_mutation_use_verified_bytes(tmp_path):
    source, _ = reader(tmp_path)
    stream = source.iter_rows()
    first, cursor = next(stream)
    assert first["event_id"] == 1
    (tmp_path / "inputs/0.ndjson").write_text("corrupted")
    assert [row["event_id"] for row, _ in stream] == [2, 3]
    source, _ = reader(tmp_path)
    for size in (1, 2, 8):
        assert [
            r["event_id"]
            for batch in source.iter_batches(size, cursor=cursor)
            for r, _ in batch
        ] == [2, 3]
    assert InputCursor.from_dict(cursor.to_dict()) == cursor
    assert list(source.iter_rows(cursor=list(source.iter_rows())[-1][1])) == []


def test_corruption_is_rejected_before_first_yield(tmp_path):
    source, _ = reader(tmp_path)
    with (tmp_path / "inputs/0.ndjson").open("ab") as handle:
        handle.write(b"x")
    with pytest.raises(ValueError):
        next(source.iter_rows())
    assert list((tmp_path / "task").iterdir()) == []


@pytest.mark.parametrize(
    "path", ["../escape", "/etc/passwd", "nested/../0.ndjson", "link"]
)
def test_paths_cannot_escape_input_root(tmp_path, path):
    root, task, mapping = fixture(tmp_path)
    (root / "link").symlink_to(tmp_path / "outside")
    mapping["partitions"][0]["path"] = path
    with pytest.raises(ValueError):
        manifest = PartitionManifest.from_dict(mapping)
        next(
            VerifiedPartitionReader(
                manifest, input_root=root, task_root=task
            ).iter_rows()
        )


@pytest.mark.parametrize(
    "raw", [b"", b"{}", b"\n", b'{"x": NaN}\n', b"x" * 1025 + b"\n"]
)
def test_empty_truncated_invalid_and_oversize_rows(tmp_path, raw):
    with pytest.raises(ValueError):
        source, _ = reader(
            tmp_path, raw=raw, limits=replace(PartitionLimits(), max_line_bytes=1024)
        )
        next(source.iter_rows())


@pytest.mark.parametrize(
    "groups",
    [
        [[event(1), event(1)]],
        [[event(2), event(1)]],
        [[event(1)], [event(1)]],
        [[event(2)], [event(1)]],
        [[event(1, 0)]],
        [[event(1, True)]],
    ],
)
def test_duplicate_ids_and_clock_and_global_order_rejected(tmp_path, groups):
    with pytest.raises(ValueError):
        source, _ = reader(tmp_path, groups)
        list(source.iter_rows())


def test_limits_manifest_metadata_and_cursors_rejected(tmp_path):
    root, task, mapping = fixture(tmp_path)
    for limits in [
        replace(PartitionLimits(), max_partitions=1),
        replace(PartitionLimits(), max_partition_bytes=1),
        replace(PartitionLimits(), max_total_bytes=1),
        replace(PartitionLimits(), max_partition_rows=1),
        replace(PartitionLimits(), max_total_rows=1),
    ]:
        with pytest.raises(ValueError):
            PartitionManifest.from_dict(mapping, limits=limits)
    source = VerifiedPartitionReader(
        PartitionManifest.from_dict(mapping), input_root=root, task_root=task
    )
    cursor = next(source.iter_rows())[1].to_dict()
    for key, value in [
        ("manifest_sha256", "0" * 64),
        ("partition", 99),
        ("row", 99),
        ("row", True),
        ("schema_version", "cursor.v999"),
    ]:
        with pytest.raises(ValueError):
            list(source.iter_rows(cursor=InputCursor.from_dict({**cursor, key: value})))
    mapping["partitions"][0]["rows"] = 3
    source = VerifiedPartitionReader(
        PartitionManifest.from_dict(mapping), input_root=root, task_root=task
    )
    with pytest.raises(ValueError):
        next(source.iter_rows())


def test_occurrence_order_allows_delayed_reports_and_immutable_manifest(tmp_path):
    source, mapping = reader(tmp_path, [[event(1, 100), event(2, 2)]])
    mapping["partitions"][0]["rows"] = 999
    mapping["partitions"][0]["first"][0] = 999
    assert [r["available_time_ns"] for r, _ in source.iter_rows()] == [100, 2]
    with pytest.raises(ValueError):
        list(source.iter_batches(0))


def test_nonfinite_payload_rejected_before_delivery(tmp_path):
    raw = b'{"event_id":1,"event_time_ns":1,"available_time_ns":1,"payload":{"price":1e999}}\n'
    source, _ = reader(tmp_path, [[event(1)]], raw)
    with pytest.raises(ValueError):
        next(source.iter_rows())


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "0" * 64},
        {"last": [9, 9]},
        {"rows": 1, "last": [1, 1]},
    ],
)
def test_whole_partition_digest_ranges_counts_checked_before_yield(tmp_path, change):
    root, task, mapping = fixture(tmp_path)
    mapping["partitions"][0].update(change)
    with pytest.raises(ValueError):
        source = VerifiedPartitionReader(
            PartitionManifest.from_dict(mapping), input_root=root, task_root=task
        )
        next(source.iter_rows())


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "partition_manifest.v2"),
        ("duplicate_policy", "allow"),
        ("global_order", "per_partition"),
        ("row_schema", "unknown"),
        ("partitions", []),
        ("extra", True),
    ],
)
def test_unknown_manifest_contracts_rejected(tmp_path, field, value):
    _, _, mapping = fixture(tmp_path)
    mapping[field] = value
    with pytest.raises(ValueError):
        PartitionManifest.from_dict(mapping)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"event_id":1,"event_id":2,"event_time_ns":1,"available_time_ns":1,"payload":{}}\n',
        b'{"event_id":1,"event_time_ns":1,"available_time_ns":1,"payload":[]}\n',
        b"\xff\n",
    ],
)
def test_strict_json_and_payload_shape(tmp_path, raw):
    source, _ = reader(tmp_path, [[event(1)]], raw)
    with pytest.raises(ValueError):
        next(source.iter_rows())


@pytest.mark.parametrize("encoding", ["utf-16-be", "utf-32-be"])
def test_non_utf8_json_is_rejected_before_first_delivery(tmp_path, encoding):
    # json.loads(bytes) autodetects these encodings; NDJSON v1 requires UTF-8.
    raw = (json.dumps(event(1)) + "\n").encode(encoding)
    source, _ = reader(tmp_path, [[event(1)]], raw)
    with pytest.raises(ValueError):
        next(source.iter_rows())
    assert list((tmp_path / "task").iterdir()) == []
