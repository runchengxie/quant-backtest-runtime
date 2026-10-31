"""Stateless legacy financial operations for the Go lifecycle owner."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .agent_access import ArtifactReader, checked_plan
from .agent_errors import emit_error
from .contracts import BacktestJobRequest
from .legacy import require_request_dependencies
from .results import read_job_result
from .worker import _run_native_backend, _write_result


def normalize(mapping: dict[str, Any]) -> dict[str, str]:
    request = BacktestJobRequest.from_mapping(mapping)
    require_request_dependencies(request.schema_version, request.quant_run_manifest_ref)
    return {
        "request_json": request._request_json,
        "request_sha256": request.request_sha256,
    }


def stage(
    mapping: dict[str, Any],
    *,
    job_id: str,
    artifact_root: Path,
    result_root: Path,
) -> dict[str, str]:
    request = BacktestJobRequest.from_mapping(mapping)
    if request.schema_version not in {1, 2, 3, 4}:
        raise ValueError("the legacy adapter only computes v1-v4 jobs")
    reader = ArtifactReader(artifact_root)
    result = _run_native_backend(
        request,
        reader,
        synchronous_parquet=True,
    )
    path, digest = _write_result(
        result,
        job_id=job_id,
        request=request,
        service=reader,
        result_root=result_root,
        staging_only=True,
    )
    return {"staging_path": path, "manifest_sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("normalize", "plan", "stage", "verify"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--result-root", type=Path)
    parser.add_argument("--engine-executable", type=Path)
    parser.add_argument("--job-id")
    parser.add_argument("--manifest-sha256")
    parser.add_argument("--staging-path", type=Path)
    args = parser.parse_args()
    mapping = json.loads(args.request.read_text(encoding="utf-8"))
    if args.operation == "normalize":
        result = normalize(mapping)
    elif args.operation == "plan":
        result = checked_plan(
            BacktestJobRequest.from_mapping(mapping),
            args.artifact_root,
            args.engine_executable,
        )
    elif args.operation == "stage":
        result = stage(
            mapping,
            job_id=args.job_id,
            artifact_root=args.artifact_root,
            result_root=args.result_root,
        )
    else:
        request = BacktestJobRequest.from_mapping(mapping)
        result = read_job_result(
            args.result_root,
            job_id=args.job_id,
            manifest_sha256=args.manifest_sha256,
            request_sha256=request.request_sha256,
            result_directory=args.staging_path,
            synchronous_parquet=True,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(emit_error(error)) from error
