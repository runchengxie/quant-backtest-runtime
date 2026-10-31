"""Session results are independently reconciled before durable publication."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_rust_engine_jobs import _put, _request

from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.engine_sessions_protocol import validate_session_result
from backtest_runtime.jobs import BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def _result() -> dict:
    return {
        "schema_version": "engine_result.v2",
        "backend": "nautilus",
        "backend_version": "0.66.0",
        "currency": "USD",
        "price_basis": "raw",
        "valuation_timezone": "UTC",
        "enforce_t1": True,
        "initial_cash": "10",
        "final_cash": "7.99",
        "final_nav": "10.99",
        "total_commission": "0.01",
        "capabilities": [
            "session_ledger",
            "configured_t_plus_one",
            "buy_lots",
            "quote_trade_permissions",
            "quote_liquidity",
        ],
        "orders": [
            {"id": "buy", "symbol": "AAA", "status": "FILLED", "filled_quantity": "1"}
        ],
        "fills": [
            {
                "order_id": "buy",
                "symbol": "AAA",
                "side": "buy",
                "ts_ns": 1735689600000000000,
                "quantity": "1",
                "price": "2",
                "commission": "0.01",
            }
        ],
        "rejections": [],
        "daily_ledger": [
            {
                "session_date": "2025-01-01",
                "valuation_ts_ns": 1735689601000000000,
                "cash": "7.99",
                "positions_value": "2",
                "nav": "9.99",
                "commission": "0.01",
            },
            {
                "session_date": "2025-01-02",
                "valuation_ts_ns": 1735776001000000000,
                "cash": "7.99",
                "positions_value": "3",
                "nav": "10.99",
                "commission": "0",
            },
        ],
        "daily_positions": [
            {
                "session_date": "2025-01-01",
                "symbol": "AAA",
                "quantity": "1",
                "mark_price": "2",
                "market_value": "2",
            },
            {
                "session_date": "2025-01-02",
                "symbol": "AAA",
                "quantity": "1",
                "mark_price": "3",
                "market_value": "3",
            },
        ],
    }


def test_valid_session_result_reconciles_realized_cash_and_daily_holdings() -> None:
    value = _result()
    assert validate_session_result(value) == value


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(currency="EUR"),
        lambda v: v.update(valuation_timezone="Asia/Shanghai"),
        lambda v: v.update(enforce_t1=1),
        lambda v: v["daily_ledger"][0].update(cash="8", nav="10"),
        lambda v: v["daily_positions"][0].update(quantity="NaN"),
        lambda v: v["daily_positions"][1].update(quantity="2", market_value="6"),
        lambda v: v["daily_ledger"][1].update(commission="0.01"),
        lambda v: v["fills"][0].update(ts_ns=1735862400000000000),
        lambda v: v.update(
            rejections=[{"order_id": "buy", "event_type": "denied", "reason": "fake"}]
        ),
    ],
)
def test_session_protocol_rejects_balanced_but_false_or_malformed_evidence(
    mutation,
) -> None:
    value = _result()
    mutation(value)
    with pytest.raises(ValueError):
        validate_session_result(value)


def _session_request() -> dict:
    t = 1735781400000000000

    def quote(ts, p):
        return {
            "symbol": "AAA",
            "ts_ns": ts,
            "bid": p,
            "ask": p,
            "bid_size": "1000000",
            "ask_size": "1000000",
            "tradable": True,
            "limit_up": False,
            "limit_down": False,
        }

    return {
        "schema_version": "engine_request.v2",
        "currency": "CNY",
        "price_basis": "raw",
        "initial_cash": "10000",
        "fee_bps": "10",
        "enforce_t1": True,
        "instruments": [{"symbol": "AAA", "price_precision": 2, "buy_lot": 100}],
        "required_capabilities": ["session_ledger"],
        "sessions": [
            {
                "session_date": "2025-01-02",
                "open_ts_ns": t,
                "valuation_ts_ns": t + 1000000000,
                "quotes": [quote(t, "10"), quote(t + 1000000000, "11")],
                "orders": [
                    {
                        "id": "buy",
                        "symbol": "AAA",
                        "information_ts_ns": t - 1,
                        "side": "buy",
                        "quantity": "100",
                    }
                ],
            }
        ],
    }


def test_v6_contract_and_real_engine_publication(tmp_path: Path) -> None:
    binary = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    if binary is None:
        pytest.skip("real engine built in dedicated CI")
    artifacts = tmp_path / "artifacts"
    results = tmp_path / "results"
    db = tmp_path / "jobs.sqlite"
    mapping = _request(artifacts, Path(binary))
    mapping.update(schema_version=6, backend="nautilus.session_replay")
    mapping["inputs"] = {
        "engine_request_ref": _put(artifacts, json.dumps(_session_request()).encode())
    }
    request = BacktestJobRequest.from_mapping(mapping)
    store = JobStore(db)
    try:
        receipt = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).submit(request)
    finally:
        store.close()
    process = subprocess.run(
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
        check=False,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr.decode()
    store = JobStore(db)
    try:
        row = store.get_backtest_job(receipt.job_id)
        assert row["status"] == "SUCCEEDED"
        manifest = read_job_result(
            results,
            job_id=receipt.job_id,
            manifest_sha256=row["result_sha256"],
            request_sha256=request.request_sha256,
        )
        assert manifest["schema_version"] == "quant.engine_job_result.v2"
        assert manifest["summary"]["equity"] == "10099"
        assert (
            json.loads((results / receipt.job_id / "engine_result.json").read_text())[
                "currency"
            ]
            == "CNY"
        )
    finally:
        store.close()


def test_v6_preflight_checks_binary_without_opening_job_storage(tmp_path: Path) -> None:
    from backtest_runtime.agent_access import AccessError, build_plan

    binary = Path(sys.executable).resolve()
    artifacts = tmp_path / "artifacts"
    mapping = _request(artifacts, binary)
    mapping.update(schema_version=6, backend="nautilus.session_replay")
    mapping["inputs"] = {
        "engine_request_ref": _put(artifacts, json.dumps(_session_request()).encode())
    }
    request = BacktestJobRequest.from_mapping(mapping)
    with pytest.raises(AccessError, match="absolute"):
        build_plan(request, artifacts, None)
    plan = build_plan(request, artifacts, binary)
    assert plan["checks"]["executable_hash"] == "passed"
    assert plan["checks"]["engine_request_semantics"] == "not_checked"
    assert not (tmp_path / "jobs.sqlite").exists()


def test_session_result_cannot_claim_a_different_request_or_t1_policy() -> None:
    from backtest_runtime.engine_sessions_protocol import bind_session_request

    value = _result()
    request = {
        "schema_version": "engine_request.v2",
        "currency": "CNY",
        "initial_cash": "10",
        "price_basis": "raw",
        "enforce_t1": True,
        "sessions": [],
    }
    with pytest.raises(ValueError, match="currency"):
        bind_session_request(value, request)
    value["fills"].append(
        {
            "order_id": "sell",
            "symbol": "AAA",
            "side": "sell",
            "ts_ns": 1735689600000000001,
            "quantity": "1",
            "price": "2",
            "commission": "0",
        }
    )
    value["orders"].append(
        {"id": "sell", "symbol": "AAA", "status": "FILLED", "filled_quantity": "1"}
    )
    value["daily_positions"] = []
    value["daily_ledger"][0].update(cash="9.99", positions_value="0")
    value["daily_ledger"][1].update(cash="9.99", positions_value="0", nav="9.99")
    value.update(final_cash="9.99", final_nav="9.99")
    with pytest.raises(ValueError, match=r"T\+1"):
        validate_session_result(value)


def test_request_binding_rejects_balanced_but_inflated_valuation() -> None:
    from backtest_runtime.engine_sessions_protocol import bind_session_request

    value = _result()
    request = {
        "schema_version": "engine_request.v2",
        **{
            k: value[k]
            for k in ("currency", "initial_cash", "price_basis", "enforce_t1")
        },
        "sessions": [
            {
                "session_date": day["session_date"],
                "valuation_ts_ns": day["valuation_ts_ns"],
                "open_ts_ns": day["valuation_ts_ns"] - 1000000000,
                "quotes": [
                    {
                        "symbol": "AAA",
                        "ts_ns": day["valuation_ts_ns"],
                        "bid": str(i + 2),
                        "ask": str(i + 2),
                    }
                ],
                "orders": [
                    {
                        "id": "buy",
                        "symbol": "AAA",
                        "quantity": "1",
                        "side": "buy",
                        "information_ts_ns": 1735689599999999999,
                    }
                ]
                if i == 0
                else [],
            }
            for i, day in enumerate(value["daily_ledger"])
        ],
    }
    bind_session_request(value, request)
    value["daily_positions"][-1].update(mark_price="999", market_value="999")
    value["daily_ledger"][-1].update(positions_value="999", nav="1006.99")
    value["final_nav"] = "1006.99"
    validate_session_result(value)
    with pytest.raises(ValueError, match="valuation mark"):
        bind_session_request(value, request)
