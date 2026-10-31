"""Side-effect-free preflight using the runtime's authoritative input validators."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

from .contracts import BacktestJobRequest
from .engine_validation import EngineValidationError, validate_request
from .jobs import BacktestJobService


class AccessError(ValueError):
    """A stable agent contract failure, without guessed retry semantics."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def load_request(path: Path) -> BacktestJobRequest:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
        raise ValueError("manifest must be a regular file no larger than 64 KiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AccessError("INVALID_REQUEST", "Request must be a JSON object")
    return BacktestJobRequest.from_mapping(value)


def build_plan(
    request: BacktestJobRequest, artifact_root: Path, engine: Path | None
) -> dict[str, Any]:
    if not sys.platform.startswith("linux"):
        raise AccessError("PLATFORM_UNAVAILABLE", "Local execution requires Linux")
    # Artifact checks do not need a store; this is deliberately not a JobStore.
    service = ArtifactReader(artifact_root)
    inputs = {}
    for name, ref in request.inputs.items():
        if ref is not None:
            if request.schema_version != 5:
                service.resolve_artifact(ref)
            inputs[name] = ref.rsplit("/", 1)[-1]
    if request.quant_run_manifest_ref is not None:
        service.validate_quant_run_manifest(request)
    validation = _engine_validation(request, service, inputs, engine)
    executable_sha256 = request.config["executable_sha256"] if validation else None
    payload = {
        "schema": "quant.backtest_plan.v1",
        "request": request.to_mapping(),
        "request_sha256": request.request_sha256,
        "backend": request.backend,
        "inputs": inputs,
        "budgets": request.budgets,
        "executable_sha256": executable_sha256,
        "effects": ["job_insert", "worker_execution", "result_publication"],
        "checks": {
            "request_structure": "passed",
            "input_hashes": "passed",
            "root_manifest": "passed"
            if request.quant_run_manifest_ref
            else "not_applicable",
            "executable_hash": "passed" if executable_sha256 else "not_applicable",
            "engine_request_semantics": "passed"
            if request.schema_version == 5
            else "not_applicable",
            "parquet_rows": "not_checked",
            "clock_table_consistency": "not_checked",
            "resource_enforcement": "not_checked",
            "historical_pit": "not_checked",
            "economic_validity": "not_checked",
            "promotion": "not_checked",
        },
    }
    if validation is not None:
        payload["engine_validation"] = validation
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return {**payload, "plan_sha256": hashlib.sha256(encoded).hexdigest()}


def _engine_validation(
    request: BacktestJobRequest,
    service: ArtifactReader,
    inputs: dict[str, str],
    engine: Path | None,
) -> dict[str, Any] | None:
    if request.schema_version != 5:
        return None
    if engine is None or not engine.is_absolute():
        raise AccessError(
            "INVALID_REQUEST", "v5 requires an absolute engine executable"
        )
    if not engine.is_file() or not os.access(engine, os.X_OK):
        raise AccessError("ENGINE_UNAVAILABLE", "Engine executable is unavailable")
    input_digest = inputs["engine_request_ref"]
    try:
        return validate_request(
            engine,
            request.config["executable_sha256"],
            service.artifact_root / "sha256" / input_digest,
            input_digest,
        )
    except EngineValidationError as error:
        raise AccessError(error.code, str(error)) from error


class ArtifactReader:
    """Reuse the service checks without opening or migrating runtime storage."""

    resolve_artifact = BacktestJobService.resolve_artifact
    validate_quant_run_manifest = BacktestJobService.validate_quant_run_manifest

    def __init__(self, root: Path) -> None:
        self.artifact_root = root.expanduser().resolve()


def checked_plan(
    request: BacktestJobRequest, root: Path, engine: Path | None
) -> dict[str, Any]:
    try:
        return build_plan(request, root, engine)
    except FileNotFoundError as error:
        raise AccessError(
            "INPUT_UNAVAILABLE", "An immutable input is unavailable"
        ) from error
    except AccessError:
        raise
    except ValueError as error:
        raise AccessError(
            "INTEGRITY_FAILURE", "Immutable input verification failed"
        ) from error
