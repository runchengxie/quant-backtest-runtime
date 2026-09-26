"""Durable backtest job contract and lifecycle tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backtest_runtime.jobs import BacktestJobRequest, BacktestJobService
from backtest_runtime.store import JobStore, RegistryConflict


def _request(*, key: str = "daily-replay", positions_ref: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "idempotency_key": key,
        "inputs": {
            "positions_ref": positions_ref or f"artifact://sha256/{'a' * 64}",
            "pricing_ref": f"artifact://sha256/{'b' * 64}",
            "periods_ref": f"artifact://sha256/{'c' * 64}",
        },
        "backend": "native.position_replay",
        "evidence_tier": "diagnostic",
        "config": {
            "price_col": "close",
            "entry_price_col": None,
            "exit_price_col": None,
            "transaction_cost_bps": 5.0,
            "trading_days_per_year": 252,
            "long_only": True,
            "preserve_gross_exposure": False,
            "exit_price_policy": "period",
            "exit_fallback_policy": "none",
            "tradable_col": None,
        },
        "execution": {
            "ledger": False,
            "allow_stale_execution_price": False,
            "intraday_execution_assumption": None,
            "ledger_config": {
                "enabled": False,
                "portfolio_value": 1_000_000.0,
                "participation_rate": 0.05,
                "liquidity_cols": ["medadv20_amount", "amount"],
                "liquidity_notional_multiplier": 1.0,
                "buy_max_days": 5,
                "sell_max_days": 10,
                "zero_fill_abort_days_buy": 5,
                "unfilled_buy_action": "keep_cash",
                "unfilled_sell_action": "keep_position",
                "round_lot": None,
                "enforce_t1": False,
                "enforce_price_limits": False,
                "enforce_listing_status": False,
                "limit_up_col": None,
                "limit_down_col": None,
                "listing_status_col": None,
                "lot_tolerance": 1e-6,
            },
        },
        "budgets": {"wall_seconds": 60, "memory_mb": 1024},
    }


def _service(tmp_path):
    registry = JobStore(tmp_path / "registry.sqlite")
    return registry, BacktestJobService(
        registry,
        artifact_root=tmp_path / "artifacts",
        result_root=tmp_path / "results",
    )


def test_request_is_strict_and_fingerprint_binds_canonical_content() -> None:
    mapping = _request()
    request = BacktestJobRequest.from_mapping(mapping)
    reordered = BacktestJobRequest.from_mapping(dict(reversed(list(mapping.items()))))
    assert request.request_sha256 == reordered.request_sha256
    assert len(request.request_sha256) == 64

    with pytest.raises(ValueError, match="unknown"):
        BacktestJobRequest.from_mapping({**mapping, "prompt": "must not persist"})
    unsupported = deepcopy(mapping)
    unsupported["backend"] = "external.unknown"
    with pytest.raises(ValueError, match="backend"):
        BacktestJobRequest.from_mapping(unsupported)
    mapping["config"]["transaction_cost_bps"] = 999
    request.config["transaction_cost_bps"] = 123
    assert request.to_mapping()["config"]["transaction_cost_bps"] == 5.0


@pytest.mark.parametrize(
    "ref",
    [
        "file:///etc/passwd",
        f"artifact://sha256/{'A' * 64}",
        f"artifact://sha256/{'a' * 63}",
        f"artifact://user:secret@sha256/{'a' * 64}",
        f"artifact://sha256/{'a' * 64}?token=x",
        f"artifact://sha256/../{'a' * 64}",
    ],
)
def test_request_rejects_non_content_addressed_artifact_refs(ref: str) -> None:
    with pytest.raises(ValueError, match=r"artifact|credentials"):
        BacktestJobRequest.from_mapping(_request(positions_ref=ref))


def test_submit_is_idempotent_and_conflicts_on_reused_key_with_new_content(
    tmp_path,
) -> None:
    registry, service = _service(tmp_path)
    try:
        request = BacktestJobRequest.from_mapping(_request())
        first = service.submit(request)
        replay = service.submit(request)
        assert replay.job_id == first.job_id
        assert replay.created is False

        changed = _request()
        changed["config"]["transaction_cost_bps"] = 7.5
        with pytest.raises(RegistryConflict, match="idempotency"):
            service.submit(BacktestJobRequest.from_mapping(changed))
        assert service.get_status(first.job_id).status == "SUBMITTED"
    finally:
        registry.close()


def test_state_machine_uses_compare_and_set_and_rejects_terminal_rewrites(
    tmp_path,
) -> None:
    registry, service = _service(tmp_path)
    try:
        receipt = service.submit(BacktestJobRequest.from_mapping(_request()))
        with pytest.raises(ValueError, match="illegal backtest job transition"):
            registry.transition_backtest_job(
                receipt.job_id,
                expected="SUBMITTED",
                status="SUCCEEDED",
                result_ref=f"backtest-job://{receipt.job_id}",
            )
        running = service.claim(receipt.job_id, worker_pid=1234, lease_seconds=60)
        assert running.status == "RUNNING"
        assert service.claim(receipt.job_id, worker_pid=5678, lease_seconds=60) is None
        succeeded = service.complete(
            receipt.job_id,
            result_ref=f"backtest-job://{receipt.job_id}",
            result_sha256="e" * 64,
        )
        assert succeeded.status == "SUCCEEDED"
        result = service.get_result(receipt.job_id)
        assert result.uri == f"backtest-job://{receipt.job_id}"
        assert result.manifest_sha256 == "e" * 64
        with pytest.raises(RegistryConflict, match="terminal"):
            service.fail(receipt.job_id, code="FAILED", message="late writer")
    finally:
        registry.close()


def test_cancel_before_claim_is_terminal_and_running_cancel_is_a_request(
    tmp_path,
) -> None:
    registry, service = _service(tmp_path)
    try:
        queued = service.submit(BacktestJobRequest.from_mapping(_request(key="queued")))
        assert service.cancel(queued.job_id).status == "CANCELLED"
        assert service.claim(queued.job_id, worker_pid=1234, lease_seconds=60) is None

        running = service.submit(
            BacktestJobRequest.from_mapping(_request(key="running"))
        )
        service.claim(running.job_id, worker_pid=1234, lease_seconds=60)
        requested = service.cancel(running.job_id)
        assert requested.status == "RUNNING"
        assert requested.cancel_requested_at is not None
        assert service.acknowledge_cancel(running.job_id).status == "CANCELLED"
    finally:
        registry.close()


def test_success_cannot_win_after_running_cancel_request(tmp_path) -> None:
    registry, service = _service(tmp_path)
    try:
        receipt = service.submit(
            BacktestJobRequest.from_mapping(_request(key="cancel-race"))
        )
        service.claim(receipt.job_id, worker_pid=1234, lease_seconds=60)
        service.cancel(receipt.job_id)
        with pytest.raises(RegistryConflict):
            service.complete(
                receipt.job_id,
                result_ref=f"backtest-job://{receipt.job_id}",
                result_sha256="a" * 64,
            )
        assert service.acknowledge_cancel(receipt.job_id).status == "CANCELLED"
    finally:
        registry.close()


def test_unclaimed_worker_launch_expires_instead_of_staying_submitted(tmp_path) -> None:
    registry, service = _service(tmp_path)
    try:
        receipt = service.submit(
            BacktestJobRequest.from_mapping(_request(key="startup-exit"))
        )
        assert registry.set_backtest_job_logs(
            receipt.job_id, stdout_path="stdout.log", stderr_path="stderr.log"
        )
        assert service.recover_expired(now=registry.job_clock() + 120) == 1
        assert service.get_status(receipt.job_id).status == "FAILED"
        assert service.get_status(receipt.job_id).error_code == "WORKER_START_EXPIRED"
    finally:
        registry.close()
