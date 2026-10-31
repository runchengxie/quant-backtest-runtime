"""Cross-language Go CLI acceptance tests using synthetic, task-owned inputs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_engine_sessions import _session_request
from test_execution_aware import _request_v2
from test_rust_engine_jobs import _put, _request
from test_sequenced_jobs import _request_v3
from test_trade_accounting_jobs import _request_v4

from backtest_runtime.agent_access import checked_plan
from backtest_runtime.capabilities import capabilities
from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.results import read_job_result


@dataclass
class Runtime:
    executable: Path
    root: Path
    engine: Path | None = None
    python: str | None = sys.executable

    @property
    def database(self) -> Path:
        return self.root / "jobs.sqlite"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def results(self) -> Path:
        return self.root / "results"

    def args(self, *args: str) -> list[str]:
        command = [
            str(self.executable),
            "--database",
            str(self.database),
            "--artifact-root",
            str(self.artifacts),
            "--result-root",
            str(self.results),
            "--error-format",
            "json",
        ]
        if self.python is not None:
            command.extend(["--python-executable", self.python])
        if self.engine is not None:
            command.extend(["--engine-executable", str(self.engine)])
        return [*command, *args]

    def call(self, *args: str, success: bool = True) -> dict:
        completed = subprocess.run(
            self.args(*args), capture_output=True, text=True, timeout=90, check=False
        )
        assert (completed.returncode == 0) is success, completed.stderr
        return json.loads(completed.stdout if success else completed.stderr)

    def write(self, mapping: dict, name: str = "request") -> Path:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return path

    def wait(self, job_id: str, states: set[str] | None = None) -> dict:
        wanted = states or {"SUCCEEDED", "FAILED", "CANCELLED"}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = self.call("status", job_id)
            if status["status"] in wanted:
                return status
            time.sleep(0.05)
        pytest.fail(f"job {job_id} did not reach {wanted}: {status}")


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Runtime:
    executable = os.environ.get("BACKTEST_GO_EXECUTABLE")
    if executable is None:
        pytest.skip("BACKTEST_GO_EXECUTABLE enables Go CLI acceptance tests")
    path = Path(executable)
    assert path.is_absolute() and path.is_file(), executable
    # Race builds normally sleep one second at CLI exit, consuming the one-second
    # timeout case before inspection. Keep instrumentation and remove only that delay.
    gorace = os.environ.get("GORACE", "")
    monkeypatch.setenv("GORACE", f"{gorace} atexit_sleep_ms=0".strip())
    return Runtime(path, tmp_path)


def engine_runtime(runtime: Runtime) -> Runtime:
    engine = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    if engine is None:
        pytest.skip("BACKTEST_ENGINE_TEST_EXECUTABLE enables real engine acceptance")
    runtime.engine = Path(engine)
    assert runtime.engine.is_absolute() and runtime.engine.is_file()
    return runtime


def test_go_discovery_preserves_shared_operation_contract(runtime: Runtime) -> None:
    found = runtime.call("capabilities")
    for key in ("implementation", "source_revision", "resource_limits"):
        del found[key]
    found["operations"] = [row for row in found["operations"] if row["id"] != "serve"]
    assert found == capabilities()


def legacy_mapping(runtime: Runtime, version: int) -> dict:
    if version == 4:
        return _request_v4(runtime.artifacts)
    if version == 3:
        return _request_v3(runtime.artifacts)
    mapping = _request_v2(runtime.artifacts)
    if version == 1:
        mapping.update(schema_version=1, evidence_tier="diagnostic")
        del mapping["research_clock"]
        del mapping["producer"]
        mapping["execution"]["ledger"] = False
        mapping["execution"]["ledger_config"]["enabled"] = False
    return mapping


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_go_legacy_plan_hash_and_python_readable_result(
    runtime: Runtime, version: int
) -> None:
    mapping = legacy_mapping(runtime, version)
    request = BacktestJobRequest.from_mapping(mapping)
    path = runtime.write(mapping)
    plan = runtime.call("plan", str(path))
    assert plan == checked_plan(request, runtime.artifacts, None)
    assert not runtime.database.exists(), "planning must not create job state"
    receipt = runtime.call("submit", str(path))
    status = runtime.wait(receipt["job_id"])
    assert status["status"] == "SUCCEEDED", status
    with sqlite3.connect(runtime.database) as connection:
        row = connection.execute(
            "SELECT request_sha256 FROM backtest_jobs WHERE job_id=?",
            (receipt["job_id"],),
        ).fetchone()
    assert row == (request.request_sha256,)
    manifest = read_job_result(
        runtime.results,
        job_id=receipt["job_id"],
        manifest_sha256=status["result_sha256"],
        request_sha256=request.request_sha256,
    )
    assert manifest["job_id"] == receipt["job_id"]
    assert manifest["request_sha256"] == request.request_sha256
    assert runtime.call("result", receipt["job_id"]) == manifest


@pytest.mark.parametrize("version", [5, 6])
def test_go_native_plan_hash_publication_and_tamper_detection(
    runtime: Runtime, version: int
) -> None:
    engine_runtime(runtime)
    runtime.python = None
    mapping = _request(runtime.artifacts, runtime.engine)
    if version == 6:
        mapping.update(schema_version=6, backend="nautilus.session_replay")
        mapping["inputs"]["engine_request_ref"] = _put(
            runtime.artifacts, json.dumps(_session_request()).encode()
        )
    request = BacktestJobRequest.from_mapping(mapping)
    path = runtime.write(mapping)
    assert runtime.call("plan", str(path)) == checked_plan(
        request, runtime.artifacts, runtime.engine
    )
    receipt = runtime.call("submit", str(path))
    status = runtime.wait(receipt["job_id"])
    assert status["status"] == "SUCCEEDED", status
    manifest = read_job_result(
        runtime.results,
        job_id=receipt["job_id"],
        manifest_sha256=status["result_sha256"],
        request_sha256=request.request_sha256,
    )
    assert manifest["summary"]["equity"] == ("10019.78" if version == 5 else "10099")
    assert runtime.call("result", receipt["job_id"]) == manifest
    output = runtime.results / receipt["job_id"] / "engine_result.json"
    output.write_bytes(output.read_bytes() + b" ")
    error = runtime.call("result", receipt["job_id"], success=False)
    assert error["schema"] == "quant.agent_error.v1"
    with pytest.raises(ValueError, match="checksum"):
        read_job_result(
            runtime.results,
            job_id=receipt["job_id"],
            manifest_sha256=status["result_sha256"],
            request_sha256=request.request_sha256,
        )


def test_go_validation_precedes_database_creation(runtime: Runtime) -> None:
    mapping = {"schema_version": 5, "idempotency_key": "invalid"}
    error = runtime.call("submit", str(runtime.write(mapping)), success=False)
    assert error["code"] == "INVALID_REQUEST"
    assert error["schema"] == "quant.agent_error.v1"
    assert not runtime.database.exists()


def test_go_idempotency_conflict_and_persisted_queue(runtime: Runtime) -> None:
    engine_runtime(runtime)
    mapping = _request(runtime.artifacts, runtime.engine)
    path = runtime.write(mapping)
    first = runtime.call("submit", "--queue", str(path))
    second = runtime.call("submit", "--queue", str(path))
    assert first["created"] is True and second["created"] is False
    assert second["job_id"] == first["job_id"]
    assert runtime.call("inspect", first["job_id"])["status"] == "SUBMITTED"
    assert not (runtime.results / first["job_id"]).exists()
    mapping["budgets"]["wall_seconds"] += 1
    error = runtime.call(
        "submit", "--queue", str(runtime.write(mapping)), success=False
    )
    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    runtime.call("serve", "--once", "--concurrency", "1")
    assert runtime.call("inspect", first["job_id"])["status"] == "SUCCEEDED"
    assert (
        runtime.call("lookup", "--idempotency-key", mapping["idempotency_key"])[
            "job_id"
        ]
        == first["job_id"]
    )


def delayed_engine(runtime: Runtime, *, gate: bool = False) -> Path:
    """The real executable handles validation; only its run command waits."""
    real = runtime.engine
    wrapper = runtime.root / "delayed-engine"
    marker = runtime.root / "started"
    release = runtime.root / "release"
    body = f"""#!{sys.executable}
