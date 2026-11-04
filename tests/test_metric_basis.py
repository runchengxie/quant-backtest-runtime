"""Legacy metric provenance stays explicit without replacing historical returns."""

import json
from pathlib import Path

import pandas as pd
import pytest
from portfolio_backtester.backends import to_json_compatible
from test_execution_aware import _request_v2

from backtest_runtime.agent_access import ArtifactReader
from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.go_adapter import stage
from backtest_runtime.worker import _run_native_backend


@pytest.mark.parametrize(("version", "ledger"), [(1, False), (1, True), (2, True)])
def test_published_legacy_metrics_identify_their_accounting_basis(
    tmp_path: Path, version: int, ledger: bool
) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    mapping["schema_version"] = version
    mapping["execution"]["ledger"] = ledger
    mapping["execution"]["ledger_config"]["enabled"] = ledger
    if version == 1:
        mapping["evidence_tier"] = "diagnostic"
        mapping.pop("research_clock")
        mapping.pop("producer")
    request = BacktestJobRequest.from_mapping(mapping)
    expected = _run_native_backend(request, ArtifactReader(artifacts))
    receipt = stage(
        mapping,
        job_id="metric-basis",
        artifact_root=artifacts,
        result_root=tmp_path / "results",
    )
    root = Path(receipt["staging_path"])
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["request_sha256"] == request.request_sha256
    assert manifest["metric_basis"] == {
        "schema_version": "quant.result_metric_basis.v1",
        "summary": "period_return_replay" if version == 1 else "not_published",
        "performance": "period_return_replay" if version == 1 else "not_published",
        "daily_ledger": "execution_sim" if ledger else "not_available",
    }
    # Ledger simulation must never replace the original period-return summary.
    if version == 1:
        assert manifest["metadata"]["accounting_mode"] == (
            "period_return_replay_with_ledger" if ledger else "period_return_replay"
        )
        assert manifest["summary"] == to_json_compatible(expected.summary)
        pd.testing.assert_frame_equal(
            pd.read_parquet(root / "performance.parquet"), expected.performance
        )
    else:
        assert "summary" not in manifest
        assert "performance" not in manifest


def test_requested_ledger_does_not_imply_backend_produced_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backtest_runtime import go_adapter

    artifacts = tmp_path / "artifacts"
    mapping = _request_v2(artifacts)
    mapping.update(schema_version=1, evidence_tier="diagnostic")
    mapping.pop("research_clock")
    mapping.pop("producer")
    # A real diagnostic result models the backend's empty-ledger fallback.
    without_ledger = json.loads(json.dumps(mapping))
    without_ledger["execution"]["ledger"] = False
    without_ledger["execution"]["ledger_config"]["enabled"] = False
    result = _run_native_backend(
        BacktestJobRequest.from_mapping(without_ledger), ArtifactReader(artifacts)
    )
    monkeypatch.setattr(go_adapter, "_run_native_backend", lambda *a, **k: result)
    receipt = stage(
        mapping,
        job_id="unavailable-ledger",
        artifact_root=artifacts,
        result_root=tmp_path / "results",
    )
    manifest = json.loads((Path(receipt["staging_path"]) / "manifest.json").read_text())
    assert mapping["execution"]["ledger"] is True
    assert manifest["metric_basis"]["daily_ledger"] == "not_available"
