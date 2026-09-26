"""Create a reproducible synthetic v2 request and hash-addressed Parquet inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from importlib.metadata import version
from pathlib import Path

import pandas as pd


def create_example(root: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    marker = source / ".release-ready"
    commit = (
        marker.read_text().strip()
        if marker.is_file()
        else subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
    )
    request = json.loads((source / "examples/request-v2.json").read_text())
    request["producer"]["commit"] = commit
    request["producer"]["version"] = version("quant-backtest-runtime")
    frames = {
        "positions_ref": pd.DataFrame(
            {
                "rebalance_date": ["20260102"],
                "entry_date": ["20260105"],
                "symbol": ["AAA"],
                "weight": [1.0],
                "side": ["long"],
            }
        ),
        "pricing_ref": pd.DataFrame(
            {
                "trade_date": ["20260105", "20260106"],
                "symbol": ["AAA", "AAA"],
                "close": [100.0, 105.0],
                "amount": [10_000_000.0, 10_000_000.0],
            }
        ),
        "periods_ref": pd.DataFrame(
            {
                "rebalance_date": ["20260102"],
                "entry_date": ["20260105"],
                "exit_date": ["20260106"],
            }
        ),
    }
    root.mkdir(parents=True, exist_ok=True)
    request_path = root / "request.json"
    if request_path.exists():
        raise FileExistsError(request_path)
    artifacts = root / "artifacts" / "sha256"
    artifacts.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        data = frame.to_parquet(index=False)
        digest = hashlib.sha256(data).hexdigest()
        with (artifacts / digest).open("xb") as output:
            output.write(data)
        request["inputs"][name] = f"artifact://sha256/{digest}"
    request_path.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    return request_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(create_example(args.output.expanduser().resolve()))


if __name__ == "__main__":
    main()
