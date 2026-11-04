"""Opt-in verified partition sessions over one engine process, with replay recovery.

This standalone API does not route queued v1-v6 jobs or restore financial state.
Reports are provisional until final EOF, reconciliation and durable completion.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .partition_inputs import VerifiedPartitionReader, _integer
from .session_stream_io import PipeProcess, StreamLimits, canonical, fields, parse
from .session_stream_journal import ReplayJournal, digest
from .session_stream_reports import Reports

__all__ = ["SessionStreamClient", "StreamLimits", "run_partition_sessions"]
_INPUT_SCHEMA = "engine_session_stream.v1"
_OUTPUT_SCHEMA = "engine_session_stream_result.v1"
_CONFIG_FIELDS = {
    "schema_version",
    "currency",
    "price_basis",
    "initial_cash",
    "fee_bps",
    "enforce_t1",
    "instruments",
    "required_capabilities",
}


def _config(value: Any) -> dict[str, Any]:
    data = fields(parse(canonical(value)), _CONFIG_FIELDS)
    if (
        data["schema_version"] != "engine_request.v2"
        or data["currency"] not in ("CNY", "USD")
        or data["price_basis"] != "raw"
        or type(data["enforce_t1"]) is not bool
    ):
        raise ValueError("unsupported session config identity")
    if any(not isinstance(data[key], str) for key in ("initial_cash", "fee_bps")):
        raise ValueError("config financial values must be strings")
    if not isinstance(data["instruments"], list) or not data["instruments"]:
        raise ValueError("config instruments required")
    for item in data["instruments"]:
        item = fields(item, {"symbol", "price_precision", "buy_lot"})
        if not isinstance(item["symbol"], str):
            raise ValueError("invalid instrument symbol")
        _integer(item["price_precision"])
        _integer(item["buy_lot"], minimum=1)
    if not isinstance(data["required_capabilities"], list) or any(
        not isinstance(v, str) for v in data["required_capabilities"]
    ):
        raise ValueError("invalid required capabilities")
    return data


class SessionStreamClient:
    """Sequential request/report transport. Context exit kills/reaps the process group."""

    def __init__(
        self,
        executable: Path,
        executable_sha256: str,
        *,
        limits: StreamLimits | None = None,
        cancelled: Callable[[], bool] | None = None,
    ):
        self.limits = limits or StreamLimits()
        self.process = PipeProcess(
            executable, executable_sha256, self.limits, cancelled
        )
        self.sequence = 0
        self.prefix = b""
        self.finished = False

    def __enter__(self) -> SessionStreamClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.process.close()

    @property
    def prefix_sha256(self) -> str:
        return self.prefix.hex()

    def start(self, config: dict[str, Any]) -> None:
        if self.prefix:
            raise ValueError("session stream already started")
        data = canonical(
            {
                "schema_version": _INPUT_SCHEMA,
                "type": "start",
                "sequence": 0,
                "config": _config(config),
            }
        )
        self.prefix = hashlib.sha256(data).digest()
        ready = fields(
            self.process.exchange(data),
            {"schema_version", "type", "sequence", "header_sha256", "max_frame_bytes"},
        )
        if (
            ready["schema_version"] != _OUTPUT_SCHEMA
            or ready["type"] != "ready"
            or type(ready["sequence"]) is not int
            or ready["sequence"] != 0
            or ready["header_sha256"] != self.prefix_sha256
            or type(ready["max_frame_bytes"]) is not int
            or ready["max_frame_bytes"] != 67108864
        ):
            raise ValueError("unsupported engine ready receipt")

    def _exchange(self, kind: str, session: dict[str, Any] | None = None) -> Any:
        if not self.prefix or self.finished:
            raise ValueError("session stream is not active")
        if kind == "session" and self.sequence >= self.limits.max_sessions:
            raise ValueError("stream session count outside limits")
        self.sequence += 1
        frame = {
            "schema_version": _INPUT_SCHEMA,
            "type": kind,
            "sequence": self.sequence,
        }
        if session is not None:
            frame["session"] = session
        data = canonical(frame)
        self.prefix = hashlib.sha256(self.prefix + data).digest()
        key = "report" if kind == "session" else "result"
        response = fields(
            self.process.exchange(data, eof=kind == "finish"),
            {"schema_version", "type", "sequence", "prefix_sha256", key},
        )
        if (
            response["schema_version"] != _OUTPUT_SCHEMA
            or response["type"] != ("session" if kind == "session" else "finished")
            or type(response["sequence"]) is not int
            or response["sequence"] != self.sequence
            or response["prefix_sha256"] != self.prefix_sha256
        ):
            raise ValueError("engine stream receipt identity/sequence/prefix mismatch")
        return response[key]

    def session(self, session: dict[str, Any]) -> dict[str, Any]:
        return self._exchange("session", session)

    def finish(self) -> dict[str, Any]:
        result = self._exchange("finish")
        self.finished = True
        self.process.finish()
        return result


def _source_identity() -> str:
    modules = [
        "session_stream",
        "session_stream_io",
        "session_stream_process",
        "session_stream_reports",
        "session_stream_journal",
        "engine_sessions_protocol",
        "engine_protocol",
        "partition_inputs",
        "durability",
    ]
    root = Path(__file__).parent
    return digest(
        canonical(
            {module: digest((root / f"{module}.py").read_bytes()) for module in modules}
        )
    )


def _session_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = fields(row["payload"], {"schema_version", "session"})
    if payload["schema_version"] != "engine_session_payload.v1":
        raise ValueError("unsupported session payload schema")
    session = fields(
        payload["session"],
        {"session_date", "open_ts_ns", "valuation_ts_ns", "quotes", "orders"},
    )
    if (
        type(session["valuation_ts_ns"]) is not int
        or session["valuation_ts_ns"] != row["event_time_ns"]
    ):
        raise ValueError("session envelope valuation clock mismatch")
    return session


def run_partition_sessions(
    reader: VerifiedPartitionReader,
    config: dict[str, Any],
    *,
    executable: Path,
    executable_sha256: str,
    output_root: Path,
    resume: bool = False,
    limits: StreamLimits | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Run/replay from row zero; return only a reconciled, durable final receipt.

    The caller owns this private output root. No SQLite, current/latest pointer,
    external callback or volatile account snapshot is updated by this function.
    """
    limits = limits or StreamLimits()
    config = _config(config)
    if sum(ref.rows for ref in reader.manifest.partitions) > limits.max_sessions:
        raise ValueError("manifest session count outside limits")
    binding = {
        "manifest_sha256": reader.manifest.sha256,
        "config_sha256": digest(canonical(config)),
        "executable_sha256": executable_sha256,
        "runtime_sha256": _source_identity(),
        "limits": asdict(limits),
    }
    journal = ReplayJournal(output_root, binding, limits, resume)
    try:
        return _run(
            reader, config, executable, executable_sha256, limits, cancelled, journal
        )
    finally:
        journal.close()


def _run(
    reader: VerifiedPartitionReader,
    config: dict[str, Any],
    executable: Path,
    executable_sha256: str,
    limits: StreamLimits,
    cancelled: Callable[[], bool] | None,
    journal: ReplayJournal,
) -> dict[str, Any]:
    reports = Reports(config)
    with SessionStreamClient(
        executable, executable_sha256, limits=limits, cancelled=cancelled
    ) as client:
        client.start(config)
        # Always replay from the beginning. A saved InputCursor cannot restore an account.
        rows = reader.iter_rows()
        try:
            for row, cursor in rows:
                client.process.check()
                session = _session_row(row)
                report = reports.add(client.session(session), session)
                journal.acknowledge(
                    report,
                    sequence=client.sequence,
                    prefix_sha256=client.prefix_sha256,
                    cursor=cursor,
                    metadata={
                        key: row[key]
                        for key in ("event_id", "event_time_ns", "available_time_ns")
                    },
                )
                del row, session, report
        finally:
            close = getattr(rows, "close", None)
            if close is not None:
                close()
        if reports.count < len(journal.data["acknowledged"]):
            raise ValueError("input ended before acknowledged prefix")
        result = reports.finish(client.finish())
        return journal.complete(result, client.sequence, client.prefix_sha256)
