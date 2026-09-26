"""Stage and switch immutable runtime releases after their commit reaches main."""

from __future__ import annotations

import argparse
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _replace_link(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    temporary.symlink_to(target)
    temporary.replace(link)


def stage(repo: Path, production: Path, commit: str, *, dry_run: bool) -> Path:
    sha = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")
    ancestor = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", sha, "origin/main"],
        check=False,
    )
    if ancestor.returncode != 0:
        raise ValueError("commit must be reachable from origin/main")
    destination = production / "releases" / sha
    if dry_run:
        print(f"would stage {sha} at {destination}")
        return destination
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="runtime-release-", dir=destination.parent
    ) as temp:
        temporary = Path(temp)
        archive = temporary / "source.tar"
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(repo), "archive", "--format=tar", sha],
                stdout=output,
                check=True,
            )
        with tarfile.open(archive) as contents:
            contents.extractall(temporary / "source", filter="data")
        source = temporary / "source"
        source.rename(destination)
    # Venv entrypoints embed the absolute interpreter path, so install only
    # after the source has reached its immutable release location.
    subprocess.run(["uv", "sync", "--locked", "--no-dev"], cwd=destination, check=True)
    (destination / ".release-ready").write_text(sha + "\n", encoding="ascii")
    return destination


def switch(production: Path, commit: str, *, dry_run: bool) -> None:
    release = production / "releases" / commit
    if (
        not release.is_dir()
        or not (release / ".venv/bin/backtest-job").is_file()
        or not (release / ".release-ready").is_file()
        or (release / ".release-ready").read_text(encoding="ascii").strip() != commit
    ):
        raise ValueError("release is missing or not installed")
    current = production / "current"
    previous = production / "previous"
    old = current.resolve() if current.is_symlink() else None
    if dry_run:
        print(f"would switch {current} from {old} to {release}")
        return
    if old is not None:
        _replace_link(previous, old)
    _replace_link(current, release)


def rollback(production: Path, *, dry_run: bool) -> None:
    """Return to the previous release, or deactivate a first installation."""
    current = production / "current"
    if not current.is_symlink():
        raise ValueError("no active release to roll back")
    previous = production / "previous"
    if previous.is_symlink():
        switch(production, previous.resolve().name, dry_run=dry_run)
    elif dry_run:
        print(f"would remove initial release pointer {current}")
    else:
        current.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest runtime release management")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--production", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stage").add_argument("commit")
    commands.add_parser("switch").add_argument("commit")
    commands.add_parser("rollback")
    args = parser.parse_args()
    repo = args.repo.resolve()
    production = args.production.resolve()
    if args.command == "stage":
        stage(repo, production, args.commit, dry_run=args.dry_run)
    elif args.command == "switch":
        switch(production, args.commit, dry_run=args.dry_run)
    else:
        rollback(production, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
