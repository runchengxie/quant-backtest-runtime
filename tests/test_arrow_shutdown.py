"""One-shot legacy computation must finish normal interpreter shutdown."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from test_execution_aware import _request_v2
from test_sequenced_jobs import _request_v3
from test_trade_accounting_jobs import _request_v4


@pytest.mark.parametrize("version", [2, 3, 4])
def test_synchronous_parquet_compute_and_verify_exit(
    tmp_path: Path, version: int
) -> None:
    artifacts = tmp_path / "artifacts"
    mapping = {2: _request_v2, 3: _request_v3, 4: _request_v4}[version](artifacts)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(mapping), encoding="utf-8")
    script = """
import json, sys
from pathlib import Path
from backtest_runtime.contracts import BacktestJobRequest
from backtest_runtime.go_adapter import ArtifactReader
from backtest_runtime.worker import _run_native_backend, _write_result
from backtest_runtime.results import read_job_result
request = BacktestJobRequest.from_mapping(json.loads(Path(sys.argv[1]).read_text()))
reader = ArtifactReader(Path(sys.argv[2]))
result = _run_native_backend(request, reader, synchronous_parquet=True)
root = Path(sys.argv[3])
_, digest = _write_result(result, job_id='shutdown-test', request=request,
                          service=reader, result_root=root)
manifest = read_job_result(root, job_id='shutdown-test', manifest_sha256=digest,
                          request_sha256=request.request_sha256,
                          synchronous_parquet=True)
print(json.dumps(manifest), flush=True)
"""
    # Separate fresh interpreters catch native destructors that run only at exit.
    for attempt in range(3):
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(request),
                str(artifacts),
                str(tmp_path / f"results-{attempt}"),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        manifest = json.loads(completed.stdout)
        assert manifest["job_id"] == "shutdown-test"
        assert manifest["request_sha256"]
        if version == 4:
            assert manifest["summary"]["turnover"] == pytest.approx(1 / 6)
