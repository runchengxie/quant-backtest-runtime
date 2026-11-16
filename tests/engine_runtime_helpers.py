"""Engine-only process harness with no legacy provider import."""

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class Runtime:
    executable: Path
    root: Path
    engine: Path | None = None
    python: str | None = sys.executable

    @property
    def database(self) -> Path:
        return self.root / "jobs.sqlite"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def results(self) -> Path:
        return self.root / "results"

    def args(self, *args: str) -> list[str]:
        command = [
            str(self.executable),
            "--database",
            str(self.database),
            "--artifact-root",
            str(self.artifacts),
            "--result-root",
            str(self.results),
            "--error-format",
            "json",
        ]
        if self.python is not None:
            command.extend(["--python-executable", self.python])
        if self.engine is not None:
            command.extend(["--engine-executable", str(self.engine)])
        return [*command, *args]

    def call(self, *args: str, success: bool = True) -> dict:
        completed = subprocess.run(
            self.args(*args), capture_output=True, text=True, timeout=90, check=False
        )
        assert (completed.returncode == 0) is success, completed.stderr
        return json.loads(completed.stdout if success else completed.stderr)

    def write(self, mapping: dict, name: str = "request") -> Path:
        path = self.root / f"{name}.json"
        path.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        return path

    def wait(self, job_id: str, states: set[str] | None = None) -> dict:
        wanted = states or {"SUCCEEDED", "FAILED", "CANCELLED"}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = self.call("status", job_id)
            if status["status"] in wanted:
                return status
            time.sleep(0.05)
        pytest.fail(f"job {job_id} did not reach {wanted}: {status}")
