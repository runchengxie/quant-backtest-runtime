"""Durable trade accounting jobs delegate math to the public platform."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
from test_execution_aware import _put_frame

from backtest_runtime.jobs import BacktestJobRequest, BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def _request_v4(root: Path) -> dict:
    return {
        "schema_version": 4,
        "idempotency_key": "synthetic-trade-accounting",
        "backend": "native.trade_accounting",
        "evidence_tier": "diagnostic",
        "inputs": {
            "accounting_ref": _put_frame(
                root,
                pd.DataFrame(
                    {
                        "symbol": ["A", "B"],
                        "previous_weight": [0.5, 0.5],
                        "target_weight": [0.5, 0.5],
                        "previous_price": [10.0, 10.0],
                        "current_price": [20.0, 10.0],
                        "tradable": [True, True],
                    }
                ),
            ),
        },
        "config": {
            "commission_rate": 0.001,
            "stamp_tax_rate": 0.005,
            "slippage_rate": 0.002,
        },
        "execution": {},
        "budgets": {"wall_seconds": 30, "memory_mb": 8192},
    }


def test_v4_rejects_invalid_config_and_input(tmp_path: Path) -> None:
    mapping = _request_v4(tmp_path / "artifacts")
    assert BacktestJobRequest.from_mapping(mapping).backend == "native.trade_accounting"
    mapping["config"]["commission_rate"] = -0.1
    with pytest.raises(ValueError, match="commission_rate"):
        BacktestJobRequest.from_mapping(mapping)
    mapping["config"]["commission_rate"] = 0.001
    del mapping["inputs"]["accounting_ref"]
    with pytest.raises(ValueError, match="accounting_ref"):
        BacktestJobRequest.from_mapping(mapping)


def test_v4_worker_publishes_verified_accounting(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    request = BacktestJobRequest.from_mapping(_request_v4(artifacts))
    database, results = tmp_path / "jobs.sqlite", tmp_path / "results"
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
        assert manifest["backend"] == "native.trade_accounting"
        assert manifest["summary"]["turnover"] == pytest.approx(1 / 6)
        assert manifest["summary"]["total_cost"] == pytest.approx(
            1 / 3000 + 1 / 1200 + 1 / 1500
        )
        path = results / receipt.job_id / "accounting.parquet"
        path.write_bytes(path.read_bytes() + b"tamper")
        with pytest.raises(ValueError, match="checksum"):
            read_job_result(
                results,
                job_id=receipt.job_id,
                manifest_sha256=status.result_sha256 or "",
                request_sha256=request.request_sha256,
            )
    finally:
        store.close()
