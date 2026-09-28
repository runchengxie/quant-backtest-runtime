"""Durable multi-decision diagnostic jobs use the public sequenced backend."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pytest
from test_execution_aware import _put_frame
from test_jobs import _request

from backtest_runtime.jobs import BacktestJobRequest, BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def _put_json(root: Path, payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True).encode()
    digest = sha256(raw).hexdigest()
    target = root / "sha256" / digest
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return f"artifact://sha256/{digest}"


def _clock(decision: str, entry: str) -> dict[str, str]:
    return {
        "schema_version": "research.clock.v1",
        "timezone": "Asia/Shanghai",
        "information_cutoff_at": f"{decision}T15:00:00+08:00",
        "signal_at": f"{decision}T15:01:00+08:00",
        "decision_at": f"{decision}T15:02:00+08:00",
        "earliest_order_at": f"{entry}T09:15:00+08:00",
        "execution_window_start_at": f"{entry}T09:30:00+08:00",
        "execution_window_end_at": "2026-01-08T15:00:00+08:00",
        "valuation_at": "2026-01-08T16:00:00+08:00",
        "timing_policy_id": "synthetic.next-session.v1",
        "trading_calendar_ref": "synthetic-calendar",
    }


def _request_v3(root: Path) -> dict:
    mapping = _request(key="sequenced-two-decisions")
    mapping["schema_version"] = 3
    mapping["backend"] = "native.sequenced_execution"
    mapping["inputs"] = {
        "positions_ref": _put_frame(
            root,
            pd.DataFrame(
                {
                    "rebalance_date": ["20260102", "20260106"],
                    "entry_date": ["20260105", "20260107"],
                    "symbol": ["AAA", "BBB"],
                    "weight": [1.0, 1.0],
                }
            ),
        ),
        "pricing_ref": _put_frame(
            root,
            pd.DataFrame(
                {
                    "trade_date": ["20260105", "20260106", "20260107", "20260108"] * 2,
                    "symbol": ["AAA"] * 4 + ["BBB"] * 4,
                    "close": [100.0, 102.0, 103.0, 104.0, 50.0, 51.0, 52.0, 53.0],
                    "amount": [10_000_000.0] * 8,
                }
            ),
        ),
        "decision_clocks_ref": _put_json(
            root,
            {
                "20260102": _clock("2026-01-02", "2026-01-05"),
                "20260106": _clock("2026-01-06", "2026-01-07"),
            },
        ),
    }
    mapping["config"] = {
        "price_col": "close",
        "tradable_col": None,
        "buy_tradable_col": None,
        "sell_tradable_col": None,
        "limit_up_col": None,
        "limit_down_col": None,
        "listing_status_col": None,
        "transaction_cost_bps": 0.0,
        "price_basis": "close",
    }
    ledger = mapping["execution"]["ledger_config"]
    ledger["enabled"] = True
    ledger["participation_rate"] = 1.0
    ledger["liquidity_cols"] = ["amount"]
    mapping["execution"] = {"ledger_config": ledger}
    mapping["budgets"]["memory_mb"] = 8192
    return mapping


def test_v3_request_rejects_unknown_backend_and_missing_clock(tmp_path: Path) -> None:
    mapping = _request_v3(tmp_path / "artifacts")
    assert (
        BacktestJobRequest.from_mapping(mapping).backend == "native.sequenced_execution"
    )
    mapping["backend"] = "native.position_replay"
    with pytest.raises(ValueError, match="backend"):
        BacktestJobRequest.from_mapping(mapping)
    mapping["backend"] = "native.sequenced_execution"
    del mapping["inputs"]["decision_clocks_ref"]
    with pytest.raises(ValueError, match="decision_clocks_ref"):
        BacktestJobRequest.from_mapping(mapping)


def test_v3_worker_publishes_hash_verified_multi_decision_result(
    tmp_path: Path,
) -> None:
    artifacts = tmp_path / "artifacts"
    request = BacktestJobRequest.from_mapping(_request_v3(artifacts))
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
        assert manifest["backend"] == "native.sequenced_execution"
        assert manifest["metadata"]["decision_count"] == 2
        assert not pd.read_parquet(
            results / receipt.job_id / "daily_ledger.parquet"
        ).empty
    finally:
        store.close()
