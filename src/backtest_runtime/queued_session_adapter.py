"""Private Go adapter for explicit v7 queued session jobs. No SQLite or publisher."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .partition_inputs import (
    PartitionLimits,
    PartitionManifest,
    VerifiedPartitionReader,
)
from .queued_sessions import stage, verify
from .session_stream import SessionStreamClient, _config
from .session_stream_io import StreamLimits, canonical, parse
from .session_stream_journal import digest, read_file


def inputs(
    mapping: dict[str, Any], artifact_root: Path
) -> tuple[dict, PartitionManifest]:
    values = {}
    for key, ref in mapping["inputs"].items():
        sha = ref.removeprefix("artifact://sha256/")
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ValueError("invalid artifact digest")
        data = read_file(artifact_root / "sha256" / sha, 1 << 20)
        if digest(data) != sha:
            raise ValueError("input artifact digest mismatch")
        values[key] = parse(data)
    manifest = PartitionManifest.from_dict(
        values["partition_manifest_ref"],
        limits=(PartitionLimits(max_total_rows=4096, max_partition_rows=4096)),
    )
    return _config(values["engine_config_ref"]), manifest


def preflight(mapping: dict, config: dict, executable: Path) -> dict:
    # Ready establishes transport/config decoding only. Financial semantics remain unchecked.
    with SessionStreamClient(
        executable,
        mapping["config"]["executable_sha256"],
        limits=StreamLimits(wall_seconds=10),
    ) as client:
        client.start(config)
    request_sha = digest(canonical(mapping))
    result = {
        "schema": "quant.backtest_plan.v1",
        "request": mapping,
        "request_sha256": request_sha,
        "backend": mapping["backend"],
        "inputs": {
            k: v.removeprefix("artifact://sha256/")
            for k, v in mapping["inputs"].items()
        },
        "budgets": mapping["budgets"],
        "executable_sha256": mapping["config"]["executable_sha256"],
        "effects": ["job_insert", "worker_execution", "result_publication"],
        "checks": {
            "request_structure": "passed",
            "input_hashes": "passed",
            "manifest_metadata": "passed",
            "stream_ready": "passed",
            "partition_bytes": "not_checked",
            "engine_request_semantics": "not_checked",
            "resource_enforcement": "not_checked",
            "economic_validity": "not_checked",
        },
    }
    result["plan_sha256"] = digest(canonical(result))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "stage", "verify"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    parser.add_argument("--partition-root", type=Path, required=True)
    parser.add_argument("--engine-executable", type=Path, required=True)
    parser.add_argument("--staging-path", type=Path)
    args = parser.parse_args()
    mapping = parse(read_file(args.request, 65536))
    if not args.partition_root.is_absolute() or not args.partition_root.is_dir():
        raise ValueError("absolute operator partition root required")
    config, manifest = inputs(mapping, args.artifact_root)
    if args.operation == "plan":
        result = preflight(mapping, config, args.engine_executable)
    else:
        if args.staging_path is None or not args.staging_path.is_absolute():
            raise ValueError("Go-owned stage required")
        with tempfile.TemporaryDirectory(prefix="backtest-session-spool-") as spool:
            reader = VerifiedPartitionReader(
                manifest, input_root=args.partition_root, task_root=Path(spool)
            )
            if args.operation == "stage":
                result = stage(
                    reader,
                    config,
                    executable=args.engine_executable,
                    executable_sha256=mapping["config"]["executable_sha256"],
                    output_root=args.staging_path,
                    wall_seconds=mapping["budgets"]["wall_seconds"],
                )
            else:
                result = verify(reader, config, output_root=args.staging_path)
            journal = parse(
                read_file(args.staging_path / "stream" / "journal.json", 8 << 20)
            )
            if (
                journal["binding"]["executable_sha256"]
                != mapping["config"]["executable_sha256"]
            ):
                raise ValueError("journal executable binding mismatch")
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
