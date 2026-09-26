"""Run the documented CLI workflow using only temporary synthetic data."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from create_example import create_example


def smoke(executable: Path, root: Path) -> None:
    request = create_example(root)
    command = [
        str(executable.resolve()),
        "--database",
        str(root / "jobs.sqlite"),
        "--artifact-root",
        str(root / "artifacts"),
        "--result-root",
        str(root / "results"),
    ]

    def call(*args: str) -> dict:
        return json.loads(
            subprocess.check_output([*command, *args], text=True, timeout=15)
        )

    receipt = call("submit", str(request))
    deadline = time.monotonic() + 90
    while True:
        status = call("status", receipt["job_id"])
        if status["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        if time.monotonic() >= deadline:
            call("cancel", receipt["job_id"])
            raise TimeoutError("synthetic backtest did not finish in 90 seconds")
        time.sleep(0.2)
    if status["status"] != "SUCCEEDED":
        raise RuntimeError(f"synthetic backtest failed: {status}")
    result = call("result", receipt["job_id"])
    if result["schema_version"] != "quant.backtest_job_result.v2":
        raise ValueError("unexpected result schema")
    print(json.dumps({"status": status["status"], "job_id": result["job_id"]}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--executable", type=Path, default=Path(sys.executable).parent / "backtest-job"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="backtest-runtime-smoke-") as temp:
        smoke(args.executable, Path(temp))


if __name__ == "__main__":
    main()
