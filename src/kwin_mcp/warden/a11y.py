"""The desktop's accessibility switch, turned on only from a card and always turned back off.

Accessibility is the channel screen readers use: with it on, apps describe their windows
and text fields over AT-SPI, so Claude can read and write a text field without the
person's keyboard focus (``keyboard_type(into=...)``). On a live desktop it is one switch
for the whole session (``org.a11y.Status.IsEnabled``), and at-spi2-core saves it to
gsettings (``toolkit-accessibility``), so it would stay on at the next login.

The warden turns it on only after the person answers "Turn On While Claude Works"
(design/mock/index.html, "Accessibility"), records that it did in
``a11y-ours`` beside the log (so a crash or a reboot can't lose the record), and the
daemon turns it back off when the last grant ends, on the stop key, from the access list,
or the next time it starts. If the daemon and every session die without doing it, a small
guard started with the switch turns it off (``guard``), and the login after a reboot
does (``after_login``). A switch that was already on (a screen reader, or the
person's own choice) is never touched.

Measured in a nested KWin (Qt 6.11, 2026-11-14): a Qt app that's already open joins the
moment the switch turns on; turning it off again stops new apps from joining, but apps
that joined stay readable until they restart.
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from kwin_mcp.session import (
    _A11Y_STATUS_INTERFACE,
    AccessibilityError,
    _a11y_dbus_send,
    accessibility_enabled,
    set_accessibility_enabled,
)
from kwin_mcp.warden import log


def _bus(dbus_address: str = "") -> str:
    return dbus_address or os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")


def _marker() -> Path:
    return log.log_path().parent / "a11y-ours"


def _screen_reader(dbus_address: str) -> bool:
    try:
        out = _a11y_dbus_send(
            dbus_address,
            [
                "org.freedesktop.DBus.Properties.Get",
                f"string:{_A11Y_STATUS_INTERFACE}",
                "string:ScreenReaderEnabled",
            ],
        )
    except AccessibilityError:
        return False
    return out.decode(errors="replace").split()[-2:] == ["boolean", "true"]


def is_on(dbus_address: str = "") -> bool:
    """Whether apps on this desktop describe themselves: the switch, or a screen reader."""
    bus = _bus(dbus_address)
    try:
        return accessibility_enabled(bus) or _screen_reader(bus)
    except AccessibilityError:
        return False


def _bus_alive(address: str) -> bool:
    """Whether a bus named by its address could still be running: its socket file exists.
    An address without a path (abstract, tcp) can't be checked and counts as gone."""
    for part in address.split(";")[0].split(":", 1)[-1].split(","):
        if part.startswith("path="):
            return os.path.exists(part[5:])
    return False


def ours(dbus_address: str = "") -> bool:
    """Whether the warden turned the switch on for this desktop (and must turn it off): on
    this bus, or on a bus that's gone (a login before a crash or a reboot; the desktop
    saved the switch and turned it back on at this login)."""
    try:
        marked = _marker().read_text(encoding="utf-8").strip()
    except OSError:
        return False
    bus = _bus(dbus_address)
    return marked == bus or (bool(marked) and not _bus_alive(marked))


def turn_on(dbus_address: str = "") -> None:
    """Turn the switch on and record that the warden did. Raises AccessibilityError."""
    bus = _bus(dbus_address)
    marker = _marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    # The record first: a Set whose reply was lost may still have applied, and turning
    # off a switch that never came on is harmless (the core's registry does the same).
    marker.write_text(bus, encoding="utf-8")
    set_accessibility_enabled(bus, True)


def turn_off_if_ours(dbus_address: str = "") -> bool:
    """Turn the switch back off if the warden turned it on here. Returns whether it did."""
    bus = _bus(dbus_address)
    if not ours(bus):
        return False
    try:
        set_accessibility_enabled(bus, False)
    except AccessibilityError:
        return False  # the record stays; the next tick or the next start tries again
    with contextlib.suppress(OSError):
        _marker().unlink()
    return True


def wait_until_on(dbus_address: str = "", timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_on(dbus_address):
            return True
        time.sleep(0.1)
    return is_on(dbus_address)


def _daemon_running() -> bool:
    from kwin_mcp.warden import state

    try:
        pid = int((state.runtime_dir() / "daemon.pid").read_text().strip())
        return b"kwin_mcp.warden.daemon" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError, RuntimeError):
        return False


GUARD_TICK_S = 1.0


def _guard_lock() -> Path:
    from kwin_mcp.warden import state

    return state.runtime_dir() / "a11y-guard.lock"


def _guard_running() -> bool:
    """Whether a guard holds its lock (one guard at a time, however many start)."""
    import fcntl

    try:
        with open(_guard_lock(), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except (OSError, RuntimeError):
        return False


def start_guard(dbus_address: str = "") -> None:
    """Start the guard that turns the switch off if the warden dies without doing it.
    Called when the switch turns on, and by the daemon whenever the switch is the
    warden's, so a guard that died comes back."""
    import subprocess
    import sys

    if _guard_running():
        return
    env = dict(os.environ)
    env["DBUS_SESSION_BUS_ADDRESS"] = _bus(dbus_address)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    with contextlib.suppress(OSError):
        subprocess.Popen(
            [sys.executable, "-m", "kwin_mcp.warden.a11y", "--guard"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def _grants_held() -> bool:
    from kwin_mcp.warden import state

    return any(g.get("kind") in ("app", "full_desktop") for g in state.all_grants())


def guard() -> str:
    """While the switch is the warden's: once neither the daemon runs nor any live session
    holds a grant (the warden crashed, or was killed, before it could turn the switch off),
    turn it off. A running daemon owns the switch, and a session still holding a grant
    brings its daemon back, so the guard waits for both. It ends when the switch isn't the
    warden's any more, or when its desktop's bus or runtime folder is gone (a logout; the
    login after it turns the switch off). One guard at a time: it holds a lock."""
    import fcntl

    bus = _bus()
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    try:
        lock = open(_guard_lock(), "a")  # noqa: SIM115 - held for the guard's life
    except (OSError, RuntimeError):
        return "no runtime folder"
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return "another guard runs"
    try:
        missing = 0
        while True:
            time.sleep(GUARD_TICK_S)
            try:
                # Looked at, never created: a logout removes the runtime folder.
                if not runtime or not os.path.isdir(runtime) or not _bus_alive(bus):
                    return "nothing left to guard"
                if not ours(bus):
                    return "the switch isn't the warden's"
                if _daemon_running() or _grants_held():
                    missing = 0
                    continue
                missing += 1
                if missing < 2:  # seen twice: not a daemon between a crash and its restart
                    continue
                if turn_off_if_ours(bus):
                    log.write("warden", "accessibility turned off", "the warden stopped without it")
                    return "turned accessibility back off"
            except (OSError, RuntimeError):
                return "its runtime folder went away"
    finally:
        lock.close()


def after_login() -> str:
    """At login (the autostart entry install.sh writes): a switch the warden left on before a
    crash or a reboot goes back off. A running daemon owns the switch, so it's left alone."""
    if _daemon_running():
        return "the warden is running; it turns the switch off itself"
    if not ours():
        return "nothing to do"
    if turn_off_if_ours():
        log.write("warden", "accessibility turned off", "left on before a restart")
        return "turned accessibility back off"
    return "couldn't turn accessibility off; the warden tries again when it starts"


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["--guard"]:
        print(guard())
    elif sys.argv[1:] == ["--after-login"]:
        print(after_login())
    else:
        sys.exit("usage: python -m kwin_mcp.warden.a11y --after-login | --guard")
