"""Verify published Job manifests and all referenced backtest files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_job_result(
    result_root: Path, *, job_id: str, manifest_sha256: str, request_sha256: str
) -> dict[str, Any]:
    """Reject altered Job or official bundle contents before returning a result."""
    root = result_root / job_id
    manifest_path = root / "manifest.json"
    if root.is_symlink() or manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("result manifest is missing or unsafe")
    if _sha256(manifest_path) != manifest_sha256:
        raise ValueError("Job manifest SHA-256 mismatch")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("Job manifest must be an object")
    if (
        payload.get("job_id") != job_id
        or payload.get("request_sha256") != request_sha256
    ):
        raise ValueError("Job result identity mismatch")
    schema = payload.get("schema_version")
    if schema == "quant.backtest_job_result.v2":
        _verify_official_bundle(root, payload, job_id, request_sha256)
        _verify_quant_run_manifest(root, payload)
    elif schema == "quant.trade_accounting_result.v1":
        _verify_trade_accounting(root, payload)
    elif schema == "ticknet.backtest_job_result.v1":
        _verify_diagnostic_frames(root, payload)
    else:
        raise ValueError("unsupported Job result schema")
    return payload


def _verify_trade_accounting(root: Path, payload: dict[str, Any]) -> None:
    if payload.get("backend") != "native.trade_accounting":
        raise ValueError("invalid trade accounting backend")
    inventory = payload.get("inventory")
    if not isinstance(inventory, list) or len(inventory) != 1:
        raise ValueError("invalid trade accounting inventory")
    item = inventory[0]
    if not isinstance(item, dict) or item.get("path") != "accounting.parquet":
        raise ValueError("invalid trade accounting frame path")
    path = root / "accounting.parquet"
    if path.is_symlink() or not path.is_file():
        raise ValueError("trade accounting frame missing or unsafe")
    if _sha256(path) != item.get("sha256") or path.stat().st_size != item.get(
        "size_bytes"
    ):
        raise ValueError("trade accounting frame checksum mismatch")
    summary = payload.get("summary")
    if not isinstance(summary, dict) or set(summary) != {
        "turnover",
        "commission",
        "stamp_tax",
        "slippage",
        "total_cost",
    }:
        raise ValueError("invalid trade accounting summary")
    import pandas as pd

    frame = pd.read_parquet(path)
    if len(frame) != 1 or frame.iloc[0].to_dict() != summary:
        raise ValueError("trade accounting summary does not match verified frame")


def _verify_official_bundle(
    root: Path, payload: dict[str, Any], job_id: str, request_sha256: str
) -> None:
    from portfolio_backtester.backtest_bundle_io import read_backtest_bundle

    if payload.get("bundle_manifest_path") != "bundle/manifest.json":
        raise ValueError("invalid official bundle path")
    bundle = root / "bundle"
    if bundle.is_symlink() or (bundle / "manifest.json").is_symlink():
        raise ValueError("official bundle path is unsafe")
    if _sha256(bundle / "manifest.json") != payload.get("bundle_manifest_sha256"):
        raise ValueError("official bundle manifest SHA-256 mismatch")
    official = read_backtest_bundle(bundle, verify_hashes=True)
    if official.run_id != job_id or official.evidence_tier.value != "execution_aware":
        raise ValueError("official bundle identity mismatch")
    if official.artifact_envelope.get("configuration_sha256") != request_sha256:
        raise ValueError("official bundle request fingerprint mismatch")


def _verify_quant_run_manifest(root: Path, payload: dict[str, Any]) -> None:
    path_name = payload.get("quant_run_manifest_path")
    digest = payload.get("quant_run_manifest_sha256")
    if path_name is None and digest is None:
        return
    if path_name != "quant_run_manifest.json" or not isinstance(digest, str):
        raise ValueError("invalid quant run manifest reference")
    path = root / path_name
    if path.is_symlink() or not path.is_file() or _sha256(path) != digest:
        raise ValueError("quant run manifest checksum mismatch")
    from research_contracts import QuantRunManifest

    try:
        manifest = QuantRunManifest.from_mapping(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError("invalid quant run manifest") from error
    if manifest.run_id != payload.get("quant_run_manifest_run_id"):
        raise ValueError("quant run manifest identity mismatch")


def _verify_diagnostic_frames(root: Path, payload: dict[str, Any]) -> None:
    expected = {
        "performance.parquet",
        "positions.parquet",
        "orders.parquet",
        "fills.parquet",
        "daily_ledger.parquet",
    }
    inventory = payload.get("inventory")
    if not isinstance(inventory, list) or len(inventory) != 5:
        raise ValueError("invalid diagnostic inventory")
    paths = {item.get("path") for item in inventory if isinstance(item, dict)}
    if paths != expected:
        raise ValueError("invalid diagnostic frame set")
    for item in inventory:
        path = root / item["path"]
        if path.is_symlink() or not path.is_file():
            raise ValueError("diagnostic frame missing or unsafe")
        if _sha256(path) != item.get("sha256") or path.stat().st_size != item.get(
            "size_bytes"
        ):
            raise ValueError("diagnostic frame checksum mismatch")
