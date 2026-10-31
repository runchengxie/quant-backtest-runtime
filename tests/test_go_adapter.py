"""The optional Go compatibility adapter never manages job state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backtest_runtime.go_adapter import normalize, stage


def test_normalize_preserves_python_request_fingerprint() -> None:
    request = {
        "schema_version": 4,
        "idempotency_key": "go-unicode-case",
        "backend": "native.trade_accounting",
        "evidence_tier": "diagnostic",
        "inputs": {"accounting_ref": "artifact://sha256/" + "a" * 64},
        "config": {
            "commission_rate": 0.0003,
            "stamp_tax_rate": 0.001,
            "slippage_rate": 0.0,
        },
        "execution": {},
        "budgets": {"wall_seconds": 30, "memory_mb": 4096},
    }
    receipt = normalize(request)
    data = receipt["request_json"].encode()
    assert hashlib.sha256(data).hexdigest() == receipt["request_sha256"]
    assert json.loads(data)["config"]["commission_rate"] == 0.0003


def test_stage_cannot_publish_or_open_registry(tmp_path: Path, monkeypatch) -> None:
    import pandas as pd

    from backtest_runtime import store

    def forbidden(*args, **kwargs):
        raise AssertionError("stateless adapter touched state or published output")

    monkeypatch.setattr(store.JobStore, "__init__", forbidden)
    monkeypatch.setattr(Path, "replace", forbidden)
    inputs = tmp_path / "artifacts" / "sha256"
    inputs.mkdir(parents=True)
    source = tmp_path / "source.parquet"
    pd.DataFrame(
        {
            "symbol": ["A"],
            "previous_weight": [0.5],
            "target_weight": [0.5],
            "previous_price": [10.0],
            "current_price": [20.0],
            "tradable": [True],
        }
    ).to_parquet(source)
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    (inputs / digest).write_bytes(data)
    request = {
        "schema_version": 4,
        "idempotency_key": "go-stage",
        "backend": "native.trade_accounting",
        "evidence_tier": "diagnostic",
        "inputs": {"accounting_ref": f"artifact://sha256/{digest}"},
        "config": {
            "commission_rate": 0.0003,
            "stamp_tax_rate": 0.001,
            "slippage_rate": 0.0,
        },
        "execution": {},
        "budgets": {"wall_seconds": 30, "memory_mb": 4096},
    }
    receipt = stage(
        request,
        job_id="00000000-0000-4000-8000-000000000001",
        artifact_root=inputs.parent,
        result_root=tmp_path / "results",
    )
    directory = Path(receipt["staging_path"])
    assert directory.parent == tmp_path / "results"
    assert directory.name.startswith(".00000000-0000-4000-8000-000000000001.tmp-")
    manifest = directory / "manifest.json"
    assert (
        hashlib.sha256(manifest.read_bytes()).hexdigest() == receipt["manifest_sha256"]
    )
    assert not (tmp_path / "results" / "00000000-0000-4000-8000-000000000001").exists()
    assert not list(tmp_path.rglob("*.sqlite"))
