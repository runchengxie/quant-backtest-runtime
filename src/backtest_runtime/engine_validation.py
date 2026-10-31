"""Bounded, read-only validation through a held, content-pinned executable inode.

Deployment must keep executable inodes immutable: descriptors prevent pathname
replacement, but cannot protect against writes to an already-open inode.
"""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import signal
import stat
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO, cast

INPUT_LIMIT = 1024 * 1024
STREAM_LIMIT = 8 * 1024
TIMEOUT_SECONDS = 10.0


class EngineValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _unavailable(message: str) -> EngineValidationError:
    return EngineValidationError("ENGINE_VALIDATION_UNAVAILABLE", message)


def _read_input(path: Path, digest: str) -> bytes:
    # Refuse special files before reading; NONBLOCK also prevents FIFO-open hangs.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("engine input must be a regular file")
        data = source.read(INPUT_LIMIT + 1)
    if len(data) > INPUT_LIMIT:
        raise EngineValidationError(
            "ENGINE_REQUEST_INVALID", "Engine request exceeds 1 MiB"
        )
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("engine input artifact digest mismatch")
    return data


def validate_request(
    executable: Path, executable_sha256: str, input_path: Path, input_sha256: str
) -> dict[str, Any]:
    data = _read_input(input_path, input_sha256)
    try:
        fd = os.open(executable, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise _unavailable("Engine executable must be a regular file")
            if hashlib.file_digest(source, "sha256").hexdigest() != executable_sha256:
                raise EngineValidationError(
                    "INTEGRITY_FAILURE", "Engine executable digest mismatch"
                )
            deadline = time.monotonic() + TIMEOUT_SECONDS
            advertisement, status = _invoke(
                source.fileno(), b"", command="capabilities", deadline=deadline
            )
            _capabilities(advertisement, status)
            output, status = _invoke(
                source.fileno(), data, command="validate", deadline=deadline
            )
    except OSError as error:
        raise _unavailable("Engine validator could not be launched") from error
    if status == 2 and not output:
        raise EngineValidationError(
            "ENGINE_REQUEST_INVALID", "Engine rejected request semantics"
        )
    if status != 0:
        raise _unavailable("Engine validator failed or is unsupported")
    return _receipt(output, input_sha256)


def _json_response(output: bytes) -> Any:
    try:
        return json.loads(output, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise _unavailable("Engine validator returned malformed JSON") from error


def _capabilities(output: bytes, status: int) -> None:
    if status != 0:
        raise _unavailable("Engine validator capabilities are unavailable")
    value = _json_response(output)
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "engine_capabilities.v1"
        or value.get("request_schema") != "engine_request.v1"
        or type(value.get("validator_version")) is not int
        or value.get("validator_version") != 1
    ):
        raise _unavailable("Engine does not advertise a compatible validator")


def _receipt(output: bytes, digest: str) -> dict[str, Any]:
    value = _json_response(output)
    expected = {
        "schema_version": "engine_validation.v1",
        "request_schema": "engine_request.v1",
        "validator_version": 1,
        "valid": True,
        "input_sha256": digest,
    }
    if (
        not isinstance(value, dict)
        or value != expected
        or type(value.get("validator_version")) is not int
        or value.get("valid") is not True
    ):
        raise _unavailable("Engine validator receipt is unsupported or mismatched")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate receipt field")
        value[key] = item
    return value


def _invoke(
    binary_fd: int, data: bytes, *, command: str, deadline: float
) -> tuple[bytes, int]:
    arguments = [command, "-"] if command == "validate" else ["capabilities"]
    with subprocess.Popen(
        [f"/proc/self/fd/{binary_fd}", *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(binary_fd,),
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
        start_new_session=True,
    ) as child:
        try:
            output = _exchange(child, data, deadline)
            child.wait(timeout=max(0.001, deadline - time.monotonic()))
            return output, child.returncode
        except subprocess.TimeoutExpired as error:
            raise _unavailable("Engine validation exceeded deadline") from error
        finally:
            # Includes descendants holding pipe ends after the direct child exited.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def _exchange(child: subprocess.Popen[bytes], data: bytes, deadline: float) -> bytes:
    assert (
        child.stdin is not None
        and child.stdout is not None
        and child.stderr is not None
    )
    outputs = {child.stdout: bytearray(), child.stderr: bytearray()}
    with selectors.DefaultSelector() as selector:
        for stream in outputs:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        os.set_blocking(child.stdin.fileno(), False)
        selector.register(child.stdin, selectors.EVENT_WRITE)
        position = 0
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _unavailable("Engine validation exceeded deadline")
            for key, _ in selector.select(remaining):
                stream = cast(BinaryIO, key.fileobj)
                if stream is child.stdin:
                    position = _write_input(selector, stream, data, position)
                else:
                    _read_output(selector, stream, outputs[stream])
    return bytes(outputs[child.stdout])


def _write_input(
    selector: selectors.BaseSelector, stream: BinaryIO, data: bytes, position: int
) -> int:
    try:
        written = os.write(stream.fileno(), data[position : position + 4096])
        position += written
    except BrokenPipeError:
        position = len(data)
    if position == len(data):
        selector.unregister(stream)
        stream.close()
    return position


def _read_output(
    selector: selectors.BaseSelector, stream: BinaryIO, output: bytearray
) -> None:
    chunk = os.read(stream.fileno(), min(4096, STREAM_LIMIT + 1 - len(output)))
    output.extend(chunk)
    if len(output) > STREAM_LIMIT:
        raise _unavailable("Engine validator output exceeds 8 KiB")
    if not chunk:
        selector.unregister(stream)
        stream.close()
