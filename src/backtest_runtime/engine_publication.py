"""Durable inventory content for independent-engine final snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .contracts import BacktestJobRequest
from .engine_protocol import engine_summary, validate_engine_result
from .engine_sessions_protocol import session_summary, validate_session_result


def write_engine_result(
    root: Path, result: Any, *, job_id: str, request: BacktestJobRequest
) -> dict[str, Any]:
    session = request.schema_version == 6
    result = (
        validate_session_result(result) if session else validate_engine_result(result)
    )
    path = root / "engine_result.json"
    path.write_text(
        json.dumps(result, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    return {
        "schema_version": "quant.engine_job_result.v2"
        if session
        else "quant.engine_job_result.v1",
        "job_id": job_id,
        "request_sha256": request.request_sha256,
        "backend": request.backend,
        "executable_sha256": request.config["executable_sha256"],
        "summary": session_summary(result) if session else engine_summary(result),
        "inventory": [
            {
                "path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size_bytes": path.stat().st_size,
            }
        ],
    }


def verify_engine_result(root: Path, manifest: dict[str, Any]) -> None:
    session = manifest.get("schema_version") == "quant.engine_job_result.v2"
    expected_backend = (
        "nautilus.session_replay" if session else "nautilus.quote_market_orders"
    )
    if manifest.get("backend") != expected_backend:
        raise ValueError("invalid independent engine backend")
    digest = manifest.get("executable_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid engine executable digest")
    inventory = manifest.get("inventory")
    if (
        not isinstance(inventory, list)
        or len(inventory) != 1
        or not isinstance(inventory[0], dict)
        or inventory[0].get("path") != "engine_result.json"
    ):
        raise ValueError("invalid engine result inventory")
    item = inventory[0]
    path = root / "engine_result.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("engine result is missing or unsafe")
    if path.stat().st_size != item.get("size_bytes") or hashlib.sha256(
        path.read_bytes()
    ).hexdigest() != item.get("sha256"):
        raise ValueError("engine result checksum mismatch")
    validator = validate_session_result if session else validate_engine_result
    result = validator(json.loads(path.read_text(encoding="utf-8")))
    if manifest.get("summary") != (
        session_summary(result) if session else engine_summary(result)
    ):
        raise ValueError("engine summary does not match verified result")
