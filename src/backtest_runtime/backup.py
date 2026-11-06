"""Bounded idle snapshots for a queue-only native v5/v6 deployment.

This is deliberately narrower than the full offline backup procedure.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import tempfile
import time
import tomllib
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

from .contracts import BacktestJobRequest, artifact_digest
from .results import read_job_result


def _no_symlinks(path: Path) -> None:
    if ".." in path.parts:
        raise ValueError("parent traversal is unsupported")
    for ancestor in (*reversed(path.parents), path):
        if ancestor.is_symlink():
            raise ValueError(f"symlink is unsupported: {ancestor}")


def _safe(path: Path, *, directory: bool = False) -> os.stat_result:
    _no_symlinks(path)
    info = path.stat()
    valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not valid or (not directory and info.st_nlink != 1):
        raise ValueError(f"unsafe file type or hardlink: {path}")
    return info


def _paths(config: dict[str, Any]) -> tuple[dict[str, Path], dict[str, Any]]:
    runtime, backup = config["runtime"], config["backup"]
    if runtime.get("queue") is not True:
        raise ValueError(
            "requires runtime.queue=true and exclusively queued submissions"
        )
    paths = {}
    for name in ("database", "artifact_root", "result_root"):
        value = runtime[name]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"runtime.{name} must be absolute")
        paths[name] = Path(value)
        _safe(paths[name], directory=name != "database")
    root = Path(backup["root"])
    if not root.is_absolute():
        raise ValueError("backup.root must be absolute")
    _no_symlinks(root)
    paths["root"] = root
    values = list(paths.values())
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise ValueError("backup and source paths must be disjoint")
    for key, default, maximum in (
        ("max_seconds", 30, 30),
        ("max_age_seconds", 7200, 31536000),
    ):
        number = backup.get(key, default)
        if type(number) is not int or not 1 <= number <= maximum:
            raise ValueError(f"backup.{key} is out of range")
    return paths, backup


def _hash(path: Path) -> str:
    _safe(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy(source: Path, target: Path) -> None:
    before = _safe(source)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as reader, target.open("xb") as writer:
        opened = os.fstat(reader.fileno())
        if opened != before:
            raise ValueError("source changed before copy")
        shutil.copyfileobj(reader, writer, 1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
        after = os.fstat(reader.fileno())
    # Reading may change atime. Compare identity, size, mtime and ctime instead.
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    current = _safe(source)
    if any(
        getattr(before, key) != getattr(after, key)
        or getattr(after, key) != getattr(current, key)
        for key in fields
    ):
        raise ValueError("source changed during copy")


def _tree(source: Path, target: Path) -> None:
    before = _safe(source, directory=True)
    target.mkdir(mode=0o700)
    for child in sorted(source.iterdir()):
        if child.is_dir() and not child.is_symlink():
            _tree(child, target / child.name)
        else:
            _copy(child, target / child.name)
    after = _safe(source, directory=True)
    if (before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise ValueError("source directory changed during copy")


def _jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute("SELECT * FROM backtest_jobs ORDER BY job_id")
    ]
    for row in rows:
        if row["status"] == "RUNNING" or (
            row["status"] == "SUBMITTED"
            and (row["worker_pid"] is not None or row["lease_expires_at"] is not None)
        ):
            raise ValueError("active or ambiguously owned job; snapshot skipped")
        if not re.fullmatch(r"[0-9a-f-]{36}", row["job_id"]):
            raise ValueError("unsafe job ID")
        mapping = json.loads(row["request_json"])
        if mapping.get("schema_version") not in {5, 6}:
            raise ValueError("scheduled snapshots support only native v5/v6 jobs")
        request = BacktestJobRequest.from_mapping(mapping)
        if request.request_sha256 != row["request_sha256"]:
            raise ValueError("request fingerprint mismatch")
        if request.quant_run_manifest_ref is not None:
            raise ValueError("external run manifests unsupported")
    return rows


def _collect(paths: dict[str, Path], data: Path, rows: list[dict[str, Any]]) -> None:
    (data / "artifacts" / "sha256").mkdir(parents=True, mode=0o700)
    (data / "results").mkdir(mode=0o700)
    for row in rows:
        request = json.loads(row["request_json"])
        digest = artifact_digest(
            request["inputs"]["engine_request_ref"], label="engine input"
        )
        target = data / "artifacts" / "sha256" / digest
        if not target.exists():
            _copy(paths["artifact_root"] / "sha256" / digest, target)
            if _hash(target) != digest:
                raise ValueError("artifact digest mismatch")
        if row["status"] == "SUCCEEDED":
            if row["result_ref"] != f"backtest-job://{row['job_id']}":
                raise ValueError("result reference mismatch")
            _tree(
                paths["result_root"] / row["job_id"], data / "results" / row["job_id"]
            )
            read_job_result(
                data / "results",
                job_id=row["job_id"],
                manifest_sha256=row["result_sha256"],
                request_sha256=row["request_sha256"],
            )


def _sync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _deadline(seconds: int):
    def expired(_signum, _frame):
        raise TimeoutError("snapshot deadline exceeded")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _create(paths: dict[str, Path], backup: dict[str, Any], temporary: Path) -> None:
    data = temporary / "data"
    data.mkdir(mode=0o700)
    uri = paths["database"].as_uri()
    with closing(sqlite3.connect(uri + "?mode=rw", uri=True, timeout=0)) as guard:
        guard.execute("BEGIN IMMEDIATE")
        rows = _jobs(guard)
        with (
            closing(sqlite3.connect(uri + "?mode=ro", uri=True, timeout=0)) as source,
            closing(sqlite3.connect(data / "jobs.sqlite")) as target,
        ):
            source.backup(target, pages=128, sleep=0, progress=lambda *_: None)
            if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ValueError("SQLite integrity check failed")
        _collect(paths, data, rows)
        guard.rollback()
    files = {
        str(path.relative_to(data)): {
            "size_bytes": path.stat().st_size,
            "sha256": _hash(path),
        }
        for path in sorted(data.rglob("*"))
        if path.is_file()
    }
    manifest = {
        "schema_version": "runtime.idle_backup.v1",
        "created_at": time.time(),
        "scope": "queue_native_v5_v6",
        "source_paths": {
            key: str(value) for key, value in paths.items() if key != "root"
        },
        "metadata": backup.get("metadata", {}),
        "jobs": len(rows),
        "files": files,
    }
    (temporary / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    _sync_tree(temporary)


def snapshot(config: dict[str, Any]) -> Path:
    paths, backup = _paths(config)
    root = paths["root"]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = _safe(root, directory=True)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("backup.root must be private and owned by current user")
    old_umask = os.umask(0o077)
    try:
        with (root / ".lock").open("a") as lock:
            _safe(root / ".lock")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            temporary = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=root))
            destination = None
            complete = False
            try:
                with _deadline(backup.get("max_seconds", 30)):
                    _create(paths, backup, temporary)
                    destination = root / f"snapshot-{time.time_ns()}-{uuid.uuid4().hex}"
                    temporary.rename(destination)
                    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                complete = True
                return destination
            finally:
                if not complete and destination is not None and destination.exists():
                    shutil.rmtree(destination)
                if temporary.exists():
                    shutil.rmtree(temporary)
    finally:
        os.umask(old_umask)


def status(config: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    # Source data need not still exist to inspect backup health.
    backup = config["backup"]
    root = Path(backup["root"])
    _safe(root, directory=True)
    candidates = sorted(root.glob("snapshot-*"))
    if not candidates:
        raise ValueError("no completed snapshots")
    latest = candidates[-1]
    _safe(latest, directory=True)
    if {path.name for path in latest.iterdir()} != {"manifest.json", "data"}:
        raise ValueError("unexpected snapshot files")
    manifest_path = latest / "manifest.json"
    _safe(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["schema_version"] != "runtime.idle_backup.v1":
        raise ValueError("unsupported backup manifest")
    data = latest / "data"
    _safe(data, directory=True)
    actual = {}
    for path in data.rglob("*"):
        if path.is_dir():
            _safe(path, directory=True)
        else:
            actual[str(path.relative_to(data))] = {
                "size_bytes": _safe(path).st_size,
                "sha256": _hash(path),
            }
    if actual != manifest["files"]:
        raise ValueError("backup inventory mismatch")
    age = (time.time() if now is None else now) - manifest["created_at"]
    if not 0 <= age <= backup.get("max_age_seconds", 7200):
        raise ValueError("backup is stale or timestamp is in the future")
    with closing(
        sqlite3.connect((data / "jobs.sqlite").as_uri() + "?mode=ro", uri=True)
    ) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("backup SQLite integrity check failed")
    return {
        "snapshot": str(latest),
        "age_seconds": age,
        "files": len(actual),
        "jobs": manifest["jobs"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text(encoding="utf-8"))
    print(
        json.dumps(
            status(config) if args.status else {"snapshot": str(snapshot(config))}
        )
    )


if __name__ == "__main__":
    main()
