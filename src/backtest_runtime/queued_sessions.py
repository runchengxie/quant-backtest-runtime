"""Stateless staged session computation and read-only financial verification."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .partition_inputs import VerifiedPartitionReader
from .session_stream import _config, _run, _session_row, _source_identity
from .session_stream_io import StreamLimits, canonical, fields, parse
from .session_stream_journal import ReplayJournal, digest, read_file
from .session_stream_reports import Reports


def stage(
    reader: VerifiedPartitionReader,
    config: dict[str, Any],
    *,
    executable: Path,
    executable_sha256: str,
    output_root: Path,
    wall_seconds: int,
) -> dict[str, Any]:
    """Write only within the Go-owned stage. Go publishes the final directory."""
    config = _config(config)
    limits = StreamLimits(wall_seconds=wall_seconds)
    if sum(part.rows for part in reader.manifest.partitions) > limits.max_sessions:
        raise ValueError("manifest session count outside limits")
    binding = {
        "manifest_sha256": reader.manifest.sha256,
        "config_sha256": digest(canonical(config)),
        "executable_sha256": executable_sha256,
        "runtime_sha256": digest(
            canonical(
                {
                    "stream": _source_identity(),
                    "queued_sessions": digest(Path(__file__).read_bytes()),
                    "queued_session_adapter": digest(
                        Path(__file__)
                        .with_name("queued_session_adapter.py")
                        .read_bytes()
                    ),
                }
            )
        ),
        "limits": asdict(limits),
    }
    journal = StagedJournal(output_root / "stream", binding, limits, False)
    try:
        receipt = _run(
            reader, config, executable, executable_sha256, limits, None, journal
        )
    finally:
        journal.close()
    result = parse(read_file(Path(receipt["result_path"]), 64 << 20))
    return {**receipt, "summary": _summary(result)}


class StagedJournal(ReplayJournal):
    """Reserve 8 MiB for the journal and cap retained reports/result at 248 MiB."""

    def __init__(self, *args: Any):
        self.publication_bytes = 0
        super().__init__(*args)

    def _reserve(self, value: Any) -> None:
        self.publication_bytes += len(canonical(value)) + 1
        if self.publication_bytes > 248 << 20:
            raise ValueError("staged output exceeds 256 MiB")

    def acknowledge(self, report: dict[str, Any], **kwargs: Any) -> None:
        self._reserve(report)
        super().acknowledge(report, **kwargs)

    def complete(
        self, result: dict[str, Any], sequence: int, prefix_sha256: str
    ) -> dict[str, Any]:
        self._reserve(result)
        return super().complete(result, sequence, prefix_sha256)


def _journal(root: Path) -> dict[str, Any]:
    value = fields(
        parse(read_file(root / "journal.json", 8 << 20)),
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
    seal = value.pop("integrity_sha256")
    if (
        value["schema_version"] != "session_stream_journal.v1"
        or digest(canonical(value)) != seal
        or value["complete"] is not True
        or value["run_binding"] != digest(canonical(value["binding"]))
    ):
        raise ValueError("invalid completed journal")
    return value


def _publication(root: Path, entry: dict[str, Any], key: str) -> Any:
    name = entry["publication_id"]
    if not isinstance(name, str) or Path(name).name != name or "\\" in name:
        raise ValueError("unsafe publication path")
    data = read_file(root / name, 64 << 20)
    if digest(data) != entry[key]:
        raise ValueError("publication digest mismatch")
    return parse(data)


def verify(
    reader: VerifiedPartitionReader,
    config: dict[str, Any],
    *,
    output_root: Path,
) -> dict[str, Any]:
    """Reconcile reports against verified inputs without executing an engine."""
    config = _config(config)
    root = output_root / "stream"
    journal = _journal(root)
    binding = journal["binding"]
    if binding["manifest_sha256"] != reader.manifest.sha256 or binding[
        "config_sha256"
    ] != digest(canonical(config)):
        raise ValueError("journal input binding mismatch")
    history = journal["acknowledged"]
    if not isinstance(history, list) or not 1 <= len(history) <= 4096:
        raise ValueError("journal history outside limits")
    prefix = hashlib.sha256(
        canonical(
            {
                "schema_version": "engine_session_stream.v1",
                "type": "start",
                "sequence": 0,
                "config": config,
            }
        )
    ).digest()
    reports = Reports(config)
    for index, (row, cursor) in enumerate(reader.iter_rows()):
        if index >= len(history):
            raise ValueError("unacknowledged input")
        session = _session_row(row)
        sequence = index + 1
        prefix = hashlib.sha256(
            prefix
            + canonical(
                {
                    "schema_version": "engine_session_stream.v1",
                    "type": "session",
                    "sequence": sequence,
                    "session": session,
                }
            )
        ).digest()
        entry = history[index]
        metadata = {
            key: row[key] for key in ("event_id", "event_time_ns", "available_time_ns")
        }
        if (
            entry["sequence"] != sequence
            or entry["prefix_sha256"] != prefix.hex()
            or entry["next_cursor"] != cursor.to_dict()
            or entry["row_metadata"] != metadata
        ):
            raise ValueError("journal prefix/cursor mismatch")
        reports.add(_publication(root, entry, "report_sha256"), session)
    if reports.count != len(history):
        raise ValueError("journal input count mismatch")
    final = journal["result"]
    prefix = hashlib.sha256(
        prefix
        + canonical(
            {
                "schema_version": "engine_session_stream.v1",
                "type": "finish",
                "sequence": reports.count + 1,
            }
        )
    ).digest()
    if final["sequence"] != reports.count + 1 or final["prefix_sha256"] != prefix.hex():
        raise ValueError("journal final prefix mismatch")
    result = reports.finish(_publication(root, final, "sha256"))
    return {
        "result_path": str(root / final["publication_id"]),
        "result_sha256": final["sha256"],
        "run_binding": journal["run_binding"],
        "session_count": reports.count,
        "summary": _summary(result),
    }


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "cash": result["final_cash"],
        "equity": result["final_nav"],
        "commission": result["total_commission"],
    }
