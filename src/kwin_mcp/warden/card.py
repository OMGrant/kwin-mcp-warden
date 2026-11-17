"""Show the consent card and wait for your answer.

The card is ``ui/card.qml``, run by Qt's own ``qml`` program as a separate
process. Its exit code is the answer, and only exit code 42 means Allow, so a
crash, a kill or a missing ``qml`` program can never allow anything. The card is
never answered over D-Bus, a socket or a file: no kwin-mcp tool can answer it.

What this doesn't stop: anything that runs as the person (a Claude session with a
shell, say) can replace the warden's files or start a fake card. The warden guards
the kwin-mcp route; Claude Code's own permission prompts guard the shell.

One card at a time across every session on the desktop (a lock file); while a
card is up, ``card-open`` exists and every live input action is refused.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from kwin_mcp.warden import state

UI_DIR = Path(__file__).resolve().parent / "ui"
CARD_QML = UI_DIR / "card.qml"

ALLOW_CODE = 42
DENY_CODE = 10
TIMEOUT_CODE = 11
GO_CODE = 12

# Qt's own qml program, from the system's Qt folders first: a program found on PATH
# could be any file the user (or a session acting as the user) put there.
_QML_SYSTEM = (
    "/usr/lib64/qt6/bin/qml",
    "/usr/lib/qt6/bin/qml",
    "/usr/lib/x86_64-linux-gnu/qt6/bin/qml",
    "/usr/lib/aarch64-linux-gnu/qt6/bin/qml",
    "/usr/bin/qml6",
    "/usr/bin/qml",
)

type Answer = Literal["allow", "deny", "timeout", "go", "failed"]


@dataclass(frozen=True)
class Outcome:
    answer: Answer
    detail: str = ""


def qml_program() -> str | None:
    for path in _QML_SYSTEM:
        if os.access(path, os.X_OK):
            return path
    for name in ("qml6", "qml"):  # last resort: a Qt installed somewhere else
        found = shutil.which(name)
        if found:
            return found
    return None


def command_line(request: dict[str, Any], *, quoted: bool = False) -> list[str]:
    program = qml_program() or "qml"
    payload = json.dumps(request, ensure_ascii=False)
    argv = [program, "--apptype", "widget", str(CARD_QML), "--", payload]
    return [shlex.quote(a) for a in argv] if quoted else argv


def primary_output(env: dict[str, str] | None = None) -> str:
    """The primary monitor's connector name, read from KDE (kscreen-doctor), or from KWin's
    own screen order when kscreen-doctor isn't installed or answers nothing; '' if neither."""
    return _primary_from_kscreen(env) or _primary_from_kwin(env)


def _primary_from_kwin(env: dict[str, str] | None) -> str:
    import sys

    try:
        out = subprocess.run(
            [sys.executable, "-m", "kwin_mcp.geometry"],
            input=json.dumps({"op": "primary"}),
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
            check=False,
        ).stdout
        resp = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return str(resp.get("result") or "") if resp.get("ok") else ""


def _primary_from_kscreen(env: dict[str, str] | None) -> str:
    tool = shutil.which("kscreen-doctor")
    if not tool:
        return ""
    try:
        out = subprocess.run(
            [tool, "-j"], capture_output=True, text=True, timeout=5, env=env, check=False
        ).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    best: tuple[int, str] | None = None
    for output in data.get("outputs", []):
        if not output.get("enabled", True) or not output.get("connected", True):
            continue
        priority = int(output.get("priority", 99) or 99)
        if best is None or priority < best[0]:
            best = (priority, str(output.get("name", "")))
    return best[1] if best else ""


@contextlib.contextmanager
def _one_card_at_a_time():
    with open(state.card_lock_path(), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        marker = state.card_open_path()
        try:
            marker.touch()
            yield
        finally:
            with contextlib.suppress(FileNotFoundError):
                marker.unlink()
            fcntl.flock(lock, fcntl.LOCK_UN)


def show(request: dict[str, Any], env: dict[str, str] | None = None) -> Outcome:
    """Show one card and return your answer. Blocks until you answer or it times out."""
    if qml_program() is None:
        return Outcome("failed", "Qt's qml program was not found, so no card could be shown")
    if not request.get("primary"):
        request = {**request, "primary": primary_output(env)}
    if "motion" not in request:
        from kwin_mcp.warden.daemon import motion_factor

        request = {**request, "motion": motion_factor()}
    run_env = dict(env if env is not None else os.environ)
    # The card is the warden's, not the session's: keep the session-window hook off it.
    run_env.pop("CLAUDE_CODE_SESSION_ID", None)
    # Qt sends its messages to the journal when stderr isn't a terminal; keep them here.
    run_env["QT_FORCE_STDERR_LOGGING"] = "1"
    limit = float(request.get("timeout_s", 120)) + 15
    geometry = state.runtime_dir() / "card-geometry.json"
    with _one_card_at_a_time():
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                command_line(request),
                env=run_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            return Outcome("failed", f"the card could not start: {exc}")
        tail: list[str] = []
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                if "WARDEN-CARD-GEOMETRY " in line:
                    with contextlib.suppress(OSError):
                        geometry.write_text(line.split("WARDEN-CARD-GEOMETRY ", 1)[1].strip())
                else:
                    tail = [*tail[-2:], line.strip()]
                if time.monotonic() - started > limit:
                    break
            proc.wait(timeout=max(1.0, limit - (time.monotonic() - started)))
        except subprocess.TimeoutExpired:
            proc.kill()
            return Outcome("timeout", "the card did not close in time")
        finally:
            with contextlib.suppress(FileNotFoundError):
                geometry.unlink()
    code = proc.returncode
    if code == ALLOW_CODE:
        return Outcome("allow")
    if code == GO_CODE:
        return Outcome("go")
    if code == TIMEOUT_CODE:
        return Outcome("timeout")
    if code == DENY_CODE:
        return Outcome("deny")
    took = time.monotonic() - started
    return Outcome(
        "failed", f"the card ended with code {code} after {took:.1f} s: {' | '.join(tail)}"
    )
