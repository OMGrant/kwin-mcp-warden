"""The warden's daemon lives only as long as the desktop it serves.

Found 2026-11-14: 16 daemons from earlier test runs were still running, each waiting out ten
idle minutes because it counted the person's own kwin-mcp-warden servers (on another
desktop) as its own, and each recreating its torn-down runtime folder. These tests start
real daemon processes from outside, with a runtime folder of their own.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _env(tmp_path: Path, runtime: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY")
    }
    env.update(
        {
            "XDG_RUNTIME_DIR": str(runtime),
            "HOME": str(tmp_path / "home"),
            "XDG_STATE_HOME": str(tmp_path / "home" / "state"),
            "XDG_CONFIG_HOME": str(tmp_path / "home" / "config"),
            # No devices, no bus: the daemon runs headless and never touches the desktop.
            "KWIN_MCP_WARDEN_INPUT_DEVICES": "",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=" + str(tmp_path / "no-bus"),
        }
    )
    return env


def _start_daemon(tmp_path: Path, runtime: Path) -> subprocess.Popen:
    runtime.mkdir(mode=0o700, parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-m", "kwin_mcp.warden.daemon"],
        env=_env(tmp_path, runtime),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while not (runtime / "kwin-mcp-warden" / "daemon.pid").exists():
        assert time.monotonic() < deadline, "the daemon never started"
        assert proc.poll() is None, "the daemon exited at start"
        time.sleep(0.05)
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
        proc.wait()


def test_the_daemon_ends_with_its_runtime_folder(tmp_path):
    runtime = tmp_path / "run"
    proc = _start_daemon(tmp_path, runtime)
    try:
        import shutil

        shutil.rmtree(runtime)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pytest.fail("the daemon outlived its desktop's runtime folder")
        assert not runtime.exists(), "the daemon recreated the runtime folder"
    finally:
        _stop(proc)


def test_another_desktops_server_doesnt_keep_the_daemon(tmp_path, monkeypatch):
    from kwin_mcp.warden import daemon

    mine, other = tmp_path / "mine", tmp_path / "other"
    mine.mkdir()
    other.mkdir()
    # A stand-in server: a process whose arguments name kwin_mcp, like `python -m kwin_mcp`.
    server = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "kwin_mcp"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(other)},
    )
    try:
        time.sleep(0.2)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(mine))
        assert not daemon._servers_alive(), "a server on another desktop kept the daemon"
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(other))
        assert daemon._servers_alive(), "a server on the daemon's own desktop wasn't seen"
    finally:
        _stop(server)


def _install_restart_block() -> str:
    """install.sh's own lines that restart a running daemon, run as they are."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "install.sh").read_text()
    start = text.index('pidfile="${XDG_RUNTIME_DIR')
    end = text.index("\nfi\n", start) + len("\nfi\n")
    return 'say() { echo "$*"; }\n' + text[start:end]


def test_install_restarts_a_running_daemon_and_nothing_else(tmp_path):
    """After an upgrade, the running daemon would keep drawing the old overlay until every
    session ended (2026-11-16); install.sh stops it, and only it."""
    runtime = tmp_path / "run"
    proc = _start_daemon(tmp_path, runtime)
    try:
        subprocess.run(
            ["bash", "-c", _install_restart_block()],
            env=_env(tmp_path, runtime),
            check=True,
        )
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("install.sh left the old daemon running")
    finally:
        _stop(proc)
    # A pid file that names something else (a reused pid): left alone.
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (runtime / "kwin-mcp-warden").mkdir(parents=True, exist_ok=True)
        (runtime / "kwin-mcp-warden" / "daemon.pid").write_text(str(other.pid))
        subprocess.run(
            ["bash", "-c", _install_restart_block()],
            env=_env(tmp_path, runtime),
            check=True,
        )
        time.sleep(0.5)
        assert other.poll() is None, "install.sh stopped a process that isn't the daemon"
    finally:
        _stop(other)
