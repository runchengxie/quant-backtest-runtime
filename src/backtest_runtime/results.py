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
        from portfolio_backtester.backtest_bundle_io import read_backtest_bundle

        if payload.get("bundle_manifest_path") != "bundle/manifest.json":
            raise ValueError("invalid official bundle path")
        bundle = root / "bundle"
        if bundle.is_symlink() or (bundle / "manifest.json").is_symlink():
            raise ValueError("official bundle path is unsafe")
        if _sha256(bundle / "manifest.json") != payload.get("bundle_manifest_sha256"):
            raise ValueError("official bundle manifest SHA-256 mismatch")
        official = read_backtest_bundle(bundle, verify_hashes=True)
        if (
            official.run_id != job_id
            or official.evidence_tier.value != "execution_aware"
        ):
            raise ValueError("official bundle identity mismatch")
        if official.artifact_envelope.get("configuration_sha256") != request_sha256:
            raise ValueError("official bundle request fingerprint mismatch")
    elif schema == "ticknet.backtest_job_result.v1":
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
    else:
        raise ValueError("unsupported Job result schema")
    return payload
