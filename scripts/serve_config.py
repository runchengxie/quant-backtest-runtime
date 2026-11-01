"""Exec the Go queue scheduler from an operator-owned TOML file."""

from __future__ import annotations

import argparse
import json
import os
import tomllib
from pathlib import Path
from typing import Any


def command(values: dict[str, Any]) -> list[str]:
    runtime = values.get("runtime", {})
    scheduler = values.get("scheduler", {})
    if not isinstance(runtime, dict) or not isinstance(scheduler, dict):
        raise ValueError("runtime and scheduler must be tables")
    if runtime.get("queue") is not True:
        raise ValueError("scheduler requires runtime.queue=true")
    paths = (
        ("executable", None),
        ("database", "--database"),
        ("artifact_root", "--artifact-root"),
        ("result_root", "--result-root"),
        ("python_executable", "--python-executable"),
        ("engine_executable", "--engine-executable"),
    )
    result = []
    for key, flag in paths:
        value = runtime.get(key)
        if value is None and key in {"python_executable", "engine_executable"}:
            continue
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError(f"runtime.{key} must be an absolute path")
        if flag is not None:
            result.append(flag)
        result.append(value)
    concurrency = scheduler.get("concurrency", 1)
    root = scheduler.get("cgroup_root", "auto")
    if type(concurrency) is not int or not 1 <= concurrency <= 256:
        raise ValueError("scheduler.concurrency must be 1-256")
    if not isinstance(root, str) or (root != "auto" and not Path(root).is_absolute()):
        raise ValueError("scheduler.cgroup_root must be auto or an absolute path")
    return [*result, "serve", "--concurrency", str(concurrency), "--cgroup-root", root]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    argv = command(tomllib.loads(args.config.read_text(encoding="utf-8")))
    if args.dry_run:
        print(json.dumps(argv))
    else:
        os.execv(argv[0], argv)


if __name__ == "__main__":
    main()
