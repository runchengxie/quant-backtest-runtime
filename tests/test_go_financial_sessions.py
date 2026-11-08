"""Actual Go/Python process death and explicit financial checkpoint successors."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_financial_sessions import fixture
from test_go_runtime import Runtime
from test_rust_engine_jobs import _put


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    executable = os.environ.get("BACKTEST_GO_EXECUTABLE")
    if executable is None:
        pytest.skip("BACKTEST_GO_EXECUTABLE enables Go financial resume acceptance")
    monkeypatch.setenv("GORACE", "atexit_sleep_ms=0")
    return Runtime(Path(executable), tmp_path, python=sys.executable)


PROVIDER = "7a618bddd8fdeee5dbe37fe5b54a9698e0de5355"


def mapping(rt, key="minute"):
    return dict(
        schema_version=8,
        idempotency_key=key,
        backend="platform.minute_session_resume",
        evidence_tier="diagnostic",
        inputs=dict(
            minute_input_ref=_put(rt.artifacts, json.dumps(fixture()).encode())
        ),
        config=dict(
            provider_revision=PROVIDER,
            checkpoint_format="minute-session.v2",
            policy="target-weights.v1",
        ),
        execution={},
        budgets=dict(wall_seconds=120, memory_mb=2048),
    )


def submit(rt, request):
    return rt.call("submit", str(rt.write(request)), "--queue")["job_id"]


def run(rt, id, *args):
    return subprocess.Popen(
        rt.args("__worker", id, *args), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def complete(rt, request):
    id = submit(rt, request)
    p = run(rt, id)
    out, err = p.communicate(timeout=120)
    assert p.returncode == 0, (out, err)
    assert rt.call("inspect", id)["status"] == "SUCCEEDED"
    result = rt.call("result", id)
    return json.loads((rt.results / id / "financial.json").read_text()), result


def test_minute_preflight_and_complete(runtime):
    request = mapping(runtime)
    plan = runtime.call("plan", str(runtime.write(request)))
    assert plan["checks"]["financial_constructor"] == "passed"
    assert not runtime.database.exists()
    payload, result = complete(runtime, request)
    assert result["schema_version"] == "quant.minute_job_result.v1"
    assert len(payload["tables"]["fills"]["rows"]) == 2
    assert payload["tables"]["capital_flow_ledger"]["rows"][0]["amount"] == 1000
    (runtime.results / result["job_id"] / "financial.json").write_text("{}")
    assert (
        runtime.call("result", result["job_id"], success=False)["code"]
        == "INTEGRITY_FAILURE"
    )


@pytest.mark.parametrize(
    "phase", ["before_write", "after_fsync", "after_rename", "after_admission"]
)
def test_killed_worker_resumes_admitted_cursor_with_exact_financial_parity(
    runtime, phase
):
    request = mapping(runtime)
    full, _ = complete(runtime, {**request, "idempotency_key": "full"})
    id = submit(runtime, request)
    child = run(runtime, id, "--financial-fault", phase + ":3")
    marker = runtime.results / ".checkpoints" / id / "fault.json"
    try:
        deadline = time.monotonic() + 40
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None, child.communicate()
            time.sleep(0.03)
        assert marker.exists()
        checkpoint_files = list(
            (runtime.results / ".checkpoints" / id).glob("*/3.json")
        )
        assert bool(checkpoint_files) == (phase in {"after_rename", "after_admission"})
        os.kill(child.pid, signal.SIGKILL)
        child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    with sqlite3.connect(runtime.database) as db:
        rows = db.execute(
            "SELECT cursor FROM financial_checkpoints WHERE job_id=? ORDER BY cursor",
            (id,),
        ).fetchall()
        assert rows[-1][0] == (3 if phase == "after_admission" else 2)
        db.execute("UPDATE backtest_jobs SET lease_expires_at=0 WHERE job_id=?", (id,))
    runtime.call("recover")
    assert runtime.call("inspect", id)["status"] == "FAILED"
    assert not list(runtime.results.glob("." + id + ".tmp-*"))
    successor = {
        **request,
        "idempotency_key": "resumed",
        "execution": {"resume_from": id},
    }
    actual, result = complete(runtime, successor)
    assert actual == full
    assert result["resumed_cursor"] == rows[-1][0] > 0
    # Repeating a lost submit acknowledgement cannot create another financial run.
    assert submit(runtime, successor) == result["job_id"]
    assert len(actual["tables"]["fills"]["rows"]) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_revision", "0" * 40),
        ("checkpoint_format", "minute-session.v1"),
        ("policy", "evil.module"),
    ],
)
def test_unsupported_contract_rejected_before_database(runtime, field, value):
    request = mapping(runtime)
    request["config"][field] = value
    runtime.call("submit", str(runtime.write(request)), success=False)
    assert not runtime.database.exists()


@pytest.mark.parametrize("action", ["cancel", "kill_before_first_checkpoint"])
def test_no_implicit_recovery_without_admitted_failed_progress(runtime, action):
    request = mapping(runtime)
    id = submit(runtime, request)
    child = run(runtime, id, "--financial-fault", "before_write:1")
    marker = runtime.results / ".checkpoints" / id / "fault.json"
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None, child.communicate()
            time.sleep(0.03)
        assert marker.exists()
        if action == "cancel":
            runtime.call("cancel", id)
            child.communicate(timeout=10)
            assert runtime.call("inspect", id)["status"] == "CANCELLED"
        else:
            child.kill()
            child.communicate(timeout=10)
            with sqlite3.connect(runtime.database) as db:
                db.execute(
                    "UPDATE backtest_jobs SET lease_expires_at=0 WHERE job_id=?", (id,)
                )
            runtime.call("recover")
        successor = {
            **request,
            "idempotency_key": "invalid-successor",
            "execution": {"resume_from": id},
        }
        runtime.call("submit", str(runtime.write(successor)), "--queue", success=False)
        with sqlite3.connect(runtime.database) as db:
            assert db.execute("SELECT COUNT(*) FROM backtest_jobs").fetchone()[0] == 1
            assert (
                db.execute("SELECT COUNT(*) FROM financial_checkpoints").fetchone()[0]
                == 0
            )
        assert not list(runtime.results.glob("." + id + ".tmp-*"))
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()


def test_changed_source_binding_and_tampered_checkpoint_reject_resume(runtime):
    request = mapping(runtime)
    id = submit(runtime, request)
    child = run(runtime, id, "--financial-fault", "after_admission:1")
    marker = runtime.results / ".checkpoints" / id / "fault.json"
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() and time.monotonic() < deadline:
            assert child.poll() is None, child.communicate()
            time.sleep(0.03)
        assert marker.exists()
        child.kill()
        child.communicate(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    with sqlite3.connect(runtime.database) as db:
        db.execute("UPDATE backtest_jobs SET lease_expires_at=0 WHERE job_id=?", (id,))
        binding = db.execute(
            "SELECT binding FROM financial_checkpoints WHERE job_id=?", (id,)
        ).fetchone()[0]
    runtime.call("recover")
    successor = {**request, "idempotency_key": "next", "execution": {"resume_from": id}}
    path = runtime.write(successor)
    with sqlite3.connect(runtime.database) as db:
        db.execute(
            "UPDATE financial_checkpoints SET binding=? WHERE job_id=?", ("0" * 64, id)
        )
    runtime.call("submit", str(path), "--queue", success=False)
    with sqlite3.connect(runtime.database) as db:
        db.execute(
            "UPDATE financial_checkpoints SET binding=? WHERE job_id=?", (binding, id)
        )
    checkpoint = next((runtime.results / ".checkpoints" / id).glob("*/1.json"))
    checkpoint.write_text("{}")
    assert (
        runtime.call("submit", str(path), "--queue", success=False)["code"]
        == "INTEGRITY_FAILURE"
    )
    with sqlite3.connect(runtime.database) as db:
        assert db.execute("SELECT COUNT(*) FROM backtest_jobs").fetchone()[0] == 1


def test_v8_does_not_select_configured_rust_engine(runtime):
    runtime.engine = runtime.root / "unused-engine"
    request = mapping(runtime)
    plan = runtime.call("plan", str(runtime.write(request)))
    assert plan["checks"]["financial_constructor"] == "passed"
    assert not runtime.database.exists()
