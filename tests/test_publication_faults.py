"""Faults at publication and cleanup boundaries must remain recoverable."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from test_jobs import _request, _service

from backtest_runtime.jobs import BacktestJobRequest, JobReceipt
from backtest_runtime.store import JobStore


def _result():
    from types import SimpleNamespace

    import pandas as pd

    return SimpleNamespace(
        validate=lambda: None,
        backend_name="synthetic",
        capabilities=SimpleNamespace(to_mapping=lambda: {}),
        summary={},
        metadata={},
        frames=lambda: {"daily": pd.DataFrame({"value": [1.0]})},
    )


def test_rename_before_state_commit_is_not_success_and_recovers(tmp_path, monkeypatch):
    from backtest_runtime import worker
    from backtest_runtime.store import RegistryConflict

    store, service = _service(tmp_path)
    request = BacktestJobRequest.from_mapping(_request())
    job_id = str(uuid4())
    store.insert_backtest_job(job_id, request)
    service.claim(job_id, worker_pid=99999999, lease_seconds=1)

    def commit_fault(*_args, **_kwargs):
        assert (tmp_path / "results" / job_id / "manifest.json").is_file()
        raise OSError("state commit fault")

    monkeypatch.setattr(service, "complete", commit_fault)
    with pytest.raises(OSError, match="state commit fault"):
        worker._publish_job_result(
            _result(),
            job_id=job_id,
            request=request,
            service=service,
            result_root=tmp_path / "results",
        )
    with pytest.raises(RegistryConflict, match="no successful result"):
        service.get_result(job_id)
    assert service.recover_expired(now=store.job_clock() + 2) == 1
    assert not (tmp_path / "results" / job_id).exists()
    store.close()


def test_stale_worker_cannot_publish_after_recovery_and_corruption_is_rejected(
    tmp_path,
):
    from backtest_runtime import worker
    from backtest_runtime.results import read_job_result
    from backtest_runtime.store import RegistryConflict

    store, service = _service(tmp_path)
    request = BacktestJobRequest.from_mapping(_request())
    job_id = str(uuid4())
    store.insert_backtest_job(job_id, request)
    service.claim(job_id, worker_pid=99999999, lease_seconds=1)
    assert service.recover_expired(now=store.job_clock() + 2) == 1
    with pytest.raises(RegistryConflict):
        worker._publish_job_result(
            _result(),
            job_id=job_id,
            request=request,
            service=service,
            result_root=tmp_path / "results",
        )
    assert not (tmp_path / "results" / job_id).exists()
    _, digest = worker._write_result(
        _result(),
        job_id=job_id,
        request=request,
        service=service,
        result_root=tmp_path / "results",
    )
    (tmp_path / "results" / job_id / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256"):
        read_job_result(
            tmp_path / "results",
            job_id=job_id,
            manifest_sha256=digest,
            request_sha256=request.request_sha256,
        )
    assert service.claim(job_id, worker_pid=99999999, lease_seconds=1) is None
    store.close()


def test_cleanup_interruption_after_expiration_is_retried(tmp_path, monkeypatch):
    store, service = _service(tmp_path)
    receipt = JobReceipt(str(uuid4()), "SUBMITTED", True)
    store.insert_backtest_job(
        receipt.job_id, BacktestJobRequest.from_mapping(_request())
    )
    service.claim(receipt.job_id, worker_pid=99999999, lease_seconds=1)
    final = tmp_path / "results" / receipt.job_id
    temporary = tmp_path / "results" / f".{receipt.job_id}.tmp-interrupted"
    final.mkdir(parents=True)
    temporary.mkdir()
    unrelated = tmp_path / "results" / "unrelated"
    unrelated.mkdir()
    original = service._remove_unpublished_output

    def interrupted(_job):
        raise OSError("cleanup interruption")

    monkeypatch.setattr(service, "_remove_unpublished_output", interrupted)
    with pytest.raises(OSError, match="cleanup interruption"):
        service.recover_expired(now=store.job_clock() + 2)
    assert service.get_status(receipt.job_id).status == "FAILED"
    monkeypatch.setattr(service, "_remove_unpublished_output", original)
    assert service.recover_expired(now=store.job_clock() + 3) == 0
    assert not final.exists()
    assert not temporary.exists()
    assert unrelated.exists()
    store.close()


def test_two_recovery_processes_expire_once(tmp_path):
    store, service = _service(tmp_path)
    receipt = JobReceipt(str(uuid4()), "SUBMITTED", True)
    store.insert_backtest_job(
        receipt.job_id, BacktestJobRequest.from_mapping(_request())
    )
    service.claim(receipt.job_id, worker_pid=99999999, lease_seconds=1)
    now = store.job_clock() + 2

    def recover(_):
        from backtest_runtime.jobs import BacktestJobService

        registry = JobStore(store.database_path)
        try:
            return BacktestJobService(
                registry,
                artifact_root=tmp_path / "artifacts",
                result_root=tmp_path / "results",
            ).recover_expired(now=now)
        finally:
            registry.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(recover, range(2))) == 1
    assert service.get_status(receipt.job_id).status == "FAILED"
    store.close()
