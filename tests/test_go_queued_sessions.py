"""Explicit v7 jobs retain Go lifecycle authority and staged publication."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_go_runtime import Runtime
from test_rust_engine_jobs import _put
from test_session_stream import config_sessions, fake_engine, source


@pytest.fixture
def queued(tmp_path, monkeypatch):
    executable = os.environ.get("BACKTEST_GO_EXECUTABLE")
    if executable is None:
        pytest.skip("BACKTEST_GO_EXECUTABLE enables Go queued integration")
    monkeypatch.setenv("GORACE", "atexit_sleep_ms=0")
    return Runtime(Path(executable), tmp_path, python=sys.executable)


def mapping(runtime, binary):
    config, sessions = config_sessions()
    reader = source(runtime.root, sessions)
    manifest = {
        "schema_version": "partition_manifest.v1",
        "row_schema": "event_envelope.v1",
        "order": "event_time_ns,event_id",
        "duplicate_policy": "reject_event_id",
        "global_order": "strict",
        "partitions": [p.to_dict() for p in reader.manifest.partitions],
    }
    return {
        "schema_version": 7,
        "idempotency_key": "synthetic-stream",
        "backend": "nautilus.partition_session_replay",
        "evidence_tier": "diagnostic",
        "inputs": {
            "engine_config_ref": _put(runtime.artifacts, json.dumps(config).encode()),
            "partition_manifest_ref": _put(
                runtime.artifacts, json.dumps(manifest).encode()
            ),
        },
        "config": {
            "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
        },
        "execution": {},
        "budgets": {"wall_seconds": 10, "memory_mb": 1024},
    }


def call(runtime, *args, **kwargs):
    return runtime.call(
        "--partition-root", str(runtime.root / "inputs"), *args, **kwargs
    )


def serve(runtime, *args):
    return subprocess.Popen(
        runtime.args(
            "--partition-root", str(runtime.root / "inputs"), "serve", "--once", *args
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_queue_success_result_tamper_and_idempotency(queued):
    binary, _ = fake_engine(queued.root)
    queued.engine = binary
    request = mapping(queued, binary)
    path = queued.write(request)
    plan = call(queued, "plan", str(path))
    assert plan["checks"]["partition_bytes"] == "not_checked"
    assert not queued.database.exists()
    submitted = call(
        queued,
        "submit",
        str(path),
        "--queue",
        "--expected-plan-sha256",
        plan["plan_sha256"],
    )
    assert call(queued, "submit", str(path), "--queue")["job_id"] == submitted["job_id"]
    scheduler = serve(queued)
    stdout, stderr = scheduler.communicate(timeout=30)
    assert scheduler.returncode == 0, (stdout, stderr)
    status = queued.wait(submitted["job_id"])
    assert status["status"] == "SUCCEEDED", status
    result = call(queued, "result", submitted["job_id"])
    assert result["schema_version"] == "quant.engine_job_result.v3"
    assert result["session_receipt"]["session_count"] == 2
    result_file = (
        queued.results / submitted["job_id"] / result["session_receipt"]["result_path"]
    )
    result_file.write_text("{}")
    assert (
        call(queued, "result", submitted["job_id"], success=False)["code"]
        == "INTEGRITY_FAILURE"
    )


def test_invalid_preflight_creates_no_database(queued):
    binary, _ = fake_engine(queued.root, "hash")
    queued.engine = binary
    request = mapping(queued, binary)
    error = call(queued, "submit", str(queued.write(request)), "--queue", success=False)
    assert error["code"] == "ENGINE_VALIDATION_UNAVAILABLE"
    assert not queued.database.exists()


@pytest.mark.parametrize("action", ["cancel", "timeout", "kill", "restart"])
def test_queued_lifecycle_controls(queued, action):
    binary, _ = fake_engine(queued.root)
    # Start/ready works during preflight; computation blocks on first session.
    # Publish the replacement PID atomically so readers never see a truncated file.
    pidfile = queued.root / "engine.pid"
    binary.write_text(
        binary.read_text()
        .replace('session=data["session"]', 'time.sleep(30); session=data["session"]')
        .replace(
            "MODE = ",
            f"pidfile = {str(pidfile)!r}\n"
            'pidtmp = pidfile + "." + str(os.getpid()) + ".tmp"\n'
            "try:\n"
            '    with open(pidtmp, "w") as target:\n'
            "        target.write(str(os.getpid()))\n"
            "    os.replace(pidtmp, pidfile)\n"
            "finally:\n"
            "    if os.path.exists(pidtmp):\n"
            "        os.unlink(pidtmp)\nMODE = ",
        )
    )
    queued.engine = binary
    request = mapping(queued, binary)
    if action == "timeout":
        request["budgets"]["wall_seconds"] = 1
    submitted = call(queued, "submit", str(queued.write(request)), "--queue")
    job_id = submitted["job_id"]
    preflight_pid = pidfile.read_text()
    scheduler = serve(queued)
    try:
        running = queued.wait(job_id, {"RUNNING"})
        deadline = time.monotonic() + 3
        while pidfile.read_text() == preflight_pid and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.read_text() != preflight_pid
        engine_pid = int(pidfile.read_text())
        if action == "restart":
            scheduler.kill()
            scheduler.communicate(timeout=10)
            scheduler = serve(queued)
            assert queued.call("inspect", job_id)["worker_pid"] == running["worker_pid"]
            queued.call("cancel", job_id)
        elif action == "cancel":
            queued.call("cancel", job_id)
        elif action == "kill":
            os.kill(running["worker_pid"], signal.SIGKILL)
            with sqlite3.connect(queued.database) as connection:
                connection.execute(
                    "UPDATE backtest_jobs SET lease_expires_at=0 WHERE job_id=?",
                    (job_id,),
                )
        status = queued.wait(job_id)
        assert status["status"] == (
            "CANCELLED" if action in {"cancel", "restart"} else "FAILED"
        ), status
        if action == "timeout":
            assert status["error_code"] == "JOB_TIMEOUT"
        assert not (queued.results / job_id).exists()
        deadline = time.monotonic() + 3
        process = Path(f"/proc/{engine_pid}/stat")
        while process.exists() and time.monotonic() < deadline:
            if process.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        assert not process.exists() or process.read_text().split()[2] == "Z"
    finally:
        if scheduler.poll() is None:
            scheduler.terminate()
        scheduler.communicate(timeout=10)


def test_real_provider_queue(queued):
    engine = os.environ.get("BACKTEST_ENGINE_STREAM_TEST_EXECUTABLE")
    if engine is None:
        pytest.skip("real stream provider required")
    queued.engine = Path(engine)
    request = mapping(queued, queued.engine)
    submitted = call(queued, "submit", str(queued.write(request)), "--queue")
    scheduler = serve(queued)
    stdout, stderr = scheduler.communicate(timeout=30)
    assert scheduler.returncode == 0, (stdout, stderr)
    status = queued.wait(submitted["job_id"])
    assert status["status"] == "SUCCEEDED", status
    assert (
        call(queued, "result", submitted["job_id"])["session_receipt"]["session_count"]
        == 2
    )


@pytest.mark.parametrize(
    "fault", ["path", "rows", "unknown", "hash", "symlink", "payload"]
)
def test_queued_invalid_input_boundaries(queued, fault):
    binary, _ = fake_engine(queued.root)
    queued.engine = binary
    request = mapping(queued, binary)
    manifest_ref = request["inputs"]["partition_manifest_ref"]
    manifest_file = queued.artifacts / "sha256" / manifest_ref.split("/")[-1]
    manifest = json.loads(manifest_file.read_text())
    if fault == "path":
        manifest["partitions"][0]["path"] = "../outside"
    elif fault == "rows":
        manifest["partitions"][0]["rows"] = 4097
    elif fault == "unknown":
        request["execution"]["resume"] = True
    elif fault == "hash":
        manifest["partitions"][0]["sha256"] = "0" * 64
    elif fault == "symlink":
        part = queued.root / "inputs/0.ndjson"
        outside = queued.root / "outside"
        part.rename(outside)
        part.symlink_to(outside)
    else:
        part = queued.root / "inputs/0.ndjson"
        rows = [json.loads(line) for line in part.read_text().splitlines()]
        rows[0]["payload"]["schema_version"] = "unsupported"
        data = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        part.write_bytes(data)
        manifest["partitions"][0].update(
            bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
        )
    request["inputs"]["partition_manifest_ref"] = _put(
        queued.artifacts, json.dumps(manifest).encode()
    )
    path = queued.write(request)
    if fault in {"path", "rows", "unknown"}:
        call(queued, "submit", str(path), "--queue", success=False)
        assert not queued.database.exists()
    else:
        job_id = call(queued, "submit", str(path), "--queue")["job_id"]
        scheduler = serve(queued)
        scheduler.communicate(timeout=30)
        assert queued.wait(job_id)["status"] == "FAILED"
        assert not (queued.results / job_id).exists()


def test_one_wall_deadline_spans_stage_and_verification(queued):
    binary, _ = fake_engine(queued.root)
    queued.engine = binary
    request = mapping(queued, binary)
    request["budgets"]["wall_seconds"] = 1
    wrapper = queued.root / "python-wrapper"
    marker = queued.root / "verification-started"
    wrapper.write_text(
        "#!/usr/bin/python3\nimport os,sys,time\n"
        "operation=sys.argv[3]\n"
        f"if operation=='verify': open({str(marker)!r},'w').write('started')\n"
        "if operation in ('stage','verify'): time.sleep(0.6)\n"
        f"os.execv({sys.executable!r},[{sys.executable!r},*sys.argv[1:]])\n"
    )
    wrapper.chmod(0o700)
    queued.python = str(wrapper)
    job_id = call(queued, "submit", str(queued.write(request)), "--queue")["job_id"]
    started = time.monotonic()
    scheduler = serve(queued)
    stdout, stderr = scheduler.communicate(timeout=10)
    assert scheduler.returncode == 0, (stdout, stderr)
    status = queued.wait(job_id)
    assert marker.exists(), status
    assert status["error_code"] == "JOB_TIMEOUT", status
    assert time.monotonic() - started < 3
    assert not (queued.results / job_id).exists()
