"""A grant record counts only while the server that wrote it runs.

A record names its server by pid and the time that process started, so a pid the
kernel later gives another process, or a server left as a zombie, holds no grant. (The
accessibility guard waits while any grant is held; a stale record would keep it waiting
for the grant's whole hour.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

from kwin_mcp.warden import state


def _grant(gid: str) -> dict:
    now = time.time()
    return {"id": gid, "kind": "window", "app": "kate", "given": now, "expires": now + 600}


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tmp_path.chmod(0o700)


def test_a_live_servers_record_counts(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    state.write_grants(os.getpid(), {"session": "a", "grants": [_grant("g1")]})
    assert [g["id"] for g in state.all_grants()] == ["g1"]


def test_a_record_whose_pid_now_names_another_process_counts_for_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    # This pid runs, but it's not the process that wrote the record: another start time.
    state.write_grants(os.getppid(), {"session": "b", "grants": [_grant("g2")]})
    path = state._sub("grants") / f"{os.getppid()}.json"
    data = json.loads(path.read_text())
    data["started"] = "1"
    path.write_text(json.dumps(data))
    assert state.all_grants() == []
    assert not path.exists(), "the stale record stayed"


def test_a_zombie_servers_record_counts_for_nothing(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state.write_grants(child.pid, {"session": "c", "grants": [_grant("g3")]})
        assert [g["id"] for g in state.all_grants()] == ["g3"]
        child.kill()
        deadline = time.monotonic() + 5
        while state._proc_start(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)  # dead but not reaped: a zombie
        assert state.all_grants() == []
    finally:
        child.kill()
        child.wait()
