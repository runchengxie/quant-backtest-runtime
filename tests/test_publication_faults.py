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


def test_publication_syncs_payload_and_namespace_before_success(tmp_path, monkeypatch):
    """Omitting any data/directory barrier must prevent this success assertion."""
    import os
    import stat
    from pathlib import Path

    from backtest_runtime import worker

    store, service = _service(tmp_path)
    request = BacktestJobRequest.from_mapping(_request())
    job_id = str(uuid4())
    store.insert_backtest_job(job_id, request)
    service.claim(job_id, worker_pid=99999999, lease_seconds=30)
    root = tmp_path / "new" / "results"
    events = []
    real_sync = os.fsync
    real_replace = Path.replace
    real_complete = service.complete
    import pandas as pd

    real_parquet = pd.DataFrame.to_parquet

    def parquet(frame, path, **kwargs):
        # Bundle writers can produce nested payloads outside the top-level manifest.
        nested = path.parent / "payload" / "tables"
        nested.mkdir(parents=True)
        (nested / "extra.bin").write_bytes(b"nested payload")
        return real_parquet(frame, path, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", parquet)

    def sync(fd):
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        events.append(("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file", path))
        real_sync(fd)

    def replace(path, target):
        events.append(("rename", path))
        return real_replace(path, target)

    def complete(*args, **kwargs):
        events.append(("complete", root))
        return real_complete(*args, **kwargs)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(service, "complete", complete)
    assert (
        worker._publish_job_result(
            _result(), job_id=job_id, request=request, service=service, result_root=root
        )
        == 0
    )
    rename = next(i for i, event in enumerate(events) if event[0] == "rename")
    staged = events[rename][1]
    for name in ("daily.parquet", "manifest.json"):
        assert ("file", staged / name) in events[:rename]
    assert ("file", staged / "payload" / "tables" / "extra.bin") in events[:rename]
    assert events.index(
        ("file", staged / "payload" / "tables" / "extra.bin")
    ) < events.index(("dir", staged / "payload" / "tables"))
    nested_sync = events.index(("dir", staged / "payload" / "tables"))
    parent_sync = events.index(("dir", staged / "payload"))
    assert nested_sync < parent_sync < events.index(("dir", staged)) < rename
    assert ("dir", staged) in events[:rename]
    assert ("dir", root) in events[rename + 1 : -1]
    assert ("dir", tmp_path) in events[:rename]
    assert ("dir", tmp_path / "new") in events[:rename]
    assert events[-1][0] == "complete"
    assert service.get_status(job_id).status == "SUCCEEDED"
    store.close()


@pytest.mark.parametrize("fault", ["file", "directory", "parent", "rename"])
def test_publication_io_failure_never_commits_success(tmp_path, monkeypatch, fault):
    import os
    import stat
    from pathlib import Path

    from backtest_runtime import worker
    from backtest_runtime.store import RegistryConflict

    store, service = _service(tmp_path)
    request = BacktestJobRequest.from_mapping(_request())
    job_id = str(uuid4())
    store.insert_backtest_job(job_id, request)
    service.claim(job_id, worker_pid=99999999, lease_seconds=1)
    root = tmp_path / "results"
    root.mkdir(exist_ok=True)
    real_sync = os.fsync

    def sync(fd):
        path = Path(os.readlink(f"/proc/self/fd/{fd}"))
        is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (
            (fault == "file" and not is_dir)
            or (fault == "directory" and is_dir and ".tmp-" in path.name)
            or (fault == "parent" and path == root and (root / job_id).exists())
        ):
            raise OSError("injected publication fault")
        real_sync(fd)

    def replace(*_args):
        raise OSError("injected publication fault")

    monkeypatch.setattr(os, "fsync", sync)
    if fault == "rename":
        monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="injected publication fault"):
        worker._publish_job_result(
            _result(), job_id=job_id, request=request, service=service, result_root=root
        )
    assert service.get_status(job_id).status == "RUNNING"
    with pytest.raises(RegistryConflict, match="no successful result"):
        service.get_result(job_id)
    assert not list(root.glob(f".{job_id}.tmp-*"))
    assert service.recover_expired(now=store.job_clock() + 2) == 1
    assert not (root / job_id).exists()
    store.close()


def test_unsupported_directory_sync_cannot_publish_success(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backtest_runtime import durability, worker

    store, service = _service(tmp_path)
    request = BacktestJobRequest.from_mapping(_request())
    job_id = str(uuid4())
    store.insert_backtest_job(job_id, request)
    service.claim(job_id, worker_pid=99999999, lease_seconds=1)
    monkeypatch.setattr(durability, "os", SimpleNamespace(name="nt"))
    with pytest.raises(OSError, match="requires POSIX fsync"):
        worker._publish_job_result(
            _result(),
            job_id=job_id,
            request=request,
            service=service,
            result_root=tmp_path / "results",
        )
    assert service.get_status(job_id).status == "RUNNING"
    assert not (tmp_path / "results" / job_id).exists()
    store.close()
