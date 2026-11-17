"""The warden's daemon: the stop key, your hands, the frame and the access list.

One per desktop, started by the first session that gets a grant, gone a
couple of minutes after the last grant and the last session end.

- Your hands: reads every real input device under /dev/input (keyboards, mice,
  touchpads), never the virtual ones (uinput), and notes only *that* you used
  one, never which key: ``hands`` for any use (a nudged mouse too), ``presses``
  for a key press, a button or a touch on a screen. Claude's typing waits only for
  presses; Claude's pointer waits for any use, since you share that pointer.
  Claude's own input reaches KWin through KWin's EIS connection and never
  appears under /dev/input, so the two can't be confused (KWin's EIS backend
  creates its devices inside the compositor:
  src/plugins/eis/eisdevice.cpp in KWin 6.7).
- The stop key: registers Meta+Alt+Esc with KDE's shortcut service (kglobalaccel),
  which KWin consumes, so no window ever sees it. Changeable in System Settings.
- The frame, the tag and the access list: ``ui/overlay.qml``, fed through
  ``overlay.json`` in the runtime directory, answering on its stderr.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import select
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from kwin_mcp.warden import a11y, apps, lengths, log, state
from kwin_mcp.warden.policy import LEVEL_NAMES, covers
from kwin_mcp.warden.settings import load

UI = Path(__file__).resolve().parent / "ui"
IDLE_EXIT_S = 120
EVENT = struct.Struct("llHHi")  # struct input_event on 64-bit Linux
EV_KEY, EV_REL, EV_ABS = 1, 2, 3
BTN_TOUCH = 0x14A
# A pen near a tablet or a finger resting on a touchpad: there, not a press.
BTN_TOOLS = frozenset({*range(0x140, 0x149), 0x14D, 0x14E, 0x14F})
BTN_TOOL_FINGER = 0x145
INPUT_PROP_POINTER = 0
COMPONENT = "kwin-mcp-warden"
STOP_ACTION = [COMPONENT, "stop", "kwin-mcp-warden", "End every grant Claude holds"]


# ── Your hands ────────────────────────────────────────────────────────────────


def real_input_devices() -> list[str]:
    """Event devices backed by hardware: /sys says they aren't under /devices/virtual.

    ``KWIN_MCP_WARDEN_INPUT_DEVICES`` (paths joined by ':') replaces the list; the
    warden's own tests use it to stand a pipe in for your mouse.
    """
    override = os.environ.get("KWIN_MCP_WARDEN_INPUT_DEVICES")
    if override is not None:
        return [p for p in override.split(":") if p]
    out = []
    for dev in sorted(Path("/dev/input").glob("event*")):
        sys_path = Path("/sys/class/input") / dev.name / "device"
        with contextlib.suppress(OSError):
            if "/devices/virtual/" in os.path.realpath(sys_path):
                continue
            out.append(str(dev))
    return out


def _bits(words: str) -> set[int]:
    """The bit numbers set in a /proc/bus/input/devices bitmap (hex longs, high first)."""
    out: set[int] = set()
    for i, word in enumerate(reversed(words.split())):
        with contextlib.suppress(ValueError):
            value = int(word, 16)
            out.update(i * 64 + b for b in range(64) if value >> b & 1)
    return out


def _device_kinds() -> dict[str, set[str]]:
    """Which event devices are keyboards, pointers and touchpads (/proc/bus/input/devices)."""
    kinds: dict[str, set[str]] = {}
    try:
        text = Path("/proc/bus/input/devices").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return kinds
    for block in text.split("\n\n"):
        lines = block.splitlines()
        handlers = next((ln for ln in lines if ln.startswith("H: Handlers=")), "")
        names = handlers.split("=", 1)[1].split() if handlers else []
        event = next((n for n in names if n.startswith("event")), None)
        if event is None:
            continue
        found = set()
        if "kbd" in names:
            found.add("keyboard")
        if any(n.startswith("mouse") for n in names):
            found.add("pointer")
        prop = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("B: PROP=")), "")
        keys = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("B: KEY=")), "")
        if INPUT_PROP_POINTER in _bits(prop) and BTN_TOOL_FINGER in _bits(keys):
            found.add("touchpad")
        kinds[f"/dev/input/{event}"] = found
    return kinds


def is_press(kind: int, code: int, value: int, touchpad: bool = False) -> bool:
    """A key or button going down, or a touch on a screen or a pen tip on a tablet.

    Not a press: moving the mouse, scrolling, letting go, a key repeating, a pen
    hovering, or a finger landing on a touchpad to move the pointer (a touchpad's
    physical click is a button, so it counts; a tap-to-click is only known once the
    finger lifts, so ``Tap`` tells it apart).
    """
    if kind != EV_KEY or value != 1 or code in BTN_TOOLS:
        return False
    return not (code == BTN_TOUCH and touchpad)


ABS_X, ABS_Y = 0, 1
# libinput's tap-to-click (src/evdev-mt-touchpad-tap.c): a finger that lifts within
# DEFAULT_TAP_TIMEOUT_PERIOD, 180 ms, having moved under DEFAULT_TAP_MOVE_THRESHOLD, 1.3 mm.
TAP_S = 0.18
TAP_MOVE_MM = 1.3


def _units_per_mm(fd: int) -> float:
    """ABS_X's resolution (units per mm) from EVIOCGABS; 0 when the device won't say."""
    size = struct.calcsize("6i")  # struct input_absinfo
    request = (2 << 30) | (size << 16) | (ord("E") << 8) | (0x40 + ABS_X)
    try:
        info = fcntl.ioctl(fd, request, bytes(size))
    except OSError:
        return 0.0
    return float(struct.unpack("6i", info)[5])


class Tap:
    """Tells a touchpad tap-to-click from a finger landing to move the pointer.

    The kernel only reports the finger; libinput makes the click when it lifts within
    180 ms without moving 1.3 mm. ``feed`` says True at that lift. Without a known
    resolution, only the time counts: a quick flick then counts too, which only ever
    pauses typing a moment longer, never lets it through a tap.
    """

    def __init__(self, units_per_mm: float = 0.0) -> None:
        self.reach = TAP_MOVE_MM * units_per_mm
        self.down_at: float | None = None
        self.start: list[int | None] = [None, None]
        self.moved = False

    def feed(self, sec: int, usec: int, kind: int, code: int, value: int) -> bool:
        at = sec + usec / 1e6
        if kind == EV_KEY and code == BTN_TOUCH:
            if value == 1:
                self.down_at, self.start, self.moved = at, [None, None], False
                return False
            if value == 0 and self.down_at is not None:
                tapped = at - self.down_at <= TAP_S and not self.moved
                self.down_at = None
                return tapped
        if kind == EV_ABS and code in (ABS_X, ABS_Y) and self.down_at is not None:
            first = self.start[code]
            if first is None:
                self.start[code] = value
            elif self.reach and abs(value - first) > self.reach:
                self.moved = True
        return False


def watch_hands(stop: threading.Event, devices_fn=real_input_devices) -> None:
    """Touch ``hands`` whenever you use a real input device and ``presses`` when you press
    something; ``hands-watch`` says it runs."""
    fds: dict[int, str] = {}
    touchpads: set[str] = set()
    taps: dict[int, Tap] = {}
    last_scan = 0.0
    last_touch = 0.0
    last_press = 0.0
    watching = False
    while not stop.is_set():
        now = time.monotonic()
        if now - last_scan > 5:
            last_scan = now
            wanted = set(devices_fn())
            for fd, path in list(fds.items()):
                if path not in wanted:
                    os.close(fd)
                    del fds[fd]
                    taps.pop(fd, None)
            have = set(fds.values())
            for path in wanted - have:
                with contextlib.suppress(OSError):
                    fds[os.open(path, os.O_RDONLY | os.O_NONBLOCK)] = path
            # The watch counts only when it can see a keyboard and a pointer: a game
            # controller alone (which systemd lets any user read) isn't your hands.
            kinds = _device_kinds()
            touchpads = {p for p, k in kinds.items() if "touchpad" in k}
            for fd, path in fds.items():
                if path in touchpads and fd not in taps:
                    taps[fd] = Tap(_units_per_mm(fd))
            if os.environ.get("KWIN_MCP_WARDEN_INPUT_DEVICES") is not None:
                watching = bool(fds)
            else:
                seen = set().union(*(kinds.get(p, set()) for p in fds.values())) if fds else set()
                watching = {"keyboard", "pointer"} <= seen
        if fds and watching and now - last_scan < 0.01:
            state.touch_hands("hands-watch")
        if not fds:
            stop.wait(1.0)
            continue
        ready, _, _ = select.select(list(fds), [], [], 1.0)
        if fds and watching and time.monotonic() - last_scan > 2:
            state.touch_hands("hands-watch")
        for fd in ready:
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                continue
            except OSError:
                os.close(fd)
                fds.pop(fd, None)
                taps.pop(fd, None)
                continue
            if not data:  # a pipe with no writer; look again at the next scan
                os.close(fd)
                fds.pop(fd, None)
                taps.pop(fd, None)
                continue
            touchpad = fds.get(fd) in touchpads
            tap = taps.get(fd) if touchpad else None
            used = pressed = False
            for i in range(0, len(data) - EVENT.size + 1, EVENT.size):
                sec, usec, kind, code, value = EVENT.unpack_from(data, i)
                used = used or kind in (EV_KEY, EV_REL, EV_ABS)
                pressed = pressed or is_press(kind, code, value, touchpad)
                if tap is not None and tap.feed(sec, usec, kind, code, value):
                    pressed = True
            now = time.monotonic()
            if pressed and now - last_press > 0.05:
                last_press = now
                state.touch_hands("presses")
            if used and now - last_touch > 0.05:
                last_touch = now
                state.touch_hands("hands")
    for fd in fds:
        with contextlib.suppress(OSError):
            os.close(fd)


# ── The stop key ──────────────────────────────────────────────────────────────


def stop_key_sequence(text: str) -> int:
    """'Meta+Alt+Esc' as the int QKeySequence uses (modifier bits | key); ValueError if unknown."""
    mods = {
        "meta": 0x10000000,
        "super": 0x10000000,
        "ctrl": 0x04000000,
        "control": 0x04000000,
        "alt": 0x08000000,
        "shift": 0x02000000,
    }
    keys = {
        "esc": 0x01000000,
        "escape": 0x01000000,
        "tab": 0x01000001,
        "backspace": 0x01000003,
        "return": 0x01000004,
        "enter": 0x01000005,
        "ins": 0x01000006,
        "insert": 0x01000006,
        "del": 0x01000007,
        "delete": 0x01000007,
        "pause": 0x01000008,
        "print": 0x01000009,
        "home": 0x01000010,
        "end": 0x01000011,
        "left": 0x01000012,
        "up": 0x01000013,
        "right": 0x01000014,
        "down": 0x01000015,
        "pgup": 0x01000016,
        "pageup": 0x01000016,
        "pgdown": 0x01000017,
        "pagedown": 0x01000017,
        "space": 0x20,
    }
    keys.update({f"f{n}": 0x01000030 + n - 1 for n in range(1, 36)})
    value, key = 0, None
    for part in text.lower().split("+"):
        part = part.strip()
        if part in mods:
            value |= mods[part]
        elif part in keys:
            key = keys[part]
        elif len(part) == 1 and part.isprintable():
            key = ord(part.upper())
        else:
            raise ValueError(f"stop_key {text!r}: unknown key {part!r}")
    if key is None or not value:
        raise ValueError(f"stop_key {text!r} needs at least one modifier and one key")
    return value | key


def session_bus() -> tuple[Any, Any, Any]:
    """The daemon's session bus connection, its well-known name and a GLib main loop."""
    import dbus
    import dbus.service
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib

    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    # Owning a name lets kglobalaccel see the component as present, and lets the stack
    # watcher's KWin script reach the daemon.
    name = dbus.service.BusName("org.kde.kwin_mcp_warden", bus)
    return bus, name, GLib.MainLoop()


