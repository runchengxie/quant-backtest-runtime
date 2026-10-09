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
from pathlib import Path
from typing import Any

from .contracts import BacktestJobRequest as BacktestJobRequest
from .contracts import artifact_digest
from .store import JobStore, RegistryConflict


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
        if request.quant_run_manifest_ref is not None:
            self.validate_quant_run_manifest(request)
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

    def validate_quant_run_manifest(self, request: BacktestJobRequest) -> Any:
        """Validate the typed run manifest and every referenced component artifact."""
        from research_contracts import QuantRunManifest

        reference = request.quant_run_manifest_ref
        if reference is None:
            raise ValueError("quant_run_manifest_ref is required")
        path = self.resolve_artifact(reference)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("quant run manifest must be valid UTF-8 JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("quant run manifest must be a JSON object")
        manifest = QuantRunManifest.from_mapping(payload)
        for component in (manifest.component_refs or {}).values():
            self.resolve_artifact(f"artifact://sha256/{component.sha256}")
        return path, manifest

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
        for job_id in self.registry.list_expired_output_cleanup():
            self._remove_unpublished_output(job_id)
        return recovered

    def _remove_unpublished_output(self, job_id: str) -> None:
        shutil.rmtree(self.result_root / job_id, ignore_errors=True)
        for temporary in self.result_root.glob(f".{job_id}.tmp-*"):
            if temporary.is_dir() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)

    def resolve_artifact(self, reference: str) -> Path:
        digest = artifact_digest(reference, label="artifact ref")
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
