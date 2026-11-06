"""Restricted snapshots preserve queue authority and fail closed."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from test_engine_sessions import _result
from test_rust_engine_jobs import _request

from backtest_runtime.backup import snapshot, status
from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.engine_publication import write_engine_result
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def setup(tmp_path: Path) -> dict:
    source = tmp_path / "source"
    source.mkdir()
    artifacts = source / "artifacts"
    results = source / "results"
    results.mkdir()
    request = _request(artifacts, Path(os.sys.executable).resolve())
    request.update(schema_version=6, backend="nautilus.session_replay")
    store = JobStore(source / "jobs.sqlite")
    parsed = BacktestJobRequest.from_mapping(request)
    store.insert_backtest_job("00000000-0000-0000-0000-000000000001", parsed)
    store.close()
    return {
        "runtime": {
            "queue": True,
            "database": str(source / "jobs.sqlite"),
            "artifact_root": str(artifacts),
            "result_root": str(results),
        },
        "backup": {
            "root": str(tmp_path / "backups"),
            "max_seconds": 30,
            "max_age_seconds": 7200,
        },
    }


def test_snapshot_restores_queue_results_and_idempotency(tmp_path: Path) -> None:
    config = setup(tmp_path)
    runtime = config["runtime"]
    database = Path(runtime["database"])
    job = "00000000-0000-0000-0000-000000000001"
    with sqlite3.connect(database) as db:
        request = BacktestJobRequest.from_mapping(
            json.loads(
                db.execute("SELECT request_json FROM backtest_jobs").fetchone()[0]
            )
        )
        result = Path(runtime["result_root"]) / job
        result.mkdir()
        manifest = write_engine_result(result, _result(), job_id=job, request=request)
        data = json.dumps(manifest).encode()
        (result / "manifest.json").write_bytes(data)
        db.execute(
            "UPDATE backtest_jobs SET status='SUCCEEDED', result_ref=?, result_sha256=?",
            (f"backtest-job://{job}", hashlib.sha256(data).hexdigest()),
        )
        queued = {**request.to_mapping(), "idempotency_key": "queued"}
    store = JobStore(database)
    store.insert_backtest_job(
        "00000000-0000-0000-0000-000000000002", BacktestJobRequest.from_mapping(queued)
    )
    store.close()
    bundle = snapshot(config)
    assert status(config)["snapshot"] == str(bundle)
    assert bundle.stat().st_mode & 0o777 == 0o700
    restored = tmp_path / "restored"
    shutil.copytree(bundle / "data", restored)
    shutil.rmtree(database.parent)
    store = JobStore(restored / "jobs.sqlite")
    row = store.get_backtest_job(job)
    assert row is not None
    assert (
        read_job_result(
            restored / "results",
            job_id=job,
            manifest_sha256=row["result_sha256"],
            request_sha256=row["request_sha256"],
        )
        == manifest
    )
    assert not store.insert_backtest_job("duplicate", request)
    assert (
        store.get_backtest_job("00000000-0000-0000-0000-000000000002")["status"]
        == "SUBMITTED"
    )
    assert store._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    store.close()


@pytest.mark.parametrize(
    "mutation", ["running", "lease", "v7", "symlink", "digest", "alias"]
)
def test_refuses_unsafe_sources_without_publishing(
    tmp_path: Path, mutation: str
) -> None:
    config = setup(tmp_path)
    runtime = config["runtime"]
    if mutation in {"running", "lease", "v7"}:
        with sqlite3.connect(runtime["database"]) as db:
            if mutation == "running":
                db.execute("UPDATE backtest_jobs SET status='RUNNING'")
            elif mutation == "lease":
                db.execute("UPDATE backtest_jobs SET lease_expires_at=1")
            else:
                db.execute(
                    "UPDATE backtest_jobs SET request_json=json_set(request_json,'$.schema_version',7)"
                )
    elif mutation == "alias":
        config["backup"]["root"] = runtime["artifact_root"]
    else:
        artifact = next((Path(runtime["artifact_root"]) / "sha256").iterdir())
        if mutation == "digest":
            artifact.write_bytes(b"tampered")
        else:
            target = tmp_path / "external"
            artifact.rename(target)
            artifact.symlink_to(target)
    with pytest.raises((ValueError, OSError)):
        snapshot(config)
    assert not list(Path(config["backup"]["root"]).glob("snapshot-*"))


def test_writer_lock_and_overlap_fail_without_state_change(tmp_path: Path) -> None:
    config = setup(tmp_path)
    with sqlite3.connect(config["runtime"]["database"]) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            snapshot(config)
    root = Path(config["backup"]["root"])
    with (root / ".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            snapshot(config)
    assert not list(root.glob("snapshot-*"))


def test_status_rejects_tampering_staleness_and_missing_snapshot(
    tmp_path: Path,
) -> None:
    config = setup(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        status(config)
    bundle = snapshot(config)
    assert status(config)["age_seconds"] >= 0
    with pytest.raises(ValueError, match="stale"):
        status(config, now=10**12)
    (bundle / "data/jobs.sqlite").write_bytes(b"tampered")
    with pytest.raises((ValueError, FileNotFoundError)):
        status(config)


def test_disk_failure_preserves_last_snapshot_and_releases_admission(
    tmp_path: Path, monkeypatch
) -> None:
    from backtest_runtime import backup

    config = setup(tmp_path)
    original = snapshot(config)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(backup, "_collect", fail)
    with pytest.raises(OSError, match="disk full"):
        snapshot(config)
    assert status(config)["snapshot"] == str(original)
    assert not list(Path(config["backup"]["root"]).glob(".incomplete-*"))
    with sqlite3.connect(config["runtime"]["database"], timeout=0) as writer:
        writer.execute("BEGIN IMMEDIATE")


def test_admission_is_reserved_through_file_copy(tmp_path: Path, monkeypatch) -> None:
    from backtest_runtime import backup

    config = setup(tmp_path)
    original = backup._collect

    def check(*args):
        with sqlite3.connect(config["runtime"]["database"], timeout=0) as other:
            with pytest.raises(sqlite3.OperationalError):
                other.execute("BEGIN IMMEDIATE")
        original(*args)

    monkeypatch.setattr(backup, "_collect", check)
    snapshot(config)


def test_deadline_aborts_without_publishing(tmp_path: Path, monkeypatch) -> None:
    import time

    from backtest_runtime import backup

    config = setup(tmp_path)
    config["backup"]["max_seconds"] = 1
    monkeypatch.setattr(backup, "_collect", lambda *args: time.sleep(2))
    with pytest.raises(TimeoutError):
        snapshot(config)
    assert not list(Path(config["backup"]["root"]).glob("snapshot-*"))


def test_hardlinks_and_copy_changes_are_rejected(tmp_path: Path, monkeypatch) -> None:
    from backtest_runtime import backup

    source = tmp_path / "input"
    source.write_bytes(b"original")
    link = tmp_path / "link"
    os.link(source, link)
    with pytest.raises(ValueError):
        backup._copy(source, tmp_path / "output")
    link.unlink()
    original = backup.shutil.copyfileobj

    def mutate(reader, writer, length):
        original(reader, writer, length)
        source.write_bytes(b"changed")

    monkeypatch.setattr(backup.shutil, "copyfileobj", mutate)
    with pytest.raises(ValueError, match="changed"):
        backup._copy(source, tmp_path / "output")


def test_failed_parent_sync_does_not_publish_new_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    from backtest_runtime import backup

    config = setup(tmp_path)
    previous = snapshot(config)
    root_info = Path(config["backup"]["root"]).stat()
    original = os.fsync

    def fail_parent(descriptor):
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) == (root_info.st_dev, root_info.st_ino):
            raise OSError("parent sync failed")
        original(descriptor)

    monkeypatch.setattr(backup.os, "fsync", fail_parent)
    with pytest.raises(OSError, match="parent sync"):
        snapshot(config)
    assert status(config)["snapshot"] == str(previous)
    assert len(list(previous.parent.glob("snapshot-*"))) == 1


def test_rejects_parent_traversal_and_destination_symlink_before_mkdir(
    tmp_path: Path,
) -> None:
    config = setup(tmp_path)
    source = Path(config["runtime"]["artifact_root"])
    config["backup"]["root"] = str(source / ".." / "artifacts")
    with pytest.raises(ValueError):
        snapshot(config)
    link = tmp_path / "alias"
    link.symlink_to(source, target_is_directory=True)
    config["backup"]["root"] = str(link / "new-backup")
    with pytest.raises(ValueError):
        snapshot(config)
    assert not (source / "new-backup").exists()


def test_release_script_runs_with_system_python_without_installation(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    config = setup(tmp_path)
    runtime, backup = config["runtime"], config["backup"]
    path = tmp_path / "runtime.toml"
    path.write_text(
        "[runtime]\nqueue=true\n"
        + "".join(
            f"{key}={json.dumps(runtime[key])}\n"
            for key in ("database", "artifact_root", "result_root")
        )
        + "\n[backup]\nroot="
        + json.dumps(backup["root"])
        + "\n"
    )
    script = Path(__file__).parents[1] / "scripts/backup_runtime.py"
    command = [sys.executable, "-I", str(script), "--config", str(path)]
    created = subprocess.run(command, check=True, capture_output=True, text=True)
    checked = subprocess.run(
        [*command, "--status"], check=True, capture_output=True, text=True
    )
    assert (
        json.loads(created.stdout)["snapshot"] == json.loads(checked.stdout)["snapshot"]
    )


def test_status_rejects_uninventoried_top_level_files(tmp_path: Path) -> None:
    config = setup(tmp_path)
    bundle = snapshot(config)
    (bundle / "unexpected").write_bytes(b"extra")
    with pytest.raises(ValueError, match="unexpected snapshot files"):
        status(config)
