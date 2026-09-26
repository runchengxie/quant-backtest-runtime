"""Local lifecycle commands for the separately deployed backtest runtime."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from .jobs import BacktestJobRequest, BacktestJobService
from .results import read_job_result
from .store import JobStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable backtest jobs")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--result-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("manifest", type=Path)
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store = JobStore(args.database)
    service = BacktestJobService(
        store, artifact_root=args.artifact_root, result_root=args.result_root
    )
    try:
        if args.command == "submit":
            manifest_stat = args.manifest.lstat()
            if (
                not stat.S_ISREG(manifest_stat.st_mode)
                or manifest_stat.st_size > 64 * 1024
            ):
                raise ValueError(
                    "manifest must be a regular file no larger than 64 KiB"
                )
            request = BacktestJobRequest.from_mapping(
                json.loads(args.manifest.read_text(encoding="utf-8"))
            )
            service.recover_expired()
            receipt = service.submit(request)
            if receipt.status == "SUBMITTED":
                _start_worker(args, store, receipt.job_id)
            output = asdict(receipt)
        elif args.command == "recover":
            output = {"recovered": service.recover_expired()}
        elif args.command == "status":
            service.recover_expired()
            output = asdict(service.get_status(args.job_id))
        elif args.command == "cancel":
            output = asdict(service.cancel(args.job_id))
        else:
            ref = service.get_result(args.job_id)
            row = store.get_backtest_job(args.job_id)
            if row is None:
                raise KeyError(f"Unknown backtest job: {args.job_id}")
            output = read_job_result(
                args.result_root.resolve(),
                job_id=args.job_id,
                manifest_sha256=ref.manifest_sha256,
                request_sha256=row["request_sha256"],
            )
        print(json.dumps(output, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
