"""Verify an installed dependency-free wheel against a real v6 engine binary."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.jobs import BacktestJobService
from backtest_runtime.results import read_job_result
from backtest_runtime.store import JobStore


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "backtest_runtime.cli", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def smoke(binary: Path, root: Path) -> None:
    for name in ("portfolio_backtester", "pandas", "research_contracts", "pyarrow"):
        assert importlib.util.find_spec(name) is None, (
            f"unexpected optional package: {name}"
        )
    assert cli("capabilities").returncode == 0
    source = Path(__file__).resolve().parents[1] / "examples"
    data = (source / "engine-request-v2.json").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    artifacts = root / "artifacts"
    artifact = artifacts / "sha256" / digest
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(data)
    mapping = {
        "schema_version": 6,
        "idempotency_key": "minimal-wheel-v6",
        "backend": "nautilus.session_replay",
        "evidence_tier": "diagnostic",
        "inputs": {"engine_request_ref": f"artifact://sha256/{digest}"},
        "config": {
            "executable_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
        },
        "execution": {},
        "budgets": {"wall_seconds": 30, "memory_mb": 4096},
    }
    request_file = root / "request.json"
    request_file.write_text(json.dumps(mapping))
    database = root / "state" / "jobs.sqlite"
    results = root / "results"
    options = [
        "--database",
        str(database),
        "--artifact-root",
        str(artifacts),
        "--result-root",
        str(results),
        "--engine-executable",
        str(binary),
    ]
    planned = cli(*options, "plan", str(request_file))
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["checks"]["engine_request_semantics"] == "not_checked"
    assert not database.parent.exists() and not results.exists()
    for command in ("plan", "submit"):
        failure = cli(
            "--error-format", "json", *options, command, str(source / "request-v2.json")
        )
        assert failure.returncode == 1, failure.stdout
        error = json.loads(failure.stderr)
        assert error["code"] == "LEGACY_DEPENDENCY_UNAVAILABLE", error
        assert "quant-backtest-runtime[legacy]" in error["message"]
        assert not database.parent.exists() and not results.exists()
    request = BacktestJobRequest.from_mapping(mapping)
    store = JobStore(database)
    try:
        receipt = BacktestJobService(
            store, artifact_root=artifacts, result_root=results
        ).submit(request)
    finally:
        store.close()
    worker = subprocess.run(
        [
            sys.executable,
            "-m",
            "backtest_runtime.worker",
            "--job-id",
            receipt.job_id,
            "--registry",
            str(database),
            "--artifact-root",
            str(artifacts),
            "--result-root",
            str(results),
            "--engine-executable",
            str(binary),
        ],
        capture_output=True,
        text=True,
        timeout=40,
        check=False,
    )
    assert worker.returncode == 0, worker.stderr
    store = JobStore(database)
    try:
        row = store.get_backtest_job(receipt.job_id)
        assert row is not None
        assert row["status"] == "SUCCEEDED", row
        result = read_job_result(
            results,
            job_id=receipt.job_id,
            manifest_sha256=row["result_sha256"],
            request_sha256=request.request_sha256,
        )
        assert result["schema_version"] == "quant.engine_job_result.v2"
        assert result["summary"]["equity"] == "10099"
    finally:
        store.close()
    assert not any(name in sys.modules for name in ("pandas", "portfolio_backtester"))
    print(
        json.dumps(
            {
                "status": "SUCCEEDED",
                "schema_version": 6,
                "request_sha256": request.request_sha256,
                "engine_sha256": mapping["config"]["executable_sha256"],
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-executable", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="runtime-core-smoke-") as temporary:
        smoke(args.engine_executable.resolve(), Path(temporary))


if __name__ == "__main__":
    main()
