"""Synthetic requests can be built from Git archive production releases."""

from __future__ import annotations

import json
import runpy
from pathlib import Path


def test_example_uses_archive_release_commit_without_git(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    commit = "a" * 40
    release = tmp_path / "releases" / commit
    (release / "scripts").mkdir(parents=True)
    (release / "examples").mkdir()
    (release / "scripts/create_example.py").write_bytes(
        (source / "scripts/create_example.py").read_bytes()
    )
    (release / "examples/request-v2.json").write_bytes(
        (source / "examples/request-v2.json").read_bytes()
    )

    create_example = runpy.run_path(str(release / "scripts/create_example.py"))[
        "create_example"
    ]
    request_path = create_example(tmp_path / "input")

    request = json.loads(request_path.read_text(encoding="utf-8"))
    assert request["producer"]["commit"] == commit
    assert all(
        value.startswith("artifact://sha256/") for value in request["inputs"].values()
    )
