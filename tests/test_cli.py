"""Exercise the public CLI and its detached worker with synthetic inputs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_execution_aware import _request_v2

from backtest_runtime import cli
from backtest_runtime.store import JobStore


def _args(root: Path) -> list[str]:
    return [
        "--database",
        str(root / "jobs.sqlite"),
        "--artifact-root",
        str(root / "artifacts"),
        "--result-root",
        str(root / "results"),
    ]


def test_cli_submit_status_result_and_terminal_cancel(tmp_path, monkeypatch, capsys):
    manifest = tmp_path / "request.json"
    manifest.write_text(json.dumps(_request_v2(tmp_path / "artifacts")))
    processes = []
    original = cli.subprocess.Popen
    monkeypatch.setenv("RUNTIME_TEST_SECRET", "must-not-reach-worker")

    def launch(*args, **kwargs):
        assert "RUNTIME_TEST_SECRET" not in kwargs["env"]
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(cli.subprocess, "Popen", launch)
    assert cli.main([*_args(tmp_path), "submit", str(manifest)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["created"] is True
    assert len(processes) == 1
    assert processes[0].wait(timeout=45) == 0
    job_id = receipt["job_id"]
    for command in ("status", "cancel"):
        assert cli.main([*_args(tmp_path), command, job_id]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "SUCCEEDED"
    assert cli.main([*_args(tmp_path), "result", job_id]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["job_id"] == job_id
    assert result["schema_version"] == "quant.backtest_job_result.v2"
    assert cli.main([*_args(tmp_path), "submit", str(manifest)]) == 0
    assert json.loads(capsys.readouterr().out)["created"] is False
    assert len(processes) == 1
    assert cli.main([*_args(tmp_path), "recover"]) == 0
    assert json.loads(capsys.readouterr().out) == {"recovered": 0}
    for log in (tmp_path / "results" / ".logs").iterdir():
        assert log.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["directory", "symlink", "fifo", "oversized"])
def test_cli_rejects_unsafe_request_files(tmp_path, kind):
    import os

    manifest = tmp_path / "request.json"
    if kind == "directory":
        manifest.mkdir()
    elif kind == "symlink":
        manifest.symlink_to(tmp_path / "missing.json")
    elif kind == "fifo":
        os.mkfifo(manifest)
    else:
        manifest.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="regular file"):
        cli.main([*_args(tmp_path), "submit", str(manifest)])


def test_cli_worker_start_failure_is_persisted(tmp_path, monkeypatch):
    manifest = tmp_path / "request.json"
    manifest.write_text(json.dumps(_request_v2(tmp_path / "artifacts")))

    def fail(*args, **kwargs):
        raise OSError("synthetic spawn failure")

    monkeypatch.setattr(cli.subprocess, "Popen", fail)
    with pytest.raises(OSError, match="synthetic spawn failure"):
        cli.main([*_args(tmp_path), "submit", str(manifest)])
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        row = store.get_backtest_job_by_key("official-bundle")
        assert row["status"] == "FAILED"
        assert row["error_code"] == "WORKER_START_FAILED"
    finally:
        store.close()
