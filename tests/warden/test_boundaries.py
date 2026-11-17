"""kwin-mcp-warden's boundary tests: each one tries to cross a limit from outside.

Everything runs inside one private nested KWin (kwin-mcp's own virtual session):
its own D-Bus, its own Wayland socket, a fresh HOME and XDG_RUNTIME_DIR, and a
pipe standing in for your mouse. Nothing here reaches your desktop, your input
devices or your journal.

Two players share the nested desktop:
- ``claude``: a WardenEngine attached to it as a live session, the way a Claude
  session attaches to your desktop. Everything it does goes through the warden.
- ``person``: a plain kwin-mcp engine that owns the nested session and plays you:
  it clicks the card's buttons and opens the test apps.

Run: tests/warden/run.sh   (or PLANT=<name> tests/warden/run.sh to prove a check
catches its planted fault; see conftest.py for the plants).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from kwin_mcp.models import Locator, Point, RefAddr, StrictAddr, WindowSelector
from kwin_mcp.warden import state
from kwin_mcp.warden.gate import Refused

pytestmark = pytest.mark.timeout(300)


def _wait_for(fn, timeout=15.0, step=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(step)
    return None


def _capture(engine):
    """A picture through the warden; skipped (with the reason) only where this KWin has no
    ScreenShot2 at all, as upstream's suite does for older virtual backends."""
    try:
        return engine.screen_capture()
    except RuntimeError as exc:
        if "No such object path '/org/kde/KWin/ScreenShot2'" in str(exc):
            pytest.skip("known KWin limit: this KWin's virtual backend has no ScreenShot2")
        raise


def _window(engine, app_fragment):
    wins = [w for w in engine.window_list().windows if app_fragment in w.app_id.lower()]
    return wins[0] if wins else None


def _center(rect):
    return Point(x=rect.x + rect.width // 2, y=rect.y + rect.height // 2)


def _answer_card(person, button: str, after: float = 0.0):
    """Play the person: wait for the card, then click one of its buttons."""
    geometry = state.runtime_dir() / "card-geometry.json"
    found = _wait_for(lambda: geometry.exists() and geometry.read_text(), timeout=20)
    assert found, "the card never showed its buttons"
    if after:
        time.sleep(after)
    x, y, w, h = json.loads(found)[button]
    person.pointer_click(target=Point(x=x + w // 2, y=y + h // 2))


def _ask(claude, person, level, app, button="allow", reason="Test the warden."):
    result: dict = {}

    def run():
        result["value"] = claude.warden.request(level, app, reason)

    thread = threading.Thread(target=run)
    thread.start()
    _answer_card(person, button)
    thread.join(60)
    assert "value" in result, "access_request never returned"
    return result["value"]


def _held(claude, app, level):
    """The grant this session holds for ``level`` in a window of ``app``, if any."""
    from kwin_mcp.warden.policy import covers
    from kwin_mcp.warden.settings import same_app

    open_ids = {str(w.get("id")) for w in claude.warden._live_stack()}
    return next(
        (
            g
            for g in claude.warden.book.alive()
            if g.kind == "window"
            and g.window in open_ids
            and same_app(g.app, app)
            and covers(g.level, level)
        ),
        None,
    )


def _ensure_control(claude, person, app="org.kde.kate"):
    """Make sure this session holds Control in a window of ``app`` (each test sets up its
    own grant)."""
    if _held(claude, app, "control") is None:
        allowed, text = _ask(claude, person, "control", app)
        assert allowed, text


# ── Before anything is granted ────────────────────────────────────────────────


def test_click_without_a_grant_is_refused(claude, person, apps):
    """Refused, and logged once, by the name the person knows (Kate, not org.kde.kate)."""
    from kwin_mcp.warden import log

    kate = _window(person, "kate")
    before = log.log_path().read_text().splitlines() if log.log_path().exists() else []
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(kate.frame))
    lines = log.log_path().read_text().splitlines()[len(before) :]
    refused = [ln for ln in lines if "refused" in ln]
    assert len(refused) == 1, refused
    assert "in Kate (not granted)" in refused[0], refused


def test_request_without_a_reason_is_refused_before_a_card(claude):
    allowed, text = claude.warden.request("control", "org.kde.kate", "   ")
    assert not allowed and "needs a reason" in text
    assert not state.card_is_open()


# ── An app that isn't open (design/mock/index.html, "Open and control") ────────
# The warden checks what's open before prompting; if an app isn't open but needs to be, it
# asks to open it and control it at the same time.


def _card_titles() -> list[str]:
    """The titles of this run's cards on screen now, read from each card's command line.

    Only this run's (its mark, run_processes.py): a card another test run or a render
    shows at the same time is someone else's (2026-11-16, a render_card.py card counted).
    """
    import run_processes
    from conftest import RUN_MARK

    titles = []
    for pid in run_processes.find(RUN_MARK):
        proc = Path(f"/proc/{pid}")
        with contextlib.suppress(OSError, ValueError):
            args = (proc / "cmdline").read_bytes().split(b"\0")
            if any(a.endswith(b"card.qml") for a in args):
                titles.append(json.loads(args[-2] or args[-1]).get("title", ""))
    return titles


def _ask_watching(claude, person, level, app, button=None):
    """Ask, record every card title seen, and answer the card with ``button`` if one shows."""
    done: dict = {}
    seen: list[str] = []

    def ask():
        done["value"] = claude.warden.request(level, app, "Test the warden.")

    thread = threading.Thread(target=ask)
    thread.start()
    answered = False
    while thread.is_alive():
        seen += [t for t in _card_titles() if t not in seen]
        if seen and button and not answered:
            _answer_card(person, button)
            answered = True
        time.sleep(0.05)
    return done["value"], seen


def _needs_gtk(version):
    """Skip where PyGObject can't load this GTK (the made-up apps are GTK windows)."""
    import subprocess
    import sys

    probe = f"import gi; gi.require_version('Gtk', '{version}'); from gi.repository import Gtk"
    if subprocess.run([sys.executable, "-c", probe], capture_output=True).returncode != 0:
        pytest.skip(f"needs PyGObject with GTK {version} for the made-up app")


def _launchable(app):
    """Skip where the app isn't installed, judged without the warden's own lookup."""
    import shutil

    entry = f"{app}.desktop"
    dirs = [
        os.environ.get("XDG_DATA_HOME", ""),
        *os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":"),
    ]
    if not shutil.which(app.rsplit(".", 1)[-1]) or not any(
        Path(d, "applications", entry).is_file() for d in dirs if d
    ):
        pytest.skip(f"{app} isn't installed here")


def test_an_app_that_isnt_open_is_opened_and_allowed_on_one_card(claude, person, apps):
    """Konsole isn't open: one card asks to open it and control it; Allow opens it, and the
    grant covers it once its window is there."""
    from kwin_mcp.warden import log

    _launchable("org.kde.konsole")
    assert _window(person, "konsole") is None
    try:
        (allowed, text), titles = _ask_watching(
            claude, person, "control", "org.kde.konsole", "allow"
        )
        assert titles == ["Claude asks to open Konsole and control its window"], titles
        assert allowed, text
        assert _window(person, "konsole") is not None, "Allow didn't open Konsole"
        grant = _held(claude, "org.kde.konsole", "control")
        assert grant is not None
        assert any("opened" in line and "Konsole" in line for line in log.tail(4)), log.tail(4)
        # Open now: the next ask is the usual card, not another open.
        state.end_grant(grant.id)
        (allowed, text), titles = _ask_watching(claude, person, "see", "org.kde.konsole", "deny")
        assert titles == ["Claude asks to see a Konsole window"], titles
    finally:
        win = _window(person, "konsole")
        if win is not None:
            person.window_close(window=WindowSelector(id=win.id))


def test_dont_allow_opens_nothing(claude, person, apps):
    _launchable("org.kde.konsole")
    assert _window(person, "konsole") is None
    (allowed, text), titles = _ask_watching(claude, person, "control", "org.kde.konsole", "deny")
    assert titles == ["Claude asks to open Konsole and control its window"], titles
    assert not allowed, text
    time.sleep(2)
    assert _window(person, "konsole") is None, "Don't Allow opened Konsole anyway"


def test_an_app_that_cant_be_opened_gets_no_card(claude, person, apps):
    (allowed, text), titles = _ask_watching(claude, person, "control", "org.example.nothing")
    assert not titles, f"a card asked about an app that isn't there: {titles}"
    assert not allowed and "can be opened" in text, text


def test_something_else_opening_grants_nothing(claude, person, nested, apps, monkeypatch):
    """A made-up app whose launcher entry opens a different program (KCharSelect): Allow
    opens it, but no window of the named app appears, so nothing is granted."""
    from kwin_mcp.warden import gate

    entry = Path(os.environ["HOME"], ".local", "share", "applications", "org.example.decoy.desktop")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Decoy\nExec=kcharselect %U\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gate.Warden, "OPEN_WAIT_S", 6.0)
    before = {w.id for w in person.window_list().windows}
    try:
        (allowed, text), titles = _ask_watching(
            claude, person, "control", "org.example.decoy", "allow"
        )
        assert titles == ["Claude asks to open Decoy and control its window"], titles
        assert not allowed and "Nothing was granted" in text, text
        assert _held(claude, "org.example.decoy", "control") is None
    finally:
        entry.unlink(missing_ok=True)
        for w in person.window_list().windows:
            if w.id not in before and "kcharselect" in w.app_id.lower():
                person.window_close(window=WindowSelector(id=w.id))


def test_an_app_that_only_opens_over_dbus_is_opened_that_way(claude, person, nested, apps):
    """A made-up app whose entry is DBusActivatable with no Exec: the card offers to open
    it, and Allow opens it the way KDE's launcher does (org.freedesktop.Application
    .Activate on its bus name), then grants it once its window is there."""
    import sys

    _needs_gtk("4.0")
    # A dash in the name: the object path turns it into an underscore (the specification's
    # rule, and GApplication's); the app opens no window until Activate reaches it.
    app_id = "org.example.Activated-App"
    entry = Path(os.environ["HOME"], ".local", "share", "applications", f"{app_id}.desktop")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        "[Desktop Entry]\nType=Application\nName=Activated\nDBusActivatable=true\n",
        encoding="utf-8",
    )
    # The private desktop's bus looks for services in its own home's data folder.
    bus_home = Path(nested["person"]._session._home_dir)
    service = bus_home / ".local" / "share" / "dbus-1" / "services" / f"{app_id}.service"
    service.parent.mkdir(parents=True, exist_ok=True)
    helper = Path(__file__).with_name("dbus_app.py")
    service.write_text(
        f"[D-BUS Service]\nName={app_id}\nExec={sys.executable} {helper} {app_id} --service\n",
        encoding="utf-8",
    )
    try:
        (allowed, text), titles = _ask_watching(claude, person, "control", app_id, "allow")
        assert titles == ["Claude asks to open Activated and control its window"], titles
        assert allowed, text
        assert _held(claude, app_id, "control") is not None
    finally:
        entry.unlink(missing_ok=True)
        service.unlink(missing_ok=True)
        win = _window(person, "activated")
        if win is not None:
            person.window_close(window=WindowSelector(id=win.id))


