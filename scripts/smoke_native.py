"""Check a Go release with a synthetic engine fixture and fresh, retained state."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare_root(root: Path) -> None:
    # Refuse even an empty existing directory; this tool never opens shared state.
    root.mkdir(parents=True, exist_ok=False)
    (root / "artifacts/sha256").mkdir(parents=True)


def check_version(version: dict[str, Any], expected: str) -> None:
    if version.get("source_revision") != expected:
        raise ValueError("runtime source revision does not match expected revision")
    if (
        version.get("schema") != "quant.runtime_build.v1"
        or version.get("implementation") != "go"
    ):
        raise ValueError("expected a Go runtime build receipt")


def call(command: list[str], *args: str) -> dict[str, Any]:
    return json.loads(subprocess.check_output([*command, *args], text=True, timeout=60))


def create_request(engine: Path, fixture: Path, root: Path) -> Path:
    payload = fixture.read_bytes()
    schema = json.loads(payload)["schema_version"]
    backends = {
        "engine_request.v1": (5, "nautilus.quote_market_orders"),
        "engine_request.v2": (6, "nautilus.session_replay"),
    }
    version, backend = backends[schema]
    sha = hashlib.sha256(payload).hexdigest()
    (root / "artifacts/sha256" / sha).write_bytes(payload)
    request = {
        "schema_version": version,
        "idempotency_key": "native-release-check",
        "backend": backend,
        "evidence_tier": "diagnostic",
        "inputs": {"engine_request_ref": f"artifact://sha256/{sha}"},
        "config": {"executable_sha256": digest(engine)},
        "execution": {},
        "budgets": {"wall_seconds": 30, "memory_mb": 4096},
    }
    path = root / "request.json"
    path.write_text(json.dumps(request) + "\n", encoding="utf-8")
    return path


def smoke(args: argparse.Namespace) -> dict[str, Any]:
    executable = args.executable.resolve(strict=True)
    engine = args.engine_executable.resolve(strict=True)
    version = call([str(executable)], "version")
    check_version(version, args.expected_runtime_revision)
    root = args.root.absolute()
    prepare_root(root)
    request = create_request(engine, args.engine_request, root)
    command = [
        str(executable),
        "--database",
        str(root / "jobs.sqlite"),
        "--artifact-root",
        str(root / "artifacts"),
        "--result-root",
        str(root / "results"),
        "--engine-executable",
        str(engine),
    ]
    plan = call(command, "plan", str(request))
    job = call(
        command,
        "submit",
        "--queue",
        str(request),
        "--expected-plan-sha256",
        plan["plan_sha256"],
    )
    duplicate = call(command, "submit", "--queue", str(request))
    lookup = call(command, "lookup", "--idempotency-key", "native-release-check")
    if job["job_id"] != duplicate["job_id"] or job["job_id"] != lookup["job_id"]:
        raise ValueError("duplicate submission or receipt lookup changed job identity")
    cancelled = cancel_queued(command, request)
    call(command, "serve", "--concurrency", "1", "--once")
    state = call(command, "status", job["job_id"])
    if state["status"] != "SUCCEEDED":
        raise ValueError(f"synthetic native job failed: {state}")
    result = call(command, "result", job["job_id"])
    receipt = {
        "schema": "quant.native_release_check.v1",
        "status": state["status"],
        "job_id": job["job_id"],
        "cancel_status": cancelled["status"],
        "runtime": version,
        "runtime_sha256": digest(executable),
        "engine_sha256": digest(engine),
        "engine_request_sha256": digest(args.engine_request),
        "result_sha256": state["result_sha256"],
        "summary": result["summary"],
    }
    (root / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def cancel_queued(command: list[str], request: Path) -> dict[str, Any]:
    mapping = json.loads(request.read_text())
    mapping["idempotency_key"] = "native-release-check-cancel"
    cancelled = request.with_name("cancel-request.json")
    cancelled.write_text(json.dumps(mapping) + "\n")
    job = call(command, "submit", "--queue", str(cancelled))
    call(command, "cancel", job["job_id"])
    state = call(command, "status", job["job_id"])
    if state["status"] != "CANCELLED":
        raise ValueError("queued cancellation failed")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--engine-executable", type=Path, required=True)
    parser.add_argument("--engine-request", type=Path, required=True)
    parser.add_argument("--expected-runtime-revision", required=True)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(smoke(parser.parse_args())))


if __name__ == "__main__":
    main()
