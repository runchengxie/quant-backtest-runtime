"""Local lifecycle commands for the separately deployed backtest runtime."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .agent_access import AccessError, checked_plan, load_request
from .agent_errors import AgentParser, emit_error
from .capabilities import capabilities
from .jobs import BacktestJobRequest, BacktestJobService
from .results import read_job_result
from .store import JobStore


def _parser() -> AgentParser:
    parser = AgentParser(description="Durable backtest jobs")
    parser.add_argument("--error-format", choices=("text", "json"), default="text")
    parser.add_argument("--database", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--result-root", type=Path)
    parser.add_argument("--engine-executable", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("manifest", type=Path)
    submit.add_argument("--expected-plan-sha256")
    commands.add_parser("capabilities")
    commands.add_parser("plan").add_argument("manifest", type=Path)
    commands.add_parser("inspect").add_argument("job_id")
    commands.add_parser("lookup").add_argument("--idempotency-key", required=True)
    for name in ("status", "cancel", "result"):
        commands.add_parser(name).add_argument("job_id")
    commands.add_parser("recover")
    return parser


def _start_worker(args: argparse.Namespace, store: JobStore, job_id: str) -> None:
    logs = args.result_root.resolve() / ".logs"
    logs.mkdir(parents=True, exist_ok=True)
    stdout_path = logs / f"{job_id}.stdout.log"
    stderr_path = logs / f"{job_id}.stderr.log"
    if not store.set_backtest_job_logs(
        job_id, stdout_path=str(stdout_path), stderr_path=str(stderr_path)
    ):
        return
    command = [
        sys.executable,
        "-m",
        "backtest_runtime.worker",
        "--job-id",
        job_id,
        "--registry",
        str(args.database.resolve()),
        "--artifact-root",
        str(args.artifact_root.resolve()),
        "--result-root",
        str(args.result_root.resolve()),
    ]
    if args.engine_executable is not None:
        command.extend(["--engine-executable", str(args.engine_executable.resolve())])
    environment = {
        name: os.environ[name]
        for name in ("PATH", "LD_LIBRARY_PATH", "LANG", "LC_ALL", "TMPDIR")
        if name in os.environ
    }
    environment["PYTHONNOUSERSITE"] = "1"
    previous_umask = os.umask(0o077)
    try:
        with stdout_path.open("ab") as stdout, stderr_path.open("ab") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
                env=environment,
            )
    except OSError as error:
        store.transition_backtest_job(
            job_id,
            expected="SUBMITTED",
            status="FAILED",
            error_code="WORKER_START_FAILED",
            error_message=str(error)[:2000],
        )
        raise
    finally:
        os.umask(previous_umask)


def _run(args: argparse.Namespace) -> dict:
    if args.command == "capabilities":
        return capabilities()
    if args.command == "plan":
        return checked_plan(
            load_request(args.manifest), args.artifact_root, args.engine_executable
        )
    request = load_request(args.manifest) if args.command == "submit" else None
    if request is not None and args.expected_plan_sha256 is not None:
        plan = checked_plan(request, args.artifact_root, args.engine_executable)
        if plan["plan_sha256"] != args.expected_plan_sha256:
            raise AccessError(
                "PLAN_MISMATCH", "Execution plan changed; inspect a new plan"
            )
    read_only = args.command in {"inspect", "lookup"}
    if read_only and not args.database.expanduser().is_file():
        raise AccessError("DATABASE_UNAVAILABLE", "Existing job database is required")
    store = JobStore(args.database, read_only=read_only)
    service = BacktestJobService(
        store, artifact_root=args.artifact_root, result_root=args.result_root
    )
    try:
        return _dispatch(args, store, service, request)
    finally:
        store.close()


def _submit(
    args: argparse.Namespace,
    store: JobStore,
    service: BacktestJobService,
    request: BacktestJobRequest,
) -> dict:
    args.idempotency_key = request.idempotency_key
    service.recover_expired()
    receipt = service.submit(request)
    args.job_id = receipt.job_id
    if receipt.status == "SUBMITTED":
        try:
            _start_worker(args, store, receipt.job_id)
        except OSError as error:
            if args.error_format != "json":
                raise
            raise AccessError(
                "WORKER_START_FAILED", "Worker startup failed; look up the job by key"
            ) from error
    return asdict(receipt)


def _dispatch(
    args: argparse.Namespace,
    store: JobStore,
    service: BacktestJobService,
    request: BacktestJobRequest | None,
) -> dict:
    if request is not None:
        return _submit(args, store, service, request)
    if args.command == "recover":
        return {"recovered": service.recover_expired()}
    if args.command == "status":
        service.recover_expired()
        return asdict(service.get_status(args.job_id))
    if args.command == "inspect":
        return asdict(service.get_status(args.job_id))
    if args.command == "lookup":
        row = store.get_backtest_job_by_key(args.idempotency_key)
        if row is None:
            raise KeyError("No matching idempotency key")
        return asdict(service.get_status(row["job_id"]))
    if args.command == "cancel":
        return asdict(service.cancel(args.job_id))
    return _result(args, store, service)


def _result(
    args: argparse.Namespace, store: JobStore, service: BacktestJobService
) -> dict:
    ref = service.get_result(args.job_id)
    row = store.get_backtest_job(args.job_id)
    if row is None:
        raise KeyError("Unknown backtest job")
    try:
        return read_job_result(
            args.result_root.resolve(),
            job_id=args.job_id,
            manifest_sha256=ref.manifest_sha256,
            request_sha256=row["request_sha256"],
        )
    except (ValueError, OSError) as error:
        if args.error_format != "json":
            raise
        raise AccessError(
            "INTEGRITY_FAILURE", "Result integrity verification failed"
        ) from error


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    json_errors = (
        any(values[i : i + 2] == ["--error-format", "json"] for i in range(len(values)))
        or "--error-format=json" in values
    )
    parser = _parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                child.json_errors = json_errors
    parser.json_errors = json_errors
    args = None
    try:
        args = parser.parse_args(values)
        if args.command != "capabilities" and any(
            getattr(args, name) is None
            for name in ("database", "artifact_root", "result_root")
        ):
            parser.error("--database, --artifact-root and --result-root are required")
        output = _run(args)
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        if not json_errors:
            raise
        return emit_error(error, args)


if __name__ == "__main__":
    sys.exit(main())
