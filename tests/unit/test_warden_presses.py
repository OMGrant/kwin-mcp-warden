"""Your hands: a nudged mouse is use, not a press.

The daemon writes ``hands`` for any use of a real input device and ``presses`` only for
a key, a button or a touch on a screen. Claude's typing waits for presses; Claude's
pointer waits for any use. Plant: make ``is_press`` always False and the watcher test
fails (the press never reaches ``presses``). A touchpad's tap-to-click counts once the
finger lifts, by libinput's rules (``daemon.Tap``).
"""

from __future__ import annotations

import os
import struct
import threading
import time

import pytest

from kwin_mcp.warden import daemon, state

EV_KEY, EV_REL = 1, 2
KEY_A, BTN_LEFT, REL_X, REL_WHEEL = 30, 0x110, 0, 8


@pytest.mark.parametrize(
    ("event", "touchpad", "pressed"),
    [
        ((EV_KEY, KEY_A, 1), False, True),  # a key goes down
        ((EV_KEY, KEY_A, 0), False, False),  # letting go
        ((EV_KEY, KEY_A, 2), False, False),  # a key repeating
        ((EV_KEY, BTN_LEFT, 1), False, True),  # a click
        ((EV_KEY, BTN_LEFT, 1), True, True),  # a touchpad's physical click
        ((EV_REL, REL_X, 3), False, False),  # a nudged mouse
        ((EV_REL, REL_WHEEL, 1), False, False),  # scrolling
        ((EV_KEY, daemon.BTN_TOUCH, 1), False, True),  # a finger on a touchscreen
        ((EV_KEY, daemon.BTN_TOUCH, 1), True, False),  # a finger landing on a touchpad
        ((EV_KEY, daemon.BTN_TOOL_FINGER, 1), True, False),
        ((EV_KEY, 0x140, 1), False, False),  # a pen hovering near a tablet
    ],
)
def test_what_counts_as_a_press(event, touchpad, pressed):
    assert daemon.is_press(*event, touchpad=touchpad) is pressed


def test_touchpads_are_told_from_mice_and_touchscreens(tmp_path, monkeypatch):
    devices = (
        'N: Name="A mouse"\nH: Handlers=event5 mouse0 \nB: PROP=0\nB: KEY=ff0000 0 0 0 0\n\n'
        'N: Name="A touchscreen"\nH: Handlers=event3 mouse3 \nB: PROP=2\n'
        "B: KEY=400 0 0 0 0 0\n\n"
        'N: Name="A touchpad"\nH: Handlers=event7 mouse4 \nB: PROP=5\n'
        "B: KEY=e520 10000 0 0 0 0\n\n"
        'N: Name="A keyboard"\nH: Handlers=sysrq kbd event4 \nB: PROP=0\nB: KEY=1 0 0 0\n'
    )
    real_read = daemon.Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == "/proc/bus/input/devices":
            return devices
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(daemon.Path, "read_text", read_text)
    kinds = daemon._device_kinds()
    assert "touchpad" in kinds["/dev/input/event7"]
    assert "touchpad" not in kinds["/dev/input/event5"]
    assert "touchpad" not in kinds["/dev/input/event3"]
    assert kinds["/dev/input/event4"] == {"keyboard"}


def _age(name: str) -> float:
    path = state.runtime_dir() / name
    return time.time() - path.stat().st_mtime if path.exists() else float("inf")


def _wait_for(name: str, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _age(name) < 1.0:
            return True
        time.sleep(0.05)
    return False


def test_the_watcher_tells_a_nudge_from_a_press(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    fifo = tmp_path / "mouse"
    os.mkfifo(fifo)
    monkeypatch.setenv("KWIN_MCP_WARDEN_INPUT_DEVICES", str(fifo))
    stop = threading.Event()
    reader = threading.Thread(target=daemon.watch_hands, args=(stop,), daemon=True)
    reader.start()
    fd = os.open(fifo, os.O_WRONLY)
    try:
        assert _wait_for("hands-watch"), "the watcher never said it runs"
        os.write(fd, struct.pack("llHHi", 0, 0, EV_REL, REL_X, 4))
        assert _wait_for("hands"), "a nudge isn't use"
        time.sleep(0.2)
        assert _age("presses") == float("inf"), "a nudge counted as a press"
        os.write(fd, struct.pack("llHHi", 0, 0, EV_KEY, KEY_A, 1))
        assert _wait_for("presses"), "a key press never reached presses"
    finally:
        stop.set()
        os.close(fd)
        reader.join(5)


EV_ABS = 3


def _touch(tap: daemon.Tap, down_ms: int, up_ms: int, xs: tuple[int, ...] = ()) -> bool:
    """Feed a finger down at ``down_ms``, moves to ``xs``, and the lift at ``up_ms``."""
    said = tap.feed(0, down_ms * 1000, EV_KEY, daemon.BTN_TOUCH, 1)
    for x in xs:
        said = tap.feed(0, down_ms * 1000 + 1000, EV_ABS, daemon.ABS_X, x) or said
    return tap.feed(0, up_ms * 1000, EV_KEY, daemon.BTN_TOUCH, 0) or said


@pytest.mark.parametrize(
    ("down", "up", "xs", "units", "tapped"),
    [
        (0, 90, (), 0.0, True),  # a quick tap
        (0, 180, (), 0.0, True),  # libinput's limit, 180 ms
        (0, 400, (), 0.0, False),  # a finger resting, then lifting
        (0, 90, (1000, 1005), 40.0, True),  # moved 5 units = 0.125 mm: still a tap
        (0, 90, (1000, 1200), 40.0, False),  # moved 5 mm: a flick that moves the pointer
        (0, 90, (1000, 1200), 0.0, True),  # no resolution known: only the time counts
    ],
)
def test_a_touchpad_tap_is_a_press_when_the_finger_lifts(down, up, xs, units, tapped):
    tap = daemon.Tap(units)
    assert _touch(tap, down, up, xs) is tapped
    assert _touch(tap, 1000, 1500) is False  # the next touch starts afresh


def test_the_watcher_counts_a_touchpad_tap(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run))
    fifo = tmp_path / "touchpad"
    os.mkfifo(fifo)
    monkeypatch.setenv("KWIN_MCP_WARDEN_INPUT_DEVICES", str(fifo))
    monkeypatch.setattr(daemon, "_device_kinds", lambda: {str(fifo): {"pointer", "touchpad"}})
    stop = threading.Event()
    reader = threading.Thread(target=daemon.watch_hands, args=(stop,), daemon=True)
    reader.start()
    fd = os.open(fifo, os.O_WRONLY)
    try:
        assert _wait_for("hands-watch"), "the watcher never said it runs"
        # A finger lands and rests 400 ms: moving the pointer, not a press.
        os.write(fd, struct.pack("llHHi", 10, 0, EV_KEY, daemon.BTN_TOUCH, 1))
        os.write(fd, struct.pack("llHHi", 10, 400_000, EV_KEY, daemon.BTN_TOUCH, 0))
        assert _wait_for("hands"), "a finger on the touchpad isn't use"
        time.sleep(0.2)
        assert _age("presses") == float("inf"), "a resting finger counted as a press"
        # A tap: down and up 80 ms apart.
        os.write(fd, struct.pack("llHHi", 11, 0, EV_KEY, daemon.BTN_TOUCH, 1))
        os.write(fd, struct.pack("llHHi", 11, 80_000, EV_KEY, daemon.BTN_TOUCH, 0))
        assert _wait_for("presses"), "a tap-to-click never reached presses"
    finally:
        stop.set()
        os.close(fd)
        reader.join(5)
