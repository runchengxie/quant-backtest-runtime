"""SQLite lifecycle store dedicated to backtest jobs."""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class RegistryConflict(RuntimeError):
    """A job key or transition conflicts with persisted state."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(self.database_path))
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS backtest_jobs (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                request_json TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('SUBMITTED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                worker_pid INTEGER,
                lease_expires_at REAL,
                cancel_requested_at TEXT,
                result_ref TEXT,
                result_sha256 TEXT,
                error_code TEXT,
                error_message TEXT,
                stdout_path TEXT,
                stderr_path TEXT
            );
        """)

    def close(self) -> None:
        self._connection.close()

    @staticmethod
    def job_clock() -> float:
        return time.time()

    def insert_backtest_job(self, job_id: str, request: Any) -> bool:
        """Insert a validated job request; return False for an existing key."""
        now = _utc_now()
        try:
            cursor = self._connection.execute(
                """
                INSERT INTO backtest_jobs (
                    job_id, idempotency_key, request_json, request_sha256, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'SUBMITTED', ?, ?)
                """,
                (
                    job_id,
                    request.idempotency_key,
                    json.dumps(
                        request.to_mapping(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    request.request_sha256,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            self._connection.rollback()
            if self.get_backtest_job_by_key(request.idempotency_key) is not None:
                return False
            raise
        self._connection.commit()
        return cursor.rowcount == 1

    def get_backtest_job(self, job_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM backtest_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def get_backtest_job_by_key(self, idempotency_key: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM backtest_jobs WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        return dict(row) if row is not None else None

    def set_backtest_job_logs(
        self, job_id: str, *, stdout_path: str, stderr_path: str
    ) -> bool:
        cursor = self._connection.execute(
            """
            UPDATE backtest_jobs
            SET stdout_path = ?, stderr_path = ?, updated_at = ?, lease_expires_at = ?
            WHERE job_id = ? AND status = 'SUBMITTED'
            """,
            (stdout_path, stderr_path, _utc_now(), self.job_clock() + 60, job_id),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def transition_backtest_job(
        self,
        job_id: str,
        *,
        expected: str,
        status: str,
        **values: Any,
    ) -> dict[str, Any] | None:
        allowed = {
            "worker_pid",
            "lease_expires_at",
            "result_ref",
            "result_sha256",
            "error_code",
            "error_message",
        }
        if set(values) - allowed:
            raise ValueError("unsupported backtest job state field")
        legal_expected = {
            "RUNNING": {"SUBMITTED"},
            "SUCCEEDED": {"RUNNING"},
            "FAILED": {"RUNNING", "SUBMITTED"},
            "CANCELLED": {"SUBMITTED", "RUNNING"},
        }
        if expected not in legal_expected.get(status, set()):
            raise ValueError(f"illegal backtest job transition: {expected} -> {status}")
        updated_at = _utc_now()
        assignments = ["status = ?", "updated_at = ?"]
        parameters: list[Any] = [status, updated_at]
        for key, value in values.items():
            assignments.append(f"{key} = ?")
            parameters.append(value)
        if status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            assignments.extend(["lease_expires_at = NULL", "worker_pid = NULL"])
        parameters.extend([job_id, expected])
        condition = " AND cancel_requested_at IS NULL" if status == "SUCCEEDED" else ""
        cursor = self._connection.execute(
            "UPDATE backtest_jobs SET "
            + ", ".join(assignments)
            + " WHERE job_id = ? AND status = ?"
            + condition,
            parameters,
        )
        self._connection.commit()
        return self.get_backtest_job(job_id) if cursor.rowcount == 1 else None

    def cancel_backtest_job(self, job_id: str) -> dict[str, Any] | None:
        now = _utc_now()
        cursor = self._connection.execute(
            """
            UPDATE backtest_jobs
            SET status = 'CANCELLED', updated_at = ?, cancel_requested_at = ?
            WHERE job_id = ? AND status = 'SUBMITTED'
            """,
            (now, now, job_id),
        )
        if cursor.rowcount != 1:
            cursor = self._connection.execute(
                """
                UPDATE backtest_jobs
                SET cancel_requested_at = COALESCE(cancel_requested_at, ?), updated_at = ?
                WHERE job_id = ? AND status = 'RUNNING'
                """,
                (now, now, job_id),
            )
        self._connection.commit()
        return self.get_backtest_job(job_id) if cursor.rowcount == 1 else None

    def list_expired_backtest_jobs(
        self, *, now: float | None = None
    ) -> list[dict[str, Any]]:
        timestamp = self.job_clock() if now is None else now
        rows = self._connection.execute(
            """
            SELECT * FROM backtest_jobs
            WHERE status = 'RUNNING' AND lease_expires_at < ?
            ORDER BY lease_expires_at, job_id
            """,
            (timestamp,),
        ).fetchall()
        return [dict(row) for row in rows]

    def expire_unclaimed_backtest_jobs(self, *, now: float) -> int:
        """Fail worker launches that never made it to the RUNNING claim."""
        cursor = self._connection.execute(
            """
            UPDATE backtest_jobs
            SET status = 'FAILED', updated_at = ?, lease_expires_at = NULL,
                error_code = 'WORKER_START_EXPIRED',
                error_message = 'Worker exited before claiming the submitted job.'
            WHERE status = 'SUBMITTED' AND lease_expires_at < ?
            """,
            (_utc_now(), now),
        )
        self._connection.commit()
        return cursor.rowcount

    def expire_backtest_job(self, job_id: str, *, now: float) -> bool:
        row = self._connection.execute(
            """
            SELECT cancel_requested_at FROM backtest_jobs
            WHERE job_id = ? AND status = 'RUNNING' AND lease_expires_at < ?
            """,
            (job_id, now),
        ).fetchone()
        if row is None:
            return False
        cancelled = row["cancel_requested_at"] is not None
        now_text = _utc_now()
        cursor = self._connection.execute(
            """
            UPDATE backtest_jobs
            SET status = ?, updated_at = ?, worker_pid = NULL, lease_expires_at = NULL,
                error_code = ?, error_message = ?
            WHERE job_id = ? AND status = 'RUNNING' AND lease_expires_at < ?
            """,
            (
                "CANCELLED" if cancelled else "FAILED",
                now_text,
                None if cancelled else "WORKER_LEASE_EXPIRED",
                None
                if cancelled
                else "Worker process ended before publishing a terminal result.",
                job_id,
                now,
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def list_expired_output_cleanup(self) -> list[str]:
        """Retain cleanup eligibility after a lease expiration state commit."""
        rows = self._connection.execute(
            "SELECT job_id FROM backtest_jobs WHERE status='FAILED' "
            "AND error_code IN ('WORKER_LEASE_EXPIRED','WORKER_START_EXPIRED') "
            "AND worker_pid IS NULL ORDER BY job_id"
        ).fetchall()
        return [row["job_id"] for row in rows]