def register_stop_key(sequence: str, on_stop, bus: Any = None) -> None:
    """Register the stop key with kglobalaccel; raises when another app holds the key."""
    import dbus

    if bus is None:
        bus = session_bus()[0]
    kga = dbus.Interface(
        bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"), "org.kde.KGlobalAccel"
    )
    action = dbus.Array(STOP_ACTION, signature="s")
    key = dbus.Array([stop_key_sequence(sequence), 0, 0, 0], signature="i")
    # Never fight another app for its key: on stock Plasma, Meta+Esc is System Monitor's
    # (X-KDE-Shortcuts=Meta+Esc in org.kde.plasma-systemmonitor.desktop), and kglobalaccel
    # would hand the press to whichever shortcut it finds first.
    holders = kga.globalShortcutsByKey(
        dbus.Struct((key,), signature="ai"), dbus.Struct((0,), signature="i")
    )
    others = [
        f"{h[3]} ({h[2]})" for h in holders if str(h[0]) != COMPONENT and str(h[2]) != COMPONENT
    ]
    if others:
        msg = f"{sequence} already belongs to {', '.join(others)}; pick a free stop_key in settings"
        raise RuntimeError(msg)
    kga.doRegister(action)
    keys = dbus.Array([dbus.Struct((key,), signature="ai")], signature="(ai)")
    # Flags: SetPresent (2) | IsDefault (1). The person's own key, if they changed it, wins.
    kga.setShortcutKeys(action, keys, dbus.UInt32(3))
    kga.setShortcutKeys(action, keys, dbus.UInt32(2))
    path = kga.getComponent(COMPONENT)

    def pressed(component: str, shortcut: str, _ts: int) -> None:
        if component == COMPONENT and shortcut == "stop":
            on_stop()

    bus.add_signal_receiver(
        pressed,
        signal_name="globalShortcutPressed",
        dbus_interface="org.kde.kglobalaccel.Component",
        path=path,
    )


