"""Cancellation, resource failures, and recovery must persist terminal states."""

from __future__ import annotations

import os
import signal

import pytest
from test_jobs import _request, _service

from backtest_runtime import worker
from backtest_runtime.jobs import BacktestJobRequest


@pytest.mark.parametrize(
    ("failure", "expected_code", "exit_code"),
    [
        (FileNotFoundError("missing"), "INPUT_ARTIFACT_MISSING", 1),
        (ValueError("bad input"), "BACKTEST_REJECTED", 1),
        (RuntimeError("backend failed"), "BACKEND_EXECUTION_FAILED", 1),
        (
            RuntimeError("RESOURCE_LIMIT_UNAVAILABLE: synthetic"),
            "RESOURCE_LIMIT_UNAVAILABLE",
            1,
        ),
        (signal.SIGALRM, "JOB_TIMEOUT", 124),
        (signal.SIGINT, None, 130),
    ],
)
def test_worker_records_failures_and_restores_handlers(
    tmp_path, monkeypatch, failure, expected_code, exit_code
):
    store, service = _service(tmp_path)
    receipt = service.submit(BacktestJobRequest.from_mapping(_request()))
    old_handlers = {
        sig: signal.getsignal(sig)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM)
    }
    monkeypatch.setattr(worker, "_apply_memory_limit", lambda _: None)

    def fail(request, service):
        if isinstance(failure, Exception):
            raise failure
        if failure == signal.SIGINT:
            service.registry.cancel_backtest_job(receipt.job_id)
        signal.raise_signal(failure)
        pytest.fail("worker signal handler did not interrupt execution")

    monkeypatch.setattr(worker, "_run_native_backend", fail)
    try:
        assert (
            worker.execute_job(
                receipt.job_id,
                registry_path=store.database_path,
                artifact_root=tmp_path / "artifacts",
                result_root=tmp_path / "results",
            )
            == exit_code
        )
        status = service.get_status(receipt.job_id)
        assert status.status == ("CANCELLED" if failure == signal.SIGINT else "FAILED")
        assert status.error_code == expected_code
        assert not (tmp_path / "results" / receipt.job_id).exists()
        assert all(
            signal.getsignal(sig) == handler for sig, handler in old_handlers.items()
        )
    finally:
        store.close()


def test_recovery_preserves_unrelated_process_and_cleans_unpublished_files(tmp_path):
    store, service = _service(tmp_path)
    try:
        receipt = service.submit(BacktestJobRequest.from_mapping(_request()))
        service.claim(receipt.job_id, worker_pid=os.getpid(), lease_seconds=1)
        final = tmp_path / "results" / receipt.job_id
        temporary = tmp_path / "results" / f".{receipt.job_id}.tmp-interrupted"
        final.mkdir(parents=True)
        temporary.mkdir()
        assert service.recover_expired(now=store.job_clock() + 2) == 1
        status = service.get_status(receipt.job_id)
        assert status.status == "FAILED"
        assert status.error_code == "WORKER_LEASE_EXPIRED"
        assert not final.exists()
        assert not temporary.exists()
        assert service.recover_expired(now=store.job_clock() + 3) == 0
    finally:
        store.close()