def test_a_single_instance_app_hands_its_window_over(claude, person, apps):
    """An app already running with no window (a single-instance app in the background):
    Allow runs its Exec, which hands the open to the running instance, and the window
    comes from that other process. The grant follows the app, not the process, and still
    covers the window once it's moved to another virtual desktop."""
    import subprocess
    import sys

    _needs_gtk("4.0")
    app_id = "org.example.Single"
    helper = Path(__file__).with_name("dbus_app.py")
    entry = Path(os.environ["HOME"], ".local", "share", "applications", f"{app_id}.desktop")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(
        f"[Desktop Entry]\nType=Application\nName=Single\nExec={sys.executable} {helper} "
        f"{app_id}\n",
        encoding="utf-8",
    )
    primary = subprocess.Popen([sys.executable, str(helper), app_id, "--service"])
    dbus = ["gdbus", "call", "--session", "--dest", "org.kde.KWin"]
    added = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # the instance owns its name on the bus
            names = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.DBus",
                    "--object-path",
                    "/org/freedesktop/DBus",
                    "--method",
                    "org.freedesktop.DBus.ListNames",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if f"'{app_id}'" in names:
                break
            time.sleep(0.1)
        else:
            pytest.fail("the background instance never took its bus name")
        assert _window(person, "single") is None
        (allowed, text), titles = _ask_watching(claude, person, "control", app_id, "allow")
        assert titles == ["Claude asks to open Single and control its window"], titles
        assert allowed, text
        win = _window(person, "single")
        pids = {
            w.get("pid")
            for w in claude.warden._live_stack()
            if "single" in str(w.get("app") or "").lower()
        }
        assert win is not None and pids == {primary.pid}, (
            "the window didn't come from the running instance",
            pids,
            primary.pid,
        )

        # Another virtual desktop: the grant is the app's wherever its window goes.
        out = subprocess.run(
            [
                *dbus,
                "--object-path",
                "/VirtualDesktopManager",
                "--method",
                "org.kde.KWin.VirtualDesktopManager.createDesktop",
                "1",
                "Elsewhere",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.returncode == 0
        elsewhere = next(d for d in person.desktop_list().desktops if d.name == "Elsewhere")
        added = elsewhere.id
        person.window_place(WindowSelector(id=win.id), desktop=elsewhere.id)
        assert claude.warden.request("control", app_id, "Test the warden.")[1].startswith(
            "Already allowed"
        )
        seen = _window(claude, "single")
        assert seen is not None and seen.caption == "Activated", seen
    finally:
        entry.unlink(missing_ok=True)
        win = _window(person, "single")
        if win is not None:
            person.window_close(window=WindowSelector(id=win.id))
        primary.terminate()
        primary.wait(10)
        if added:
            subprocess.run(
                [
                    *dbus,
                    "--object-path",
                    "/VirtualDesktopManager",
                    "--method",
                    "org.kde.KWin.VirtualDesktopManager.removeDesktop",
                    added,
                ],
                capture_output=True,
                check=False,
            )


def test_own_screen_needs_a_grant(claude):
    with pytest.raises(Refused, match="own_screen"):
        claude.session_start()


def test_window_list_shows_no_window_without_see(claude, person, apps):
    """A window Claude isn't granted isn't listed at all, not even its app's name; a
    window it is granted is listed with its title."""
    claude.warden.book.end_all()
    assert _window(person, "kate") is not None and _window(person, "kcharselect") is not None
    assert _window(claude, "kate") is None
    assert _window(claude, "kcharselect") is None
    allowed, text = _ask(claude, person, "see", "org.kde.kate")
    assert allowed, text
    kate = _window(claude, "kate")
    assert kate is not None and kate.caption, kate
    assert _window(claude, "kcharselect") is None


# ── The card ──────────────────────────────────────────────────────────────────


def test_dont_allow_grants_nothing(claude, person, apps):
    allowed, text = _ask(claude, person, "control", "org.kde.kate", button="deny")
    assert not allowed and "Don't Allow" in text
    kate = _window(person, "kate")
    with pytest.raises(Refused):
        claude.pointer_click(target=_center(kate.frame))


def test_claude_cannot_click_the_card(claude, other_claude, person, apps):
    """While a card is up, no session's input moves, even one holding Control, even at Allow."""
    _ensure_control(other_claude, person)  # the other session may type in Kate...
    kate = _window(other_claude, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    result: dict = {}

    def run():
        result["value"] = claude.warden.request(
            "control", "org.kde.kcharselect", "Test the warden."
        )

    thread = threading.Thread(target=run)
    thread.start()
    geometry = state.runtime_dir() / "card-geometry.json"
    found = _wait_for(lambda: geometry.exists() and geometry.read_text(), timeout=20)
    assert found
    x, y, w, h = json.loads(found)["allow"]
    with pytest.raises(Refused, match="card is on screen"):  # ...but not while a card is up
        other_claude.keyboard_type(text="x")
    with pytest.raises(Refused, match="card is on screen"):
        other_claude.pointer_click(target=Point(x=x + w // 2, y=y + h // 2))
    _answer_card(person, "deny")
    thread.join(60)
    assert result["value"][0] is False, "a click from Claude answered the card"


def test_allow_grants_control_in_that_app_only(claude, person, apps):
    allowed, text = _ask(claude, person, "control", "org.kde.kate")
    assert allowed, text
    kate = _window(person, "kate")
    chars = _window(person, "kcharselect")
    claude.pointer_click(target=_center(kate.frame))  # granted: no refusal
    time.sleep(1.5)  # the frame and the tag are up by now
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(chars.frame))


def _second_kate(person):
    """A second Kate window over KCharSelect's spot, on top; returns (first, second)."""
    from kwin_mcp.models import PartialRect

    first = _window(person, "kate")
    person.app_launch(command="kate --new")
    second = _wait_for(
        lambda: next(
            (
                w
                for w in person.window_list().windows
                if "kate" in w.app_id.lower() and w.id != first.id
            ),
            None,
        ),
        30,
    )
    assert second, "the second Kate window never opened"
    person.window_place(
        window=WindowSelector(id=second.id), frame=PartialRect(x=980, y=60, width=900, height=700)
    )
    person.window_focus(window=WindowSelector(id=second.id))
    time.sleep(1.0)
    second = next(w for w in person.window_list().windows if w.id == second.id)
    return first, second


def _close_second_kate(person, second):
    with contextlib.suppress(Exception):
        person.window_close(window=WindowSelector(id=second.id))
    # Kate may ask to save: its untitled document is empty, so it closes without asking.
    assert _wait_for(lambda: all(w.id != second.id for w in person.window_list().windows), 10), (
        "the second Kate window stayed open"
    )


def test_a_grant_covers_one_window_not_the_whole_app(claude, person, apps):
    """Two Kate windows, Control allowed in one: the other one isn't listed, can't be
    clicked, comes back black in a picture, and asking again names it on its own card."""
    from PIL import Image

    claude.warden.book.end_all()
    first, second = _second_kate(person)
    try:
        # The card asks about the Kate window you used last: the first one, brought forward.
        person.window_focus(window=WindowSelector(id=first.id))
        time.sleep(0.5)
        allowed, text = _ask(claude, person, "control", "org.kde.kate")
        assert allowed, text
        (grant,) = [g for g in claude.warden.book.alive() if g.kind == "window"]
        assert grant.window == first.id, (grant.window, first.id, second.id)

        listed = {w.id for w in claude.window_list().windows}
        assert first.id in listed and second.id not in listed, listed

        claude.pointer_click(target=_center(first.frame))  # the granted window: allowed
        with pytest.raises(Refused, match="isn't granted"):
            claude.pointer_click(target=_center(second.frame))
        with pytest.raises(Refused):
            claude.window_focus(window=WindowSelector(id=second.id))

        # The other Kate window sits over KCharSelect, on top of everything there.
        person.window_focus(window=WindowSelector(id=second.id))
        time.sleep(0.5)
        frame = _capture(claude)
        with Image.open(frame.path) as img:
            rgb = img.convert("RGB")
            m = frame.mapping
            cx = int((second.frame.x + second.frame.width // 2 - m.origin.x) * m.scale)
            cy = int((second.frame.y + second.frame.height // 2 - m.origin.y) * m.scale)
            assert rgb.getpixel((cx, cy)) == (0, 0, 0), "the other Kate window showed"
            fx = int((first.frame.x + first.frame.width // 2 - m.origin.x) * m.scale)
            fy = int((first.frame.y + first.frame.height // 2 - m.origin.y) * m.scale)
            assert rgb.getpixel((fx, fy)) != (0, 0, 0), "the granted window came back black"

        found = claude.element_query(locator=Locator(app="kate"), max_depth=1)
        assert {str(el.window_id) for el in found.elements} <= {first.id}, found.elements

        # Asking again is for the other window, by its title, on its own card.
        (allowed, text), titles = _ask_watching(claude, person, "see", "org.kde.kate", "deny")
        assert titles == ["Claude asks to see a Kate window"], titles
        assert not allowed, text
    finally:
        _close_second_kate(person, second)
        claude.warden.book.end_all()


def test_a_windows_grant_ends_when_it_closes(claude, person, apps):
    """The access list drops a window's grant once the window is gone, and a new window of
    the same app isn't covered by it."""
    claude.warden.book.end_all()
    _first, second = _second_kate(person)
    try:
        allowed, text = _ask(claude, person, "see", "org.kde.kate")
        assert allowed, text
        (grant,) = [g for g in claude.warden.book.alive() if g.kind == "window"]
        assert grant.window == second.id  # the one you used last
        _close_second_kate(person, second)
        assert _wait_for(lambda: not grant.alive(), 15), "the grant outlived its window"
        assert _window(claude, "kate") is None
    finally:
        _close_second_kate(person, second)
        claude.warden.book.end_all()


def test_a_granted_windows_dialog_comes_with_it(claude, person, apps):
    """A dialog the granted window opens (KWin: transient for it) is listed and can be
    clicked; the card never asks about the dialog apart from its window."""
    import sys

    _needs_gtk("3.0")
    claude.warden.book.end_all()
    helper = Path(__file__).with_name("gtk3_app.py")
    person.app_launch(command=f"{sys.executable} {helper} --dialog")
    dialog = _wait_for(
        lambda: next(
            (w for w in person.window_list().windows if w.caption == "GTK 3 dialog"), None
        ),
        20,
    )
    assert dialog, "the dialog never opened"
    try:
        allowed, text = _ask(claude, person, "control", "gtk3-fields")
        assert allowed, text
        (grant,) = [g for g in claude.warden.book.alive() if g.kind == "window"]
        assert grant.window != dialog.id, "the card asked about the dialog, not its window"
        listed = {w.id for w in claude.window_list().windows}
        assert dialog.id in listed, listed
        claude.pointer_click(target=_center(dialog.frame))  # no refusal: it's the window's
    finally:
        with contextlib.suppress(Exception):
            person.window_close(window=WindowSelector(id=dialog.id))
        with contextlib.suppress(Exception):
            person.window_close(window=WindowSelector(app_id="gtk3-fields"))
        assert _wait_for(lambda: _window(person, "gtk3-fields") is None, 10), "it stayed open"
        claude.warden.book.end_all()


@pytest.mark.parametrize(("asked", "picked"), [(480, 60), (60, 480), (60, 0)])
def test_the_person_picks_how_long_not_the_agent(claude, person, apps, asked, picked):
    """The agent asks for one length; the person clicks another on the card, shorter or
    longer, and the grant lasts what the person picked."""
    claude.warden.book.end_all()
    result: dict = {}

    def run():
        result["value"] = claude.warden.request(
            "control", "org.kde.kate", "Test the warden.", minutes=asked
        )

    thread = threading.Thread(target=run)
    thread.start()
    geometry = state.runtime_dir() / "card-geometry.json"
    found = _wait_for(lambda: geometry.exists() and geometry.read_text(), timeout=20)
    assert found, "the card never showed its buttons"
    spots = json.loads(found)
    assert f"length-{picked}" in spots, spots
    for name in (f"length-{picked}", "allow"):
        x, y, w, h = spots[name]
        person.pointer_click(target=Point(x=x + w // 2, y=y + h // 2))
        time.sleep(0.3)
    thread.join(60)
    allowed, text = result["value"]
    assert allowed, text
    (grant,) = [g for g in claude.warden.book.alive() if g.kind == "window"]
    if picked == 0:  # "Until session ends"
        assert grant.describe().endswith("(until the session ends)"), grant.describe()
    else:
        assert abs((grant.expires - grant.given) - picked * 60) < 5, grant.expires - grant.given
    claude.warden.book.end_all()


def test_typing_follows_focus(claude, person, apps):
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    claude.keyboard_press(keys="shift")  # focus on the granted app: allowed
    chars = _window(person, "kcharselect")
    person.window_focus(window=WindowSelector(id=chars.id))
    with pytest.raises(Refused, match="isn't granted"):
        claude.keyboard_type(text="x")


def test_a_caption_selector_cant_find_hidden_windows(claude, person, apps):
    """Selecting a window by its title answers the same for a window Claude may not see as
    for no window at all, so a title can't be probed (review item, fixed 2026-11-14)."""
    _ensure_control(claude, person)
    chars = _window(person, "kcharselect")
    assert chars.caption, "the test needs KCharSelect's real title"
    with pytest.raises(Refused) as hidden:
        claude.window_focus(window=WindowSelector(caption=chars.caption))
    with pytest.raises(Refused) as nothing:
        claude.window_focus(window=WindowSelector(caption="no window has this title 7f3a"))
    assert str(hidden.value) == str(nothing.value), (str(hidden.value), str(nothing.value))


def test_see_cuts_the_picture_to_the_granted_app(claude, person, apps):
    """Control in Kate covers See in Kate; KCharSelect's pixels come back black."""
    from PIL import Image

    _ensure_control(claude, person)
    chars = _window(person, "kcharselect")
    frame = _capture(claude)
    with Image.open(frame.path) as img:
        rgb = img.convert("RGB")
        m = frame.mapping
        cx = int((chars.frame.x + chars.frame.width // 2 - m.origin.x) * m.scale)
        cy = int((chars.frame.y + 10 - m.origin.y) * m.scale)
        assert rgb.getpixel((cx, cy)) == (0, 0, 0), "an ungranted window showed in the picture"


def test_closing_a_window_asks_every_time(claude, person, apps):
    """Even a window Claude is granted: closing it asks each time."""
    if _held(claude, "org.kde.kcharselect", "see") is None:
        allowed, text = _ask(claude, person, "see", "org.kde.kcharselect")
        assert allowed, text
    chars = _window(person, "kcharselect")
    claude._note_reason("window_close", "Test the warden.")
    done: dict = {}

    def run():
        try:
            claude._close(WindowSelector(id=chars.id), None, None)
            done["ok"] = True
        except Refused as exc:
            done["refused"] = str(exc)

    thread = threading.Thread(target=run)
    thread.start()
    _answer_card(person, "deny")
    thread.join(60)
    assert "refused" in done, "window_close ran without the person's yes"


# ── Your hands come first ─────────────────────────────────────────────────────


def test_your_input_pauses_claude(claude, person, hands, apps):
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    hands()  # the person moves the mouse
    started = time.monotonic()
    claude.pointer_click(target=_center(kate.frame))
    waited = time.monotonic() - started
    assert waited >= claude.warden.settings.pause_seconds - 0.2, (
        f"Claude went on after {waited:.2f} s"
    )


def _drag_in_kate(claude, kate, dwell_ms=4000):
    """Start a drag across Kate's text area that holds at the end; returns (thread, out).

    ``out["underway"]`` is set once the button goes down or the drag's first mid-action
    check runs, so the person's input from then on stops it midway (before then it would only
    make the drag wait, which is right too, but not what these tests check)."""
    out: dict = {"underway": threading.Event()}
    original = type(claude)._mid_action_checks

    def checks(*args, **kwargs):
        between, before_press = original(claude, *args, **kwargs)

        def counted():
            out["underway"].set()
            if between is not None:
                between()

        def pressing(x, y):
            if before_press is not None:
                before_press(x, y)
            out["underway"].set()

        return counted, pressing

    claude._mid_action_checks = checks
    y = kate.frame.y + kate.frame.height // 2
    start = Point(x=kate.frame.x + 120, y=y)
    end = Point(x=kate.frame.x + kate.frame.width - 120, y=y)

    def run():
        began = time.monotonic()
        try:
            out["result"] = claude.pointer_drag(from_=start, to=end, dwell_ms=dwell_ms)
        except Refused as exc:
            out["refused"] = str(exc)
        out["took"] = time.monotonic() - began
        out["ended"] = time.monotonic()

    def run_then_unwrap():
        try:
            run()
        finally:
            claude.__dict__.pop("_mid_action_checks", None)

    thread = threading.Thread(target=run_then_unwrap)
    thread.start()
    return thread, out


def test_your_input_stops_a_drag_midway(claude, person, hands, apps):
    """A drag, long-press or long scroll already under way stops and lets go the moment the
    person touches the mouse, or presses the stop key (deferred item 2, fixed 2026-11-14)."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    thread, out = _drag_in_kate(claude, kate)
    assert out["underway"].wait(15), "the drag never got under way"
    time.sleep(0.5)
    touched = time.monotonic()
    hands()
    thread.join(30)
    assert "refused" in out, "the drag ran to its end through the person's input"
    assert "stopped in the middle" in out["refused"], out["refused"]
    late = out["ended"] - touched
    assert late < 1.5, f"the drag stopped only {late:.1f} s after the touch"
    time.sleep(claude.warden.settings.pause_seconds + 0.3)
    thread, out = _drag_in_kate(claude, kate)
    assert out["underway"].wait(15), "the drag never got under way"
    time.sleep(0.5)
    stopped = time.monotonic()
    state.press_stop()
    thread.join(30)
    assert "refused" in out and "grant ended" in out["refused"], out
    late = out["ended"] - stopped
    assert late < 1.5, f"the drag outlived the stop key by {late:.1f} s"


def test_a_touch_while_the_window_is_checked_after_a_pause_waits_again(claude, person, hands, apps):
    """After a pause for the person's hands, the warden checks the window again before the
    drag starts. A touch during that check means waiting again: the drag starts only a
    full quiet pause after it, never with the touch slipped in between."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    hands()  # the drag starts by waiting for the person
    original = type(claude)._targets
    calls = {"n": 0, "touched": 0.0}

    def targets(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # the check again after the pause
            hands()
            calls["touched"] = time.monotonic()
        return original(claude, *args, **kwargs)

    claude._targets = targets
    try:
        thread, out = _drag_in_kate(claude, kate, dwell_ms=300)
        assert out["underway"].wait(20), "the drag never got under way"
        began = time.monotonic()
        thread.join(30)
    finally:
        claude.__dict__.pop("_targets", None)
    assert calls["touched"], "the drag never paused for the person"
    quiet = claude.warden.settings.pause_seconds
    assert began - calls["touched"] >= quiet - 0.2, (
        f"the drag started {began - calls['touched']:.2f} s after the touch, inside the pause"
    )
    assert "result" in out, out


def test_a_window_raised_before_the_press_is_checked(claude, person, apps, monkeypatch):
    """The window under a click is looked up again after the pointer arrives, right before
    the button goes down: a window raised in between is checked too (deferred item 3)."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    chars = _window(person, "kcharselect")
    target = _center(kate.frame)
    original_targets = type(claude)._targets

    def targets_then_raise(self, rule, bound):
        found = original_targets(self, rule, bound)
        from kwin_mcp.models import PartialRect

        # Right after the warden's first look: another app's window lands under the point.
        person.window_place(
            window=WindowSelector(id=chars.id),
            frame=PartialRect(x=target.x - 200, y=target.y - 200, width=400, height=400),
        )
        person.window_focus(window=WindowSelector(id=chars.id))
        time.sleep(0.5)
        return found

    monkeypatch.setattr(type(claude), "_targets", targets_then_raise)
    try:
        with pytest.raises(Refused, match="isn't granted"):
            claude.pointer_click(target=target)
    finally:
        monkeypatch.undo()
        from kwin_mcp.models import PartialRect

        person.window_place(
            window=WindowSelector(id=chars.id),
            frame=PartialRect(x=980, y=60, width=900, height=700),
        )
        person.window_focus(window=WindowSelector(id=kate.id))
        time.sleep(0.5)


# ── Grants end ────────────────────────────────────────────────────────────────


def test_a_grant_past_its_time_is_refused(claude, person, apps, monkeypatch):
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    claude.pointer_click(target=_center(kate.frame))  # inside its hour: allowed
    later = time.time() + 61 * 60
    monkeypatch.setattr(time, "time", lambda: later)
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(kate.frame))


def test_the_stop_ends_every_grant(claude, person, apps):
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    claude.pointer_click(target=_center(kate.frame))  # before the stop: allowed
    time.sleep(0.05)
    state.press_stop()
    kate = _window(person, "kate")
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(kate.frame))


def _press_stop_shortcut_directly() -> None:
    """Fire the warden's shortcut inside kglobalaccel, as KWin does on a key press."""
    import subprocess

    out = subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.kde.kglobalaccel",
            "--object-path",
            "/kglobalaccel",
            "--method",
            "org.kde.KGlobalAccel.getComponent",
            "kwin-mcp-warden",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    path = out.split("'")[1]
    subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.kde.kglobalaccel",
            "--object-path",
            path,
            "--method",
            "org.kde.kglobalaccel.Component.invokeShortcut",
            "stop",
        ],
        capture_output=True,
        text=True,
        check=True,
    )


def _grant_and_wait_for_daemon(claude, person):
    allowed, _ = _ask(claude, person, "control", "org.kde.kate")
    assert allowed
    assert _wait_for(lambda: (state.runtime_dir() / "daemon.pid").exists(), 10), "no daemon"
    time.sleep(1.5)  # the daemon registers the key


def test_the_stop_shortcut_ends_every_grant(claude, person, apps):
    """The stop shortcut, fired in kglobalaccel, reaches the daemon and ends every grant."""
    _grant_and_wait_for_daemon(claude, person)
    before = state.stop_time()
    _press_stop_shortcut_directly()
    assert _wait_for(lambda: state.stop_time() > before, 5), "the shortcut didn't end the grants"
    kate = _window(person, "kate")
    with pytest.raises(Refused):
        claude.pointer_click(target=_center(kate.frame))


def test_the_stop_key_pressed_reaches_the_warden(claude, person, apps):
    """The stop key, pressed on the keyboard, ends every grant (a key no other app holds)."""
    _grant_and_wait_for_daemon(claude, person)
    before = state.stop_time()
    person.keyboard_press(keys="meta+alt+Escape")
    assert _wait_for(lambda: state.stop_time() > before, 5), "the stop key didn't end the grants"


def test_the_daemon_comes_back_between_calls(claude, person, apps):
    """While Claude holds a grant, a daemon that dies (and with it the stop key, the frame
    and the notice) is back within seconds, without waiting for Claude's next call."""
    from kwin_mcp.warden.gate import _cmdline

    _ensure_control(claude, person)
    pid_file = state.runtime_dir() / "daemon.pid"
    pid = int(_wait_for(lambda: pid_file.exists() and pid_file.read_text().strip(), 10))
    # Only ever the test's own daemon: its command line and its sandboxed runtime folder.
    environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    assert "kwin_mcp.warden.daemon" in _cmdline(pid)
    assert f"XDG_RUNTIME_DIR={os.environ['XDG_RUNTIME_DIR']}".encode() in environ
    os.kill(pid, 9)

    def new_daemon() -> bool:
        try:
            now = int(pid_file.read_text().strip())
        except (OSError, ValueError):
            return False
        return now != pid and "kwin_mcp.warden.daemon" in _cmdline(now)

    back = _wait_for(new_daemon, 8)
    assert back, "the daemon stayed dead until Claude's next call"

    def overlays() -> list[int]:
        found = []
        for proc in os.listdir("/proc"):
            if proc.isdigit() and "overlay.qml" in _cmdline(int(proc)):
                with contextlib.suppress(OSError):
                    if (
                        os.environ["XDG_RUNTIME_DIR"].encode()
                        in Path(f"/proc/{proc}/environ").read_bytes()
                    ):
                        found.append(int(proc))
        return found

    time.sleep(2.0)
    assert len(overlays()) == 2, f"overlays left over from the dead daemon: {overlays()}"


MADE_UP_APP = "warden-test-made-up-app"


def test_a_stop_key_another_app_holds_is_refused(nested):
    """The warden never fights another app for its key: a made-up app the warden has never
    seen holds Meta+Alt+F12, and the warden's stop key refuses that key and names the app.
    (On stock Plasma the same goes for System Monitor's Meta+Esc, checked where installed.)"""
    import dbus

    from kwin_mcp.warden import daemon

    bus = dbus.SessionBus()
    kga = dbus.Interface(
        bus.get_object("org.kde.kglobalaccel", "/kglobalaccel"), "org.kde.KGlobalAccel"
    )
    action = dbus.Array([MADE_UP_APP, "act", "Made-up App", "Its own action"], signature="s")
    key = dbus.Array([daemon.stop_key_sequence("Meta+Alt+F12"), 0, 0, 0], signature="i")
    kga.doRegister(action)
    kga.setShortcutKeys(
        action, dbus.Array([dbus.Struct((key,), signature="ai")], signature="(ai)"), dbus.UInt32(3)
    )
    with pytest.raises(RuntimeError, match="Made-up App"):
        daemon.register_stop_key("Meta+Alt+F12", lambda: None)
    if Path("/usr/share/applications/org.kde.plasma-systemmonitor.desktop").exists():
        with pytest.raises(RuntimeError, match="System Monitor"):
            daemon.register_stop_key("Meta+Esc", lambda: None)


# ── Never granted ─────────────────────────────────────────────────────────────


def test_never_list_refuses_without_a_card(claude):
    """Every app on the never list (taken from the warden's own defaults) is refused, no card."""
    from kwin_mcp.warden.settings import DEFAULT_NEVER

    for app in DEFAULT_NEVER:
        allowed, text = claude.warden.request("control", app, "Test the warden.")
        assert not allowed and "never granted" in text, app
    assert not state.card_is_open()


def test_the_wardens_own_windows_are_never_granted(claude, person, apps):
    """Even with Full desktop, Claude can't click the warden's tag or list."""

    from kwin_mcp.warden.gate import _cmdline

    _ensure_control(claude, person)
    tag = _wait_for(
        lambda: [
            w
            for w in claude._stack()
            if "overlay.qml -- controls" in _cmdline(int(w.get("pid") or 0))
        ],
        timeout=10,
    )
    assert tag, "no warden window to test against"
    fx, fy, fw, fh = tag[0]["frame"]
    with pytest.raises(Refused, match="never granted"):
        claude.pointer_click(target=Point(x=fx + fw // 2, y=fy + fh // 2))


def test_ask_every_time_tools_need_a_reason(claude, person, apps):
    """Each ask-every-time tool (taken from the levels table) is refused without a reason,
    before any card."""
    from kwin_mcp.warden.policy import RULES

    kate = _window(person, "kate")
    calls = {
        "app_launch": lambda: claude.app_launch(command="kcharselect"),
        "window_close": lambda: claude._close(WindowSelector(id=kate.id), None, None),
        "clipboard_read": lambda: claude.clipboard_read(),
        "clipboard_write": lambda: claude.clipboard_write(text="x"),
        "dbus_call": lambda: claude.dbus_call(
            service="org.kde.KWin", path="/KWin", interface="org.kde.KWin", method="currentDesktop"
        ),
    }
    asks = [name for name, rule in RULES.items() if rule.kind == "ask"]
    assert set(asks) == set(calls), f"a new ask-every-time tool needs a case here: {asks}"
    for name in asks:
        with pytest.raises(Refused, match="needs a reason"):
            calls[name]()
        assert not state.card_is_open(), name


def test_dbus_never_paths_are_refused_through_kwin(claude):
    """The shortcut service lives inside KWin's connection; through org.kde.KWin is refused."""
    claude._note_reason("dbus_call", "Test the warden.")
    with pytest.raises(Refused, match="never allowed"):
        claude.dbus_call(
            service="org.kde.KWin",
            path="/kglobalaccel",
            interface="org.kde.KGlobalAccel",
            method="allMainComponents",
        )
    with pytest.raises(Refused, match="never allowed"):
        claude.dbus_call(
            service=":1.5", path="/KWin", interface="org.kde.KWin", method="currentDesktop"
        )


# ── A screen of Claude's own ──────────────────────────────────────────────────


# ── Pictures for the design check (only with WARDEN_SHOTS=<folder>) ───────────


@pytest.mark.skipif(not os.environ.get("WARDEN_SHOTS"), reason="pictures only on request")
def test_render_shots(claude, person, hands, apps):
    import shutil
    from pathlib import Path

    out = Path(os.environ["WARDEN_SHOTS"])
    out.mkdir(parents=True, exist_ok=True)
    # A session name like a real one (a terminal's title), not the test's sandbox folder.
    claude.warden._session_name = claude.warden.book.session = "Website Redesign"
    claude.warden.book.publish()
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    claude.pointer_click(target=_center(kate.frame))
    time.sleep(2.0)
    shutil.copy(person.screen_capture().path, out / "held.png")
    # The same moment as Claude gets it: only the granted window, the rest black.
    shutil.copy(_capture(claude).path, out / "claude-sees.png")
    claude.pointer_click(target=_center(kate.frame))
    time.sleep(0.5)  # the ring on its way in
    shutil.copy(person.screen_capture().path, out / "ring.png")
    time.sleep(2.0)
    claude.pointer_click(target=_center(kate.frame))
    time.sleep(0.85)  # the ring closing on the core
    shutil.copy(person.screen_capture().path, out / "ring-late.png")
    hands()
    time.sleep(0.4)
    thread = threading.Thread(target=lambda: claude.pointer_click(target=_center(kate.frame)))
    thread.start()
    time.sleep(0.5)
    shutil.copy(person.screen_capture().path, out / "paused.png")
    thread.join(30)


def test_your_input_pauses_claude_mid_typing(claude, person, hands, apps):
    """Press a key while Claude types and it stops mid-word, then finishes."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    text = "the quick brown fox jumps over the lazy dog " * 2
    started_wall = time.time()
    done: dict = {}

    def run():
        done["result"] = claude.keyboard_type(text=text)

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.15)
    hands("key")  # the person presses a key while Claude types
    thread.join(60)
    assert done["result"].chars == len(text)
    paused_at = (
        (state.runtime_dir() / "paused").stat().st_mtime
        if (state.runtime_dir() / "paused").exists()
        else 0
    )
    assert paused_at > started_wall + 0.1, "Claude typed straight through the person's key"


def test_a_nudged_mouse_doesnt_pause_typing(claude, person, hands, apps):
    """Moving the mouse can't send Claude's typing anywhere else, so it doesn't pause it
    (a mouse gets nudged all the time). A click or key still does,
    and a pointer action still waits for any use of the mouse (test_your_input_pauses_claude).
    """
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    time.sleep(claude.warden.settings.pause_seconds + 0.2)
    text = "the quick brown fox jumps over the lazy dog " * 2
    started_wall = time.time()
    done: dict = {}

    def run():
        done["result"] = claude.keyboard_type(text=text)

    thread = threading.Thread(target=run)
    thread.start()
    for _ in range(4):  # the person keeps nudging the mouse while Claude types
        time.sleep(0.1)
        hands("move")
    thread.join(60)
    assert done["result"].chars == len(text)
    took = time.time() - started_wall
    paused = state.runtime_dir() / "paused"
    paused_at = paused.stat().st_mtime if paused.exists() else 0
    assert paused_at < started_wall, f"a nudged mouse paused Claude's typing ({took:.1f} s)"


def test_a_granted_apps_menu_shows_in_the_picture(claude, person, apps):
    """A menu Kate opens belongs to Kate: it shows in See-level pictures, not blacked out."""
    from PIL import Image

    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    person.keyboard_press(keys="alt+f")  # Kate's File menu
    try:
        popup = _wait_for(
            lambda: [
                w
                for w in claude._stack()
                if w.get("visible") and w.get("popup") and w.get("parent")
            ],
            timeout=5,
        )
        assert popup, "the test couldn't open a menu in Kate: " + str(
            [
                (w["app"], w.get("popup"), w.get("parent"), w.get("managed"), w["frame"])
                for w in claude._stack()
            ]
        )
        fx, fy, fw, fh = popup[-1]["frame"]
        frame = _capture(claude)
        with Image.open(frame.path) as img:
            m = frame.mapping
            px = img.convert("RGB").getpixel(
                (
                    int((fx + fw // 2 - m.origin.x) * m.scale),
                    int((fy + fh // 2 - m.origin.y) * m.scale),
                )
            )
        assert px != (0, 0, 0), "Kate's own menu was blacked out of the picture"
    finally:
        person.keyboard_press(keys="Escape")


def test_go_to_session_brings_the_card_back(claude, person, apps):
    """Go to Session is not an answer: the card comes back, still waiting on the person."""
    result: dict = {}

    def run():
        result["value"] = claude.warden.request(
            "arrange", "org.kde.kcharselect", "Test the warden."
        )

    thread = threading.Thread(target=run)
    thread.start()
    _answer_card(person, "go")
    geometry = state.runtime_dir() / "card-geometry.json"
    _wait_for(lambda: not geometry.exists(), timeout=5)
    assert _wait_for(lambda: geometry.exists(), timeout=15), "the card didn't come back"
    assert "value" not in result, "Go to Session answered the request"
    _answer_card(person, "deny")
    thread.join(60)
    assert result["value"][0] is False


# ── Through the real MCP server, as a Claude session reaches it ───────────────


def test_the_mcp_route_goes_through_the_warden(nested, person, apps):
    """kwin-mcp-warden's own server over stdio: refused, asked, allowed, then confined."""
    import sys

    import anyio
    from mcp import ClientSession, StdioServerParameters, types
    from mcp.client.stdio import stdio_client

    info = nested["info"]
    titles: list[str] = []

    def read_and_answer(button):
        """The card names the agent the client said it is, not a fixed "Claude"."""
        deadline = time.monotonic() + 30
        while not titles and time.monotonic() < deadline:
            titles.extend(_card_titles())
            time.sleep(0.05)
        _answer_card(person, button)

    async def text(session, name, args=None):
        result = await session.call_tool(name, args or {})
        body = " ".join(getattr(c, "text", "") for c in result.content)
        return bool(result.is_error), body

    async def main():
        # The installed command, as a Claude session's MCP config starts it.
        command = os.path.join(os.path.dirname(sys.executable), "kwin-mcp-warden")
        params = StdioServerParameters(command=command, args=[], env=dict(os.environ))
        errlog = open(nested["root"] / "server-stderr.log", "w")  # noqa: SIM115
        async with (
            stdio_client(params, errlog=errlog) as (read, write),
            ClientSession(
                read, write, client_info=types.Implementation(name="stranger-agent", version="1")
            ) as session,
        ):
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            assert {"access_request", "access_list"} <= tools
            err, body = await text(
                session,
                "session_connect",
                {"dbus_address": info.dbus_address, "wayland_display": info.wayland_socket},
            )
            assert not err, body
            kate = _window(person, "kate")
            chars = _window(person, "kcharselect")
            at_kate = {"target": {"x": kate.frame.x + 450, "y": kate.frame.y + 400}}
            at_chars = {"target": {"x": chars.frame.x + 450, "y": chars.frame.y + 400}}
            err, body = await text(session, "pointer_click", at_kate)
            assert err and "isn't granted" in body, body
            err, body = await text(
                session,
                "dbus_call",
                {
                    "service": "org.kde.KWin",
                    "path": "/KWin",
                    "interface": "org.kde.KWin",
                    "method": "currentDesktop",
                },
            )
            assert err and "needs a reason" in body, body
            async with anyio.create_task_group() as tg:
                tg.start_soon(anyio.to_thread.run_sync, read_and_answer, "allow")
                err, body = await text(
                    session,
                    "access_request",
                    {"level": "control", "app": "org.kde.kate", "reason": "Test the warden."},
                )
            assert not err and "Allowed" in body, body
            assert titles == ["stranger-agent asks to control a Kate window"], titles
            err, body = await text(session, "pointer_click", at_kate)
            assert not err, body
            err, body = await text(session, "pointer_click", at_chars)
            assert err and "isn't granted" in body, body
            err, body = await text(session, "access_list")
            assert "Control in one Kate window" in body, body
            await text(session, "session_stop")

    try:
        anyio.run(main)
    finally:
        log_path = nested["root"] / "server-stderr.log"
        print("SERVER STDERR:", log_path.read_text()[-3000:] if log_path.exists() else "")
    state.press_stop()


def test_the_frame_follows_the_window(claude, person, apps):
    """The orange frame moves with the window Claude controls (pushed by the KWin script)."""
    from kwin_mcp.models import PartialRect
    from kwin_mcp.warden import log

    _ensure_control(claude, person)
    kate = _window(person, "kate")
    overlay = state.runtime_dir() / "overlay.json"

    def frame_x():
        try:
            frames = json.loads(overlay.read_text())["frames"]
        except (OSError, ValueError, KeyError):
            return None
        return frames[0]["frame"][0] if frames else None

    assert _wait_for(lambda: frame_x() is not None, 10), "no frame drawn"
    person.window_place(window=WindowSelector(id=kate.id), frame=PartialRect(x=kate.frame.x + 40))
    try:
        assert _wait_for(lambda: frame_x() == kate.frame.x + 40, 3), "the frame stayed behind"
    finally:
        person.window_place(window=WindowSelector(id=kate.id), frame=PartialRect(x=kate.frame.x))
    assert not [line for line in log.tail(50) if "stack watch" in line], log.tail(50)


# ── The corner notice ─────────────────────────────────────────────────────────


def test_the_corner_notice_ends_control(claude, person, apps):
    """While Claude holds Control, a notice sits top right of the primary screen; the person
    clicking End Control ends every grant. (The button lives in the overlay's own process, so
    its plant is done by hand: End Control sending nothing fails this test, 2026-11-14.)"""
    from kwin_mcp.warden.gate import _cmdline

    _ensure_control(claude, person)
    geometry = state.runtime_dir() / "notice-geometry.json"
    found = _wait_for(lambda: geometry.exists() and geometry.read_text(), timeout=10)
    assert found, "no corner notice while Claude holds Control"
    g = json.loads(found)
    left = g["screenX"] + g["screenWidth"] - g["window"][0]
    notice = _wait_for(
        lambda: [
            w
            for w in claude._stack()
            if "overlay.qml -- controls" in _cmdline(int(w.get("pid") or 0))
            and w["frame"][0] == left
            and w["frame"][1] == g["screenY"]
        ],
        timeout=5,
    )
    assert notice, "the notice isn't in the top-right corner of the primary screen"
    bx, by, bw, bh = g["button"]
    before = state.stop_time()
    person.pointer_click(target=Point(x=left + bx + bw // 2, y=g["screenY"] + by + bh // 2))
    assert _wait_for(lambda: state.stop_time() > before, 5), "End Control didn't end the grants"
    kate = _window(person, "kate")
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(kate.frame))


def test_the_corner_notice_rings_only_for_what_claude_did(claude, person, apps):
    """The marker's motion (E, "Live, inward"): one ring for each
    click, keypress or scroll Claude delivered, none for one the warden refused, and none at
    all while Plasma's animations are off. (The daemon reads Plasma's setting in its own
    process, so that part's plant is done by hand: motion_factor() returning 1 fails this
    test, 2026-11-14.)"""
    from pathlib import Path

    _ensure_control(claude, person)
    rings = state.runtime_dir() / "notice-rings.json"

    def count() -> int:
        try:
            return int(json.loads(rings.read_text())["rings"])
        except (OSError, ValueError, KeyError):
            return 0

    time.sleep(1.5)  # the notice is up and watching
    kate = _window(person, "kate")
    chars = _window(person, "kcharselect")
    before = count()
    claude.pointer_click(target=_center(kate.frame))
    assert _wait_for(lambda: count() > before, 3), "no ring for a click Claude delivered"
    time.sleep(1.0)
    before = count()
    with pytest.raises(Refused, match="isn't granted"):
        claude.pointer_click(target=_center(chars.frame))
    time.sleep(1.0)
    assert count() == before, "the marker rang for a click the warden refused"
    kdeglobals = Path(os.environ["XDG_CONFIG_HOME"]) / "kdeglobals"
    kdeglobals.write_text("[KDE]\nAnimationDurationFactor=0\n", encoding="utf-8")
    try:
        time.sleep(1.2)  # the daemon's next look at the setting
        before = count()
        claude.pointer_click(target=_center(kate.frame))
        time.sleep(1.0)
        assert count() == before, "the marker moved with Plasma's animations off"
    finally:
        kdeglobals.unlink()


def test_the_primary_monitor_without_kscreen_doctor(nested, monkeypatch):
    """The card and the corner notice find the primary monitor from KWin itself when KDE's
    kscreen-doctor isn't installed (review item, fixed 2026-11-14)."""
    from kwin_mcp.warden import card

    names = [
        s["name"] for s in nested["person"]._run_kwin_query({"op": "outputs"})["result"]["screens"]
    ]
    real_which = card.shutil.which
    monkeypatch.setattr(
        card.shutil, "which", lambda n: None if n == "kscreen-doctor" else real_which(n)
    )
    found = card.primary_output()
    assert found and found in names, (
        f"no primary monitor without kscreen-doctor: {found!r} of {names}"
    )


def _tray_item(nested) -> dict:
    """The warden's tray icon on the private bus, as the stand-in tray saw it register."""
    import subprocess

    from kwin_mcp.warden.gate import _cmdline

    items = nested["root"] / "tray-items"

    def warden_item() -> dict | None:
        if not items.exists():
            return None
        for line in items.read_text().splitlines():
            item = json.loads(line)
            owner = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.DBus",
                    "--object-path",
                    "/org/freedesktop/DBus",
                    "--method",
                    "org.freedesktop.DBus.GetConnectionUnixProcessID",
                    item["name"],
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
            found = re.search(r"uint32 (\d+)", owner)
            digits = found.group(1) if found else ""
            if digits and "overlay.qml -- controls" in _cmdline(int(digits)):
                return {**item, "pid": int(digits)}
        return None

    item = _wait_for(warden_item, 15)
    registered = items.read_text() if items.exists() else None
    assert item, f"the warden's tray icon never registered: {registered}"
    return item


def _tray_call(item: dict, *args: str) -> str:
    import subprocess

    return subprocess.run(
        [
            "gdbus",
            "call",
            "--session",
            "--dest",
            item["name"],
            "--object-path",
            item["path"],
            "--method",
            *args,
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    ).stdout


def _access_list(claude, item: dict) -> list:
    return [
        w for w in claude._stack() if int(w.get("pid") or 0) == item["pid"] and w["frame"][2] == 420
    ]


def test_the_tray_icon_opens_the_access_list(claude, person, nested, apps):
    """The tray icon shows while Claude holds a grant, says so, and clicking it opens the
    access list (the panel's own route: org.kde.StatusNotifierItem.Activate). Deferred
    item 8, tested 2026-11-14 with a stand-in for Plasma's tray on the private bus."""
    _ensure_control(claude, person)
    item = _tray_item(nested)

    def call(*args: str) -> str:
        return _tray_call(item, *args)

    def access_list() -> list:
        return _access_list(claude, item)

    def tooltip() -> str:
        return call("org.freedesktop.DBus.Properties.Get", "org.kde.StatusNotifierItem", "ToolTip")

    # The overlay reads the grants a few times a second, so a fresh grant shows a moment later.
    said = _wait_for(lambda: "1 grant" in tooltip(), 5)
    assert said, f"the tray icon doesn't say the agent holds a grant: {tooltip()!r}"

    assert not access_list(), "the access list was open before anyone clicked the icon"
    call("org.kde.StatusNotifierItem.Activate", "0", "0")
    opened = _wait_for(access_list, 5)
    assert opened, "clicking the tray icon didn't open the access list"
    # It stays put: the same window the whole time it's open, not shown again each time
    # the overlay reads its state (four times a second).
    seen = {opened[0]["id"]}
    for _ in range(8):
        time.sleep(0.2)
        seen |= {w["id"] for w in access_list()}
    assert len(seen) == 1 and access_list(), f"the open list was shown again: {seen}"
    call("org.kde.StatusNotifierItem.Activate", "0", "0")
    assert _wait_for(lambda: not access_list(), 5), "a second click didn't close the access list"

    # A real click moves focus right after the list opens (Plasma's panel, its focus token):
    # the list must stay open through that (the tray icon's first click once did nothing).
    # The list has focus as it opens; focus moves to Kate a moment later. Only a move that
    # landed inside the first moment (well under the overlay's 600 ms) is judged:
    # a slow moment in this test's own calls once made a late move look early (2026-11-16).
    time.sleep(0.5)
    kate = _window(person, "kate")
    judged = 0
    for _ in range(4):
        started = time.monotonic()
        call("org.kde.StatusNotifierItem.Activate", "0", "0")
        opened = _wait_for(access_list, 5, step=0.02)
        assert opened, "clicking the tray icon didn't open the access list"
        person.window_focus(window=WindowSelector(id=kate.id))
        early = time.monotonic() - started < 0.4
        time.sleep(1.0)
        if early:
            judged += 1
            assert access_list(), "the access list closed when focus moved in its first moment"
            break
        if access_list():  # too late to judge: close it and try again
            call("org.kde.StatusNotifierItem.Activate", "0", "0")
            assert _wait_for(lambda: not access_list(), 5)
        time.sleep(0.5)
    assert judged, "focus never moved inside the list's first moment, in four tries"
    x, y, w, _h = opened[0]["frame"]
    # Later, focus moving away is the person clicking elsewhere: it closes.
    person.pointer_click(target=Point(x=x + w // 2, y=y + 22))
    time.sleep(0.2)
    person.window_focus(window=WindowSelector(id=kate.id))
    assert _wait_for(lambda: not access_list(), 5), "clicking elsewhere didn't close the list"


def test_the_overlay_names_the_agent_that_holds_the_grant(claude, person, nested, apps):
    """Every name the overlay draws is the agent's that holds the grant, never a fixed
    "Claude", whatever agent is connecting to it: the
    tray icon's tooltip, the tag over the window, the corner notice, the label on the
    pointer and the access list's title, read from the overlay itself."""

    def shown(text: str, exact: bool = False) -> bool:
        """Whether the overlay shows ``text`` (Kate and KCharSelect are the test's apps)."""
        return any(
            (e.name == text if exact else text in (e.name or ""))
            and not any(app in e.app.lower() for app in ("kate", "kcharselect"))
            for e in person.element_query(locator=Locator(query=text)).elements
        )

    claude.warden.set_client("stranger-agent")
    try:
        _ensure_control(claude, person)
        item = _tray_item(nested)
        tooltip = _wait_for(
            lambda: (
                "stranger-agent holds 1 grant"
                in _tray_call(
                    item,
                    "org.freedesktop.DBus.Properties.Get",
                    "org.kde.StatusNotifierItem",
                    "ToolTip",
                )
            ),
            timeout=5,
        )
        assert tooltip, "the tray icon's tooltip doesn't name the agent"
        assert _wait_for(lambda: shown("stranger-agent is controlling Kate"), 10), (
            "the tag over Kate doesn't name the agent"
        )
        assert _wait_for(lambda: shown("stranger-agent is in control"), 10), (
            "the corner notice doesn't name the agent"
        )
        kate = _window(person, "kate")
        claude.pointer_move(target=_center(kate.frame))
        assert _wait_for(lambda: shown("stranger-agent", exact=True), 1.5, step=0.05), (
            "the pointer label doesn't name the agent"
        )
        _tray_call(item, "org.kde.StatusNotifierItem.Activate", "0", "0")
        assert _wait_for(lambda: _access_list(claude, item), 5), "the access list didn't open"
        assert _wait_for(lambda: shown("stranger-agent's access"), 5), (
            "the access list's title doesn't name the agent"
        )
        assert not shown("Claude"), "the overlay still says Claude somewhere"
        _tray_call(item, "org.kde.StatusNotifierItem.Activate", "0", "0")
        assert _wait_for(lambda: not _access_list(claude, item), 5)
    finally:
        claude.warden.set_client("claude-code")


# ── Typing into a field, not at your focus ────────────────────────────────────


def _kate_editor(engine):
    """Kate's editable text area, found the way Claude would find it."""
    found = _wait_for(
        lambda: [
            e
            for e in engine.element_query(locator=Locator(app="kate", states=["editable"])).elements
            if e.ref
        ],
        timeout=15,
    )
    assert found, "Kate shows no editable text over AT-SPI"
    return found[0]


def test_typing_into_a_field_needs_no_focus(claude, person, hands, apps):
    """keyboard_type(into=...) writes into Kate while the person works in another window: no
    focus taken, no keys sent, and no wait for the person's keys."""
    _ensure_control(claude, person)
    charselect = _window(person, "kcharselect")
    person.window_focus(window=WindowSelector(id=charselect.id))
    editor = _kate_editor(claude)
    hands("key")  # the person is typing in their own window
    started = time.monotonic()
    result = claude.keyboard_type(text="written without focus", into=RefAddr(ref=editor.ref))
    took = time.monotonic() - started
    assert result.route == "accessibility"
    assert not [w for w in result.warnings if "didn't find" in w], result.warnings
    assert took < claude.warden.settings.pause_seconds - 0.5, f"it waited {took:.1f} s for keys"
    active = next(w for w in person.window_list().windows if w.active)
    assert active.id == charselect.id, "writing into Kate took the person's focus"
    # Any text, sized in the toolkit's own units (Qt counts UTF-16: an emoji is two).
    claude.keyboard_type(text=" café 🦊 ✓", into=RefAddr(ref=_kate_editor(claude).ref))
    # Read Kate's text back another way: the person's own query, not the write's read-back.
    # Exactly that text, in that order, with nothing after it (a length in the wrong unit
    # makes Qt pad the text with stray characters; a caret left behind reverses the order).
    text = _kate_editor(person).text
    at = text.find("written without focus café 🦊 ✓")
    assert at >= 0, repr(text[-80:])
    rest = text[at + len("written without focus café 🦊 ✓") :]
    assert rest.strip("\n") == "" or rest[0] == "\n", f"stray text after the write: {rest[:20]!r}"


def _kwin_js(body: str) -> str:
    """Run a KWin script on the nested desktop (the person's side) and return its report."""
    import subprocess
    import sys

    js = body + ' callDBus("{sink}", "{path}", "{sink}", "Report", String(out));'
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from kwin_mcp.geometry import run_kwin_script;"
            " print(run_kwin_script(sys.argv[1]))",
            js,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    return out.stdout.strip() or out.stderr.strip()


def _app_windows_js(fragment: str) -> str:
    return (
        "const wins = workspace.windowList().filter(w => w.normalWindow &&"
        f" (w.resourceClass || '').toLowerCase().indexOf({fragment!r}) >= 0);"
    )


def _set_minimized(fragment: str, flag: bool) -> None:
    """The person minimizes (or brings back) an app's windows, through KWin's own scripting."""
    value = "true" if flag else "false"
    out = _kwin_js(
        _app_windows_js(fragment) + f" wins.forEach(w => w.minimized = {value});"
        " const out = wins.length;"
    )
    assert out not in ("", "0"), f"no {fragment} window to minimize: {out}"


def _minimized(fragment: str) -> bool:
    out = _kwin_js(
        _app_windows_js(fragment) + " const out = wins.length > 0 && wins.every(w => w.minimized);"
    )
    return out == "true"


def test_replace_puts_its_text_in_place_of_the_fields(claude, person, apps):
    """keyboard_type(into=..., replace=True) takes the place of everything in the field,
    and the log says the field's text was replaced."""
    from kwin_mcp.warden import log

    _ensure_control(claude, person)
    claude.keyboard_type(text="old text to go", into=RefAddr(ref=_kate_editor(claude).ref))
    claude.keyboard_type(
        text="only this ✓", into=RefAddr(ref=_kate_editor(claude).ref), replace=True
    )
    assert _kate_editor(person).text.rstrip("\n") == "only this ✓", _kate_editor(person).text
    assert "replaced a field's text with 11 characters" in log.tail(3)[-1], log.tail(3)


def test_typing_into_a_minimized_window(claude, person, apps):
    """A minimized window's field takes the text too: no focus, nothing shown."""
    _ensure_control(claude, person)
    editor = _kate_editor(claude)
    _set_minimized("kate", True)
    try:
        assert _wait_for(lambda: _minimized("kate"), 5), "Kate didn't minimize"
        claude.keyboard_type(text="while minimized", into=RefAddr(ref=editor.ref), replace=True)
        assert _kate_editor(person).text.rstrip("\n") == "while minimized"
        assert _minimized("kate"), "writing brought the minimized window back"
    finally:
        _set_minimized("kate", False)


def test_typing_into_a_gtk_field(claude, person, apps):
    """A GTK 4 field (zenity's entry): GTK counts the length in bytes, offsets in
    characters, so accents and an emoji must land whole; then replace."""
    if not shutil.which("zenity"):
        pytest.skip("zenity (GTK 4) isn't installed here")
    person.app_launch(command="zenity --entry --title=warden-gtk --text=Name")
    win = _wait_for(lambda: _window(person, "zenity"), 20)
    assert win, "zenity never opened"
    try:
        app_id = win.app_id
        _ensure_control(claude, person, app=app_id)
        field = StrictAddr(strict=Locator(app="zenity", states=["editable"]))
        claude.keyboard_type(text="café 🦊 ✓", into=field)

        def text() -> str:
            found = person.element_query(locator=Locator(app="zenity", states=["editable"]))
            return found.elements[0].text if found.elements else ""

        assert text() == "café 🦊 ✓", repr(text())
        claude.keyboard_type(text="replaced", into=field, replace=True)
        assert text() == "replaced", repr(text())
    finally:
        with contextlib.suppress(Exception):
            person.window_close(window=WindowSelector(app_id="zenity"))
        # Gone before the next test, which may open a zenity of its own.
        assert _wait_for(lambda: _window(person, "zenity") is None, 10), "zenity stayed open"


@pytest.mark.parametrize("kind", ["single-line", "multi-line"])
def test_typing_into_gtk3_fields(claude, person, apps, kind):
    """GTK 3 (through ATK and at-spi2-atk, not GTK 4's own AT-SPI code): a one-line entry
    and a text view take accents and an emoji whole, then replace."""
    import sys

    _needs_gtk("3.0")
    helper = Path(__file__).with_name("gtk3_app.py")
    person.app_launch(command=f"{sys.executable} {helper}")
    win = _wait_for(lambda: _window(person, "gtk3-fields"), 20)
    assert win, "the GTK 3 window never opened"
    locator = Locator(app="gtk3-fields", states=["editable", kind])
    try:
        _ensure_control(claude, person, app=win.app_id)
        assert _wait_for(lambda: person.element_query(locator=locator).elements, 10)
        field = StrictAddr(strict=locator)
        claude.keyboard_type(text="café 🦊 ✓", into=field)

        def text() -> str:
            found = person.element_query(locator=locator)
            return found.elements[0].text if found.elements else ""

        assert text() == "café 🦊 ✓", repr(text())
        claude.keyboard_type(text="replaced", into=field, replace=True)
        assert text() == "replaced", repr(text())
    finally:
        with contextlib.suppress(Exception):
            person.window_close(window=WindowSelector(app_id="gtk3-fields"))
        assert _wait_for(lambda: _window(person, "gtk3-fields") is None, 10), "it stayed open"


def _open_chrome(person, nested, flags=""):
    """Google Chrome on a page whose one field has the focus and copies what it holds into
    the window's title, so the test can read the field without accessibility. A fresh
    profile in the sandbox, never the person's."""
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("Google Chrome isn't installed here")
    page = nested["root"] / "field.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>empty</title>"
        '<input aria-label="Name" autofocus '
        "oninput=\"document.title = '[' + this.value + ']'\">",
        encoding="utf-8",
    )
    person.app_launch(
        command=f"{chrome} --user-data-dir={nested['root'] / 'chrome-profile'} --no-first-run"
        f" --no-default-browser-check --password-store=basic {flags} file://{page}"
    )
    win = _wait_for(lambda: _window(person, "chrome"), 30)
    assert win, "Chrome never opened"
    assert _wait_for(lambda: (_window(person, "chrome") or win).caption.startswith("empty"), 15)
    return win


def _close_chrome(person, win):
    with contextlib.suppress(Exception):
        person.window_close(window=WindowSelector(app_id=win.app_id))
    assert _wait_for(lambda: _window(person, "chrome") is None, 15), "Chrome stayed open"


def test_typing_into_chrome(claude, person, nested, apps):
    """Chrome as a person runs it: accessibility shows its window but nothing inside
    (Chromium builds that only for Orca or --force-renderer-accessibility), so typing
    straight into a field is refused with that reason; typing by keys into the focused
    field works, accents and an emoji whole."""
    win = _open_chrome(person, nested)
    try:
        _ensure_control(claude, person, app=win.app_id)
        with pytest.raises(RuntimeError, match="nothing inside them"):
            claude.keyboard_type(
                text="x", into=StrictAddr(strict=Locator(app="chrome", states=["editable"]))
            )
        person.window_focus(window=WindowSelector(id=win.id))
        claude.keyboard_type(text="café 🦊 ✓", method="unicode")
        typed = lambda: (_window(person, "chrome") or win).caption  # noqa: E731
        assert _wait_for(lambda: typed().startswith("[café 🦊 ✓]"), 10), typed()
    finally:
        _close_chrome(person, win)


def test_chrome_fields_cant_be_written_over_accessibility(claude, person, nested, apps):
    """Chrome started with its accessibility flag shows its fields, but Chromium has no
    AT-SPI EditableText, so a write into one is refused and says to click and type."""
    win = _open_chrome(person, nested, "--force-renderer-accessibility")
    try:
        _ensure_control(claude, person, app=win.app_id)
        field = Locator(app="chrome", query="Name", states=["editable"])
        assert _wait_for(lambda: person.element_query(locator=field).elements, 20)
        with pytest.raises(RuntimeError, match="isn't editable text"):
            claude.keyboard_type(text="x", into=StrictAddr(strict=field))
    finally:
        _close_chrome(person, win)


def _open_firefox(person, nested, body):
    """Firefox from PATH or $WARDEN_FIREFOX on a page of ``body``, in a fresh profile in
    the sandbox (never the person's), with the first start's pages and dialogs off."""
    firefox = os.environ.get("WARDEN_FIREFOX") or shutil.which("firefox")
    if not firefox:
        pytest.skip("Firefox isn't installed here (or set WARDEN_FIREFOX)")
    profile = nested["root"] / "firefox-profile"
    profile.mkdir(exist_ok=True)
    prefs = {
        "browser.aboutwelcome.enabled": "false",
        "browser.startup.homepage_override.mstone": '"ignore"',
        "startup.homepage_welcome_url": '""',
        "datareporting.policy.dataSubmissionEnabled": "false",
        "browser.shell.checkDefaultBrowser": "false",
        "browser.tabs.warnOnClose": "false",
        # the first start's terms of use dialog, over the page
        "browser.preonboarding.enabled": "false",
        "termsofuse.bypassNotification": "true",
        "datareporting.policy.dataSubmissionPolicyBypassNotification": "true",
    }
    (profile / "user.js").write_text(
        "".join(f'user_pref("{key}", {value});\n' for key, value in prefs.items()),
        encoding="utf-8",
    )
    page = nested["root"] / "fields.html"
    page.write_text(f"<!doctype html><meta charset=utf-8><title>empty</title>{body}", "utf-8")
    person.app_launch(command=f"{firefox} --no-remote --profile {profile} file://{page}")
    win = _wait_for(lambda: _window(person, "firefox"), 40)
    if not win:
        pytest.fail("Firefox never opened")
    return win


def _close_firefox(person, win):
    with contextlib.suppress(Exception):
        person.window_close(window=WindowSelector(app_id=win.app_id))
    assert _wait_for(lambda: _window(person, "firefox") is None, 15), "Firefox stayed open"


# Each field copies what it holds into the window's title, so the test reads the page
# itself, not only what accessibility says.
_FIREFOX_FIELDS = (
    '<input aria-label="One line" autofocus '
    "oninput=\"document.title = '[' + this.value + ']'\"><br>"
    '<textarea aria-label="Many lines" '
    "oninput=\"document.title = '[' + this.value + ']'\"></textarea>"
)


def test_typing_into_firefox(claude, person, nested, apps):
    """Firefox shows its page's fields to accessibility; typing by keys into the focused
    field works, accents and an emoji whole."""
    win = _open_firefox(person, nested, _FIREFOX_FIELDS)
    try:
        _ensure_control(claude, person, app=win.app_id)
        one = Locator(app="firefox", query="One line", states=["editable", "single-line"])
        many = Locator(app="firefox", query="Many lines", states=["editable", "multi-line"])
        assert _wait_for(lambda: person.element_query(locator=one).elements, 20)
        assert person.element_query(locator=many).elements, "the textarea isn't shown"
        person.window_focus(window=WindowSelector(id=win.id))
        claude.keyboard_type(text="café 🦊 ✓", method="unicode")
        caption = lambda: (_window(person, "firefox") or win).caption  # noqa: E731
        assert _wait_for(lambda: caption().startswith("[café 🦊 ✓]"), 10), caption()
    finally:
        _close_firefox(person, win)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Firefox takes an AT-SPI write and drops it (Mozilla bug 1861026, open); "
    "this turns green when Firefox fixes it",
)
@pytest.mark.parametrize(("kind", "name"), [("single-line", "One line"), ("multi-line", "Many")])
def test_writing_into_firefox_fields(claude, person, nested, apps, kind, name):
    """Typing straight into a field (Gecko has AT-SPI EditableText): an <input> and a
    <textarea> take accents and an emoji whole, then replace. The page must see it."""
    win = _open_firefox(person, nested, _FIREFOX_FIELDS)
    locator = Locator(app="firefox", query=name, states=["editable", kind])
    try:
        # Setup failures are real failures, never the known one (pytest.fail isn't an
        # AssertionError).
        if _held(claude, win.app_id, "control") is None:
            allowed, text = _ask(claude, person, "control", win.app_id)
            if not allowed:
                pytest.fail(text)
        if not _wait_for(lambda: person.element_query(locator=locator).elements, 20):
            pytest.fail("Firefox never showed the field to accessibility")
        field = StrictAddr(strict=locator)
        done = claude.keyboard_type(text="café 🦊 ✓", into=field)
        caption = lambda: (_window(person, "firefox") or win).caption  # noqa: E731
        assert _wait_for(lambda: caption().startswith("[café 🦊 ✓]"), 5), (
            caption(),
            done.warnings,
        )
        claude.keyboard_type(text="replaced", into=field, replace=True)
        assert _wait_for(lambda: caption().startswith("[replaced]"), 5), caption()
    finally:
        _close_firefox(person, win)


def test_typing_into_a_gtk_text_view(claude, person, nested, apps):
    """A GTK 4 text view (zenity's editable text): unlike a one-line entry, which ignores
    the length, it takes the length in bytes (gtk/a11y/gtkatspieditabletext.c,
    gtk_text_buffer_insert), so a length in the wrong unit cuts the text short."""
    empty = nested["root"] / "empty.txt"
    empty.write_text("", encoding="utf-8")
    if not shutil.which("zenity"):
        pytest.skip("zenity (GTK 4) isn't installed here")
    person.app_launch(command=f"zenity --text-info --editable --filename={empty}")
    win = _wait_for(lambda: _window(person, "zenity"), 20)
    assert win, "zenity never opened"
    try:
        _ensure_control(claude, person, app=win.app_id)
        field = StrictAddr(strict=Locator(app="zenity", states=["editable", "multi-line"]))
        claude.keyboard_type(text="café 🦊 ✓", into=field)

        def text() -> str:
            found = person.element_query(
                locator=Locator(app="zenity", states=["editable", "multi-line"])
            )
            return found.elements[0].text if found.elements else ""

        assert text() == "café 🦊 ✓", repr(text())
        claude.keyboard_type(text="replaced", into=field, replace=True)
        assert text() == "replaced", repr(text())
    finally:
        with contextlib.suppress(Exception):
            person.window_close(window=WindowSelector(app_id="zenity"))
        # Gone before the next test, which may open a zenity of its own.
        assert _wait_for(lambda: _window(person, "zenity") is None, 10), "zenity stayed open"


def test_a_field_in_an_app_not_granted_is_refused(claude, person, apps):
    """The grant is checked on the field's own window: Control in Kate, with Kate focused,
    doesn't let Claude write into Character Selector's search field."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    time.sleep(0.3)
    with pytest.raises(Refused, match="Nothing in a window you're granted matches"):
        claude.keyboard_type(
            text="nope",
            into=StrictAddr(strict=Locator(app="kcharselect", states=["editable"])),
        )


def test_a_field_that_matches_nothing_in_an_app_not_granted_is_refused(claude, person, apps):
    """A field that matches nothing goes on to the engine (which says why) only when every
    window of the app it names is granted: with Control in Kate, a missing field in
    Character Selector is refused as not granted, and its windows aren't described."""
    _ensure_control(claude, person)
    kate = _window(person, "kate")
    person.window_focus(window=WindowSelector(id=kate.id))
    time.sleep(0.3)
    with pytest.raises(Refused, match="Nothing in a window you're granted matches"):
        claude.keyboard_type(
            text="nope",
            into=StrictAddr(strict=Locator(app="kcharselect", query="no such field")),
        )


def test_a_locator_cant_read_windows_not_granted(claude, person, apps):
    """A locator naming no app can't tell what's in windows that aren't granted: with
    Control in Kate, a query that matches only Character Selector's field is refused in
    the same words as one that matches nothing, for typing and for actions alike."""
    _ensure_control(claude, person)
    fields = person.element_query(locator=Locator(app="kcharselect", states=["editable"]))
    named = [e.name for e in fields.elements if e.name]
    assert named, "Character Selector shows no named field to look for"
    hidden = named[0]
    in_kate = person.element_query(locator=Locator(app="kate", query=hidden)).elements
    assert not in_kate, f"Kate has something named {hidden!r} too; pick another field"

    def refusals(query: str) -> tuple[str, str]:
        field = StrictAddr(strict=Locator(query=query))
        said = []
        for call in (
            lambda: claude.keyboard_type(text="x", into=field),
            lambda: claude.element_act(element=field, action="x"),
        ):
            with pytest.raises(Refused) as caught:
                call()
            said.append(str(caught.value).replace(repr(query), "Q"))
        return said[0], said[1]

    assert refusals(hidden) == refusals("zz no such zz"), (
        "a refusal told a hidden match from no match"
    )


# ── A private desktop leaves the person's desktop services alone ──────────────


def test_a_private_desktop_cant_take_the_document_portal(tmp_path):
    """A private desktop's bus can't start a document portal of its own: on the person's
    runtime folder it would take over $XDG_RUNTIME_DIR/doc and theirs would exit, leaving
    every Flatpak app without its files (upstream issue #132; it happened twice on
    2026-11-14). Tried here in a runtime folder of the test's own."""
    import shutil
    import subprocess

    from kwin_mcp.session import _write_private_bus_config

    if not shutil.which("dbus-run-session"):
        pytest.skip("dbus-run-session isn't installed")
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    conf = _write_private_bus_config(tmp_path / "bus")
    assert conf is not None, "this system has no stock session.conf"

    def mounted() -> bool:
        return (
            subprocess.run(["findmnt", "-n", str(run / "doc")], capture_output=True).returncode == 0
        )

    # Ask for the portal's mount point, then look while the private bus still runs.
    probe = (
        "gdbus call --session --dest org.freedesktop.portal.Documents "
        "--object-path /org/freedesktop/portal/documents "
        "--method org.freedesktop.portal.Documents.GetMountPoint >/dev/null 2>&1; "
        'sleep 0.5; findmnt -n "$XDG_RUNTIME_DIR/doc" >/dev/null && echo MOUNTED || true'
    )
    try:
        out = subprocess.run(
            ["dbus-run-session", f"--config-file={conf}", "--", "sh", "-c", probe],
            env={**os.environ, "XDG_RUNTIME_DIR": str(run)},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        ).stdout
        assert "MOUNTED" not in out, "the private bus started a document portal of its own"
    finally:
        if mounted():
            subprocess.run(["fusermount3", "-u", "-z", str(run / "doc")], check=False)


def test_a_private_desktop_has_no_portal_that_half_starts(tmp_path):
    """Without its permission store the desktop portal can't run on a private bus; it must
    fail at once, not take its name with nothing behind it and exit, to be started again
    by every request (Firefox then never opens a window)."""
    import shutil
    import subprocess

    from kwin_mcp.session import _write_private_bus_config

    if not shutil.which("dbus-run-session") or not shutil.which("gdbus"):
        pytest.skip("dbus-run-session or gdbus isn't installed")
    if not Path("/usr/share/dbus-1/services/org.freedesktop.portal.Desktop.service").exists():
        pytest.skip("xdg-desktop-portal isn't installed here")
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    conf = _write_private_bus_config(tmp_path / "bus")
    assert conf is not None, "this system has no stock session.conf"
    call = (
        "gdbus call --session --dest org.freedesktop.portal.Desktop "
        "--object-path /org/freedesktop/portal/desktop "
        "--method org.freedesktop.portal.Settings.ReadAll '[]'"
    )
    done = subprocess.run(
        ["dbus-run-session", f"--config-file={conf}", "--", "sh", "-c", call],
        env={**os.environ, "XDG_RUNTIME_DIR": str(run)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    said = done.stdout + done.stderr
    assert "Successfully activated service 'org.freedesktop.portal.Desktop'" not in said, said
    assert "UnknownMethod" not in said, said
    # The stand-in answered, so the portal never ran at all (not just absent here).
    assert "Spawn.ChildExited" in said, said


# ── The accessibility switch ──────────────────────────────────────────────────


def _switch(value: bool | None = None) -> bool:
    """The nested desktop's accessibility switch: set it, then read it."""
    from kwin_mcp.session import accessibility_enabled, set_accessibility_enabled

    bus = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    if value is not None:
        set_accessibility_enabled(bus, value)
    return accessibility_enabled(bus)


def test_accessibility_is_asked_for_and_turned_back_off(claude, person, apps):
    """Off at the start: Claude asks on a card, the switch turns on, and it goes back off
    when the last grant ends (design/mock/index.html, "Accessibility")."""
    from kwin_mcp.warden import a11y

    try:
        assert _switch(False) is False
        _ensure_control(claude, person)
        allowed, text = _ask(claude, person, "accessibility", "")
        assert allowed, text
        assert _switch() is True, "the person allowed it, but the switch stayed off"
        assert a11y.ours(), "the warden didn't record that it turned the switch on"
        state.press_stop()
        assert _wait_for(lambda: not _switch(), 10), "the switch stayed on after the last grant"
        # The daemon turns the switch off, then drops its record a moment later.
        assert _wait_for(lambda: not a11y.ours(), 3), "the warden's record outlived the switch"
    finally:
        _switch(True)


def test_the_switch_goes_off_when_the_warden_dies_without_doing_it(
    claude, person, apps, monkeypatch
):
    """The daemon is killed and the session's grants end with nothing left to bring it
    back (a crash of the whole warden): the guard started with the switch turns it off,
    with no reboot and no new warden start."""
    import signal

    from kwin_mcp.warden import a11y, gate, log

    try:
        claude.warden.a11y_declined = False
        assert _switch(False) is False
        _ensure_control(claude, person)
        allowed, text = _ask(claude, person, "accessibility", "")
        assert allowed and _switch() is True, text
        assert _wait_for(a11y._guard_running, 5), "no guard started with the switch"
        # The crash: nothing may start the daemon again, then it dies, then the grants end.
        monkeypatch.setattr(gate, "ensure_daemon", lambda: None)
        pid = int((state.runtime_dir() / "daemon.pid").read_text())
        assert b"kwin_mcp.warden.daemon" in Path(f"/proc/{pid}/cmdline").read_bytes()
        os.kill(pid, signal.SIGKILL)

        def dead() -> bool:  # gone, or a zombie (this test process started it)
            try:
                return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] in "ZX"
            except (OSError, IndexError):
                return True

        assert _wait_for(dead, 5), "the daemon didn't die"
        state.press_stop()
        assert _wait_for(lambda: not _switch(), 10), "the switch stayed on after the crash"
        assert not a11y.ours()
        assert any("the warden stopped without it" in line for line in log.tail(5)), log.tail(5)
        assert _wait_for(lambda: not a11y._guard_running(), 5), "the guard outlived its job"
    finally:
        monkeypatch.undo()
        gate.ensure_daemon()
        _switch(True)


def test_the_access_lists_turn_off_ends_accessibility(claude, person, nested, apps):
    """The access list's Accessibility row: Turn Off puts the switch back off at once,
    while Claude still holds its other grants (design/mock/index.html, "Accessibility")."""
    from kwin_mcp.warden import a11y

    try:
        claude.warden.a11y_declined = False
        assert _switch(False) is False
        _ensure_control(claude, person)
        allowed, text = _ask(claude, person, "accessibility", "")
        assert allowed and _switch() is True, text
        item = _tray_item(nested)
        _tray_call(item, "org.kde.StatusNotifierItem.Activate", "0", "0")
        assert _wait_for(lambda: _access_list(claude, item), 5), (
            "the access list didn't open: the overlay's windows are "
            f"{[w['frame'] for w in claude._stack() if int(w.get('pid') or 0) == item['pid']]}"
        )
        # The person's click, at the button the list shows (found over accessibility, which
        # is on: the warden's own list joined when the switch turned on). Where KWin gives
        # it no position (KWin 6.3, Debian 13), the person's screen reader presses it.
        found = _wait_for(
            lambda: (
                person.element_query(
                    locator=Locator(query="Turn Off", role="button", states=["showing"])
                ).elements
            ),
            timeout=10,
        )
        if not found:
            seen = [
                (e.app, e.role, e.name, e.states)
                for e in person.element_query(locator=Locator(query="Turn Off")).elements
            ]
            pytest.fail(f"no Turn Off button showing over accessibility: {seen}")
        assert len(found) == 1, f"more than one Turn Off showing: {found}"
        b = found[0].rect
        if b is not None:
            person.pointer_click(target=Point(x=b.x + b.width // 2, y=b.y + b.height // 2))
        else:
            person.element_act(element=RefAddr(ref=found[0].ref), action="Press")
        assert _wait_for(lambda: not _switch(), 5), "Turn Off left the switch on"
        # The daemon turns the switch off, then drops its record a moment later.
        assert _wait_for(lambda: not a11y.ours(), 3), "the warden's record outlived the switch"
        assert _held(claude, "org.kde.kate", "control"), "Turn Off ended Claude's other grants too"
    finally:
        with contextlib.suppress(Exception):
            item = _tray_item(nested)
            if _access_list(claude, item):
                _tray_call(item, "org.kde.StatusNotifierItem.Activate", "0", "0")
        _switch(True)


def test_a_switch_left_on_before_a_restart_goes_back_off(claude, person, nested, apps):
    """A crash or a reboot kept the warden from turning the switch off, and the desktop
    turned it back on at login: at the next login (install.sh's autostart entry), or the
    next time the warden starts, it goes back off. The record names the old login's bus,
    which is gone (a bus address can change between logins)."""
    import subprocess
    import sys

    from conftest import _stop_daemon

    from kwin_mcp.warden import a11y, gate

    marker = a11y._marker()
    marker.parent.mkdir(parents=True, exist_ok=True)
    gone_bus = f"unix:path={nested['root']}/a-login-before-a-reboot/bus"
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])

    def daemon_down() -> bool:
        return not a11y._daemon_running()

    try:
        state.press_stop()
        time.sleep(1.0)
        # At login no warden runs; the entry turns the switch off.
        _stop_daemon(runtime)
        assert _wait_for(daemon_down, 5), "the daemon didn't stop"
        _switch(True)
        marker.write_text(gone_bus, encoding="utf-8")
        out = subprocess.run(
            [sys.executable, "-m", "kwin_mcp.warden.a11y", "--after-login"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert "turned accessibility back off" in out.stdout, out.stdout + out.stderr
        assert _switch() is False, "the login entry left the switch on"
        assert not marker.exists()
        # Without the login entry, the warden's next start does it.
        _switch(True)
        marker.write_text(gone_bus, encoding="utf-8")
        gate.ensure_daemon()
        assert _wait_for(lambda: not _switch(), 10), "the next start left the switch on"
        # A record of a bus that's still running, but isn't this desktop's, isn't ours.
        _switch(True)
        marker.write_text(f"unix:path={runtime}/bus-of-another-desktop", encoding="utf-8")
        (runtime / "bus-of-another-desktop").touch()
        time.sleep(3)
        assert _switch() is True, "the warden turned off another desktop's switch"
    finally:
        marker.unlink(missing_ok=True)
        (runtime / "bus-of-another-desktop").unlink(missing_ok=True)
        _switch(True)


def test_keep_it_off_holds_for_the_session(claude, person, apps):
    """Keep It Off: Claude is told no, and isn't asked again this session."""
    try:
        claude.warden.a11y_declined = False
        assert _switch(False) is False
        _ensure_control(claude, person)
        allowed, text = _ask(claude, person, "accessibility", "", button="deny")
        assert not allowed and "kept accessibility off" in text, text
        assert _switch() is False
        done: dict = {}

        def again():
            done["value"] = claude.warden.request("accessibility", "", "Please, once more.")

        thread = threading.Thread(target=again)
        thread.start()
        carded = _wait_for(state.card_is_open, 2)
        if carded:
            _answer_card(person, "deny")
        thread.join(60)
        assert not carded, "a second card asked after the person kept it off"
        assert not done["value"][0] and "don't ask again" in done["value"][1], done
    finally:
        claude.warden.a11y_declined = False
        _switch(True)


def test_a_switch_already_on_is_left_on(claude, person, apps):
    """On already (a screen reader, or the person's choice): no card, and the warden never
    turns it off."""
    _switch(True)
    _ensure_control(claude, person)
    started = time.monotonic()
    allowed, text = claude.warden.request("accessibility", "", "Read the text field.")
    assert allowed and "already on" in text, text
    assert time.monotonic() - started < 3 and not state.card_is_open()
    state.press_stop()
    time.sleep(3)
    assert _switch() is True, "the warden turned off a switch it didn't turn on"


def test_session_connect_cant_flip_the_switch(nested):
    """session_connect(enable_accessibility=true) leaves the switch alone: only the card
    turns it on."""
    from kwin_mcp.warden.gate import A11Y_OFF_HINT, WardenEngine

    engine = WardenEngine()
    try:
        assert _switch(False) is False
        info = engine.session_connect(
            dbus_address=nested["info"].dbus_address,
            wayland_display=nested["info"].wayland_socket,
            enable_accessibility=True,
        )
        assert _switch() is False, "session_connect turned the switch on without a card"
        assert A11Y_OFF_HINT in info.warnings, info.warnings
    finally:
        engine.session_stop()
        _switch(True)


# ── Tests that start a second private desktop ─────────────────────────────────
# Each private desktop's bus has a runtime folder of its own now, so a second one no
# longer takes over the first one's accessibility socket
# (test_a_second_screen_leaves_the_first_ones_accessibility_alone, docs/deferred.md 17).


def test_own_screen_apps_cant_reach_your_desktop(nested, person):
    """Launches in Claude's own screen can't point at the person's display or bus."""
    from kwin_mcp.warden.gate import WardenEngine

    own = WardenEngine()
    allowed, text = _ask(own, person, "own_screen", "")
    assert allowed, text
    own.session_start(screen_width=640, screen_height=480, home="isolated")
    try:
        with pytest.raises(Refused, match="WAYLAND_DISPLAY"):
            own.app_launch(command="kcharselect", env={"WAYLAND_DISPLAY": "wayland-0"})
        with pytest.raises(Refused, match="can reach outside"):
            own.app_launch(command="sh -c 'kcharselect'")
    finally:
        own.session_stop()
        state.press_stop()


def test_own_screen_apps_dont_inherit_your_session_manager(nested, person, monkeypatch):
    """An app opened in Claude's own screen gets neither the person's X display nor their
    session manager (XSMP), and can't be handed it: it would register with the person's
    real session, to be restored at their next login."""
    from kwin_mcp.warden.gate import WardenEngine

    fake = "local/host:@/tmp/.ICE-unix/1,unix/host:/tmp/.ICE-unix/1"
    monkeypatch.setenv("SESSION_MANAGER", fake)
    monkeypatch.setenv("DISPLAY", ":99")
    own = WardenEngine()
    allowed, text = _ask(own, person, "own_screen", "")
    assert allowed, text
    own.session_start(screen_width=640, screen_height=480, home="isolated")
    try:
        with pytest.raises(Refused, match="SESSION_MANAGER"):
            own.app_launch(command="kcharselect", env={"SESSION_MANAGER": fake})
        launched = own.app_launch(command="kcharselect")
        # Read once its window is up: a process still in exec shows an empty environment
        # (seen 2026-11-17 under a full run's load, state D, no memory mapped yet).
        assert _wait_for(lambda: _window(own, "kcharselect"), 20), "it never opened"
        environ = Path(f"/proc/{launched.pid}/environ").read_bytes().split(b"\0")
        names = {item.split(b"=", 1)[0] for item in environ}
        assert b"WAYLAND_DISPLAY" in names, "the app's environment couldn't be read"
        assert b"SESSION_MANAGER" not in names, "the app got the person's session manager"
        assert b"DISPLAY" not in names, "the app got the person's X display"
    finally:
        own.session_stop()
        state.press_stop()


def test_own_screen_apps_get_a_runtime_folder_of_their_own(nested, person, monkeypatch):
    """An app opened in Claude's own screen isn't told where the person's runtime folder
    is (their sound server, SSH agent and user services live there) and gets none of the
    caller's settings beyond the plain ones: made-up ones stand in for what a desktop
    exports that the code has never seen."""
    from kwin_mcp.warden.gate import WardenEngine

    host_runtime = os.environ["XDG_RUNTIME_DIR"]
    monkeypatch.setenv("SSH_AUTH_SOCK", f"{host_runtime}/ssh-agent.socket")
    monkeypatch.setenv("PULSE_SERVER", f"unix:{host_runtime}/pulse/native")
    monkeypatch.setenv("SOMEDESKTOP_PRIVATE_SOCKET", f"{host_runtime}/somewhere")
    own = WardenEngine()
    allowed, text = _ask(own, person, "own_screen", "")
    assert allowed, text
    own.session_start(screen_width=640, screen_height=480, home="isolated")
    try:
        launched = own.app_launch(command="kcharselect")
        assert _wait_for(lambda: _window(own, "kcharselect"), 20), "it never opened"
        raw = Path(f"/proc/{launched.pid}/environ").read_bytes().split(b"\0")
        env = dict(item.decode().split("=", 1) for item in raw if b"=" in item)
        assert "WAYLAND_DISPLAY" in env, "the app's environment couldn't be read"
        assert env.get("XDG_RUNTIME_DIR") not in (None, host_runtime), env.get("XDG_RUNTIME_DIR")
        leaked = [k for k, v in env.items() if host_runtime in v]
        assert not leaked, f"the app was told about the person's runtime folder: {leaked}"
        for name in ("SSH_AUTH_SOCK", "PULSE_SERVER", "SOMEDESKTOP_PRIVATE_SOCKET"):
            assert name not in env, f"the app got {name}"
    finally:
        own.session_stop()
        state.press_stop()


def test_own_screen_is_a_window_that_closes_with_its_grant(nested, person, apps):
    """Allowed, Claude's screen opens as a window on your desktop; ended, it closes."""
    from kwin_mcp.warden.gate import WardenEngine

    own = WardenEngine()
    before = {w.id for w in person.window_list().windows}
    allowed, text = _ask(own, person, "own_screen", "")
    assert allowed, text
    own.session_start(screen_width=800, screen_height=600, home="isolated")
    try:
        new = _wait_for(
            lambda: [
                w
                for w in person.window_list().windows
                if w.id not in before and w.caption.startswith("KDE Wayland Compositor")
            ],
            timeout=20,
        )
        assert new, "Claude's own screen didn't show as a window on the desktop: " + str(
            [(w.app_id, w.caption) for w in person.window_list().windows if w.id not in before]
        )
        own.window_list()  # inside its own screen: no card, no refusal
        state.press_stop()
        # It closes on its own, without waiting for Claude's next call.
        gone = _wait_for(
            lambda: (
                not [
                    w
                    for w in person.window_list().windows
                    if w.caption.startswith("KDE Wayland Compositor")
                ]
            ),
            timeout=20,
        )
        assert gone, "Claude's own screen stayed open after its grant ended"
        with pytest.raises((Refused, RuntimeError)):
            own.window_list()
    finally:
        own.session_stop()


def test_a_second_screen_leaves_the_first_ones_accessibility_alone(nested, person, apps):
    """A connection already open survives; a new one (an app opened later, a restarted
    helper, another session attaching) must reach the first desktop's bus too. And the
    desktop that started them keeps its own accessibility socket: no private desktop puts
    one in its runtime folder (at-spi-bus-launcher unlinks the path before binding it, so
    a screen of Claude's own took over the person's; upstream issue #132, option A)."""
    from kwin_mcp.core import AutomationEngine

    host_atspi = Path(os.environ["XDG_RUNTIME_DIR"]) / "at-spi"
    assert _kate_editor(person).ref  # reading Kate's text works before
    other = AutomationEngine()
    other.session_start(screen_width=800, screen_height=600, home="isolated")
    try:
        assert not host_atspi.exists(), (
            f"a private desktop put its accessibility bus in the host's {host_atspi}"
        )
    finally:
        other.session_stop()
    fresh = AutomationEngine()
    fresh.session_connect(
        dbus_address=nested["info"].dbus_address, wayland_display=nested["info"].wayland_socket
    )
    try:
        assert _kate_editor(fresh).ref, "the second desktop broke the first one's accessibility"
    finally:
        fresh.session_stop()


def test_every_warden_window_sits_on_its_own_monitor(nested):
    """With two monitors, each of the card's dim layers covers its own monitor, the frames
    layer covers each monitor, and the access list opens on the primary one. Left at Qt's
    default position, every layer-shell window went to the first monitor: a smaller
    monitor's dim sat as a darker box in the middle of the primary one and the list opened
    far from the tray. A second private KWin with two outputs; the
    first nested session is untouched."""
    import subprocess
    import sys

    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.warden import card
    from kwin_mcp.warden.daemon import UI, _qml

    owner = AutomationEngine()
    info = owner.session_start(
        screen_width=1280, screen_height=800, output_count=2, home="isolated"
    )
    env = {
        **os.environ,
        "WAYLAND_DISPLAY": info.wayland_socket,
        "DBUS_SESSION_BUS_ADDRESS": info.dbus_address,
        "QML_XHR_ALLOW_FILE_READ": "1",
    }
    procs: list[subprocess.Popen] = []

    def kwin(js: str):
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from kwin_mcp.geometry import run_kwin_script;"
                " print(run_kwin_script(sys.argv[1]))",
                js + ' callDBus("{sink}", "{path}", "{sink}", "Report", JSON.stringify(out));',
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
            check=False,
        )
        return json.loads(out.stdout)

    def surfaces(pid: int) -> list:
        return kwin(
            f"const out = workspace.stackingOrder.filter(w => w.pid === {pid}).map(w =>"
            " [w.output.name, w.frameGeometry.x, w.frameGeometry.y,"
            " w.frameGeometry.width, w.frameGeometry.height]);"
        )

    try:
        outputs = {
            o[0]: o[1:]
            for o in kwin(
                "const out = workspace.screens.map(s => [s.name, s.geometry.x, s.geometry.y,"
                " s.geometry.width, s.geometry.height]);"
            )
        }
        assert len(outputs) == 2, outputs
        primary = sorted(outputs)[1]  # the second monitor, so the default can't pass
        # The card: one dim layer per monitor, each covering exactly its own.
        req = {"level": "see", "app": "x", "reason": "x", "timeout_s": 30, "primary": primary}
        procs.append(subprocess.Popen(card.command_line(req), env=env, stderr=subprocess.DEVNULL))
        dims = _wait_for(lambda: len(surfaces(procs[-1].pid)) == 2 and surfaces(procs[-1].pid), 10)
        assert dims, "the card's dim layers never showed"
        assert sorted((d[0], *d[1:]) for d in dims) == sorted(
            (name, *geo) for name, geo in outputs.items()
        ), f"a dim layer isn't on its own monitor: {dims} for {outputs}"
        # And KWin doesn't zoom them open: its Scale effect animates normal windows and
        # dialogs (effects/scale, isScaleWindow), which zoomed the dim with the card; the
        # card plays that entrance itself.
        kinds = kwin(
            f"const out = workspace.stackingOrder.filter(w => w.pid === {procs[-1].pid})"
            ".map(w => [w.normalWindow, w.dialog, w.utility]);"
        )
        assert kinds and all(k == [False, False, True] for k in kinds), (
            f"KWin would zoom the card's windows open (normal, dialog, utility): {kinds}"
        )
        # The overlay: a frames layer per monitor, and the access list on the primary one.
        state_file = nested["root"] / "two-monitor-overlay.json"
        state_file.write_text(
            json.dumps(
                {"grants": [], "frames": [], "log": [], "showList": True, "primary": primary}
            ),
            encoding="utf-8",
        )
        qml = _qml()
        assert qml, "no qml program"
        for part in ("frames", "controls"):
            procs.append(
                subprocess.Popen(
                    [
                        qml,
                        "--apptype",
                        "widget",
                        str(UI / "overlay.qml"),
                        "--",
                        part,
                        str(state_file),
                    ],
                    env=env,
                    stderr=subprocess.DEVNULL,
                )
            )
        frames = _wait_for(
            lambda: len(surfaces(procs[-2].pid)) == 2 and surfaces(procs[-2].pid), 10
        )
        assert frames and {f[0] for f in frames} == set(outputs), f"frames layers: {frames}"
        listed = _wait_for(lambda: [w for w in surfaces(procs[-1].pid) if w[3] == 420], 10)
        assert listed, "the access list never showed"
        name, x, y, w, h = listed[0]
        px, py, pw, ph = outputs[primary]
        assert name == primary and px <= x and x + w <= px + pw and py + ph - 200 < y + h, (
            f"the access list opened at {listed[0]}, not above the tray on "
            f"{primary} {outputs[primary]}"
        )
    finally:
        for proc in procs:
            proc.kill()
            proc.wait()
        owner.session_stop()


def test_the_lock_screen_is_never_granted(nested, monkeypatch):
    """With the screen locked, nothing of Claude's reaches it, even under a Full desktop
    grant: a second private KWin started locked (its own bus and screen; the first nested
    session is untouched), never unlocked, and stopped at the end (review item, 2026-11-14)."""
    from kwin_mcp import session as session_mod
    from kwin_mcp.core import AutomationEngine
    from kwin_mcp.warden.gate import WardenEngine

    built = session_mod.Session._build_wrapper_script

    def locked(self, config):
        script = built(self, config)
        assert "--no-lockscreen" in script
        return script.replace("--no-lockscreen", "--lockscreen")

    monkeypatch.setattr(session_mod.Session, "_build_wrapper_script", locked)
    owner = AutomationEngine()
    info = owner.session_start(screen_width=1280, screen_height=800, home="isolated")
    monkeypatch.undo()
    claude = WardenEngine()
    try:
        claude.session_connect(dbus_address=info.dbus_address, wayland_display=info.wayland_socket)
        import subprocess

        def locked_now() -> bool:
            out = subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--session",
                    "--dest",
                    "org.freedesktop.ScreenSaver",
                    "--object-path",
                    "/ScreenSaver",
                    "--method",
                    "org.freedesktop.ScreenSaver.GetActive",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                env={**os.environ, "DBUS_SESSION_BUS_ADDRESS": info.dbus_address},
            ).stdout
            return "true" in out

        assert _wait_for(locked_now, 15), "the second session never showed locked"
        claude.warden.book.give(
            "full_desktop", app="", app_name="", level="full_desktop", reason="test", minutes=5
        )
        with pytest.raises(Refused, match="locked"):
            claude.pointer_click(target=Point(x=640, y=400))
        with pytest.raises(Refused, match="locked"):
            claude.keyboard_type(text="not a password")
    finally:
        claude.session_stop()
        owner.session_stop()