# ── The overlay ───────────────────────────────────────────────────────────────


def _qml() -> str | None:
    from kwin_mcp.warden.card import qml_program

    return qml_program()


def _die_with_parent() -> None:
    """In the overlay's process before it starts: end it when the daemon dies, however the
    daemon dies, so a restarted daemon never finds a second overlay still drawing."""
    import ctypes
    import signal

    pr_set_pdeathsig = 1
    ctypes.CDLL(None, use_errno=True).prctl(pr_set_pdeathsig, signal.SIGTERM, 0, 0, 0)


class Overlay:
    def __init__(self, part: str) -> None:
        self.part = part  # "frames" (click-through) or "controls" (tags, tray, list)
        self.proc: subprocess.Popen[str] | None = None
        self.path = state.runtime_dir() / "overlay.json"

    def start(self) -> None:
        program = _qml()
        if program is None or (self.proc and self.proc.poll() is None):
            return
        env = dict(os.environ)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["QT_FORCE_STDERR_LOGGING"] = "1"
        env["QML_XHR_ALLOW_FILE_READ"] = "1"
        self.proc = subprocess.Popen(
            [
                program,
                "--apptype",
                "widget",
                str(UI / "overlay.qml"),
                "--",
                self.part,
                str(self.path),
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=_die_with_parent,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        rings = 0
        for line in proc.stderr:
            if "WARDEN-NOTICE-RING " in line:
                # How many rings the notice drew, and for which action, for the warden's tests.
                rings += 1
                with contextlib.suppress(OSError):
                    (state.runtime_dir() / "notice-rings.json").write_text(
                        json.dumps(
                            {
                                "rings": rings,
                                "acted": line.split("WARDEN-NOTICE-RING ", 1)[1].strip(),
                            }
                        ),
                        encoding="utf-8",
                    )
                continue
            if "WARDEN-NOTICE-GEOMETRY " in line:
                with contextlib.suppress(OSError):
                    (state.runtime_dir() / "notice-geometry.json").write_text(
                        line.split("WARDEN-NOTICE-GEOMETRY ", 1)[1].strip(), encoding="utf-8"
                    )
                continue
            if "WARDEN:" not in line:
                continue
            command = line.split("WARDEN:", 1)[1].strip()
            if command == "STOP":
                end_everything("the Stop button")
            elif command.startswith("END "):
                grant_id = command[4:].strip()
                state.end_grant(grant_id)
                log.write("you", "ended", f"grant {grant_id} from the access list")
            elif command == "A11Y-OFF":
                if a11y.turn_off_if_ours():
                    log.write("you", "accessibility turned off", "from the access list")
            elif command == "LOG":
                with contextlib.suppress(OSError):
                    subprocess.Popen(
                        ["xdg-open", str(log.log_path())],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )

    def write(self, payload: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(self.path)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(3)


def end_everything(how: str) -> None:
    state.press_stop()
    log.write("you", "ended all", f"every grant ({how})")


class StackWatch:
    """The window stack, pushed by ui/stack-watch.js whenever it changes (see that file)."""

    SCRIPT_NAME = "kwin-mcp-warden-stack"

    def __init__(self) -> None:
        self.latest: list[dict[str, Any]] | None = None
        self.at = 0.0
        self.loaded = False

    def export(self, bus: Any) -> None:
        import dbus.service

        watch = self

        class _Stack(dbus.service.Object):
            @dbus.service.method("org.kde.kwin_mcp_warden", in_signature="s", out_signature="")
            def Report(self, payload: str) -> None:  # noqa: N802 - D-Bus method name
                with contextlib.suppress(ValueError):
                    data = json.loads(str(payload))
                    if isinstance(data, list):
                        watch.latest, watch.at = data, time.time()

        self._object = _Stack(bus, "/Stack")

    def load(self, bus: Any) -> None:
        import dbus

        source = (UI / "stack-watch.js").read_text(encoding="utf-8")
        path = state.runtime_dir() / "stack-watch.js"
        path.write_text(source.replace("__SERVICE__", "org.kde.kwin_mcp_warden"), encoding="utf-8")
        scripting = dbus.Interface(
            bus.get_object("org.kde.KWin", "/Scripting"), "org.kde.kwin.Scripting"
        )
        with contextlib.suppress(dbus.DBusException):
            scripting.unloadScript(self.SCRIPT_NAME, signature="s")
        scripting.loadScript(
            str(path), self.SCRIPT_NAME, signature="ss"
        )  # the two-argument overload
        scripting.start()
        self.loaded = True

    def unload(self) -> None:
        if not self.loaded:
            return
        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.kde.KWin",
                    "--object-path",
                    "/Scripting",
                    "--method",
                    "org.kde.kwin.Scripting.unloadScript",
                    self.SCRIPT_NAME,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )

    def fresh(self) -> list[dict[str, Any]] | None:
        # The script sends on every change, so a quiet desktop keeps its last report.
        return self.latest if self.loaded else None


STACK = StackWatch()


def _stack() -> list[dict[str, Any]]:
    pushed = STACK.fresh()
    if pushed is not None:
        return pushed
    try:
        out = subprocess.run(
            [sys.executable, "-m", "kwin_mcp.geometry"],
            input='{"op": "stack"}',
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        resp = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    return resp.get("result", []) if resp.get("ok") else []


def _mtime(name: str) -> float:
    try:
        return (state.runtime_dir() / name).stat().st_mtime
    except FileNotFoundError:
        return 0.0


_PRIMARY: list[Any] = ["", 0.0]


def _primary() -> str:
    """The primary monitor's name from KDE, read at most once a minute."""
    if time.time() - _PRIMARY[1] > 60:
        from kwin_mcp.warden.card import primary_output

        _PRIMARY[0], _PRIMARY[1] = primary_output(), time.time()
    return str(_PRIMARY[0])


def motion_factor() -> float:
    """Plasma's animation speed (System Settings, Animation speed): kdeglobals [KDE]
    AnimationDurationFactor, 1 when unset; 0 means animations are off and nothing moves."""
    import configparser

    home = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read(os.path.join(home, "kdeglobals"), encoding="utf-8")
        factor = float(parser.get("KDE", "AnimationDurationFactor", fallback="1"))
    except (configparser.Error, ValueError, OSError):
        return 1.0
    return max(0.0, min(10.0, factor))


def names(agents: list[str]) -> str:
    """ "Claude", "Claude and OpenCode", "Claude, Codex and OpenCode"; first seen first."""
    unique = list(dict.fromkeys(a for a in agents if a))
    if len(unique) < 3:
        return " and ".join(unique)
    return ", ".join(unique[:-1]) + " and " + unique[-1]


def _lineage_ids(window: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> set[str]:
    """The window's id and the ids of the windows it's transient for, up (as the gate
    traces a dialog or menu to its window)."""
    ids = [str(window.get("id"))]
    for _ in range(8):
        parent = by_id.get(str(by_id.get(ids[-1], window).get("parent") or ""))
        if parent is None or str(parent.get("id")) in ids:
            break
        ids.append(str(parent.get("id")))
    return set(ids)


def overlay_state(settings: Any) -> dict[str, Any]:
    """What the overlay draws now: grants, frames, tags, the pointer label.

    Every name on it is the agent that holds the grant (each server's record says which),
    so two agents at once each get their own.
    """
    now = time.time()
    grants = state.all_grants()
    windowed = [g for g in grants if g["kind"] == "window"]
    held = [
        g
        for g in grants
        if g["kind"] == "full_desktop"
        or (g["kind"] == "window" and covers(g.get("level", "see"), "control"))
    ]
    stack = _stack() if windowed or held else []
    if stack and windowed:
        # A window's grant ends when the window closes: a window that opens later under
        # the same app is a new one, and the person is asked about it.
        open_ids = {str(w.get("id")) for w in stack}
        for g in windowed:
            if str(g.get("window") or "") not in open_ids:
                state.end_grant(str(g["id"]))
                log.write("warden", "ended", f"a {g.get('app_name') or g['app']} window closed")
        grants = [g for g in grants if not state.ended(str(g["id"]))]
        held = [g for g in held if not state.ended(str(g["id"]))]
    agents = list(dict.fromkeys(str(g.get("agent") or "Claude") for g in grants))
    rows = []
    for g in grants:
        icon = "desk" if g["kind"] != "window" else apps.describe(g["app"])[1]
        app_name = g.get("app_name") or g.get("app") or ""
        title = str(g.get("window_title") or "")
        rows.append(
            {
                "id": g["id"],
                "session": g.get("session", ""),
                "agent": g.get("agent") or "Claude",
                # The row says whose grant it is once more than one agent holds one.
                "who": (
                    f"{g.get('agent') or 'Claude'} · {g.get('session', '')}"
                    if len(agents) > 1
                    else g.get("session", "")
                ),
                "icon": icon,
                "app": app_name,
                # The window's title, so you can tell which window of the app it is.
                "label": f"{app_name} · {title}" if title else app_name,
                "kind": g["kind"],
                "level": g.get("level", ""),
                "levelName": LEVEL_NAMES.get(g.get("level", ""), "Own screen"),
                "minutes": max(0, int((g["expires"] - now) // 60)),
                "left": lengths.left(g["expires"] - now),
            }
        )
    frames = []
    if held:
        by_id = {str(w.get("id")): w for w in stack}
        for g in held:
            for w in stack:
                if not w.get("visible") or not w.get("managed"):
                    continue
                if g["kind"] == "full_desktop":
                    if not w.get("active"):
                        continue
                elif str(g.get("window") or "") not in _lineage_ids(w, by_id):
                    continue
                frames.append(
                    {
                        "id": w["id"],
                        "frame": w["frame"],
                        "app": g.get("app_name") or w.get("app"),
                        "session": g.get("session", ""),
                        "agent": g.get("agent") or "Claude",
                        "minutes": max(0, int((g["expires"] - now) // 60)),
                        "left": lengths.left(g["expires"] - now),
                    }
                )
    # The corner notice: every grant that lets Claude click or type on your screen.
    control = [
        g
        for g in rows
        if g["kind"] == "full_desktop"
        or (g["kind"] == "window" and covers(str(g.get("level", "see")), "control"))
    ]
    sessions = sorted({str(g["session"]) for g in control})
    apps_held = list(
        dict.fromkeys(
            "your whole desktop" if g["kind"] == "full_desktop" else str(g["app"]) for g in control
        )
    )
    notice = None
    if control:
        in_control = list(dict.fromkeys(str(g["agent"]) for g in control))
        notice = {
            "who": names(in_control),
            "plural": len(in_control) > 1,
            "sessions": len(sessions),
            "line": (
                f"{len(sessions)} sessions · {', '.join(apps_held)}"
                if len(sessions) > 1
                else f"{sessions[0]} · {', '.join(apps_held)} · "
                f"{min(control, key=lambda g: g['minutes'])['left']}"
            ),
        }
    paused = now - _mtime("paused") < 0.6
    why = ""
    with contextlib.suppress(OSError):
        why = (state.runtime_dir() / "paused").read_text(encoding="utf-8").strip()
    # What Claude waits for: any use of the mouse (its pointer is yours) or only a press.
    hands_ago = now - (state.last_press() if why == "presses" else state.last_hands())
    pointer: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        pointer = json.loads((state.runtime_dir() / "pointer").read_text())
    return {
        "grants": rows,
        "who": names(agents),
        "plural": len(agents) > 1,
        "listTitle": (
            f"{agents[0]}'s access"
            if len(agents) == 1
            else "Agents' access"
            if agents
            else "Access"
        ),
        "frames": frames,
        "notice": notice,
        "primary": _primary(),
        "paused": paused,
        "resumeIn": max(0.0, settings.pause_seconds - hands_ago) if paused else 0.0,
        "handsAgo": hands_ago,
        "pausedFor": why or "pointer",
        "pointer": pointer if now - float(pointer.get("t", 0)) < 1.5 else None,
        "stopKey": settings.stop_key,
        "motion": motion_factor(),
        "log": log.tail(4),
        "now": now,
        "showList": bool(os.environ.get("KWIN_MCP_WARDEN_SHOW_LIST")),
    }


# ── Main ──────────────────────────────────────────────────────────────────────


def _runtime_base() -> str:
    return os.environ.get("XDG_RUNTIME_DIR", "")


def _runtime_gone() -> bool:
    """The desktop this daemon serves has ended: its runtime folder is gone (a logout, or a
    private test desktop torn down). Nothing is left to guard, and staying would only
    recreate the folder (2026-11-14: 16 test daemons outlived their runs)."""
    base = _runtime_base()
    return bool(base) and not os.path.isdir(base)


def _servers_alive() -> bool:
    """Whether a kwin-mcp-warden server for this daemon's desktop is still running: one that
    shares its runtime folder. A server for another desktop (another user's session, a
    private test desktop, the person's own desktop seen from a test) doesn't count."""
    mine = _runtime_base().encode()
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        with contextlib.suppress(OSError):
            args = (proc / "cmdline").read_bytes().split(b"\0")
            names = {os.path.basename(a) for a in args[:3]}
            if not (
                b"kwin-mcp-warden" in names
                or b"kwin-mcp-warden-cli" in names
                or (b"kwin_mcp.server" in args or b"kwin_mcp" in args)
            ):
                continue
            env = (proc / "environ").read_bytes().split(b"\0")
            theirs = next((e[16:] for e in env if e.startswith(b"XDG_RUNTIME_DIR=")), b"")
            if theirs == mine:
                return True
    return False


def main() -> None:
    lock = open(state.runtime_dir() / "daemon.lock", "a+")  # noqa: SIM115
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    (state.runtime_dir() / "daemon.pid").write_text(str(os.getpid()))
    settings = load()
    stop = threading.Event()
    stop_key_ok = threading.Event()
    stop_key_ok.set()
    threading.Thread(target=watch_hands, args=(stop,), daemon=True).start()
    overlays = [Overlay("frames"), Overlay("controls")]

    def tick() -> None:
        idle_since = time.monotonic()
        while not stop.is_set():
            if _runtime_gone():
                stop.set()
                break
            payload = overlay_state(settings)
            # The tag and the list name the key only when it really works.
            payload["stopKey"] = settings.stop_key if stop_key_ok.is_set() else ""
            # The accessibility switch the warden turned on goes back off with the last grant
            # (the stop key, the access list, expiry, a session ending; also after a crash,
            # the next time the daemon starts).
            holding = [g for g in payload["grants"] if g.get("kind") in ("window", "full_desktop")]
            if not holding and a11y.ours() and a11y.turn_off_if_ours():
                log.write("warden", "accessibility turned off", "the last grant ended")
            payload["a11y"] = {"ours": a11y.ours()}
            if payload["a11y"]["ours"]:
                a11y.start_guard()  # back if it died; one at a time (its lock)
            if payload["grants"]:
                idle_since = time.monotonic()
                for overlay in overlays:
                    overlay.start()
            elif _servers_alive():
                idle_since = min(idle_since, time.monotonic())
                if time.monotonic() - idle_since > IDLE_EXIT_S * 5:
                    stop.set()
            elif time.monotonic() - idle_since > IDLE_EXIT_S:
                stop.set()
            with contextlib.suppress(OSError):
                overlays[0].write(payload)
            stop.wait(0.25 if payload["frames"] or payload["paused"] or payload["notice"] else 0.5)

    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    loop = None
    try:
        bus, _name, loop = session_bus()
    except Exception as exc:
        log.write("warden", "session bus", f"not reached: {exc}")
        bus = None
        stop_key_ok.clear()
    if bus is not None:
        try:
            register_stop_key(settings.stop_key, lambda: end_everything(settings.stop_key), bus)
        except Exception as exc:
            log.write("warden", "stop key", f"not registered: {exc}")
            stop_key_ok.clear()
        try:
            STACK.export(bus)
            STACK.load(bus)
        except Exception as exc:
            log.write("warden", "stack watch", f"not loaded, polling instead: {exc}")
    try:
        if loop is not None:
            from gi.repository import GLib

            GLib.timeout_add(500, lambda: loop.quit() if stop.is_set() else True)
            loop.run()
        else:
            while not stop.is_set():
                stop.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for overlay in overlays:
            overlay.stop()
        STACK.unload()
        with contextlib.suppress(OSError):
            (state.runtime_dir() / "daemon.pid").unlink()


if __name__ == "__main__":
    main()
