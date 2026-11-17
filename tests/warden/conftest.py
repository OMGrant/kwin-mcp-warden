"""The nested desktop for kwin-mcp-warden's boundary tests, and the planted faults.

PLANT=<name> applies one fault to the warden for the whole run; the run must
then fail the test named beside it. tests/warden/run.sh --plants runs them all.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Planted faults: name -> (what it breaks, the test that must catch it).
PLANTS = {
    "no-grant-check": "test_click_without_a_grant_is_refused",
    "card-open-ignored": "test_claude_cannot_click_the_card",
    "allow-on-any-exit": "test_dont_allow_grants_nothing",
    "no-mask": "test_see_cuts_the_picture_to_the_granted_app",
    "no-hands": "test_your_input_pauses_claude",
    "no-expiry": "test_a_grant_past_its_time_is_refused",
    "no-stop": "test_the_stop_ends_every_grant",
    "focus-unchecked": "test_typing_follows_focus",
    "close-unasked": "test_closing_a_window_asks_every_time",
    "stop-key-unwired": "test_the_stop_shortcut_ends_every_grant",
    "typing-in-one-piece": "test_your_input_pauses_claude_mid_typing",
    "menus-blacked-out": "test_a_granted_apps_menu_shows_in_the_picture",
    "go-answers": "test_go_to_session_brings_the_card_back",
    "reason-optional": "test_request_without_a_reason_is_refused_before_a_card",
    "own-screen-ungated": "test_own_screen_needs_a_grant",
    "windows-listed": "test_window_list_shows_no_window_without_see",
    "any-window-grant": "test_allow_grants_control_in_that_app_only",
    "app-wide-grant": "test_a_grant_covers_one_window_not_the_whole_app",
    "agent-picks-length": "test_the_person_picks_how_long_not_the_agent",
    "never-list-ignored": "test_never_list_refuses_without_a_card",
    "warden-windows-open": "test_the_wardens_own_windows_are_never_granted",
    "ask-reason-optional": "test_ask_every_time_tools_need_a_reason",
    "dbus-paths-open": "test_dbus_never_paths_are_refused_through_kwin",
    "own-screen-env-open": "test_own_screen_apps_cant_reach_your_desktop",
    "session-manager-inherited": "test_own_screen_apps_dont_inherit_your_session_manager",
    "portal-half-starts": "test_a_private_desktop_has_no_portal_that_half_starts",
    "stop-key-press-unwired": "test_the_stop_key_pressed_reaches_the_warden",
    "stop-key-clash-ignored": "test_a_stop_key_another_app_holds_is_refused",
    "own-screen-outlives-grant": "test_own_screen_is_a_window_that_closes_with_its_grant",
    "ring-before-check": "test_the_corner_notice_rings_only_for_what_claude_did",
    "gestures-unchecked": "test_your_input_stops_a_drag_midway",
    "pause-gap": "test_a_touch_while_the_window_is_checked_after_a_pause_waits_again",
    "no-a11y-guard": "test_the_switch_goes_off_when_the_warden_dies_without_doing_it",
    "dbus-wrong-path": "test_an_app_that_only_opens_over_dbus_is_opened_that_way",
    "typing-waits-for-nudges": "test_a_nudged_mouse_doesnt_pause_typing",
    "a11y-without-a-card": "test_accessibility_is_asked_for_and_turned_back_off",
    "private-bus-starts-portals": "test_a_private_desktop_cant_take_the_document_portal",
    "a11y-asks-again": "test_keep_it_off_holds_for_the_session",
    "a11y-connect-flips": "test_session_connect_cant_flip_the_switch",
    "into-checks-focus": "test_a_field_in_an_app_not_granted_is_refused",
    "hidden-match-told": "test_a_locator_cant_read_windows_not_granted",
    "own-screen-not-contained": "test_own_screen_apps_get_a_runtime_folder_of_their_own",
    "into-waits-for-hands": "test_typing_into_a_field_needs_no_focus",
    "press-unchecked": "test_a_window_raised_before_the_press_is_checked",
    "no-keeper": "test_the_daemon_comes_back_between_calls",
    "captions-selectable": "test_a_caption_selector_cant_find_hidden_windows",
    "primary-kscreen-only": "test_the_primary_monitor_without_kscreen_doctor",
    "lock-ignored": "test_the_lock_screen_is_never_granted",
    "shared-runtime": "test_a_second_screen_leaves_the_first_ones_accessibility_alone",
    "no-open-card": "test_an_app_that_isnt_open_is_opened_and_allowed_on_one_card",
    "grant-before-window": "test_something_else_opening_grants_nothing",
}


def _apply_plant(name: str, mp: pytest.MonkeyPatch) -> None:
    from kwin_mcp.warden import card, gate, grants, state
    from kwin_mcp.warden.policy import covers

    if name == "no-open-card":
        # The warden doesn't know how to open an app that isn't open.
        from kwin_mcp.warden import apps as app_names

        mp.setattr(app_names, "launch_command", lambda app_id: None)
    elif name == "grant-before-window":
        # The grant is given on Allow without waiting for the named app's window.
        mp.setattr(gate.Warden, "_open_for_grant", lambda self, app, display, argv: (None, ""))
    elif name == "shared-runtime":
        # A private desktop's bus shares the runtime folder of the desktop that started it.
        from kwin_mcp import session as session_mod

        mp.setattr(
            session_mod.Session,
            "_bus_runtime_dir",
            lambda self: Path(os.environ["XDG_RUNTIME_DIR"]),
        )
    elif name == "no-grant-check":
        mp.setattr(
            gate.WardenEngine,
            "_allowed",
            lambda self, window, level, by_id=None: grants.Grant(
                "x", "full_desktop", "", "", level, "", time.time(), time.time() + 3600
            ),
        )
    elif name == "card-open-ignored":
        mp.setattr(state, "card_is_open", lambda: False)
    elif name == "allow-on-any-exit":
        mp.setattr(card, "DENY_CODE", -999)
        mp.setattr(card, "ALLOW_CODE", 10)
    elif name == "no-mask":
        mp.setattr(gate.WardenEngine, "_mask", lambda self, frames: None)
    elif name == "no-hands":
        mp.setattr(gate.WardenEngine, "_hands_first", lambda self, rule, bound: time.time())
    elif name == "into-checks-focus":
        # The warden checks the window with focus instead of the field's own window.
        def focused(self, stack=None):
            return next(s for s in (stack or self._stack()) if s.get("active"))

        def element_windows(self, element, stack):
            return [focused(self, stack)]

        mp.setattr(gate.WardenEngine, "_element_windows", element_windows)
        mp.setattr(
            gate.WardenEngine,
            "_element_allowed",
            lambda self, element, by_id, level: bool(self._allowed(focused(self), level)),
        )
    elif name == "hidden-match-told":
        # Strict locators go straight to the old check, which names the app a hidden
        # match sits in.
        mp.setattr(gate.WardenEngine, "_pin_strict", lambda self, name, bound, level: None)
    elif name == "own-screen-not-contained":
        from kwin_mcp import session as session_mod

        never = property(lambda s: False, lambda s, v: None)
        mp.setattr(session_mod.Session, "contain_apps", never)
    elif name == "into-waits-for-hands":
        original_level = gate.WardenEngine._level

        def level_with_wait(self, name, rule, original, bound):
            if bound.arguments.get("into") is not None:
                self._hands_first(rule, bound)
            return original_level(self, name, rule, original, bound)

        mp.setattr(gate.WardenEngine, "_level", level_with_wait)
    elif name == "private-bus-starts-portals":
        from kwin_mcp import session as session_mod

        mp.setattr(session_mod, "_HOST_ONLY_SERVICES", ())
    elif name == "a11y-without-a-card":
        from kwin_mcp.warden import a11y

        def switch_on_silently(self, reason):
            a11y.turn_on(os.environ["DBUS_SESSION_BUS_ADDRESS"])
            return True, "on"

        mp.setattr(gate.Warden, "_ask_accessibility", switch_on_silently)
    elif name == "a11y-asks-again":
        # The warden forgets "Keep It Off" (a property that always reads False).
        mp.setattr(
            gate.Warden,
            "a11y_declined",
            property(lambda self: False, lambda self, value: None),
            raising=False,
        )
    elif name == "a11y-connect-flips":
        from kwin_mcp.core import AutomationEngine

        mp.setattr(
            gate.WardenEngine,
            "_live_accessibility",
            staticmethod(AutomationEngine._live_accessibility),
        )
    elif name == "typing-waits-for-nudges":
        mp.setattr(gate, "_your_input", lambda rule: state.last_hands())
    elif name == "no-expiry":
        mp.setattr(grants.Grant, "alive", lambda self, now=None: not state.ended(self.id))
    elif name == "no-stop":
        mp.setattr(state, "stop_time", lambda: 0.0)
    elif name == "focus-unchecked":
        original = gate.WardenEngine._targets

        def targets(self, rule, bound):
            if rule.scope == "focus":
                return [{"app": "org.kde.kate", "managed": True, "pid": 0}]
            return original(self, rule, bound)

        mp.setattr(gate.WardenEngine, "_targets", targets)
    elif name == "stop-key-unwired":
        # The daemon is its own process, so the plant keeps it from starting at all.
        mp.setattr(gate, "ensure_daemon", lambda: None)
    elif name == "typing-in-one-piece":
        mp.setattr(gate.WardenEngine, "TYPE_PIECE", 10_000)
    elif name == "menus-blacked-out":
        original_sees = gate.WardenEngine._sees
        mp.setattr(
            gate.WardenEngine,
            "_sees",
            lambda self, window, by_id: (
                not window.get("popup") and original_sees(self, window, by_id)
            ),
        )
    elif name == "go-answers":
        mp.setattr(card, "GO_CODE", -998)  # Go to Session falls through to Don't Allow
    elif name == "reason-optional":
        original_request = gate.Warden.request

        def request(self, level, app, reason):
            return original_request(self, level, app, (reason or "").strip() or "no reason given")

        mp.setattr(gate.Warden, "request", request)
    elif name == "own-screen-ungated":
        mp.setattr(
            grants.GrantBook,
            "own_screen",
            lambda self: grants.Grant(
                "x", "own_screen", "", "", "", "", time.time(), time.time() + 3600
            ),
        )
    elif name == "windows-listed":
        from kwin_mcp.core import AutomationEngine

        # Both places that leave out a window Claude may not see: the read and the lookup.
        mp.setattr(
            gate.WardenEngine,
            "_filtered",
            lambda self, name, original, bound: original(*bound.args, **bound.kwargs),
        )
        mp.setattr(gate.WardenEngine, "_windows", AutomationEngine._windows)
    elif name == "any-window-grant":
        # A grant covers whatever window is asked about.
        def any_window(self, lineage, level):
            return next(
                (g for g in self.alive() if g.kind != "own_screen" and covers(g.level, level)),
                None,
            )

        mp.setattr(grants.GrantBook, "for_window", any_window)
    elif name == "app-wide-grant":
        # A grant covers every window of its app, as grants did before 2026-11-17.
        from kwin_mcp.warden.settings import same_app

        def app_wide(self, lineage, level):
            full = self.full_desktop()
            if full is not None:
                return full
            for g in self.alive():
                if g.kind == "window" and covers(g.level, level):
                    for w in lineage:
                        if same_app(str(w.get("app", "")), g.app):
                            return g
            return None

        mp.setattr(grants.GrantBook, "for_window", app_wide)
    elif name == "agent-picks-length":
        # The grant lasts what the agent asked for, whatever the person picked.
        mp.setattr(gate, "_chosen", lambda outcome, choices, selected: selected)
    elif name == "never-list-ignored":
        from kwin_mcp.warden import settings as settings_mod

        mp.setattr(gate, "app_matches", lambda app, names: False)
        mp.setattr(settings_mod, "app_matches", lambda app, names: False)
    elif name == "warden-windows-open":
        mp.setattr(gate, "_is_warden_process", lambda pid: False)
    elif name == "ask-reason-optional":
        original_ask = gate.Warden.ask_once

        def ask_once(self, tool, *a, **k):
            self.reasons.setdefault(tool, "no reason given")
            return original_ask(self, tool, *a, **k)

        mp.setattr(gate.Warden, "ask_once", ask_once)
    elif name == "dbus-paths-open":
        from kwin_mcp.warden import settings as settings_mod

        mp.setattr(settings_mod, "DEFAULT_NEVER_DBUS_PATHS", ())
        mp.setattr(settings_mod.Settings, "__init__", _settings_without_paths(settings_mod))
    elif name == "portal-half-starts":
        from kwin_mcp import session as session_mod

        portal = "org.freedesktop.portal.Desktop"
        kept = tuple(s for s in session_mod._HOST_ONLY_SERVICES if s != portal)
        mp.setattr(session_mod, "_HOST_ONLY_SERVICES", kept)
    elif name == "session-manager-inherited":
        from kwin_mcp import session as session_mod

        # Every guard lets SESSION_MANAGER through: the list stripped from apps, the
        # allow-list for contained screens, and the warden's refusal of an agent's env.
        mp.setattr(session_mod, "HOST_DESKTOP_ENV", ("DISPLAY",))
        names = session_mod.CONTAINED_ENV_NAMES | {"SESSION_MANAGER"}
        mp.setattr(session_mod, "CONTAINED_ENV_NAMES", names)
        kept = tuple(n for n in gate.WardenEngine._HOST_ENV if n != "SESSION_MANAGER")
        mp.setattr(gate.WardenEngine, "_HOST_ENV", kept)
    elif name == "own-screen-env-open":
        mp.setattr(gate.WardenEngine, "_own_screen_launch", lambda self, bound: None)
    elif name == "stop-key-press-unwired":
        mp.setattr(gate, "ensure_daemon", lambda: None)
    elif name == "stop-key-clash-ignored":
        from kwin_mcp.warden import daemon

        # The warden takes the made-up app's key (and System Monitor's) for its own.
        mp.setattr(daemon, "COMPONENT", "warden-test-made-up-app")
    elif name == "own-screen-outlives-grant":
        from kwin_mcp.warden import grants as grants_mod

        original_alive = grants_mod.GrantBook.alive

        def alive(self):
            return [g for g in self._grants if g.kind == "own_screen"] or original_alive(self)

        mp.setattr(grants_mod.GrantBook, "alive", alive)
    elif name == "ring-before-check":
        # The ring for every attempt, before the warden has looked at it.
        original_level = gate.WardenEngine._level

        def level(self, name, rule, original, bound):
            if rule.scope not in ("filter", "capture"):
                state.note_acted()
            return original_level(self, name, rule, original, bound)

        mp.setattr(gate.WardenEngine, "_level", level)
    elif name in ("gestures-unchecked", "press-unchecked"):
        original_checks = gate.WardenEngine._mid_action_checks

        def checks(self, rule, level, grants, since):
            between, before_press = original_checks(self, rule, level, grants, since)
            return (None, before_press) if name == "gestures-unchecked" else (between, None)

        mp.setattr(gate.WardenEngine, "_mid_action_checks", checks)
    elif name == "pause-gap":
        # The old _hands_first: once quiet, the check after a pause, then go, with no look at
        # the hands again, so a touch during that check slips in before the action.
        def hands_first(self, rule, bound):
            paused = False
            while time.time() - gate._your_input(rule) < self.warden.settings.pause_seconds:
                paused = True
                time.sleep(0.1)
            if paused:
                for window in self._targets(rule, bound):
                    self._allowed(window, rule.level or "control")
            return time.time()

        mp.setattr(gate.WardenEngine, "_hands_first", hands_first)
    elif name == "no-a11y-guard":
        from kwin_mcp.warden import a11y

        # A guard that holds the guard's lock but never does its job, there before the
        # switch turns on, so no real guard (from the server or the daemon) can start.
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import fcntl, sys, time; lock = open(sys.argv[1], 'a'); "
                "fcntl.flock(lock, fcntl.LOCK_EX); time.sleep(60)",
                str(a11y._guard_lock()),
            ]
        )
        time.sleep(0.5)
    elif name == "dbus-wrong-path":
        from kwin_mcp.warden import apps as app_names

        original_launch = app_names.launch_command

        def launch(app_id):
            argv = original_launch(app_id)
            if argv and argv[0] == "gdbus":
                argv[argv.index("--object-path") + 1] = "/org/example/Wrong"
            return argv

        mp.setattr(app_names, "launch_command", launch)
    elif name == "no-keeper":
        mp.setattr(gate.Warden, "_keep_daemon", lambda self: None)
    elif name == "captions-selectable":
        from kwin_mcp.core import AutomationEngine

        mp.setattr(gate.WardenEngine, "_windows", AutomationEngine._windows)
    elif name == "primary-kscreen-only":
        mp.setattr(card, "_primary_from_kwin", lambda env: "")
    elif name == "lock-ignored":
        mp.setattr(gate.WardenEngine, "_screen_locked", lambda self: False)
    elif name == "close-unasked":
        mp.setattr(gate.Warden, "ask_once", lambda self, *a, **k: None)
    else:
        raise ValueError(f"unknown plant {name!r}; known: {', '.join(PLANTS)}")


def _settings_without_paths(settings_mod):
    original_init = settings_mod.Settings.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.never_dbus_paths = []
        self.never_dbus = [s for s in self.never_dbus if s != "org.kde.kglobalaccel"]

    return init


# Before anything else runs: this test process and everything it starts can never reach
# the real desktop, session bus or home. Nothing is restored afterwards, so a thread
# still running when a test fails (a request waiting on a card, say) has nowhere real to
# go. (2026-11-14: such threads wrote "card failed" lines into the real ~/.local/state
# after an earlier version restored the environment at teardown.)
_SANDBOX = Path(tempfile.mkdtemp(prefix="kwin-mcp-warden-test-"))
for _name in (
    "WAYLAND_DISPLAY",
    "WAYLAND_SOCKET",
    "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS",
    "CLAUDE_CODE_SESSION_ID",
    "XAUTHORITY",
    "SESSION_MANAGER",
):
    os.environ.pop(_name, None)
# Every process this run starts carries the mark (they all inherit this environment), so
# the run can find and stop whatever outlived it (sweep_run below; run.sh checks from
# outside that nothing did). run.sh sets its own mark; a bare pytest run gets this one.
os.environ.setdefault("KWIN_MCP_WARDEN_TEST_RUN", str(_SANDBOX))
RUN_MARK = f"KWIN_MCP_WARDEN_TEST_RUN={os.environ['KWIN_MCP_WARDEN_TEST_RUN']}".encode()


def _stopped_by_signal(signum, _frame):
    # A run stopped from outside (timeout, Ctrl+C from a script) still tears its desktop
    # down: pytest runs every fixture's teardown for KeyboardInterrupt.
    raise KeyboardInterrupt(f"signal {signum}")


signal.signal(signal.SIGTERM, _stopped_by_signal)
(_SANDBOX / "run").mkdir(mode=0o700)
(_SANDBOX / "home" / ".config" / "kwin-mcp-warden").mkdir(parents=True)
os.environ.update(
    {
        "XDG_RUNTIME_DIR": str(_SANDBOX / "run"),
        "HOME": str(_SANDBOX / "home"),
        "XDG_CONFIG_HOME": str(_SANDBOX / "home" / ".config"),
        "XDG_STATE_HOME": str(_SANDBOX / "home" / ".local" / "state"),
        "XDG_CACHE_HOME": str(_SANDBOX / "home" / ".cache"),
        "QT_FORCE_STDERR_LOGGING": "1",
        "PYTHONNOUSERSITE": "1",
    }
)
# Session names come from the working folder, so any log line a test session writes
# carries "kwin-mcp-warden-test-": run.sh looks for that mark in the real log.
os.chdir(_SANDBOX)


@pytest.fixture(scope="session")
def nested():
    """A private nested KWin with its own bus, home and runtime folder."""
    root = _SANDBOX
    runtime = root / "run"
    home = root / "home"
    (home / ".config" / "kwin-mcp-warden" / "settings.toml").write_text(
        'pause_seconds = 1.0\nanswer_seconds = 30\nstop_key = "Meta+Alt+Esc"\n', encoding="utf-8"
    )
    fifo = root / "fake-mouse"
    os.mkfifo(fifo)
    os.environ["KWIN_MCP_WARDEN_INPUT_DEVICES"] = str(fifo)

    from kwin_mcp.core import AutomationEngine

    person = AutomationEngine()
    info = person.session_start(screen_width=1920, screen_height=1080, home="isolated")
    # From here on, "your desktop" for every process in this test is the nested one.
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = info.dbus_address
    os.environ["WAYLAND_DISPLAY"] = info.wayland_socket
    writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    # Plasma's tray stands in as a small watcher on the private bus, before any overlay
    # starts, so the warden's tray icon registers the way it does under a real panel.
    tray = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("fake_tray_host.py")),
            str(root / "tray-items"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert tray.stdout is not None and tray.stdout.readline().strip() == "ready"
    try:
        yield {"person": person, "info": info, "fifo_fd": writer, "root": root}
    finally:
        tray.terminate()
        os.close(writer)
        _stop_daemon(runtime)
        person.session_stop()
        # The environment stays pointed at the dead nested session on purpose (see above).
        shutil.rmtree(root, ignore_errors=True)
        sweep_run()


def _stop_daemon(runtime: Path) -> None:
    pid_file = runtime / "kwin-mcp-warden" / "daemon.pid"
    try:
        pid = int(pid_file.read_text())
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return
    if b"kwin_mcp.warden.daemon" in cmd:  # only ever our own daemon
        os.kill(pid, 15)


def _ancestors() -> set[int]:
    pids, pid = set(), os.getpid()
    while pid > 1:
        pids.add(pid)
        try:
            pid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pids


def sweep_run(grace: float = 5.0) -> None:
    """Stop whatever this run started that its fixtures didn't: a daemon the keeper started
    again, a helper a test session spawned, a private desktop's KWin (run_processes.py).
    Each pid is checked again right before the signals go out (a pid can be reused)."""
    import run_processes

    deadline = time.monotonic() + grace
    sig = signal.SIGTERM
    while pids := run_processes.find(RUN_MARK, exclude=_ancestors()):
        ours = [pid for pid in pids if run_processes.still_ours(pid, RUN_MARK)]
        for pid in ours:
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
        if time.monotonic() > deadline:
            if sig == signal.SIGKILL:
                return
            sig, deadline = signal.SIGKILL, time.monotonic() + 2
        time.sleep(0.2)


@pytest.fixture(scope="session")
def person(nested):
    return nested["person"]


@pytest.fixture(scope="session")
def apps(nested, person):
    person.app_launch(command="kate --new")
    person.app_launch(command="kcharselect")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        ids = {w.app_id.lower() for w in person.window_list().windows}
        if any("kate" in i for i in ids) and any("kcharselect" in i for i in ids):
            break
        time.sleep(0.5)
    # Side by side, so each has its own spot to click.
    for fragment, x in (("kate", 20), ("kcharselect", 980)):
        win = next(w for w in person.window_list().windows if fragment in w.app_id.lower())
        from kwin_mcp.models import PartialRect, WindowSelector

        person.window_place(
            window=WindowSelector(id=win.id), frame=PartialRect(x=x, y=60, width=900, height=700)
        )
    time.sleep(1.0)
    return True


def _attached(nested):
    from kwin_mcp.warden.gate import WardenEngine

    engine = WardenEngine()
    engine.session_connect(
        dbus_address=nested["info"].dbus_address, wayland_display=nested["info"].wayland_socket
    )
    return engine


@pytest.fixture(scope="session")
def claude(nested, plant_session):
    engine = _attached(nested)
    yield engine
    engine.session_stop()


@pytest.fixture(scope="session")
def other_claude(nested, plant_session):
    engine = _attached(nested)
    yield engine
    engine.session_stop()


@pytest.fixture
def hands(nested):
    def touch(kind: str = "move") -> None:
        """The person's hands: "move" nudges the mouse (EV_REL REL_X 1); "key" presses
        and lets go of A (EV_KEY KEY_A 1 then 0), as struct input_event."""
        if kind == "move":
            os.write(nested["fifo_fd"], struct.pack("llHHi", 0, 0, 2, 0, 1))
        else:
            os.write(
                nested["fifo_fd"],
                struct.pack("llHHi", 0, 0, 1, 30, 1) + struct.pack("llHHi", 0, 0, 1, 30, 0),
            )
        time.sleep(0.15)

    return touch


@pytest.fixture(scope="session", autouse=True)
def plant_session():
    name = os.environ.get("PLANT", "")
    if not name:
        yield ""
        return
    mp = pytest.MonkeyPatch()
    _apply_plant(name, mp)
    yield name
    mp.undo()
