"""Release pointer recovery without touching a production directory."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

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


def test_go_release_can_be_selected_and_rolled_back(tmp_path: Path) -> None:
    commit = "b" * 40
    release = tmp_path / "releases" / commit
    executable = release / "bin/backtest-job"
    executable.parent.mkdir(parents=True)
    executable.write_text("stub", encoding="ascii")
    (release / ".runtime-implementation").write_text("go\n", encoding="ascii")
    (release / ".release-ready").write_text(commit + "\n", encoding="ascii")
    switch(tmp_path, commit, dry_run=False)
    assert (tmp_path / "current").resolve() == release
    rollback(tmp_path, dry_run=False)
    assert not (tmp_path / "current").exists()


def test_rollback_restores_exact_generation_release(tmp_path: Path) -> None:
    production = tmp_path / "production"
    production.mkdir()
    old = tmp_path / "generations" / "generation-1" / "releases" / ("a" * 40)
    old_cli = old / ".venv/bin/backtest-job"
    old_cli.parent.mkdir(parents=True)
    old_cli.write_text("old", encoding="ascii")
    (old / ".release-ready").write_text(old.name + "\n", encoding="ascii")
    (production / "current").symlink_to(old)
    new = production / "releases" / ("b" * 40)
    new_cli = new / "bin/backtest-job"
    new_cli.parent.mkdir(parents=True)
    new_cli.write_text("new", encoding="ascii")
    (new / ".release-ready").write_text(new.name + "\n", encoding="ascii")
    (new / ".runtime-implementation").write_text("go\n", encoding="ascii")

    switch(production, new.name, dry_run=False)
    rollback(production, dry_run=True)
    assert (production / "current").resolve() == new
    rollback(production, dry_run=False)
    assert (production / "current").resolve() == old
    assert (production / "previous").resolve() == new


@pytest.mark.parametrize("missing", [".release-ready", ".venv/bin/backtest-job"])
def test_invalid_previous_release_preserves_both_pointers(
    tmp_path: Path, missing: str
) -> None:
    active = tmp_path / "active"
    active.mkdir()
    previous = tmp_path / "generation" / ("a" * 40)
    executable = previous / ".venv/bin/backtest-job"
    executable.parent.mkdir(parents=True)
    executable.write_text("old", encoding="ascii")
    (previous / ".release-ready").write_text(previous.name + "\n", encoding="ascii")
    (previous / missing).unlink()
    (tmp_path / "current").symlink_to(active)
    (tmp_path / "previous").symlink_to(previous)
    with pytest.raises(ValueError, match="missing or not installed"):
        rollback(tmp_path, dry_run=False)
    assert (tmp_path / "current").resolve() == active
    assert (tmp_path / "previous").resolve() == previous
