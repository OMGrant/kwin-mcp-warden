"""Shared state between kwin-mcp-warden processes on one desktop.

Every Claude session runs its own server process. They share, under
``$XDG_RUNTIME_DIR/kwin-mcp-warden/``:

- ``card.lock``: one card at a time across every session (flock).
- ``card-open``: present while a card is on screen; every live input is refused then.
- ``grants/<pid>.json``: the grants each server holds, for the access list and frame.
- ``ended/<grant id>``: a grant you ended from the access list.
- ``stop``: its modification time ends every grant given before it (the stop key).
- ``hands``: its modification time is the last time you touched a real input device.
- ``daemon.pid``: the access list, stop key and frame process.

These files only carry what the warden decided. A grant is never created from
a file: each server holds its own grants in memory and only writes them out.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any


def runtime_dir() -> Path:
    given = os.environ.get("XDG_RUNTIME_DIR")
    base = given or f"/tmp/kwin-mcp-warden-{os.getuid()}"
    path = Path(base) / "kwin-mcp-warden"
    # The desktop's own runtime folder is never recreated: once it's gone, the desktop has
    # ended (FileNotFoundError), and a folder made again would outlive it.
    path.mkdir(mode=0o700, parents=not given, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        msg = f"{path} isn't private to this user; the warden won't use it"
        raise RuntimeError(msg)
    return path


def _sub(name: str) -> Path:
    path = runtime_dir() / name
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def card_lock_path() -> Path:
    return runtime_dir() / "card.lock"


def card_open_path() -> Path:
    return runtime_dir() / "card-open"


def card_is_open() -> bool:
    """Whether any session's card is on screen now (a stale marker older than 5 min is not)."""
    try:
        age = time.time() - card_open_path().stat().st_mtime
    except FileNotFoundError:
        return False
    return age < 300


def stop_time() -> float:
    """When the stop key (or End All) was last used; 0 when never."""
    try:
        return (runtime_dir() / "stop").stat().st_mtime
    except FileNotFoundError:
        return 0.0


def press_stop() -> None:
    path = runtime_dir() / "stop"
    path.touch()
    now = time.time()
    os.utime(path, (now, now))


def ended(grant_id: str) -> bool:
    return (_sub("ended") / grant_id).exists()


def end_grant(grant_id: str) -> None:
    if grant_id and "/" not in grant_id and not grant_id.startswith("."):
        (_sub("ended") / grant_id).touch()


def note_acted() -> None:
    """Claude just clicked, typed or scrolled on your screen (the corner notice's ring)."""
    path = runtime_dir() / "acted"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{time.time():.3f}", encoding="utf-8")
    tmp.replace(path)


def last_hands() -> float:
    """The last time you used a real mouse, keyboard or touchpad, a nudge included; 0 when
    unknown."""
    try:
        return (runtime_dir() / "hands").stat().st_mtime
    except FileNotFoundError:
        return 0.0


def last_press() -> float:
    """The last time you pressed a key or button or touched a screen; 0 when unknown."""
    try:
        return (runtime_dir() / "presses").stat().st_mtime
    except FileNotFoundError:
        return 0.0


def hands_known() -> bool:
    """Whether a watcher is reporting your input (the daemon writes ``hands-watch``)."""
    try:
        return time.time() - (runtime_dir() / "hands-watch").stat().st_mtime < 10
    except FileNotFoundError:
        return False


def note_paused(why: str) -> None:
    """Claude is waiting for you: ``pointer`` (any use of the mouse) or ``presses``."""
    path = runtime_dir() / "paused"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(why, encoding="utf-8")
    tmp.replace(path)


def touch_hands(path_name: str = "hands") -> None:
    path = runtime_dir() / path_name
    path.touch()
    now = time.time()
    os.utime(path, (now, now))


def _proc_start(pid: int) -> str:
    """When the process started (/proc/PID/stat field 22, in clock ticks since boot), and
    "" when it's gone or a zombie: with the pid, it names one process, never a later one
    that was given the same pid."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, IndexError):
        return ""
    return "" if fields[0] in ("Z", "X") else fields[19]


def write_grants(pid: int, payload: dict[str, Any]) -> None:
    path = _sub("grants") / f"{pid}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**payload, "started": _proc_start(pid)}), encoding="utf-8")
    tmp.replace(path)


def clear_grants(pid: int) -> None:
    with contextlib.suppress(FileNotFoundError):
        (_sub("grants") / f"{pid}.json").unlink()


def all_grants() -> list[dict[str, Any]]:
    """Every live grant every running server holds, oldest first."""
    out: list[dict[str, Any]] = []
    for path in sorted(_sub("grants").glob("*.json")):
        try:
            pid = int(path.stem)
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        except PermissionError:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        # The same process, not a later one given its pid, and not a zombie.
        if data.get("started") is not None and data["started"] != _proc_start(pid):
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        now = time.time()
        for grant in data.get("grants", []):
            if (
                grant.get("expires", 0) > now
                and not ended(grant.get("id", ""))
                and grant.get("given", 0) > stop_time()
            ):
                # A record from before agents were named came from a Claude session.
                agent = str(data.get("agent") or "Claude")
                out.append(
                    {**grant, "pid": pid, "session": data.get("session", ""), "agent": agent}
                )
    out.sort(key=lambda g: g.get("given", 0))
    return out
