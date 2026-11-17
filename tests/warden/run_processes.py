"""Find every process a test run started, so the run can stop what outlived it.

Every process a run starts inherits the mark ``KWIN_MCP_WARDEN_TEST_RUN=<id>`` (conftest.py;
run.sh sets its own). KWin hides its environment (it turns off ptrace access, so reading
/proc/PID/environ fails), so a process also counts when its session (setsid) is led by a
marked process: a private desktop's KWin runs in the session of its marked
dbus-run-session. A session led by anything else (the person's terminal) never counts.

    python3 -I tests/warden/run_processes.py KWIN_MCP_WARDEN_TEST_RUN=<id>   # prints pids
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path


def _session_id(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return int(fields[3])


def _marked(pid: int, mark: bytes) -> bool:
    try:
        return mark in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return False


def find(mark: bytes, exclude: set[int] | None = None) -> list[int]:
    """Marked processes, and the processes of every session a marked process leads."""
    exclude = exclude or set()
    sessions: dict[int, int] = {}
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            with contextlib.suppress(OSError, ValueError, IndexError):
                sessions[int(proc.name)] = _session_id(int(proc.name))
    marked = {pid for pid in sessions if _marked(pid, mark)}
    led = {pid for pid in marked if sessions[pid] == pid}
    found = marked | {pid for pid, sid in sessions.items() if sid in led}
    return sorted(found - exclude - {os.getpid()})


def still_ours(pid: int, mark: bytes) -> bool:
    """Checked again right before each signal (a pid can be reused)."""
    if _marked(pid, mark):
        return True
    try:
        leader = _session_id(pid)
    except (OSError, ValueError, IndexError):
        return False
    return leader != pid and _marked(leader, mark) and _session_id(leader) == leader


if __name__ == "__main__":
    for found_pid in find(sys.argv[1].encode()):
        print(found_pid)
