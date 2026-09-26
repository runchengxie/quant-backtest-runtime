"""Durable local backtest job contracts and state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import shutil
import signal
import sys
import uuid
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .store import JobStore, RegistryConflict

_DIGEST_REF = re.compile(r"artifact://sha256/([0-9a-f]{64})\Z")
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_INPUT_KEYS = {"positions_ref", "pricing_ref", "periods_ref", "intraday_bars_ref"}
_CONFIG_KEYS = {
    "price_col",
    "entry_price_col",
    "exit_price_col",
    "transaction_cost_bps",
    "trading_days_per_year",
    "long_only",
    "preserve_gross_exposure",
    "exit_price_policy",
    "exit_fallback_policy",
    "tradable_col",
}
_EXECUTION_KEYS = {
    "ledger",
    "ledger_config",
    "allow_stale_execution_price",
    "intraday_execution_assumption",
}
_LEDGER_CONFIG_KEYS = {
    "enabled",
    "portfolio_value",
    "participation_rate",
    "liquidity_cols",
    "liquidity_notional_multiplier",
    "buy_max_days",
    "sell_max_days",
    "zero_fill_abort_days_buy",
    "unfilled_buy_action",
    "unfilled_sell_action",
    "round_lot",
    "enforce_t1",
    "enforce_price_limits",
    "enforce_listing_status",
    "limit_up_col",
    "limit_down_col",
    "listing_status_col",
    "lot_tolerance",
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _exact_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    missing = expected - set(value)
    extra = set(value) - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if extra:
            details.append("unknown: " + ", ".join(sorted(extra)))
        raise ValueError(f"{label} keys invalid ({'; '.join(details)})")
    return value


def _artifact_digest(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_REF.fullmatch(value):
        parsed = urlsplit(value if isinstance(value, str) else "")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                f"{label} must not contain credentials, query, or fragment"
            )
        raise ValueError(f"{label} must be artifact://sha256/<lowercase-sha256>")
    return value.rsplit("/", 1)[-1]


def _validate_request_identity(mapping: dict[str, Any]) -> tuple[str, str]:
    version = mapping["schema_version"]
    if type(version) is not int or version not in {1, 2}:
        raise ValueError("schema_version must be integer 1 or 2")
    key = mapping["idempotency_key"]
    if not isinstance(key, str) or not _IDEMPOTENCY_KEY.fullmatch(key):
        raise ValueError("idempotency_key must be 1–128 safe ASCII characters")
    if mapping["backend"] != "native.position_replay":
        raise ValueError("backend must be native.position_replay in v1")
    evidence_tier = mapping["evidence_tier"]
    if evidence_tier != ("diagnostic" if version == 1 else "execution_aware"):
        raise ValueError("unsupported evidence_tier")
    return key, evidence_tier


def _validate_inputs(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) not in (
        {"positions_ref", "pricing_ref", "periods_ref"},
        {"positions_ref", "pricing_ref", "periods_ref", "intraday_bars_ref"},
    ):
        raise ValueError(
            "inputs must contain positions_ref, pricing_ref, periods_ref "
            "and optional intraday_bars_ref"
        )
    return {
        name: f"artifact://sha256/{_artifact_digest(ref, label=name)}"
        for name, ref in value.items()
    }


def _require_string_fields(
    values: dict[str, Any], names: tuple[str, ...], *, label: str
) -> None:
    for name in names:
        if (
            not isinstance(values[name], str)
            or not values[name].strip()
            or len(values[name]) > 128
        ):
            raise ValueError(f"{label}.{name} must be a non-empty string")


def _require_boolean_fields(
    values: dict[str, Any], names: tuple[str, ...], *, label: str
) -> None:
    for name in names:
        if type(values[name]) is not bool:
            raise ValueError(f"{label}.{name} must be boolean")


def _validate_position_config(config: dict[str, Any]) -> None:
    _require_string_fields(config, ("price_col",), label="config")
    for name in ("entry_price_col", "exit_price_col", "tradable_col"):
        value = config[name]
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 128
        ):
            raise ValueError(f"config.{name} must be a non-empty string or null")
    if (
        isinstance(config["transaction_cost_bps"], bool)
        or not isinstance(config["transaction_cost_bps"], (int, float))
        or not isfinite(config["transaction_cost_bps"])
        or config["transaction_cost_bps"] < 0
    ):
        raise ValueError("config.transaction_cost_bps must be a non-negative number")
    if (
        type(config["trading_days_per_year"]) is not int
        or config["trading_days_per_year"] <= 0
    ):
        raise ValueError("config.trading_days_per_year must be a positive integer")
    _require_boolean_fields(
        config, ("long_only", "preserve_gross_exposure"), label="config"
    )
    if not isinstance(config["exit_price_policy"], str) or config[
        "exit_price_policy"
    ] not in {"period", "strict", "ffill", "delay"}:
        raise ValueError("config.exit_price_policy is unsupported")
    if not isinstance(config["exit_fallback_policy"], str) or config[
        "exit_fallback_policy"
    ] not in {"ffill", "none"}:
        raise ValueError("config.exit_fallback_policy is unsupported")


def _validate_ledger_numeric_fields(ledger_config: dict[str, Any]) -> None:
    for name in (
        "portfolio_value",
        "participation_rate",
        "liquidity_notional_multiplier",
    ):
        value = ledger_config[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive finite number"
            )
    for name in ("buy_max_days", "sell_max_days"):
        if type(ledger_config[name]) is not int or ledger_config[name] <= 0:
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive integer"
            )
    for name in ("zero_fill_abort_days_buy", "round_lot"):
        value = ledger_config[name]
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(
                f"execution.ledger_config.{name} must be a positive integer or null"
            )
    if (
        isinstance(ledger_config["lot_tolerance"], bool)
        or not isinstance(ledger_config["lot_tolerance"], (int, float))
        or not isfinite(ledger_config["lot_tolerance"])
        or ledger_config["lot_tolerance"] < 0
    ):
        raise ValueError(
            "execution.ledger_config.lot_tolerance must be a non-negative finite number"
        )


def _validate_ledger_behavior_fields(ledger_config: dict[str, Any]) -> None:
    if ledger_config["unfilled_buy_action"] != "keep_cash":
        raise ValueError(
            "execution.ledger_config.unfilled_buy_action must be keep_cash"
        )
    if ledger_config["unfilled_sell_action"] != "keep_position":
        raise ValueError(
            "execution.ledger_config.unfilled_sell_action must be keep_position"
        )
    if (
        not isinstance(ledger_config["liquidity_cols"], list)
        or not ledger_config["liquidity_cols"]
        or len(ledger_config["liquidity_cols"]) > 32
        or any(
            not isinstance(col, str) or not col.strip() or len(col) > 128
            for col in ledger_config["liquidity_cols"]
        )
    ):
        raise ValueError(
            "execution.ledger_config.liquidity_cols must be an array of strings"
        )
    for name in ("limit_up_col", "limit_down_col", "listing_status_col"):
        value = ledger_config[name]
        if value is not None and (
            not isinstance(value, str) or not value.strip() or len(value) > 128
        ):
            raise ValueError(
                f"execution.ledger_config.{name} must be a non-empty string or null"
            )


def _validate_ledger_config(ledger_config: dict[str, Any]) -> None:
    _require_boolean_fields(
        ledger_config,
        ("enabled", "enforce_t1", "enforce_price_limits", "enforce_listing_status"),
        label="execution.ledger_config",
    )
    _validate_ledger_numeric_fields(ledger_config)
    _validate_ledger_behavior_fields(ledger_config)


def _validate_configs(mapping: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = _exact_keys(mapping["config"], _CONFIG_KEYS, label="config")
    execution = _exact_keys(mapping["execution"], _EXECUTION_KEYS, label="execution")
    ledger_config = _exact_keys(
        execution["ledger_config"], _LEDGER_CONFIG_KEYS, label="execution.ledger_config"
    )
    if (
        type(execution["ledger"]) is not bool
        or type(ledger_config["enabled"]) is not bool
    ):
        raise ValueError(
            "execution.ledger and execution.ledger_config.enabled must be boolean"
        )
    if execution["ledger"] != ledger_config["enabled"]:
        raise ValueError("execution.ledger must match execution.ledger_config.enabled")
    if type(execution["allow_stale_execution_price"]) is not bool:
        raise ValueError("execution.allow_stale_execution_price must be boolean")
    if execution["intraday_execution_assumption"] is not None and not isinstance(
        execution["intraday_execution_assumption"], str
    ):
        raise ValueError(
            "execution.intraday_execution_assumption must be a string or null"
        )
    if execution["intraday_execution_assumption"] not in {
        None,
        "signal_before_session",
        "caller_windowed",
    }:
        raise ValueError("execution.intraday_execution_assumption is unsupported")
    if (
        config["exit_price_policy"] == "ffill"
        and not execution["allow_stale_execution_price"]
    ):
        raise ValueError("ffill exit pricing requires explicit stale-price opt-in")
    if ("intraday_bars_ref" in mapping["inputs"]) != (
        execution["intraday_execution_assumption"] is not None
    ):
        raise ValueError(
            "intraday bars and their execution assumption must be supplied together"
        )
    _validate_position_config(config)
    _validate_ledger_config(ledger_config)
    return dict(config), {
        "ledger": execution["ledger"],
        "ledger_config": dict(ledger_config),
        "allow_stale_execution_price": execution["allow_stale_execution_price"],
        "intraday_execution_assumption": execution["intraday_execution_assumption"],
    }


def _validate_budgets(value: Any) -> dict[str, int]:
    budgets = _exact_keys(value, {"wall_seconds", "memory_mb"}, label="budgets")
    for name, low, high in (("wall_seconds", 1, 86400), ("memory_mb", 256, 65536)):
        number = budgets[name]
        if type(number) is not int or not low <= number <= high:
            raise ValueError(f"budgets.{name} must be an integer from {low} to {high}")
    return dict(budgets)


@dataclass(frozen=True, slots=True)
class BacktestJobRequest:
    """Validated, versioned immutable request stored in the job ledger."""

    schema_version: int
    idempotency_key: str
    backend: str
    evidence_tier: str
    request_sha256: str
    _request_json: str

    @property
    def inputs(self) -> dict[str, str | None]:
        return json.loads(self._request_json)["inputs"]

    @property
    def config(self) -> dict[str, Any]:
        return json.loads(self._request_json)["config"]

    @property
    def execution(self) -> dict[str, Any]:
        return json.loads(self._request_json)["execution"]

    @property
    def budgets(self) -> dict[str, int]:
        return json.loads(self._request_json)["budgets"]

    @property
    def research_clock(self) -> dict[str, Any] | None:
        return self.to_mapping().get("research_clock")

    @property
    def producer(self) -> dict[str, Any] | None:
        return self.to_mapping().get("producer")

    @classmethod
    def from_mapping(cls, value: Any) -> BacktestJobRequest:
        expected = {
            "schema_version",
            "idempotency_key",
            "inputs",
            "backend",
            "evidence_tier",
            "config",
            "execution",
            "budgets",
        }
        if not isinstance(value, dict):
            raise TypeError("request must be a JSON object")
        version = value.get("schema_version")
        if type(version) is int and version == 2:
            expected |= {"research_clock", "producer"}
        mapping = _exact_keys(value, expected, label="request")
        key, evidence_tier = _validate_request_identity(mapping)
        inputs = _validate_inputs(mapping["inputs"])
        config, execution = _validate_configs(mapping)
        budgets = _validate_budgets(mapping["budgets"])
        if version == 2:
            from research_contracts import ProducerIdentity, validate_research_clock

            if not execution["ledger"]:
                raise ValueError("execution-aware jobs require execution.ledger=true")
            clock = mapping["research_clock"]
            if not isinstance(clock, dict):
                raise ValueError("research_clock must be an object")
            validate_research_clock(clock, require_execution=True)
            producer = mapping["producer"]
            if not isinstance(producer, dict):
                raise ValueError("producer must be an object")
            ProducerIdentity.from_mapping(producer)
            if producer.get("backend") != mapping["backend"]:
                raise ValueError("producer.backend must match backend")
        normalized = {
            "schema_version": version,
            "idempotency_key": key,
            "inputs": inputs,
            "backend": "native.position_replay",
            "evidence_tier": evidence_tier,
            "config": config,
            "execution": execution,
            "budgets": budgets,
        }
        if version == 2:
            normalized["research_clock"] = clock
            normalized["producer"] = producer
        request_json = _canonical_json(normalized)
        request_bytes = request_json.encode("utf-8")
        if len(request_bytes) > 64 * 1024:
            raise ValueError("serialized backtest job request must not exceed 64 KiB")
        digest = hashlib.sha256(request_bytes).hexdigest()
        return cls(
            schema_version=version,
            idempotency_key=key,
            backend="native.position_replay",
            evidence_tier=evidence_tier,
            request_sha256=digest,
            _request_json=request_json,
        )

    def to_mapping(self) -> dict[str, Any]:
        return json.loads(self._request_json)


@dataclass(frozen=True, slots=True)
class JobReceipt:
    job_id: str
    status: str
    created: bool


@dataclass(frozen=True, slots=True)
class BacktestJobResultRef:
    uri: str
    manifest_sha256: str


@dataclass(frozen=True, slots=True)
class JobStatus:
    job_id: str
    status: str
    created_at: str
    updated_at: str
    cancel_requested_at: str | None
    result_ref: str | None
    result_sha256: str | None
    error_code: str | None
    error_message: str | None
    worker_pid: int | None


class BacktestJobService:
    """Job lifecycle facade backed by an additive SQLite Registry table."""

    def __init__(
        self,
        registry: JobStore,
        *,
        artifact_root: str | Path,
        result_root: str | Path,
    ) -> None:
        self.registry = registry
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.result_root = Path(result_root).expanduser().resolve()

    def submit(self, request: BacktestJobRequest) -> JobReceipt:
        if not sys.platform.startswith("linux"):
            message = (
                "local backtest jobs v1 requires Linux resource and process controls"
            )
            raise RuntimeError(message)
        checked_request = BacktestJobRequest.from_mapping(request.to_mapping())
        if (
            checked_request.request_sha256 != request.request_sha256
            or checked_request.idempotency_key != request.idempotency_key
            or checked_request.backend != request.backend
            or checked_request.evidence_tier != request.evidence_tier
            or checked_request.schema_version != request.schema_version
        ):
            raise ValueError(
                "BacktestJobRequest fields do not match the stored fingerprint"
            )
        request = checked_request
        existing = self.registry.get_backtest_job_by_key(request.idempotency_key)
        if existing is not None:
            if existing["request_sha256"] != request.request_sha256:
                raise RegistryConflict(
                    "idempotency key is already bound to a different backtest request"
                )
            return JobReceipt(existing["job_id"], existing["status"], False)
        job_id = str(uuid.uuid4())
        created = self.registry.insert_backtest_job(job_id, request)
        if not created:
            existing = self.registry.get_backtest_job_by_key(request.idempotency_key)
            if existing is None or existing["request_sha256"] != request.request_sha256:
                raise RegistryConflict(
                    "idempotency key is already bound to a different backtest request"
                )
            return JobReceipt(existing["job_id"], existing["status"], False)
        return JobReceipt(job_id, "SUBMITTED", True)

    def get_status(self, job_id: str) -> JobStatus:
        row = self.registry.get_backtest_job(job_id)
        if row is None:
            raise KeyError(f"Unknown backtest job: {job_id}")
        return _status(row)

    def get_result(self, job_id: str) -> BacktestJobResultRef:
        status = self.get_status(job_id)
        if (
            status.status != "SUCCEEDED"
            or status.result_ref is None
            or status.result_sha256 is None
        ):
            raise RegistryConflict(f"backtest job {job_id} has no successful result")
        return BacktestJobResultRef(status.result_ref, status.result_sha256)

    def claim(
        self, job_id: str, *, worker_pid: int, lease_seconds: float
    ) -> JobStatus | None:
        if type(worker_pid) is not int or worker_pid <= 0:
            raise ValueError("worker_pid must be a positive integer")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed = self.registry.transition_backtest_job(
            job_id,
            expected="SUBMITTED",
            status="RUNNING",
            worker_pid=worker_pid,
            lease_expires_at=self.registry.job_clock() + lease_seconds,
        )
        return _status(claimed) if claimed is not None else None

    def complete(
        self, job_id: str, *, result_ref: str, result_sha256: str
    ) -> JobStatus:
        if not re.fullmatch(r"backtest-job://[0-9a-f-]{36}", result_ref):
            raise ValueError("result_ref must be a backtest-job UUID reference")
        if not re.fullmatch(r"[0-9a-f]{64}", result_sha256):
            raise ValueError("result_sha256 must be a lowercase SHA-256 digest")
        return self._terminal(
            job_id,
            status="SUCCEEDED",
            result_ref=result_ref,
            result_sha256=result_sha256,
        )

    def fail(self, job_id: str, *, code: str, message: str) -> JobStatus:
        return self._terminal(
            job_id,
            status="FAILED",
            error_code=code,
            error_message=message[:2000],
        )

    def fail_submitted(self, job_id: str, *, code: str, message: str) -> JobStatus:
        row = self.registry.transition_backtest_job(
            job_id,
            expected="SUBMITTED",
            status="FAILED",
            error_code=code,
            error_message=message[:2000],
        )
        if row is None:
            return self.get_status(job_id)
        return _status(row)

    def acknowledge_cancel(self, job_id: str) -> JobStatus:
        return self._terminal(job_id, status="CANCELLED")

    def _terminal(self, job_id: str, *, status: str, **values: Any) -> JobStatus:
        row = self.registry.transition_backtest_job(
            job_id,
            expected="RUNNING",
            status=status,
            **values,
        )
        if row is None:
            current = self.registry.get_backtest_job(job_id)
            if current is None:
                raise KeyError(f"Unknown backtest job: {job_id}")
            raise RegistryConflict(
                f"cannot change terminal or non-running job {job_id} from {current['status']}"
            )
        return _status(row)

    def cancel(self, job_id: str) -> JobStatus:
        row = self.registry.cancel_backtest_job(job_id)
        if row is None:
            current = self.registry.get_backtest_job(job_id)
            if current is None:
                raise KeyError(f"Unknown backtest job: {job_id}")
            return _status(current)
        if row["status"] == "RUNNING" and row["worker_pid"] is not None:
            is_worker, proc_available = _is_job_worker(row["worker_pid"], job_id)
            if not proc_available or not is_worker:
                return _status(row)
            try:
                os.kill(row["worker_pid"], signal.SIGINT)
            except ProcessLookupError:
                pass
            except PermissionError:
                raise RuntimeError(
                    "Cannot signal the backtest worker process"
                ) from None
        return _status(row)

    def recover_expired(self, *, now: float | None = None) -> int:
        timestamp = self.registry.job_clock() if now is None else now
        recovered = self.registry.expire_unclaimed_backtest_jobs(now=timestamp)
        for row in self.registry.list_expired_backtest_jobs(now=timestamp):
            pid = row["worker_pid"]
            if pid is not None:
                is_worker, proc_available = _is_job_worker(pid, row["job_id"])
                if not proc_available:
                    continue
                if is_worker and not _terminate_expired_worker(pid, row["job_id"]):
                    continue
            expired = self.registry.expire_backtest_job(row["job_id"], now=timestamp)
            recovered += int(expired)
            if expired:
                self._remove_unpublished_output(row["job_id"])
        return recovered

    def _remove_unpublished_output(self, job_id: str) -> None:
        shutil.rmtree(self.result_root / job_id, ignore_errors=True)
        for temporary in self.result_root.glob(f".{job_id}.tmp-*"):
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)

    def resolve_artifact(self, reference: str) -> Path:
        digest = _artifact_digest(reference, label="artifact ref")
        root = self.artifact_root / "sha256"
        path = root / digest
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"Immutable input artifact is missing: {reference}")
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()):
            raise ValueError("Artifact resolves outside the configured artifact root")
        calculated = hashlib.sha256()
        with resolved.open("rb") as artifact_file:
            for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                calculated.update(chunk)
        if calculated.hexdigest() != digest:
            raise ValueError(f"Artifact digest mismatch for {reference}")
        return resolved


def _is_job_worker(pid: int, job_id: str) -> tuple[bool, bool]:
    """Return (matching_worker, /proc_available), preventing PID reuse signals."""
    cmdline_path = Path("/proc") / str(pid) / "cmdline"
    try:
        arguments = cmdline_path.read_bytes().split(b"\0")
    except FileNotFoundError:
        return False, True
    except OSError:
        return False, False
    return (
        b"backtest_runtime.worker" in arguments and job_id.encode("ascii") in arguments,
        True,
    )


def _terminate_expired_worker(pid: int, job_id: str) -> bool:
    """Kill a matching expired worker and confirm it exited before cleanup."""
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or pidfd_send_signal is None:
        return False
    try:
        pidfd = pidfd_open(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    try:
        is_worker, proc_available = _is_job_worker(pid, job_id)
        if not proc_available:
            return False
        if not is_worker:
            return True
        try:
            pidfd_send_signal(pidfd, signal.SIGKILL)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)
        return bool(poller.poll(2000))
    finally:
        os.close(pidfd)


def _status(row: dict[str, Any]) -> JobStatus:
    return JobStatus(
        job_id=row["job_id"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        cancel_requested_at=row["cancel_requested_at"],
        result_ref=row["result_ref"],
        result_sha256=row["result_sha256"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        worker_pid=row["worker_pid"],
    )
