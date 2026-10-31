"""Opt-in engine jobs retain durable lifecycle and a separate result contract."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backtest_runtime.jobs import BacktestJobRequest, BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def _put(root: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    path = root / "sha256" / digest
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"artifact://sha256/{digest}"


def _request(root: Path, binary: Path) -> dict:
    payload = {
        "schema_version": "engine_request.v1",
        "initial_cash": "10000.00",
        "fee_bps": "10",
        "instruments": [{"symbol": "AAA", "price_precision": 2}],
        "quotes": [
            {
                "symbol": "AAA",
                "ts_ns": 1735689600000000000,
                "bid": "10.00",
                "ask": "10.00",
            },
            {
                "symbol": "AAA",
                "ts_ns": 1735689601000000000,
                "bid": "12.00",
                "ask": "12.00",
            },
        ],
        "orders": [
            {
                "id": "b",
                "symbol": "AAA",
                "information_ts_ns": 1735689599999999999,
                "side": "buy",
                "quantity": "10",
            },
            {
                "id": "s",
                "symbol": "AAA",
                "information_ts_ns": 1735689600000000000,
                "side": "sell",
                "quantity": "10",
            },
        ],
        "required_capabilities": [],
    }
    return {
        "schema_version": 5,
        "idempotency_key": "rust-synthetic",
        "backend": "nautilus.quote_market_orders",
        "evidence_tier": "diagnostic",
        "inputs": {"engine_request_ref": _put(root, json.dumps(payload).encode())},
        "config": {
            "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
        },
        "execution": {},
        "budgets": {"wall_seconds": 10, "memory_mb": 4096},
    }


def test_v5_contract_pins_binary_and_keeps_input_immutable(tmp_path: Path) -> None:
    mapping = _request(tmp_path, Path(sys.executable).resolve())
    request = BacktestJobRequest.from_mapping(mapping)
    assert request.backend == "nautilus.quote_market_orders"
    mapping["config"]["executable_sha256"] = "x" * 64
    with pytest.raises(ValueError, match="executable_sha256"):
        BacktestJobRequest.from_mapping(mapping)
    mapping["config"]["executable_sha256"] = "a" * 64
    mapping["execution"] = {"shell": "echo unsafe"}
    with pytest.raises(ValueError, match="execution"):
        BacktestJobRequest.from_mapping(mapping)


def test_v5_real_engine_publishes_verified_result(tmp_path: Path) -> None:
    binary = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    if binary is None:
        pytest.skip("real Rust engine integration runs in dedicated CI")
    artifacts = tmp_path / "artifacts"
    results = tmp_path / "results"
    db = tmp_path / "jobs.sqlite"
    request = BacktestJobRequest.from_mapping(_request(artifacts, Path(binary)))
    store = JobStore(db)
    service = BacktestJobService(store, artifact_root=artifacts, result_root=results)
    receipt = service.submit(request)
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtest_runtime.worker",
            "--job-id",
            receipt.job_id,
            "--registry",
            str(db),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
            "--engine-executable",
            binary,
        ],
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode()
    store = JobStore(db)
    try:
        status = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).get_status(receipt.job_id)
        assert status.status == "SUCCEEDED"
        args = {
            "job_id": receipt.job_id,
            "manifest_sha256": status.result_sha256 or "",
            "request_sha256": request.request_sha256,
        }
        manifest = read_job_result(results, **args)
        assert manifest["summary"]["equity"] == "10019.78"
        assert manifest["executable_sha256"] == request.config["executable_sha256"]
        output = results / receipt.job_id / "engine_result.json"
        output.write_bytes(output.read_bytes() + b" ")
        with pytest.raises(ValueError, match="checksum"):
            read_job_result(results, **args)
    finally:
        store.close()


def _fixture_program(tmp_path: Path, body: str) -> Path:
    binary = tmp_path / "engine-fixture"
    binary.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    binary.chmod(0o700)
    return binary


def test_runner_rejects_unpinned_executable_and_invalid_result(tmp_path: Path) -> None:
    from backtest_runtime.engine_runner import run_engine

    binary = _fixture_program(tmp_path, 'print("{}")')
    mapping = _request(tmp_path / "artifacts", binary)
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        service = BacktestJobService(
            store,
            artifact_root=tmp_path / "artifacts",
            result_root=tmp_path / "results",
        )
        request = BacktestJobRequest.from_mapping(mapping)
        with pytest.raises(ValueError, match="operator-configured"):
            run_engine(request, service, None)
        mapping["config"]["executable_sha256"] = "a" * 64
        with pytest.raises(ValueError, match="SHA-256"):
            run_engine(BacktestJobRequest.from_mapping(mapping), service, binary)
        with pytest.raises(ValueError, match="result fields"):
            run_engine(request, service, binary)
    finally:
        store.close()


def _worker(
    tmp_path: Path, binary: Path, *, wall_seconds: int = 30
) -> tuple[subprocess.Popen, str, Path]:
    artifacts = tmp_path / "artifacts"
    db = tmp_path / "jobs.sqlite"
    results = tmp_path / "results"
    mapping = _request(artifacts, binary)
    mapping["budgets"]["wall_seconds"] = wall_seconds
    store = JobStore(db)
    try:
        receipt = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).submit(BacktestJobRequest.from_mapping(mapping))
    finally:
        store.close()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backtest_runtime.worker",
            "--job-id",
            receipt.job_id,
            "--registry",
            str(db),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
            "--engine-executable",
            str(binary),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return process, receipt.job_id, db


def _wait_for_pid(marker: Path) -> int:
    import time

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if marker.is_file() and marker.read_text().strip():
            return int(marker.read_text())
        time.sleep(0.02)
    raise AssertionError("child did not start")


def _dead(pid: int) -> bool:
    path = Path("/proc") / str(pid) / "status"
    try:
        return "State:\tZ" in path.read_text()
    except FileNotFoundError:
        return True


@pytest.mark.parametrize("hard_kill", [False, True])
def test_cancellation_and_hard_worker_death_leave_no_engine(
    tmp_path: Path, hard_kill: bool
) -> None:
    import signal
    import time

    marker = tmp_path / "child.pid"
    binary = _fixture_program(
        tmp_path,
        f"import os,time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)",
    )
    worker, job_id, db = _worker(tmp_path, binary)
    pid = None
    try:
        pid = _wait_for_pid(marker)
        worker.send_signal(signal.SIGKILL if hard_kill else signal.SIGINT)
        worker.wait(timeout=5)
        deadline = time.monotonic() + 5
        while not _dead(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _dead(pid), "engine survived worker termination"
        assert not (tmp_path / "results" / job_id).exists()
        if not hard_kill:
            store = JobStore(db)
            try:
                assert store.get_backtest_job(job_id)["status"] == "CANCELLED"
            finally:
                store.close()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait()
        if pid is not None and not _dead(pid):
            os.kill(pid, signal.SIGKILL)


def test_worker_timeout_kills_child_and_records_failure(tmp_path: Path) -> None:
    marker = tmp_path / "child.pid"
    binary = _fixture_program(
        tmp_path,
        f"import os,time\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(60)",
    )
    worker, job_id, db = _worker(tmp_path, binary, wall_seconds=1)
    pid = _wait_for_pid(marker)
    worker.wait(timeout=5)
    assert _dead(pid)
    store = JobStore(db)
    try:
        row = store.get_backtest_job(job_id)
        assert row["status"] == "FAILED"
        assert row["error_code"] == "JOB_TIMEOUT"
    finally:
        store.close()


def _engine_result() -> dict:
    return {
        "schema_version": "engine_result.v1",
        "backend": "nautilus",
        "backend_version": "0.66.0",
        "capabilities": [
            "quote_market_orders",
            "multi_asset",
            "final_balances",
            "proportional_fees",
        ],
        "currency": "USD",
        "cash": "1",
        "positions_value": "0",
        "equity": "1",
        "commission": "0",
        "orders": [],
        "fills": [],
        "positions": [],
    }


def test_pinned_inode_survives_atomic_executable_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backtest_runtime.engine_runner import run_engine

    original = _engine_result()
    binary = _fixture_program(tmp_path, f"print({json.dumps(original)!r})")
    mapping = _request(tmp_path / "artifacts", binary)
    replacement = tmp_path / "replacement"
    altered = _engine_result()
    altered["cash"] = "2"
    altered["equity"] = "2"
    replacement.write_text(f"#!{sys.executable}\nprint({json.dumps(altered)!r})\n")
    replacement.chmod(0o700)
    real_digest = hashlib.file_digest

    def switch_after_hash(source, digest):
        result = real_digest(source, digest)
        os.replace(replacement, binary)
        return result

    monkeypatch.setattr(hashlib, "file_digest", switch_after_hash)
    store = JobStore(tmp_path / "jobs.sqlite")
    try:
        service = BacktestJobService(
            store,
            artifact_root=tmp_path / "artifacts",
            result_root=tmp_path / "results",
        )
        assert (
            run_engine(BacktestJobRequest.from_mapping(mapping), service, binary)[
                "cash"
            ]
            == "1"
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "field,row",
    [
        ("orders", {"id": "x"}),
        ("positions", {"market_value": "0"}),
        (
            "positions",
            {
                "symbol": "AAA",
                "quantity": "NaN",
                "mark_price": "1",
                "market_value": "0",
            },
        ),
        ("fills", {"order_id": "missing", "commission": "0"}),
    ],
)
def test_engine_protocol_rejects_incomplete_or_invalid_rows(
    field: str, row: dict
) -> None:
    from backtest_runtime.engine_protocol import validate_engine_result

    payload = _engine_result()
    payload[field] = [row]
    with pytest.raises(ValueError):
        validate_engine_result(payload)
