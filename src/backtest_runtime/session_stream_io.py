"""Strict JSON and bounded, concurrently drained pipes for the opt-in Linux stream."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .partition_inputs import _bad_constant, _finite_float, _no_duplicates

WIRE_MAX_FRAME_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class StreamLimits:
    """Frame bytes include newline. Output history grows with sessions, never quotes."""

    wall_seconds: float = 300
    max_frame_bytes: int = WIRE_MAX_FRAME_BYTES
    max_stderr_bytes: int = 8192
    max_sessions: int = 4096
    max_journal_bytes: int = 8 * 1024 * 1024

    def __post_init__(self) -> None:
        import math

        if (
            type(self.wall_seconds) not in (int, float)
            or not math.isfinite(self.wall_seconds)
            or not 0 < self.wall_seconds <= 86400
        ):
            raise ValueError("wall_seconds outside limits")
        for name in (
            "max_frame_bytes",
            "max_stderr_bytes",
            "max_sessions",
            "max_journal_bytes",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("stream limits must be positive integers")
        if self.max_frame_bytes > WIRE_MAX_FRAME_BYTES:
            raise ValueError("frame limit exceeds wire maximum")


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as error:
        raise ValueError("invalid finite JSON value") from error


def parse(data: bytes) -> Any:
    try:
        return json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_bad_constant,
            parse_float=_finite_float,
        )
    except (UnicodeError, RecursionError) as error:
        raise ValueError("invalid UTF-8 JSON frame") from error


def fields(value: Any, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError("unexpected stream fields")
    return value


class PipeProcess:
    """One held executable inode; no shell, preexec_fn, or pathname reopen."""

    def __init__(
        self,
        executable: Path,
        sha256: str,
        limits: StreamLimits,
        cancelled: Callable[[], bool] | None,
    ):
        self.limits = limits
        self.cancelled = cancelled
        self.deadline = time.monotonic() + limits.wall_seconds
        self.selector = selectors.DefaultSelector()
        self.output = bytearray()
        self.stderr = bytearray()
        self.child: subprocess.Popen[bytes] | None = None
        try:
            self._launch(executable, sha256)
        except BaseException:
            self.close()
            raise

    def _launch(self, executable: Path, sha256: str) -> None:
        if not executable.is_absolute():
            raise ValueError("absolute engine executable required")
        fd = os.open(executable, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("engine executable must be a regular file")
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise ValueError("engine executable SHA-256 mismatch")
            package_root = str(Path(__file__).resolve().parent.parent)
            self.child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "backtest_runtime.session_stream_process",
                    str(os.getpid()),
                    str(fd),
                ],
                cwd=package_root,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(fd,),
                start_new_session=True,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONPATH": package_root},
            )
        assert self.child.stdin and self.child.stdout and self.child.stderr
        for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
            os.set_blocking(stream.fileno(), False)
        self.selector.register(self.child.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.child.stderr, selectors.EVENT_READ, "stderr")

    def check(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise RuntimeError("session stream cancelled")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("session stream wall deadline exceeded")

    def _read(self, key: selectors.SelectorKey) -> None:
        name = key.data
        target = self.output if name == "stdout" else self.stderr
        cap = (
            self.limits.max_frame_bytes
            if name == "stdout"
            else self.limits.max_stderr_bytes
        )
        chunk = os.read(key.fd, min(65536, cap - len(target) + 1))
        if not chunk:
            self.selector.unregister(key.fileobj)
            return
        target.extend(chunk)
        if len(target) > cap:
            raise ValueError(f"engine {name} byte limit exceeded")

    def _write(
        self, key: selectors.SelectorKey, pending: memoryview, eof: bool
    ) -> memoryview:
        assert self.child and self.child.stdin
        try:
            pending = pending[os.write(key.fd, pending) :]
        except BrokenPipeError as error:
            raise RuntimeError("engine closed input") from error
        if not pending:
            self.selector.unregister(key.fileobj)
            if eof:
                self.child.stdin.close()
        return pending

    def exchange(self, data: bytes, *, eof: bool = False) -> Any:
        if len(data) + 1 > self.limits.max_frame_bytes:
            raise ValueError("engine input frame byte limit exceeded")
        assert self.child and self.child.stdin
        pending = memoryview(data + b"\n")
        self.selector.register(self.child.stdin, selectors.EVENT_WRITE, "stdin")
        while True:
            self.check()
            for key, _events in self.selector.select(
                min(0.05, max(0, self.deadline - time.monotonic()))
            ):
                if key.data == "stdin":
                    pending = self._write(key, pending, eof)
                else:
                    self._read(key)
            if not pending and b"\n" in self.output:
                line, _, extra = self.output.partition(b"\n")
                if extra:
                    raise ValueError("extra engine stdout frame")
                self.output.clear()
                return parse(bytes(line))
            if not any(k.data == "stdout" for k in self.selector.get_map().values()):
                raise RuntimeError(
                    "engine stdout truncated or process failed: "
                    + self.stderr.decode("utf-8", "replace")
                )

    def finish(self) -> None:
        assert self.child
        # WNOWAIT pins the leader PID/PGID until we have signalled its group.
        # Popen.poll()/wait() would reap it and permit that numeric PGID's reuse.
        while self.selector.get_map() or not self._exited():
            self.check()
            for key, _events in self.selector.select(0.05):
                self._read(key)
            if self.output:
                raise ValueError("extra engine stdout after finished")
        self._stop()
        if self.child.returncode != 0:
            raise RuntimeError(
                "engine process failed: " + self.stderr.decode("utf-8", "replace")
            )

    def _exited(self) -> bool:
        assert self.child
        return (
            os.waitid(os.P_PID, self.child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            is not None
        )

    def _stop(self) -> None:
        assert self.child
        if self.child.returncode is None:
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.child.wait()

    def close(self) -> None:
        if self.child is not None:
            self._stop()
            for stream in (self.child.stdin, self.child.stdout, self.child.stderr):
                if stream is not None:
                    stream.close()
        self.selector.close()
