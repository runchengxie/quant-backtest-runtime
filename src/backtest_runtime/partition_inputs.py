"""Bounded, verified NDJSON inputs. This is not an engine/job protocol or checkpoint."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


@dataclass(frozen=True)
class PartitionLimits:
    """Caller resource policy, enforced before opening partition input files."""

    max_partitions: int = 4096
    max_partition_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024 * 1024
    max_partition_rows: int = 1_000_000
    max_total_rows: int = 100_000_000
    max_line_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        for value in vars(self).values():
            _integer(value, minimum=1)


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**63 - 1:
        raise ValueError("expected bounded integer")
    return value


def _fields(value: Any, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError("unexpected schema fields")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch("[0-9a-f]{64}", value) is None:
        raise ValueError("expected lowercase SHA-256")
    return value


def _key(value: Any) -> tuple[int, int]:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("expected [event_time_ns,event_id]")
    return _integer(value[0]), _integer(value[1])


def _ordered(previous: tuple[int, int], current: tuple[int, int]) -> None:
    if current[0] < previous[0] or current[1] <= previous[1]:
        raise ValueError(
            "clocks must not decrease and event IDs must strictly increase"
        )


def _relative(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("invalid relative input path")
    parts = value.split("/")
    if PurePosixPath(value).is_absolute() or any(p in {"", ".", ".."} for p in parts):
        raise ValueError("input path must be relative without traversal")
    return value


@dataclass(frozen=True)
class PartitionRef:
    path: str
    sha256: str
    bytes: int
    rows: int
    first: tuple[int, int]
    last: tuple[int, int]

    @classmethod
    def from_dict(cls, value: Any) -> PartitionRef:
        data = _fields(value, {"path", "sha256", "bytes", "rows", "first", "last"})
        ref = cls(
            _relative(data["path"]),
            _digest(data["sha256"]),
            _integer(data["bytes"], minimum=1),
            _integer(data["rows"], minimum=1),
            _key(data["first"]),
            _key(data["last"]),
        )
        if ref.rows == 1 and ref.first != ref.last:
            raise ValueError("single row range mismatch")
        if ref.rows > 1:
            _ordered(ref.first, ref.last)
        return ref

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "rows": self.rows,
            "first": list(self.first),
            "last": list(self.last),
        }


@dataclass(frozen=True, init=False)
class PartitionManifest:
    """Validated immutable manifest; only from_dict constructs instances."""

    partitions: tuple[PartitionRef, ...]
    limits: PartitionLimits
    sha256: str

    @classmethod
    def from_dict(
        cls, value: Any, *, limits: PartitionLimits | None = None
    ) -> PartitionManifest:
        limits = limits or PartitionLimits()
        data = _fields(
            value,
            {
                "schema_version",
                "row_schema",
                "order",
                "duplicate_policy",
                "global_order",
                "partitions",
            },
        )
        fixed = {
            "schema_version": "partition_manifest.v1",
            "row_schema": "event_envelope.v1",
            "order": "event_time_ns,event_id",
            "duplicate_policy": "reject_event_id",
            "global_order": "strict",
        }
        if any(data[k] != v for k, v in fixed.items()):
            raise ValueError("unsupported manifest policy/schema")
        refs = data["partitions"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= limits.max_partitions:
            raise ValueError("partition count outside limits")
        parts = tuple(PartitionRef.from_dict(ref) for ref in refs)
        _validate_limits(parts, limits)
        canonical = {**fixed, "partitions": [p.to_dict() for p in parts]}
        digest = hashlib.sha256(
            json.dumps(
                canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
        result = object.__new__(cls)
        object.__setattr__(result, "partitions", parts)
        object.__setattr__(result, "limits", limits)
        object.__setattr__(result, "sha256", digest)
        return result


def _validate_limits(parts: tuple[PartitionRef, ...], limits: PartitionLimits) -> None:
    total_bytes = total_rows = 0
    paths: set[str] = set()
    previous = None
    for part in parts:
        if part.path in paths:
            raise ValueError("duplicate partition path")
        paths.add(part.path)
        if (
            part.bytes > limits.max_partition_bytes
            or part.rows > limits.max_partition_rows
        ):
            raise ValueError("partition outside resource limits")
        total_bytes += part.bytes
        total_rows += part.rows
        if previous is not None:
            _ordered(previous, part.first)
        previous = part.last
    if total_bytes > limits.max_total_bytes or total_rows > limits.max_total_rows:
        raise ValueError("manifest outside total resource limits")


@dataclass(frozen=True)
class InputCursor:
    """Position of the next row; financial state must be checkpointed separately."""

    manifest_sha256: str
    partition: int
    row: int

    def __post_init__(self) -> None:
        _digest(self.manifest_sha256)
        _integer(self.partition)
        _integer(self.row)

    @classmethod
    def from_dict(cls, value: Any) -> InputCursor:
        data = _fields(value, {"schema_version", "manifest_sha256", "partition", "row"})
        if data["schema_version"] != "partition_input_cursor.v1":
            raise ValueError("unsupported input cursor schema")
        return cls(data["manifest_sha256"], data["partition"], data["row"])

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "partition_input_cursor.v1",
            "manifest_sha256": self.manifest_sha256,
            "partition": self.partition,
            "row": self.row,
        }


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _bad_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number: {value}")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _parse(line: bytes) -> tuple[dict[str, Any], tuple[int, int]]:
    try:
        data = json.loads(
            line.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_bad_constant,
            parse_float=_finite_float,
        )
        row = _fields(
            data, {"event_id", "event_time_ns", "available_time_ns", "payload"}
        )
        event_id = _integer(row["event_id"])
        occurred = _integer(row["event_time_ns"])
        available = _integer(row["available_time_ns"])
        if available < occurred or not isinstance(row["payload"], dict):
            raise ValueError("invalid event clocks/payload")
        return row, (occurred, event_id)
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid JSON row") from error


def _open_input(root: Path, relative: str) -> BinaryIO:
    """Walk held directory descriptors: no symlink races or FIFO/device reads."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    directory = os.open(root, flags | os.O_DIRECTORY)
    try:
        components = relative.split("/")
        for component in components[:-1]:
            child = os.open(component, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(components[-1], flags | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ValueError("partition must be a regular file")
        return os.fdopen(descriptor, "rb")
    except OSError as error:
        raise ValueError("input path unavailable or unsafe") from error
    finally:
        os.close(directory)


class VerifiedPartitionReader:
    """Verify each complete partition into a private disk spool before delivery.

    Callers own the input allowlist root and existing task data directory. Sources
    may be concurrently replaced; delivery always reads the verified spool bytes.
    """

    def __init__(
        self, manifest: PartitionManifest, *, input_root: Path, task_root: Path
    ):
        if not input_root.is_absolute() or not task_root.is_absolute():
            raise ValueError("explicit absolute input/task roots required")
        if not input_root.is_dir() or not task_root.is_dir():
            raise ValueError("roots must be existing directories")
        self.manifest = manifest
        self.input_root = input_root.resolve(strict=True)
        self.task_root = task_root.resolve(strict=True)

    def _position(self, cursor: InputCursor | None) -> tuple[int, int]:
        if cursor is None:
            return 0, 0
        parts = self.manifest.partitions
        if cursor.manifest_sha256 != self.manifest.sha256 or cursor.partition > len(
            parts
        ):
            raise ValueError("cursor does not match manifest")
        if cursor.partition == len(parts):
            if cursor.row != 0:
                raise ValueError("invalid terminal cursor")
        elif cursor.row >= parts[cursor.partition].rows:
            raise ValueError("cursor row outside partition")
        return cursor.partition, cursor.row

    def _verify(self, ref: PartitionRef, spool: BinaryIO) -> None:
        count = size = 0
        digest = hashlib.sha256()
        first = previous = None
        with _open_input(self.input_root, ref.path) as source:
            if os.fstat(source.fileno()).st_size != ref.bytes:
                raise ValueError("partition byte count mismatch")
            while line := source.readline(
                min(self.manifest.limits.max_line_bytes, ref.bytes - size) + 1
            ):
                size += len(line)
                if size > ref.bytes or len(line) > self.manifest.limits.max_line_bytes:
                    raise ValueError("partition/line byte limit exceeded")
                if not line.endswith(b"\n"):
                    raise ValueError("truncated NDJSON row")
                _, key = _parse(line)
                if previous is not None:
                    _ordered(previous, key)
                first = first if first is not None else key
                previous = key
                count += 1
                if count > ref.rows:
                    raise ValueError("partition row count exceeded")
                digest.update(line)
                spool.write(line)
        if (size, count, first, previous, digest.hexdigest()) != (
            ref.bytes,
            ref.rows,
            ref.first,
            ref.last,
            ref.sha256,
        ):
            raise ValueError("partition digest/count/range mismatch")
        spool.seek(0)

    def iter_rows(
        self, *, cursor: InputCursor | None = None
    ) -> Iterator[tuple[dict[str, Any], InputCursor]]:
        partition, start_row = self._position(cursor)
        for index in range(partition, len(self.manifest.partitions)):
            ref = self.manifest.partitions[index]
            with tempfile.TemporaryFile(mode="w+b", dir=self.task_root) as spool:
                self._verify(ref, spool)
                for offset, line in enumerate(spool):
                    if offset < start_row:
                        continue
                    row, _ = _parse(line)
                    next_part, next_row = (
                        (index + 1, 0)
                        if offset + 1 == ref.rows
                        else (index, offset + 1)
                    )
                    yield row, InputCursor(self.manifest.sha256, next_part, next_row)
            start_row = 0

    def iter_batches(
        self, batch_size: int, *, cursor: InputCursor | None = None
    ) -> Iterator[list[tuple[dict[str, Any], InputCursor]]]:
        _integer(batch_size, minimum=1)
        if batch_size > self.manifest.limits.max_partition_rows:
            raise ValueError("batch size outside row limit")
        batch = []
        for item in self.iter_rows(cursor=cursor):
            batch.append(item)
            if len(batch) == batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
