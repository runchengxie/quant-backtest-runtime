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
from engine_runtime_helpers import Runtime
from test_engine_financial_sessions import mapping as engine_mapping


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    pytest.importorskip(
        "quant_backtest_core", reason="separate engine-only process gate"
    )
    executable = os.environ.get("BACKTEST_GO_EXECUTABLE")
    if executable is None:
        pytest.fail("BACKTEST_GO_EXECUTABLE is mandatory for the v9 process gate")
    monkeypatch.setenv("GORACE", "atexit_sleep_ms=0")
    return Runtime(Path(executable), tmp_path, python=sys.executable)


def mapping(rt, key="engine"):
    return engine_mapping(rt.artifacts, key)


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


def test_engine_preflight_and_complete(runtime):
    request = mapping(runtime)
    plan = runtime.call("plan", str(runtime.write(request)))
    assert plan["checks"]["financial_constructor"] == "passed"
    assert not runtime.database.exists()
    payload, result = complete(runtime, request)
    assert result["schema_version"] == "quant.engine_financial_job_result.v1"
    assert len(next(t for t in payload["tables"] if t["name"] == "fills")["rows"]) == 2
    assert (
        next(t for t in payload["tables"] if t["name"] == "capital_flows")["rows"][0][
            "amount"
        ]
        == "100.0"
    )
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
    full, full_manifest = complete(runtime, {**request, "idempotency_key": "full"})
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
    original = runtime.results / full_manifest["job_id"] / full["raw_result"]["uri"]
    restored = runtime.results / result["job_id"] / actual["raw_result"]["uri"]
    assert restored.read_bytes() == original.read_bytes()
    assert result["resumed_cursor"] == rows[-1][0] > 0
    # Repeating a lost submit acknowledgement cannot create another financial run.
    assert submit(runtime, successor) == result["job_id"]
    assert len(next(t for t in actual["tables"] if t["name"] == "fills")["rows"]) == 2


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


def test_v9_does_not_select_configured_rust_engine(runtime):
    runtime.engine = runtime.root / "unused-engine"
    request = mapping(runtime)
    plan = runtime.call("plan", str(runtime.write(request)))
    assert plan["checks"]["financial_constructor"] == "passed"
    assert not runtime.database.exists()


def test_retained_old_worker_uses_separate_store_and_never_changes_v9(
    runtime, tmp_path
):
    old_executable = os.environ.get("BACKTEST_LEGACY_GO_EXECUTABLE")
    assert old_executable, (
        "retained old binary is required to demonstrate queue isolation"
    )
    request = mapping(runtime)
    job = submit(runtime, request)
    before = runtime.database.read_bytes()
    old_root = tmp_path / "old-runtime"
    old_root.mkdir()
    old = Runtime(Path(old_executable), old_root, python=None)
    old.call("serve", "--once")
    assert old.database != runtime.database
    assert runtime.database.read_bytes() == before
    assert runtime.call("inspect", job)["status"] == "SUBMITTED"
    assert "engine_financial_session" not in old.call("capabilities")


def test_missing_core_fails_preflight_before_job_store(runtime):
    minimal = os.environ.get("BACKTEST_ENGINE_MINIMAL_PYTHON")
    assert minimal, (
        "base-only Runtime interpreter is required for missing-provider gate"
    )
    runtime.python = minimal
    request = mapping(runtime)
    error = runtime.call(
        "submit", str(runtime.write(request)), "--queue", success=False
    )
    assert error["code"] == "ENGINE_DEPENDENCY_UNAVAILABLE"
    assert not runtime.database.exists()


def test_valid_stale_runtime_bytecode_cannot_replace_audited_source(runtime, tmp_path):
    import py_compile
    import shutil
    import venv

    import backtest_runtime

    environment = tmp_path / "bytecode-env"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin/python"
    site = next((environment / "lib").glob("python*/site-packages"))
    package = site / "backtest_runtime"
    shutil.copytree(
        Path(backtest_runtime.__file__).parent,
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    source = package / "engine_financial_adapter.py"
    original = source.read_bytes()
    info = source.stat()
    forged = (
        b'import json\nprint(json.dumps({"financial_binding":"'
        + b"a" * 64
        + b'","checks":{"financial_constructor":"forged-bytecode"}}))\n'
    )
    assert len(forged) < len(original)
    source.write_bytes(forged + b" " * (len(original) - len(forged)))
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    py_compile.compile(str(source), doraise=True)
    source.write_bytes(original)
    os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
    ordinary = subprocess.run(
        [str(python), "-B", "-m", "backtest_runtime.engine_financial_adapter"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert (
        json.loads(ordinary.stdout)["checks"]["financial_constructor"]
        == "forged-bytecode"
    )
    runtime.python = str(python)
    request = mapping(runtime)
    error = runtime.call("plan", str(runtime.write(request)), success=False)
    assert error["code"] == "ENGINE_DEPENDENCY_UNAVAILABLE"
    assert not runtime.database.exists()


def test_unconfigured_engine_interpreter_fails_before_store(runtime):
    runtime.python = None
    request = mapping(runtime)
    error = runtime.call(
        "submit", str(runtime.write(request)), "--queue", success=False
    )
    assert error["code"] == "ENGINE_DEPENDENCY_UNAVAILABLE"
    assert not runtime.database.exists()
