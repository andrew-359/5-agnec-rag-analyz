"""Launch the extraction CLI with a Linux parent-death signal."""

import ctypes
import os
import signal
import sys


PR_SET_PDEATHSIG = 1


def main() -> None:
    worker_pid = int(sys.argv[1])
    libc = ctypes.CDLL(None, use_errno=True)
    unused = ctypes.c_ulong(0)
    if libc.prctl(PR_SET_PDEATHSIG, ctypes.c_ulong(signal.SIGKILL), unused, unused, unused) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    # The worker can die after spawning this process and before the signal is installed.
    if os.getppid() != worker_pid:
        sys.exit(1)
    os.execv(sys.executable, [sys.executable, "-m", "rag_mvp", *sys.argv[2:]])


if __name__ == "__main__":
    main()
