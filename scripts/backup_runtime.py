"""Run the dependency-free backup utility from an immutable Go source release."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    runpy.run_module("backtest_runtime.backup", run_name="__main__")
