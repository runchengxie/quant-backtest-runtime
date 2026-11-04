"""Fresh Linux helper: install parent-death handling without preexec_fn in a host."""

from __future__ import annotations

import ctypes
import os
import signal
import sys


def main() -> None:
    parent, descriptor = (int(value) for value in sys.argv[1:])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(126)
    os.set_inheritable(descriptor, True)
    os.execve(
        f"/proc/self/fd/{descriptor}", ["engine", "session-stream"], dict(os.environ)
    )


if __name__ == "__main__":
    main()