import os, sys, time
if len(sys.argv)>1 and sys.argv[1] == "run":
    with open({str(marker)!r}, "a") as stream:
        stream.write(str(os.getpid()) + "\\n")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if {gate!r} and os.path.exists({str(release)!r}):
            break
        time.sleep(0.02)
os.execv({str(real)!r}, [{str(real)!r}, *sys.argv[1:]])
"""
    wrapper.write_text(body, encoding="utf-8")
    wrapper.chmod(0o700)
    runtime.engine = wrapper
    return marker


def wait_marker(marker: Path, count: int = 1) -> list[int]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if marker.exists():
            pids = [int(line) for line in marker.read_text().splitlines()]
            if len(pids) >= count:
                return pids
        time.sleep(0.02)
    pytest.fail("engine child did not start")


@pytest.mark.parametrize("cancel", [True, False], ids=["cancel", "timeout"])
def test_go_cancellation_and_timeout_reap_real_child(
    runtime: Runtime, cancel: bool
) -> None:
    engine_runtime(runtime)
    marker = delayed_engine(runtime)
    mapping = _request(runtime.artifacts, runtime.engine)
    mapping["budgets"]["wall_seconds"] = 10 if cancel else 1
    receipt = runtime.call("submit", str(runtime.write(mapping)))
    pids = wait_marker(marker)
    running = runtime.call("inspect", receipt["job_id"])
    worker_pid = running["worker_pid"]
    arguments = Path(f"/proc/{worker_pid}/cmdline").read_bytes().split(b"\0")
    assert b"__worker" in arguments
    assert receipt["job_id"].encode() in arguments
    assert Path(f"/proc/{worker_pid}/exe").resolve() == runtime.executable.resolve()
    if cancel:
        status = runtime.call("cancel", receipt["job_id"])
        assert status["cancel_requested_at"] is not None
    status = runtime.wait(receipt["job_id"])
    assert status["status"] == ("CANCELLED" if cancel else "FAILED"), status
    assert status["worker_pid"] is None
    assert not (runtime.results / receipt["job_id"]).exists()
    for pid in pids:
        assert not Path(f"/proc/{pid}").exists(), (
            f"engine {pid} survived terminal state"
        )


def test_go_scheduler_bounds_slots_and_drains_persisted_queue(runtime: Runtime) -> None:
    engine_runtime(runtime)
    marker = delayed_engine(runtime, gate=True)
    ids = []
    for number in range(4):
        mapping = _request(runtime.artifacts, runtime.engine)
        mapping["idempotency_key"] = f"queued-{number}"
        mapping["budgets"]["wall_seconds"] = 30
        ids.append(
            runtime.call("submit", "--queue", str(runtime.write(mapping)))["job_id"]
        )
    scheduler = subprocess.Popen(
        runtime.args("serve", "--once", "--concurrency", "2"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_marker(marker, 2)
        time.sleep(0.25)
        assert len(marker.read_text().splitlines()) == 2
        states = [runtime.call("inspect", job_id)["status"] for job_id in ids]
        assert states.count("RUNNING") == 2 and states.count("SUBMITTED") == 2
        (runtime.root / "release").touch()
        stdout, stderr = scheduler.communicate(timeout=45)
        assert scheduler.returncode == 0, (stdout, stderr)
        assert len(marker.read_text().splitlines()) == 4
        assert all(
            runtime.call("inspect", job_id)["status"] == "SUCCEEDED" for job_id in ids
        )
    finally:
        (runtime.root / "release").touch()
        if scheduler.poll() is None:
            scheduler.terminate()
            scheduler.communicate(timeout=15)


def test_go_scheduler_restart_counts_surviving_worker(runtime: Runtime) -> None:
    """A killed scheduler leaves its existing worker occupying the single slot."""
    engine_runtime(runtime)
    marker = delayed_engine(runtime, gate=True)
    ids = []
    for number in range(2):
        mapping = _request(runtime.artifacts, runtime.engine)
        mapping["idempotency_key"] = f"restart-{number}"
        mapping["budgets"]["wall_seconds"] = 30
        ids.append(
            runtime.call("submit", "--queue", str(runtime.write(mapping)))["job_id"]
        )
    scheduler = subprocess.Popen(
        runtime.args("serve", "--concurrency", "1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    replacement = None
    try:
        wait_marker(marker)
        statuses = [runtime.call("inspect", job_id) for job_id in ids]
        running = [status for status in statuses if status["status"] == "RUNNING"]
        assert len(running) == 1
        active = running[0]
        scheduler.kill()
        scheduler.communicate(timeout=10)
        assert runtime.call("inspect", active["job_id"])["status"] == "RUNNING"
        replacement = subprocess.Popen(
            runtime.args("serve", "--once", "--concurrency", "1"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.3)
        assert replacement.poll() is None
        assert len(marker.read_text().splitlines()) == 1
        assert (
            runtime.call("inspect", active["job_id"])["worker_pid"]
            == active["worker_pid"]
        )
        (runtime.root / "release").touch()
        stdout, stderr = replacement.communicate(timeout=45)
        assert replacement.returncode == 0, (stdout, stderr)
        assert len(marker.read_text().splitlines()) == 2
        assert all(
            runtime.call("inspect", job_id)["status"] == "SUCCEEDED" for job_id in ids
        )
    finally:
        (runtime.root / "release").touch()
        for process in (scheduler, replacement):
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)


def test_go_recovery_cleans_interrupted_worker_output(runtime: Runtime) -> None:
    """Recover a dead owned worker and its persisted, unpublished output."""
    import signal

    engine_runtime(runtime)
    marker = delayed_engine(runtime, gate=True)
    mapping = _request(runtime.artifacts, runtime.engine)
    mapping["budgets"]["wall_seconds"] = 30
    receipt = runtime.call("submit", str(runtime.write(mapping)))
    wait_marker(marker)
    running = runtime.call("inspect", receipt["job_id"])
    pid = running["worker_pid"]
    assert Path(f"/proc/{pid}/exe").resolve() == runtime.executable.resolve()
    arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    assert b"__worker" in arguments and receipt["job_id"].encode() in arguments
    os.kill(pid, signal.SIGKILL)
    # Simulate output left after filesystem publication but before the DB commit.
    orphan = runtime.results / receipt["job_id"]
    orphan.mkdir(parents=True)
    (orphan / "manifest.json").write_text("{}", encoding="utf-8")
    stage = runtime.results / f".{receipt['job_id']}.tmp-interrupted"
    stage.mkdir()
    (stage / "partial").write_bytes(b"incomplete")
    with sqlite3.connect(runtime.database) as connection:
        connection.execute(
            "UPDATE backtest_jobs SET lease_expires_at=0 WHERE job_id=?",
            (receipt["job_id"],),
        )
    recovered = runtime.call("recover")
    assert recovered["recovered"] == 1
    status = runtime.call("inspect", receipt["job_id"])
    assert status["status"] == "FAILED"
    assert status["error_code"] == "WORKER_LEASE_EXPIRED"
    assert status["worker_pid"] is None
    assert not orphan.exists() and not stage.exists()
    assert runtime.call("recover")["recovered"] == 0


@pytest.mark.parametrize("invalid_input", ["missing", "malformed"])
def test_go_native_preflight_failure_creates_no_database(
    runtime: Runtime, invalid_input: str
) -> None:
    engine_runtime(runtime)
    mapping = _request(runtime.artifacts, runtime.engine)
    if invalid_input == "missing":
        digest = mapping["inputs"]["engine_request_ref"].rsplit("/", 1)[-1]
        (runtime.artifacts / "sha256" / digest).unlink()
    else:
        mapping["inputs"]["engine_request_ref"] = _put(runtime.artifacts, b"{}")
    # The outer job remains valid: failure concerns its referenced engine input.
    BacktestJobRequest.from_mapping(mapping)
    error = runtime.call("submit", str(runtime.write(mapping)), success=False)
    assert error["schema"] == "quant.agent_error.v1"
    assert error["code"] in {"INPUT_UNAVAILABLE", "ENGINE_REQUEST_INVALID"}
    assert not runtime.database.exists()
    assert not runtime.results.exists()


def test_go_scheduler_database_failure_reaps_owned_workers(runtime: Runtime) -> None:
    engine_runtime(runtime)
    marker = delayed_engine(runtime, gate=True)
    mapping = _request(runtime.artifacts, runtime.engine)
    mapping["budgets"]["wall_seconds"] = 30
    receipt = runtime.call("submit", "--queue", str(runtime.write(mapping)))
    scheduler = subprocess.Popen(
        runtime.args("serve", "--concurrency", "1"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        engine_pids = wait_marker(marker)
        running = runtime.call("inspect", receipt["job_id"])
        assert running["status"] == "RUNNING"
        worker_pid = running["worker_pid"]
        assert Path(f"/proc/{worker_pid}/exe").resolve() == runtime.executable.resolve()
        arguments = Path(f"/proc/{worker_pid}/cmdline").read_bytes().split(b"\0")
        assert b"__worker" in arguments and receipt["job_id"].encode() in arguments
        # Fault injection affects only this test's database and owned processes.
        with sqlite3.connect(runtime.database) as connection:
            connection.execute("DROP TABLE backtest_jobs")
        stdout, stderr = scheduler.communicate(timeout=10)
        assert scheduler.returncode != 0, (stdout, stderr)
        error = json.loads(stderr)
        assert error["schema"] == "quant.agent_error.v1"
        for pid in (worker_pid, *engine_pids):
            assert not Path(f"/proc/{pid}").exists(), (
                f"owned process {pid} survived scheduler failure"
            )
        assert not (runtime.results / receipt["job_id"]).exists()
    finally:
        (runtime.root / "release").touch()
        if scheduler.poll() is None:
            scheduler.terminate()
            scheduler.communicate(timeout=15)
