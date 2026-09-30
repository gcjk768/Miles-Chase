#!/usr/bin/env python3
"""Docker healthcheck: healthy while scheduler.py keeps its heartbeat file fresh.

The scheduler touches state/heartbeat every few seconds, including while a report runs.
If it hasn't for 10 minutes, the scheduler is stuck. With --restart, this then force-stops
the scheduler, the container exits, and Docker's restart policy starts it again.
"""

import os
import signal
import sys
import time
from pathlib import Path

HEARTBEAT = Path(__file__).resolve().parent / "state" / "heartbeat"
STALE_AFTER_SECONDS = 10 * 60


def scheduler_pids():
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            args = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if len(args) > 1 and b"python" in args[0] and args[1].endswith(b"scheduler.py"):
            yield int(proc.name)


def main():
    try:
        age = time.time() - HEARTBEAT.stat().st_mtime
    except OSError:
        sys.exit("no heartbeat yet")
    if age <= STALE_AFTER_SECONDS:
        print(f"ok, heartbeat {age:.0f}s old")
        return
    if "--restart" in sys.argv:
        for pid in scheduler_pids():
            os.kill(pid, signal.SIGKILL)  # works even if the process is stuck or stopped
    sys.exit(f"heartbeat is {age / 60:.0f} minutes old")


if __name__ == "__main__":
    main()
