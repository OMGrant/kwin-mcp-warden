"""The warden's log: one plain line per request, per answer and per refused action.

File: ``$XDG_STATE_HOME/kwin-mcp-warden/log`` (``~/.local/state`` when unset).
Lines look like ``2026-11-14 09:12:03  my-project  allowed  Control in Google Chrome (1 h)``.
Text Claude typed is never written here, only what kind of action it was.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base) / "kwin-mcp-warden" / "log"


def write(session: str, event: str, detail: str) -> None:
    text = f"{session}  {event}  {detail}"
    # One line per entry, whatever a session name or a refusal says.
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}"
    path = log_path()
    with _lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def tail(n: int = 5) -> list[str]:
    try:
        with log_path().open(encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()[-n:]
    except OSError:
        return []
