"""Exclusive, fsynced publication and an integrity-digested replay journal."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .durability import prepare_result_root, sync_directory
from .partition_inputs import InputCursor, _digest, _integer
from .session_stream_io import StreamLimits, canonical, fields, parse


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_file(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("publication must be a regular file")
        data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("publication byte limit exceeded")
        return data


def commit_file(path: Path, data: bytes, *, replace: bool = False) -> None:
    """No replacement of publications; an existing identical file is durable again."""
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(staged, path)
        else:
            try:
                os.link(staged, path)
            except FileExistsError:
                if read_file(path, len(data)) != data:
                    raise ValueError("existing publication bytes mismatch") from None
                with path.open("rb") as existing:
                    os.fsync(existing.fileno())
        sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


class ReplayJournal:
    """A cursor is verification metadata; it is never a financial resume point."""

    def __init__(
        self, root: Path, binding: dict[str, Any], limits: StreamLimits, resume: bool
    ):
        if not root.is_absolute():
            raise ValueError("absolute operator output root required")
        prepare_result_root(root)
        self.root = root
        self.limits = limits
        self.lock = os.open(
            root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.binding = binding
            self.run_binding = digest(canonical(binding))
            self.data = self._load(resume)
        except BaseException:
            self.close()
            raise

    def _load(self, resume: bool) -> dict[str, Any]:
        path = self.root / "journal.json"
        if not resume:
            if any(p.name != ".lock" for p in self.root.iterdir()):
                raise ValueError(
                    "output root already contains work; explicit resume required"
                )
            result = {
                "schema_version": "session_stream_journal.v1",
                "binding": self.binding,
                "run_binding": self.run_binding,
                "acknowledged": [],
                "complete": False,
                "result": None,
            }
            self._write(result)
            return result
        data = fields(
            parse(read_file(path, self.limits.max_journal_bytes)),
            {
                "schema_version",
                "binding",
                "run_binding",
                "acknowledged",
                "complete",
                "result",
                "integrity_sha256",
            },
        )
        integrity = data.pop("integrity_sha256")
        if digest(canonical(data)) != integrity:
            raise ValueError("journal integrity mismatch")
        if data["schema_version"] != "session_stream_journal.v1":
            raise ValueError("unsupported journal version")
        if data["binding"] != self.binding or data["run_binding"] != self.run_binding:
            raise ValueError("journal run binding mismatch")
        self._validate_history(data)
        return data

    def _validate_history(self, data: dict[str, Any]) -> None:
        history = data["acknowledged"]
        if not isinstance(history, list) or len(history) > self.limits.max_sessions:
            raise ValueError("journal session count outside limits")
        for index, entry in enumerate(history):
            fields(
                entry,
                {
                    "sequence",
                    "prefix_sha256",
                    "report_sha256",
                    "publication_id",
                    "next_cursor",
                    "row_metadata",
                },
            )
            if type(entry["sequence"]) is not int or entry["sequence"] != index + 1:
                raise ValueError("journal sequence mismatch")
            _digest(entry["prefix_sha256"])
            _digest(entry["report_sha256"])
            cursor = InputCursor.from_dict(entry["next_cursor"])
            if cursor.manifest_sha256 != self.binding["manifest_sha256"]:
                raise ValueError("journal cursor manifest mismatch")
            fields(
                entry["row_metadata"],
                {"event_id", "event_time_ns", "available_time_ns"},
            )
            for value in entry["row_metadata"].values():
                _integer(value)
            expected = self.report_name(index + 1, entry["report_sha256"])
            self._verify_publication(entry, expected, "report_sha256")
        if type(data["complete"]) is not bool:
            raise ValueError("invalid journal completion flag")
        if data["complete"]:
            result = fields(
                data["result"],
                {"sequence", "prefix_sha256", "sha256", "publication_id"},
            )
            if (
                type(result["sequence"]) is not int
                or result["sequence"] != len(history) + 1
            ):
                raise ValueError("journal final sequence mismatch")
            _digest(result["prefix_sha256"])
            _digest(result["sha256"])
            self._verify_publication(
                result, self.result_name(result["sha256"]), "sha256"
            )
        elif data["result"] is not None:
            raise ValueError("incomplete journal has final receipt")

    def _verify_publication(
        self, entry: dict[str, Any], expected: str, hash_key: str
    ) -> None:
        if entry["publication_id"] != expected:
            raise ValueError("journal publication identity mismatch")
        try:
            actual = digest(
                read_file(self.root / expected, self.limits.max_frame_bytes)
            )
        except OSError as error:
            raise ValueError("acknowledged publication unavailable") from error
        if actual != entry[hash_key]:
            raise ValueError("acknowledged publication digest mismatch")

    def report_name(self, sequence: int, sha256: str) -> str:
        return f"report-{self.run_binding}-{sequence:08d}-{sha256}.json"

    def result_name(self, sha256: str) -> str:
        return f"result-{self.run_binding}-{sha256}.json"

    def _write(self, data: dict[str, Any]) -> None:
        sealed = {**data, "integrity_sha256": digest(canonical(data))}
        encoded = canonical(sealed) + b"\n"
        if len(encoded) > self.limits.max_journal_bytes:
            raise ValueError("journal byte limit exceeded")
        commit_file(self.root / "journal.json", encoded, replace=True)

    def acknowledge(
        self,
        report: dict[str, Any],
        *,
        sequence: int,
        prefix_sha256: str,
        cursor: InputCursor,
        metadata: dict[str, Any],
    ) -> None:
        data = canonical(report) + b"\n"
        if len(data) > self.limits.max_frame_bytes:
            raise ValueError("publication byte limit exceeded")
        sha256 = digest(data)
        name = self.report_name(sequence, sha256)
        entry = {
            "sequence": sequence,
            "prefix_sha256": prefix_sha256,
            "report_sha256": sha256,
            "publication_id": name,
            "next_cursor": cursor.to_dict(),
            "row_metadata": metadata,
        }
        history = self.data["acknowledged"]
        if sequence <= len(history):
            if entry != history[sequence - 1]:
                raise ValueError("replayed report/prefix/cursor mismatch")
            return
        commit_file(self.root / name, data)
        updated = {**self.data, "acknowledged": [*history, entry]}
        self._write(updated)
        self.data = updated

    def complete(
        self, result: dict[str, Any], sequence: int, prefix_sha256: str
    ) -> dict[str, Any]:
        data = canonical(result) + b"\n"
        if len(data) > self.limits.max_frame_bytes:
            raise ValueError("publication byte limit exceeded")
        sha256 = digest(data)
        name = self.result_name(sha256)
        receipt = {
            "sequence": sequence,
            "prefix_sha256": prefix_sha256,
            "sha256": sha256,
            "publication_id": name,
        }
        if self.data["complete"] and self.data["result"] != receipt:
            raise ValueError("replayed final receipt mismatch")
        commit_file(self.root / name, data)
        updated = {**self.data, "complete": True, "result": receipt}
        self._write(updated)
        self.data = updated
        return {
            "run_binding": self.run_binding,
            "result_sha256": sha256,
            "result_path": str(self.root / name),
            "session_count": sequence - 1,
        }

    def close(self) -> None:
        os.close(self.lock)
