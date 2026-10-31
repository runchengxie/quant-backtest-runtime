"""One-shot Linux child supervision for an explicitly pinned Rust executable."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .contracts import BacktestJobRequest
from .engine_protocol import validate_engine_result
from .jobs import BacktestJobService


def _parent_death_setup() -> Any:
    # Resolve libc before fork. The worker is single-threaded here; do not call this
    # runner from a multithreaded host. Check the race where parent dies before prctl.
    libc = ctypes.CDLL(None, use_errno=True)
    parent_pid = os.getpid()

    def setup() -> None:
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(126)
        if os.getppid() != parent_pid:
            os._exit(126)

    return setup


def run_engine(
    request: BacktestJobRequest, service: BacktestJobService, executable: Path | None
) -> dict[str, Any]:
    if executable is None or not executable.is_absolute():
        raise ValueError(
            "v5 requires an operator-configured absolute engine executable"
        )
    binary = executable.resolve(strict=True)
    with binary.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
        if digest != request.config["executable_sha256"]:
            raise ValueError("engine executable SHA-256 mismatch")
        return _run_verified(request, service, source.fileno())


def _run_verified(
    request: BacktestJobRequest, service: BacktestJobService, binary_fd: int
) -> dict[str, Any]:
    reference = request.inputs["engine_request_ref"]
    if reference is None:
        raise ValueError("engine request artifact is required")
    source = service.resolve_artifact(reference)
    # Child output uses files, not unbounded PIPE buffers. The binary is trusted and
    # content-pinned; resource limits and wall timeout are inherited from the worker.
    with tempfile.TemporaryDirectory(prefix="backtest-engine-") as temporary:
        path = Path(temporary) / "request.json"
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != reference.rsplit("/", 1)[-1]:
            raise ValueError("engine input artifact changed before execution")
        path.write_bytes(data)
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            child = subprocess.Popen(
                [f"/proc/self/fd/{binary_fd}", "run", str(path)],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                preexec_fn=_parent_death_setup(),
                pass_fds=(binary_fd,),
            )
            try:
                child.wait(timeout=request.budgets["wall_seconds"])
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()
            if child.returncode:
                stderr.seek(0)
                message = stderr.read(2000).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"engine process failed ({child.returncode}): {message}"
                )
            if stdout.tell() > 64 * 1024 * 1024:
                raise ValueError("engine result exceeds 64 MiB")
            stdout.seek(0)
            payload = validate_engine_result(json.load(stdout))
    return payload
