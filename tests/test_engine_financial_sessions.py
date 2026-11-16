"""Fixed Engine provider adapter contract; no task database or publication."""

import argparse
import json
from pathlib import Path

import pytest
from test_rust_engine_jobs import _put

from backtest_runtime.engine_financial_adapter import preflight, step
from backtest_runtime.engine_financial_protocol import PROVIDER


def mapping(root, key="engine"):
    root.mkdir(parents=True, exist_ok=True)
    calendar = Path(__file__).parent / "fixtures/engine-financial/minute-calendar.json"
    (root / calendar.name).write_bytes(calendar.read_bytes())
    raw = (
        Path(__file__).parent / "fixtures/engine-financial/manifest.json"
    ).read_bytes()
    return dict(
        schema_version=9,
        idempotency_key=key,
        backend="engine.minute_session_resume",
        evidence_tier="diagnostic",
        inputs=dict(product_manifest_ref=_put(root, raw)),
        config=dict(
            provider_revision=PROVIDER,
            checkpoint_format="engine-financial-checkpoint.v1",
            policy="target-weights.v1",
            provider_binding_schema="runtime-engine-binding.v1",
        ),
        execution={},
        budgets=dict(wall_seconds=120, memory_mb=2048),
    )


def test_staging_adapter_preserves_exact_owner_checkpoint(tmp_path):
    pytest.importorskip("quant_backtest_core")
    root, stage = tmp_path / "artifacts", tmp_path / "stage"
    stage.mkdir()
    request = mapping(root)
    plan = preflight(request, root)
    args = argparse.Namespace(
        artifact_root=root,
        staging_path=stage,
        runtime_source=None,
        runtime_binary=None,
        binding=plan["financial_binding"],
        checkpoint=None,
        job_id="test",
        token="token",
    )
    receipt = step(request, args)
    assert receipt["cursor"] == 1
    wrapper = json.loads((stage / "candidate.json").read_bytes())
    assert wrapper["schema"] == "runtime-engine-checkpoint.v1"
    assert wrapper["attempt"] == "token"
    assert not (tmp_path / "jobs.sqlite").exists()
    assert not (stage / "manifest.json").exists()


def test_fixed_provider_adapter_exists():
    from backtest_runtime import engine_financial_adapter

    assert callable(engine_financial_adapter.prepare)


def test_manifest_calendar_and_tape_mutations_fail_closed(tmp_path):
    pytest.importorskip("quant_backtest_core")
    from backtest_runtime.engine_financial_adapter import prepare

    request = mapping(tmp_path)
    original = prepare(request, tmp_path)["binding"]
    path = tmp_path / "minute-calendar.json"
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest mismatch"):
        prepare(request, tmp_path)
    request = mapping(tmp_path)
    request["config"]["policy"] = "changed"
    assert prepare(request, tmp_path)["binding"] != original


def test_cross_provider_checkpoint_rejected():
    from backtest_runtime.engine_financial_protocol import decode_checkpoint

    with pytest.raises(ValueError, match="binding/schema"):
        decode_checkpoint(
            {"schema": "runtime-minute-checkpoint.v1", "binding": "same"}, "same"
        )


def test_poisoned_owner_error_never_requests_another_checkpoint(tmp_path, monkeypatch):
    from backtest_runtime import engine_financial_adapter as adapter

    original = ValueError("original owner failure")

    class Poisoned:
        def step(self):
            raise original

        def checkpoint(self):
            pytest.fail("checkpoint requested from poisoned owner")

    monkeypatch.setattr(
        adapter, "prepare", lambda *a: {"binding": "bound", "manifest": None}
    )
    monkeypatch.setattr(adapter, "create", lambda *a: Poisoned())
    args = argparse.Namespace(
        artifact_root=tmp_path,
        staging_path=tmp_path,
        runtime_source=None,
        runtime_binary=None,
        binding="bound",
        checkpoint=None,
    )
    with pytest.raises(ValueError) as failure:
        adapter.step({}, args)
    assert failure.value is original
    assert not list(tmp_path.iterdir())


def test_source_mutation_changes_current_binding(tmp_path, monkeypatch):
    pytest.importorskip("quant_backtest_core")
    from backtest_runtime import engine_financial_protocol as protocol

    source = tmp_path / "source.py"
    source.write_text("original = True\n")
    monkeypatch.setattr(protocol, "__file__", str(source))
    first = protocol.installed_identity("fixed")
    source.write_text("original = False\n")
    second = protocol.installed_identity("fixed")
    assert first["runtime"] != second["runtime"]
    assert first["distributions"] == second["distributions"]


def test_bytecode_only_provider_modules_rejected(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from backtest_runtime import engine_financial_protocol as protocol

    source = tmp_path / "__init__.py"
    source.write_text("")
    (tmp_path / "hidden.pyc").write_bytes(b"bytecode without auditable source")
    spec = SimpleNamespace(
        origin=str(source), submodule_search_locations=[str(tmp_path)]
    )
    monkeypatch.setattr(protocol, "find_spec", lambda _name: spec)
    with pytest.raises(ValueError, match="bytecode-only"):
        protocol.require_source_modules()


@pytest.mark.parametrize("mutation", ["financial_config", "capital_tape"])
def test_changed_product_financial_config_and_tape_get_new_binding(tmp_path, mutation):
    pytest.importorskip("quant_backtest_core")
    from backtest_runtime.engine_financial_adapter import prepare

    request = mapping(tmp_path)
    original = prepare(request, tmp_path)["binding"]
    path = (
        tmp_path / "sha256" / request["inputs"]["product_manifest_ref"].split("/")[-1]
    )
    manifest = json.loads(path.read_bytes())
    if mutation == "financial_config":
        manifest["settings"]["config"]["buy_fee_bp"] = "9"
    else:
        manifest["tapes"]["capital_flows"][0]["amount"] = "101"
    request["inputs"]["product_manifest_ref"] = _put(
        tmp_path, json.dumps(manifest).encode()
    )
    assert prepare(request, tmp_path)["binding"] != original
