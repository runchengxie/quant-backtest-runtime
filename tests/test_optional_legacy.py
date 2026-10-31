"""Legacy absence fails before storage; core paths never import its packages."""

import json

import pytest
from test_jobs import _request

from backtest_runtime import cli
from backtest_runtime.contracts import BacktestJobRequest


@pytest.mark.parametrize("command", ["plan", "submit"])
def test_missing_legacy_is_actionable_and_does_not_create_storage(
    tmp_path, capsys, monkeypatch, command
):
    import backtest_runtime.legacy as legacy

    monkeypatch.setattr(legacy, "find_spec", lambda name: None)
    manifest = tmp_path / "request.json"
    manifest.write_text(json.dumps(_request()))
    database = tmp_path / "state" / "jobs.sqlite"
    assert (
        cli.main(
            [
                "--error-format",
                "json",
                "--database",
                str(database),
                "--artifact-root",
                str(tmp_path / "artifacts"),
                "--result-root",
                str(tmp_path / "results"),
                command,
                str(manifest),
            ]
        )
        == 1
    )
    error = json.loads(capsys.readouterr().err)
    assert error["code"] == "LEGACY_DEPENDENCY_UNAVAILABLE"
    assert "quant-backtest-runtime[legacy]" in error["message"]
    assert not database.parent.exists()
    assert not (tmp_path / "results").exists()


def test_dependency_preflight_does_not_change_fingerprint(monkeypatch):
    import backtest_runtime.legacy as legacy

    before = BacktestJobRequest.from_mapping(_request()).request_sha256
    monkeypatch.setattr(legacy, "find_spec", lambda name: None)
    assert BacktestJobRequest.from_mapping(_request()).request_sha256 == before
    with pytest.raises(legacy.LegacyDependencyUnavailable):
        legacy.require_legacy()


@pytest.mark.parametrize(
    "version, expected",
    [
        (1, "65878a6a7e737d41107df495f14d551ff1c97d599d5cf9ffed3f8e3ca39acf66"),
        (2, "2449ab9c9fdd44d6eaeb9dd5c3263e77f276583b5b16f759c50c6bc00b67cb4e"),
        (3, "9838a81b02e6b64b60b108c617d6865df7e473367c04d084e25daa95f168eec3"),
        (4, "00b55a510d317980bee7758e72a0a4b91d3582124e01b7e8fa61898331d3594e"),
        (5, "56003d4870233ecdd11d2a1074ad53d614d6a0180bd6cefa080b5d64f5f32ed6"),
        (6, "e1e52eb8451cec3d993e417e44e335274687747d20cc0493c4402dcad711e32c"),
    ],
)
def test_all_wire_fingerprints_match_pre_migration_baseline(version, expected):
    # Captured using contracts.py at dd636a8 before dependency migration.
    from pathlib import Path

    if version == 1:
        mapping = _request()
    elif version < 5:
        source = Path(__file__).resolve().parents[1] / "examples"
        mapping = json.loads((source / f"request-v{version}.json").read_text())
    else:
        mapping = {
            "schema_version": version,
            "idempotency_key": "fingerprint-baseline",
            "backend": "nautilus.quote_market_orders"
            if version == 5
            else "nautilus.session_replay",
            "evidence_tier": "diagnostic",
            "inputs": {"engine_request_ref": "artifact://sha256/" + "a" * 64},
            "config": {"executable_sha256": "b" * 64},
            "execution": {},
            "budgets": {"wall_seconds": 30, "memory_mb": 4096},
        }
    assert BacktestJobRequest.from_mapping(mapping).request_sha256 == expected
