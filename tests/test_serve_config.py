"""Service launch uses the same operator configuration as research clients."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

load = runpy.run_path(str(Path(__file__).parents[1] / "scripts/serve_config.py"))


def test_service_requires_queue_and_pins_adapter_and_engine(tmp_path: Path) -> None:
    values = {
        "runtime": {
            "executable": "/releases/go/bin/backtest-job",
            "database": "/data/jobs.sqlite",
            "artifact_root": "/data/artifacts",
            "result_root": "/data/results",
            "python_executable": "/releases/adapter/bin/python",
            "engine_executable": "/releases/engine/engine",
            "queue": True,
        },
        "scheduler": {"concurrency": 2, "cgroup_root": "auto"},
    }
    command = load["command"](values)
    assert command[0] == values["runtime"]["executable"]
    assert command[-5:] == ["serve", "--concurrency", "2", "--cgroup-root", "auto"]
    assert (
        command[command.index("--python-executable") + 1]
        == values["runtime"]["python_executable"]
    )
    assert (
        command[command.index("--engine-executable") + 1]
        == values["runtime"]["engine_executable"]
    )
    values["runtime"]["queue"] = False
    with pytest.raises(ValueError, match="queue"):
        load["command"](values)


def test_service_rejects_relative_paths() -> None:
    with pytest.raises(ValueError, match="absolute"):
        load["command"]({"runtime": {"queue": True, "executable": "relative"}})
