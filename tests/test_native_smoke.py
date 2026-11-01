"""Native release checks refuse shared paths and unverified runtime builds."""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from test_rust_engine_jobs import _request

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_native.py"


def test_native_smoke_rejects_reused_directory(tmp_path: Path) -> None:
    prepare = runpy.run_path(str(SCRIPT))["prepare_root"]
    marker = tmp_path / "keep"
    marker.write_text("existing input")
    with pytest.raises(FileExistsError):
        prepare(tmp_path)
    assert marker.read_text() == "existing input"


def test_native_smoke_requires_exact_go_source_revision() -> None:
    check = runpy.run_path(str(SCRIPT))["check_version"]
    expected = "a" * 40
    with pytest.raises(ValueError, match="revision"):
        check({"source_revision": "b" * 40}, expected)
    with pytest.raises(ValueError, match="Go"):
        check({"source_revision": expected}, expected)


def test_native_smoke_real_queue_and_receipt(tmp_path: Path) -> None:
    cli = os.environ.get("BACKTEST_GO_EXECUTABLE")
    engine = os.environ.get("BACKTEST_ENGINE_TEST_EXECUTABLE")
    if not cli or not engine:
        pytest.skip("requires separately built Go CLI and Rust engine")
    version = json.loads(subprocess.check_output([cli, "version"], text=True))
    expected = os.environ.get(
        "BACKTEST_EXPECTED_RUNTIME_REVISION", version["source_revision"]
    )
    mapping = _request(tmp_path / "input", Path(engine))
    digest = mapping["inputs"]["engine_request_ref"].rsplit("/", 1)[1]
    engine_input = tmp_path / "input/sha256" / digest
    root = tmp_path / "isolated-check"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--executable",
            cli,
            "--engine-executable",
            engine,
            "--engine-request",
            str(engine_input),
            "--expected-runtime-revision",
            expected,
            "--root",
            str(root),
        ],
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "SUCCEEDED"
    assert receipt["cancel_status"] == "CANCELLED"
    assert receipt["runtime"]["source_revision"] == expected
    assert receipt == json.loads((root / "receipt.json").read_text())
