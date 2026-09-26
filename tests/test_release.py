"""Release pointer recovery without touching a production directory."""

from __future__ import annotations

import runpy
from pathlib import Path

_release = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/release.py")
)
rollback = _release["rollback"]
switch = _release["switch"]


def test_first_release_can_roll_back_to_no_active_runtime(tmp_path: Path) -> None:
    commit = "a" * 40
    release = tmp_path / "releases" / commit
    executable = release / ".venv/bin/backtest-job"
    executable.parent.mkdir(parents=True)
    executable.write_text("stub", encoding="ascii")
    (release / ".release-ready").write_text(commit + "\n", encoding="ascii")

    switch(tmp_path, commit, dry_run=False)
    assert (tmp_path / "current").resolve() == release
    rollback(tmp_path, dry_run=True)
    assert (tmp_path / "current").is_symlink()
    rollback(tmp_path, dry_run=False)
    assert not (tmp_path / "current").exists()
