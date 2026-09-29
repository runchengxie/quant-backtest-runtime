"""Full execution-aware Job publication and verification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from research_contracts import build_quant_run_manifest
from test_jobs import _request

from backtest_runtime.jobs import BacktestJobRequest, BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore
from backtest_runtime.worker import (
    _validate_input_clock_dates,
    _validate_ledger_clock_dates,
)


def _put_frame(root: Path, frame: pd.DataFrame) -> str:
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / "input.parquet"
    frame.to_parquet(temporary, index=False)
    digest = sha256(temporary.read_bytes()).hexdigest()
    target = root / "sha256" / digest
    target.parent.mkdir(exist_ok=True)
    temporary.replace(target)
    return f"artifact://sha256/{digest}"


def _put_bytes(root: Path, name: str, content: bytes) -> str:
    source = root / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    digest = sha256(content).hexdigest()
    target = root / "sha256" / digest
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(content)
    return f"artifact://sha256/{digest}"


def _put_quant_run_manifest(root: Path, clock: dict[str, str]) -> str:
    source = root / "manifest-source"
    (source / "backtest_bundle").mkdir(parents=True)
    (source / "backtest_bundle" / "manifest.json").write_text("{}\n")
    (source / "config.used.yml").write_text("seed: 1\n")
    (source / "execution.json").write_bytes(b'{"execution": true}\n')
    manifest_path = build_quant_run_manifest(
        source,
        run_id="research-run-1",
        strategy_ref="strategy:synthetic-v1",
        research_purpose="runtime consumer test",
        evidence_tier="diagnostic",
        clock=clock,
        producer_versions=[{"repository": "quant-backtest-runtime", "commit": "test"}],
        data_refs=[],
        signal_refs=[],
        component_refs={
            "execution": {"path": "execution.json", "artifact_id": "execution"}
        },
    )
    _put_bytes(root, "quant-run.manifest.json", manifest_path.read_bytes())
    _put_bytes(root, "execution-copy.json", (source / "execution.json").read_bytes())
    return f"artifact://sha256/{sha256(manifest_path.read_bytes()).hexdigest()}"


def _request_v2(root: Path) -> dict:
    request = _request(key="official-bundle")
    request["schema_version"] = 2
    request["evidence_tier"] = "execution_aware"
    request["execution"]["ledger"] = True
    request["execution"]["ledger_config"]["enabled"] = True
    request["execution"]["ledger_config"]["portfolio_value"] = 100_000.0
    request["execution"]["ledger_config"]["participation_rate"] = 1.0
    request["execution"]["ledger_config"]["liquidity_cols"] = ["amount"]
    request["budgets"]["memory_mb"] = 8192
    request["inputs"] = {
        "positions_ref": _put_frame(
            root,
            pd.DataFrame(
                {
                    "rebalance_date": ["20260102"],
                    "entry_date": ["20260105"],
                    "symbol": ["AAA"],
                    "weight": [1.0],
                    "side": ["long"],
                }
            ),
        ),
        "pricing_ref": _put_frame(
            root,
            pd.DataFrame(
                {
                    "trade_date": ["20260105", "20260106"],
                    "symbol": ["AAA", "AAA"],
                    "close": [100.0, 105.0],
                    "amount": [10_000_000.0, 10_000_000.0],
                }
            ),
        ),
        "periods_ref": _put_frame(
            root,
            pd.DataFrame(
                {
                    "rebalance_date": ["20260102"],
                    "entry_date": ["20260105"],
                    "exit_date": ["20260106"],
                }
            ),
        ),
    }
    request["research_clock"] = {
        "schema_version": "research.clock.v1",
        "timezone": "Asia/Shanghai",
        "information_cutoff_at": "2026-01-02T15:00:00+08:00",
        "signal_at": "2026-01-02T15:01:00+08:00",
        "decision_at": "2026-01-02T15:02:00+08:00",
        "earliest_order_at": "2026-01-05T09:15:00+08:00",
        "execution_window_start_at": "2026-01-05T09:30:00+08:00",
        "execution_window_end_at": "2026-01-06T10:00:00+08:00",
        "valuation_at": "2026-01-06T15:00:00+08:00",
        "timing_policy_id": "a-share.close-next-open.v1",
        "trading_calendar_ref": "sse-szse-202601",
    }
    request["producer"] = {
        "repository": "quant-backtest-runtime",
        "version": "0.1.0",
        "commit": "3ede35924d124ebdb2ada89bbcd23d3f9ced82b9",
        "backend": "native.position_replay",
    }
    return request


def test_v2_rejects_incomplete_clock_before_submit(tmp_path: Path) -> None:
    mapping = _request_v2(tmp_path / "artifacts")
    del mapping["research_clock"]["execution_window_end_at"]
    with pytest.raises(ValueError, match="execution_window_end_at"):
        BacktestJobRequest.from_mapping(mapping)
    assert not (tmp_path / "jobs.sqlite").exists()


def test_v2_input_clock_rejects_future_signal_and_quote_dates(tmp_path: Path) -> None:
    clock = _request_v2(tmp_path / "artifacts")["research_clock"]
    frames = {
        "positions_ref": pd.DataFrame(
            {"rebalance_date": ["20260102"], "entry_date": ["20260105"]}
        ),
        "periods_ref": pd.DataFrame(
            {
                "rebalance_date": ["20260102"],
                "entry_date": ["20260105"],
                "exit_date": ["20260106"],
            }
        ),
        "pricing_ref": pd.DataFrame({"trade_date": ["20260105", "20260106"]}),
    }
    _validate_input_clock_dates(frames, clock)
    frames["positions_ref"].loc[0, "rebalance_date"] = "20260105"
    with pytest.raises(ValueError, match="decision cutoff"):
        _validate_input_clock_dates(frames, clock)
    frames["positions_ref"].loc[0, "rebalance_date"] = "20260102"
    frames["pricing_ref"].loc[1, "trade_date"] = "20260107"
    with pytest.raises(ValueError, match="future dates"):
        _validate_input_clock_dates(frames, clock)


def test_zero_trade_nav_cannot_be_assigned_to_future_clock(tmp_path: Path) -> None:
    clock = _request_v2(tmp_path / "artifacts")["research_clock"]
    for name in (
        "information_cutoff_at",
        "signal_at",
        "decision_at",
        "earliest_order_at",
        "execution_window_start_at",
        "execution_window_end_at",
        "valuation_at",
    ):
        clock[name] = clock[name].replace("2026", "2027")
    result = SimpleNamespace(
        unified_ledger=SimpleNamespace(
            orders=pd.DataFrame(),
            fills=pd.DataFrame(),
            daily_nav=pd.DataFrame({"trade_date": ["20260105"], "nav": [100_000.0]}),
        )
    )
    with pytest.raises(ValueError, match="daily NAV"):
        _validate_ledger_clock_dates(result, clock)


def test_v2_worker_publishes_and_verifies_official_bundle(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    request = BacktestJobRequest.from_mapping(mapping)
    database = tmp_path / "jobs.sqlite"
    results = tmp_path / "results"
    store = JobStore(database)
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
            str(database),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
        ],
        capture_output=True,
        check=False,
        timeout=45,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    store = JobStore(database)
    try:
        status = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).get_status(receipt.job_id)
        assert status.status == "SUCCEEDED"
        manifest = read_job_result(
            results,
            job_id=receipt.job_id,
            manifest_sha256=status.result_sha256 or "",
            request_sha256=request.request_sha256,
        )
        assert manifest["schema_version"] == "quant.backtest_job_result.v2"
        bundle = json.loads(
            (results / receipt.job_id / "bundle" / "manifest.json").read_text()
        )
        assert bundle["evidence_tier"] == "execution_aware"
        assert bundle["reconciliation"]["status"] == "passed"
        frame_path = results / receipt.job_id / "bundle" / "daily_nav.parquet"
        frame_path.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="SHA-256"):
            read_job_result(
                results,
                job_id=receipt.job_id,
                manifest_sha256=status.result_sha256 or "",
                request_sha256=request.request_sha256,
            )
    finally:
        store.close()


def test_v2_worker_consumes_and_publishes_quant_run_manifest(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    mapping["quant_run_manifest_ref"] = _put_quant_run_manifest(
        artifacts, mapping["research_clock"]
    )
    request = BacktestJobRequest.from_mapping(mapping)
    database = tmp_path / "jobs.sqlite"
    results = tmp_path / "results"
    store = JobStore(database)
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
            str(database),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
        ],
        capture_output=True,
        check=False,
        timeout=45,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    store = JobStore(database)
    try:
        status = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).get_status(receipt.job_id)
        manifest = read_job_result(
            results,
            job_id=receipt.job_id,
            manifest_sha256=status.result_sha256 or "",
            request_sha256=request.request_sha256,
        )
        assert manifest["quant_run_manifest_run_id"] == "research-run-1"
        assert (results / receipt.job_id / "quant_run_manifest.json").is_file()
    finally:
        store.close()


def test_quant_run_manifest_component_refs_must_be_available(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    manifest_ref = _put_quant_run_manifest(artifacts, mapping["research_clock"])
    component = artifacts / "sha256"
    for path in component.iterdir():
        if path.is_file() and path.read_bytes() == b'{"execution": true}\n':
            path.unlink()
    mapping["quant_run_manifest_ref"] = manifest_ref
    request = BacktestJobRequest.from_mapping(mapping)
    service = BacktestJobService(
        JobStore(tmp_path / "jobs.sqlite"),
        artifact_root=artifacts,
        result_root=tmp_path / "results",
    )
    with pytest.raises(FileNotFoundError, match="Immutable input artifact"):
        service.submit(request)


def test_v1_diagnostic_worker_retains_historical_schema(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    mapping["schema_version"] = 1
    mapping["evidence_tier"] = "diagnostic"
    mapping["execution"]["ledger"] = False
    mapping["execution"]["ledger_config"]["enabled"] = False
    del mapping["research_clock"]
    del mapping["producer"]
    request = BacktestJobRequest.from_mapping(mapping)
    database = tmp_path / "jobs.sqlite"
    results = tmp_path / "results"
    store = JobStore(database)
    receipt = BacktestJobService(
        store, artifact_root=artifacts, result_root=results
    ).submit(request)
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtest_runtime.worker",
            "--job-id",
            receipt.job_id,
            "--registry",
            str(database),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
        ],
        capture_output=True,
        check=False,
        timeout=45,
        env=os.environ.copy(),
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    store = JobStore(database)
    try:
        status = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).get_status(receipt.job_id)
        assert status.status == "SUCCEEDED"
        manifest = read_job_result(
            results,
            job_id=receipt.job_id,
            manifest_sha256=status.result_sha256 or "",
            request_sha256=request.request_sha256,
        )
        assert manifest["schema_version"] == "ticknet.backtest_job_result.v1"
    finally:
        store.close()


def test_v2_rejects_clock_window_unrelated_to_execution_dates(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    clock = mapping["research_clock"]
    for name in (
        "information_cutoff_at",
        "signal_at",
        "decision_at",
        "earliest_order_at",
        "execution_window_start_at",
        "execution_window_end_at",
        "valuation_at",
    ):
        clock[name] = clock[name].replace("2026", "2027")
    request = BacktestJobRequest.from_mapping(mapping)
    database = tmp_path / "jobs.sqlite"
    results = tmp_path / "results"
    store = JobStore(database)
    receipt = BacktestJobService(
        store, artifact_root=artifacts, result_root=results
    ).submit(request)
    store.close()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtest_runtime.worker",
            "--job-id",
            receipt.job_id,
            "--registry",
            str(database),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
        ],
        capture_output=True,
        check=False,
        timeout=45,
        env=os.environ.copy(),
    )
    assert completed.returncode == 1
    store = JobStore(database)
    try:
        status = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).get_status(receipt.job_id)
        assert status.status == "FAILED"
        assert status.error_code == "BACKTEST_REJECTED"
        assert "decision cutoff date" in (status.error_message or "")
        assert not (results / receipt.job_id).exists()
    finally:
        store.close()
