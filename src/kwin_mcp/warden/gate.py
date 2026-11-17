"""The warden in front of every kwin-mcp tool.

``WardenEngine`` is kwin-mcp's ``AutomationEngine`` with every tool method
wrapped. The server and the CLI both build a ``WardenEngine``, so neither path
reaches the desktop without passing here. On your screen (a live session) each
call is checked against this session's grants, or refused with what Claude has
to ask for. In a screen of Claude's own (a virtual session), the screen itself
was the grant.

What it guards, and what it doesn't: the warden guards the kwin-mcp route, that
is, an agent that reaches the desktop only through kwin-mcp's tools. Everything
here runs as the person, so anything else that runs as the person (a Claude
session with a shell, the kwin-mcp-warden-cli started by such a session with its
own environment) can change the warden's files, settings and environment, or
reach the desktop by other routes. Claude Code's own permission prompts guard those.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import os
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, cast

from kwin_mcp.core import AutomationEngine
from kwin_mcp.input import input_checks
from kwin_mcp.models import (
    ElementPoint,
    Locator,
    Point,
    RefAddr,
    WindowCloseResult,
    WindowList,
    WindowSelector,
)
from kwin_mcp.session import HOST_DESKTOP_ENV, LiveSession, Session
from kwin_mcp.warden import a11y, apps, card, log, state
from kwin_mcp.warden.grants import Grant, GrantBook
from kwin_mcp.warden.policy import (
    LEVEL_NAMES,
    LEVEL_VERBS,
    LEVELS,
    RULES,
    Rule,
    covers,
)
from kwin_mcp.warden.settings import Settings, app_matches, load, same_app, short_app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

ASK_FIRST = (
    "Ask first: call access_request(level, app, reason) with the least level you need "
    "(see, arrange, control or full_desktop). The person answers a card on their screen."
)


A11Y_OFF_HINT = (
    "Accessibility is off on this desktop, so apps that were already open show no text "
    "fields (apps you launch show theirs). To read or write a field without the person's "
    'focus, ask with access_request(level="accessibility", reason="...").'
)


class Refused(RuntimeError):  # noqa: N818 - the word Claude reads in every refusal
    """The warden refused this call; the message tells Claude why and what to ask for.

    ``logged`` is set when the place that refused already wrote its own log line, so the
    gate doesn't write a second one for the same refusal.
    """

    logged = False


# ── Session context: who is asking, from which desktop ───────────────────────


def _ancestors(pid: int) -> list[int]:
    out: list[int] = []
    for _ in range(40):
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if ppid <= 1:
            break
        out.append(ppid)
        pid = ppid
    return out


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _is_warden_process(pid: int) -> bool:
    return "kwin_mcp/warden" in _cmdline(pid) or "kwin_mcp.warden" in _cmdline(pid)


def _clean_caption(caption: str) -> str:
    text = caption.strip()
    while text and not text[0].isalnum():
        text = text[1:].lstrip()
    for tail in (" — Konsole", " - Konsole"):
        if text.endswith(tail):
            text = text[: -len(tail)]
    return text.strip()


# ── Who is asking ─────────────────────────────────────────────────────────────

# MCP client names that aren't the name a person knows the agent by. "mcp" is the
# Python SDK's default, sent by a client that didn't say who it is.
_AGENT_NAMES = {"claude-code": "Claude", "claude-ai": "Claude", "mcp": ""}


def agent_name(client_name: str = "", client_title: str = "", configured: str = "") -> str:
    """The name the cards give the agent: the settings file's, else the MCP client's own.

    Claude Code says "claude-code", which reads as "Claude". A client that names no one
    gets "Claude", the agent this fork was written for.
    """
    name = client_name.strip()
    known = _AGENT_NAMES.get(name.lower())
    if known is None and name.lower().startswith("claude"):
        known = "Claude"
    for candidate in (configured, known, client_title, "" if known == "" else name):
        # One line, no markup: the overlay draws some lines as rich text.
        text = " ".join(str(candidate or "").replace("<", "").replace(">", "").split())[:40]
        if text:
            return text
    return "Claude"


# ── The warden ────────────────────────────────────────────────────────────────


class Warden:
    """One session's warden: its grants, its cards and its checks."""

    def __init__(self, engine: AutomationEngine, settings: Settings | None = None) -> None:
        self.engine = engine
        self.settings = settings or load()
        self._session_name = ""
        self._terminal: dict[str, Any] | None = None
        # Who is asking is read from KWin only when a card or a log line needs it.
        self.book = GrantBook(os.path.basename(os.getcwd()) or "Claude")
        # The name every card gives the agent that asks; the MCP client's own name
        # replaces it on the first call (set_client).
        self.agent = agent_name(configured=self.settings.agent_name)
        self.book.agent = self.agent
        self.reasons: dict[str, str] = {}
        self.a11y_declined = False  # "Keep It Off": not asked again this session
        self._lock = threading.Lock()
        self._keeper: threading.Thread | None = None
        if self.settings.problem:
            log.write(self.book.session, "settings", self.settings.problem)

    KEEP_DAEMON_S = 2.0

    def _keep_daemon(self) -> None:
        """While this session holds a grant, keep the daemon (stop key, frame, notice)
        running between Claude's calls too: if it dies, it's back within two seconds."""
        if self._keeper is not None and self._keeper.is_alive():
            return

        def keep() -> None:
            while self.book.alive():
                ensure_daemon()
                time.sleep(self.KEEP_DAEMON_S)

        self._keeper = threading.Thread(target=keep, name="kwin-mcp-warden keeper", daemon=True)
        self._keeper.start()

    # Who is asking

    def _host_stack(self) -> list[dict[str, Any]]:
        import json
        import subprocess
        import sys

        try:
            out = subprocess.run(
                [sys.executable, "-m", "kwin_mcp.geometry"],
                input=json.dumps({"op": "stack"}),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
            resp = json.loads(out or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        return resp.get("result", []) if resp.get("ok") else []

    def terminal(self) -> dict[str, Any] | None:
        """The window this session runs in (its terminal), found by process ancestry."""
        if self._terminal is None:
            chain = _ancestors(os.getpid())
            for window in reversed(self._host_stack()):
                if window.get("pid") in chain and window.get("managed"):
                    self._terminal = window
                    break
        return self._terminal

    def set_client(self, name: str, title: str = "") -> None:
        """The MCP client said who it is (its initialize clientInfo)."""
        agent = agent_name(name, title, self.settings.agent_name)
        if agent != self.agent:
            self.agent = self.book.agent = agent
            self.book.publish()

    def warning(self, app: str) -> str:
        """The warning for an app's card, with the agent's name in it."""
        text = self.settings.warnings.get(short_app(app)) or self.settings.warnings.get(
            app.lower(), ""
        )
        return text.replace("{agent}", self.agent)

    def session_name(self) -> str:
        if not self._session_name:
            term = self.terminal()
            name = _clean_caption(str(term.get("caption", ""))) if term else ""
            self._session_name = name or os.path.basename(os.getcwd()) or "Claude"
            self.book.session = self._session_name
            self.book.publish()
        return self._session_name

    def desktop_label(self) -> str:
        term = self.terminal()
        names = term.get("desktopNames") if term else None
        return str(names[0]) if names else ""

    # The card

    def _card(self, request: dict[str, Any]) -> card.Outcome:
        request = {
            "session": self.session_name(),
            "agent": self.agent,
            "desktop": self.desktop_label(),
            "timeout_s": self.settings.answer_seconds,
            "delay_ms": self.settings.allow_delay_ms,
            "dim": self.settings.dim,
            **request,
        }
        # The card goes on the screen the session works on: your desktop for a live
        # session (and before any session, for a screen of Claude's own).
        env = self.engine._session_env() if isinstance(self.engine._session, LiveSession) else None
        for _ in range(4):
            outcome = card.show(request, env)
            if outcome.answer != "go":
                return outcome
            self._go_to_session()
        return card.Outcome("deny", "Go to Session was pressed four times")

    def _go_to_session(self) -> None:
        term = self.terminal()
        if term:
            with contextlib.suppress(Exception):
                from kwin_mcp import geometry

                geometry.activate(str(term["id"]))
        time.sleep(1.5)

    # access_request

    def request(self, level: str, app: str, reason: str) -> tuple[bool, str]:
        """Open the card for a grant. Returns (allowed, text for Claude)."""
        name = self.session_name()
        reason = (reason or "").strip()
        if not reason:
            log.write(name, "refused", f"access_request without a reason ({level} {app})")
            return False, "Refused before it reached the person: a request needs a reason."
        if level not in (*LEVELS, "own_screen", "accessibility"):
            return (
                False,
                f"Unknown level {level!r}: use see, arrange, control, full_desktop, "
                "own_screen or accessibility.",
            )
        if level == "own_screen":
            return self._ask_own_screen(reason)
        if level == "accessibility":
            return self._ask_accessibility(reason)
        if level == "full_desktop":
            return self._ask_full_desktop(reason)
        app = (app or "").strip()
        if not app:
            return False, "Name the app: its app id as window_list shows it (e.g. google-chrome)."
        if app_matches(app, self.settings.never):
            log.write(name, "never", f"{LEVEL_NAMES[level]} in {app}")
            return False, f"{app} is never granted (password prompts, the lock screen, the warden)."
        app, problem = self._full_app_id(app)
        if problem:
            return False, problem
        # A grant is for one window (design/mock/index.html, "The levels"): the app's window
        # you used last that this session doesn't hold yet. Its title goes on the card, so
        # you see which window it is; Claude doesn't, until you allow it.
        # What's open decides the card (design/mock/index.html, "Open and control"):
        # an open app gets the card for its window; one that isn't open gets one card that
        # opens it and allows its new window; an app that can't be opened gets no card,
        # since a card never asks about something the person can't see.
        full = self.book.full_desktop()
        if full is not None:
            return True, f"Already allowed: {full.describe()}."
        window, held = self._window_to_ask_for(app, level)
        if window is None and held is not None:
            return True, f"Already allowed: {held.describe()}."
        opening: list[str] | None = None
        if window is None:
            opening = apps.launch_command(app)
            if opening is None:
                log.write(name, "refused", f"{level} in {app}: not open")
                return False, (
                    f"No open window has the app id {app!r}, and no installed app by that id "
                    "can be opened; use the app id window_list shows."
                )
        display, icon = apps.describe(app)
        shown = _markup_safe(display)  # the "means" line is rich text; the title is plain
        agent = _markup_safe(self.agent)
        title = _window_title(window)
        this = f"this {shown} window" + (f", \u201c{_markup_safe(title)}\u201d" if title else "")
        if opening is not None:
            this, title = f"the new {shown} window", ""
        aside = "," if title else ""  # a quoted title sits between commas mid-sentence
        # Copy pattern: who can do what, then "Nothing else." last.
        means = {
            "see": f"{agent} can see {this}{aside} and read its text.",
            "arrange": f"{agent} can move, resize and focus {this}.",
            "control": f"{agent} can click and type in {this}, and pauses while you use "
            "your mouse or keyboard.",
        }[level]
        risk = self.warning(app)
        minutes = self.settings.grant_minutes
        request = {
            "kind": "window",
            "icon": icon,
            "title": f"{self.agent} asks to {LEVEL_VERBS[level]} a {display} window",
            "reason": reason,
            "level": level,
            "means": means,
            "means_more": "Its own dialogs and menus too. Nothing else.",
            "risk": risk if level != "see" else "",
            "allow": _allow_label(minutes),
        }
        if opening is not None:
            request["title"] = (
                f"{self.agent} asks to open {display} and {LEVEL_VERBS[level]} its window"
            )
            request["note"] = f"{display} isn't open. Allow opens a new {display} window first."
            request["allow"] = "Open and " + _allow_label(minutes)
        outcome = self._card(request)
        if opening is not None and outcome.answer == "allow":
            window, problem = self._open_for_grant(app, display, opening)
            if problem:
                log.write(name, "nothing granted", f"{display}, {LEVEL_NAMES[level]}: {problem}")
                return False, f"Not allowed: {problem} Nothing was granted."
        return self._settle(outcome, "window", level, app, display, reason, minutes, window=window)

    def _window_to_ask_for(
        self, app: str, level: str
    ) -> tuple[dict[str, Any] | None, Grant | None]:
        """The window of ``app`` a card would ask about, and the grant already held for
        one of its windows. The window: the app's window you used last (the active one,
        else the top of the stack) that no grant of this session covers for ``level``;
        a dialog or menu of a window is never asked about apart from its window."""
        try:
            stack = self._live_stack()
        except Exception:
            return None, None
        by_id = {str(w.get("id")): w for w in stack}
        mine = [
            w
            for w in stack
            if w.get("managed")
            and not w.get("desktop")
            and not w.get("dock")
            and not w.get("parent")
            and (
                same_app(str(w.get("app", "")), app) or same_app(str(w.get("desktopFile", "")), app)
            )
            and not self._never_window(w)
        ]
        held = None
        free = []
        for w in mine:
            grant = self.book.for_window(_lineage(w, by_id), level)
            if grant is not None and grant.kind == "window":
                held = held or grant
            else:
                free.append(w)
        active = [w for w in free if w.get("active")]
        visible = [w for w in free if w.get("visible")]
        pick = (active or visible or free or [None])[-1]
        return pick, held

    def _never_window(self, window: dict[str, Any]) -> bool:
        return app_matches(str(window.get("app", "")), self.settings.never) or app_matches(
            str(window.get("desktopFile", "")), self.settings.never
        )

    # How long an app the person allowed opening has to show its window.
    OPEN_WAIT_S = 30.0

    def _open_for_grant(
        self, app: str, display: str, argv: list[str]
    ) -> tuple[dict[str, Any] | None, str]:
        """Open the app the card named, the way the app launcher does, and wait for its
        new window; returns that window, or what went wrong. The grant comes only after
        that, and is for that window, so a launch that opens something else grants nothing."""
        import shlex

        try:
            before = {str(w.get("id")) for w in self._live_stack()}
        except Exception:
            before = set()
        engine = cast("Any", self.engine)
        try:
            if engine._session is not None:
                AutomationEngine.app_launch(engine, command=shlex.join(argv))
            else:
                env = dict(os.environ)
                subprocess.Popen(
                    argv,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
        except Exception as exc:
            return None, f"{display} couldn't be opened ({exc})."
        log.write(self.session_name(), "opened", display)
        deadline = time.monotonic() + self.OPEN_WAIT_S
        while time.monotonic() < deadline:
            with contextlib.suppress(Exception):
                for w in self._live_stack():
                    if (
                        str(w.get("id")) not in before
                        and w.get("managed")
                        and not w.get("parent")
                        and (
                            same_app(str(w.get("app", "")), app)
                            or same_app(str(w.get("desktopFile", "")), app)
                        )
                    ):
                        return w, ""
            time.sleep(0.25)
        return None, f"no {display} window appeared within {self.OPEN_WAIT_S:.0f} s."

    def _full_app_id(self, app: str) -> tuple[str, str]:
        """A bare name (``kate``) becomes the full id of the open app it names
        (``org.kde.kate``), so the grant can't also cover another vendor's ``kate``.
        Two open apps ending in the same name: Claude has to name one."""
        if "." in app:
            return app, ""
        try:
            stack = self._live_stack()
        except Exception:
            return app, ""
        full = sorted(
            {
                str(w.get(key) or "").lower().removesuffix(".desktop")
                for w in stack
                for key in ("app", "desktopFile")
                if "." in str(w.get(key) or "") and same_app(str(w.get(key) or ""), app)
            }
        )
        if len(full) > 1:
            return app, f"More than one open app is called {app!r} ({', '.join(full)}); name one."
        return (full[0], "") if full else (app, "")

    def _live_stack(self) -> list[dict[str, Any]]:
        env = self.engine._session_env() if isinstance(self.engine._session, LiveSession) else None
        engine = cast("Any", self.engine)
        return engine._stack() if env is not None else self._host_stack()

    def _ask_full_desktop(self, reason: str) -> tuple[bool, str]:
        held = self.book.full_desktop()
        if held is not None:
            return True, f"Already allowed: {held.describe()}."
        minutes = min(self.settings.full_desktop_minutes, self.settings.grant_minutes)
        outcome = self._card(
            {
                "kind": "full_desktop",
                "tile": "desk",
                "title": f"{self.agent} asks to control your whole desktop",
                "reason": reason,
                "level": "full_desktop",
                "means": "Click and type in every window, on every desktop.",
                "means_more": "Never password prompts or the lock screen.",
                "risk": "Includes your terminals: the same as running any command on this "
                "computer.",
                "hard": True,
                "allow": _allow_label(minutes),
            }
        )
        return self._settle(
            outcome, "full_desktop", "full_desktop", "", "your whole desktop", reason, minutes
        )

    def _ask_own_screen(self, reason: str) -> tuple[bool, str]:
        held = self.book.own_screen()
        if held is not None:
            return True, f"Already allowed: {held.describe()}."
        minutes = self.settings.own_screen_minutes
        where = self.desktop_label() or "this session's desktop"
        outcome = self._card(
            {
                "kind": "own_screen",
                "tile": "desk",
                "title": f"{self.agent} asks for a screen of its own",
                "reason": reason,
                "means": f"A separate desktop in a window on {where}, with its own pointer and "
                "keyboard.",
                "means_more": "Nothing of yours is inside it, and it closes when the grant ends.",
                "risk": f"Apps {self.agent} opens there run as you, so they can read your files.",
                "allow": _allow_label(minutes),
            }
        )
        return self._settle(outcome, "own_screen", "", "", "a screen of its own", reason, minutes)

    def _ask_accessibility(self, reason: str) -> tuple[bool, str]:
        """The desktop's accessibility switch (design/mock/index.html, "Accessibility")."""
        from kwin_mcp.session import LiveSession

        name = self.session_name()
        if not isinstance(self.engine._session, LiveSession):
            return False, "Accessibility is asked for on a live desktop; connect first."
        bus = self.engine._session_env().get("DBUS_SESSION_BUS_ADDRESS", "")
        if a11y.is_on(bus):
            return True, "Accessibility is already on: apps describe their text fields."
        if self.a11y_declined:
            return False, (
                "The person kept accessibility off this session. Type at the focus with "
                "keyboard_type (no into); don't ask again."
            )
        if not [g for g in self.book.alive() if g.kind in ("window", "full_desktop")]:
            return False, (
                "Ask for the app first: accessibility only opens the channel, and you "
                "still need a grant for each app you read or type in."
            )
        outcome = self._card(
            {
                "kind": "accessibility",
                "tile": "a11y",
                "title": f"{self.agent} asks to turn on accessibility",
                "reason": reason,
                "means": f"So {_markup_safe(self.agent)} can read and type into text fields in "
                "the apps you allowed, "
                "without your keyboard focus.",
                "means_more": "Apps describe their windows to assistive tools, as they do for a "
                "screen reader. Nothing changes on screen.",
                "risk": "While it's on, any program running as you can read what your apps show.",
                "allow": f"Turn On While {self.agent} Works",
                "deny": "Keep It Off",
            }
        )
        if outcome.answer != "allow":
            self.a11y_declined = outcome.answer == "deny"
            what = {"timeout": "unanswered", "failed": "card failed"}.get(outcome.answer, "denied")
            log.write(name, what, "accessibility")
            if outcome.answer == "failed":
                return False, f"Not turned on: the card could not be shown ({outcome.detail})."
            return False, (
                "Not turned on: the person kept accessibility off. Type at the focus with "
                "keyboard_type (no into)."
            )
        try:
            a11y.turn_on(bus)
        except Exception as exc:
            log.write(name, "failed", f"accessibility: {exc}")
            # A Set whose reply was lost may still have applied: the record stands, so
            # the guard and the daemon still turn it off.
            a11y.start_guard(bus)
            ensure_daemon()
            return False, f"Allowed, but the switch didn't turn on: {exc}"
        log.write(name, "allowed", "accessibility turned on")
        a11y.start_guard(bus)
        ensure_daemon()
        self._keep_daemon()
        a11y.wait_until_on(bus)
        return True, (
            "Accessibility is on: apps describe their text fields. It goes back off when the "
            "last grant ends; the person can turn it off at any time."
        )

    def _settle(
        self,
        outcome: card.Outcome,
        kind: Any,
        level: str,
        app: str,
        display: str,
        reason: str,
        minutes: int,
        window: dict[str, Any] | None = None,
    ) -> tuple[bool, str]:
        name = self.session_name()
        what = display if kind != "window" else f"a {display} window, {LEVEL_NAMES[level]}"
        if outcome.answer == "allow":
            grant = self.book.give(
                kind,
                app=app,
                app_name=display,
                level=level,
                reason=reason,
                minutes=minutes,
                window=str(window.get("id")) if window else "",
                window_title=_window_title(window),
            )
            log.write(name, "allowed", what)
            ensure_daemon()
            self._keep_daemon()
            return True, f"Allowed: {grant.describe()}. The person can end it at any time."
        if outcome.answer == "timeout":
            log.write(name, "unanswered", what)
            return False, "Not allowed: the person didn't answer within 2 minutes."
        if outcome.answer == "failed":
            log.write(name, "card failed", f"{what}: {outcome.detail}")
            return False, f"Not allowed: the consent card could not be shown ({outcome.detail})."
        log.write(name, "denied", what)
        return False, "Not allowed: the person chose Don't Allow. You may ask again for less."

    # Asks every time

    def ask_once(
        self,
        tool: str,
        title: str,
        means: str,
        risk: str = "",
        *,
        allow: str = "Allow Once",
        deny: str = "Don't Allow",
        icon: str = "",
        tile: str = "shield",
        hard: bool = False,
    ) -> None:
        reason = self.reasons.pop(tool, "").strip()
        name = self.session_name()
        if not reason:
            log.write(name, "refused", f"{tool} without a reason")
            raise Refused(
                f'{tool} asks the person every time and needs a reason: pass reason="...".'
            )
        outcome = self._card(
            {
                "kind": "ask",
                "icon": icon,
                "tile": "" if icon else tile,
                "title": title,
                "reason": reason,
                "means": means,
                "risk": risk,
                "hard": hard,
                "allow": allow,
                "deny": deny,
            }
        )
        if outcome.answer == "allow":
            log.write(name, "allowed once", title)
            return
        log.write(name, outcome.answer if outcome.answer != "deny" else "denied", title)
        asked = title.removeprefix(f"{self.agent} asks to ")
        why = {"timeout": "no answer", "deny": "the person said no"}.get(
            outcome.answer, outcome.detail
        )
        raise Refused(f"Not allowed: {asked} ({why}).")


def _lineage(window: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """``window``, then the window it's transient for, and so on up (KWin's ``parent``):
    a submenu's parent is a menu, a dialog's is its window. Eight steps at most."""
    chain = [window]
    for _ in range(8):
        parent = by_id.get(str(chain[-1].get("parent") or ""))
        if parent is None or parent in chain:
            break
        chain.append(parent)
    return chain


def _window_title(window: dict[str, Any] | None, limit: int = 80) -> str:
    """A window's title for the card and the access list: one line, ``limit`` characters."""
    title = " ".join(str((window or {}).get("caption") or "").split())
    return title if len(title) <= limit else title[: limit - 1] + "\u2026"


_DID = {
    "pointer_click": "clicked",
    "pointer_move": "moved the pointer",
    "pointer_scroll": "scrolled",
    "pointer_drag": "dragged",
    "pointer_hold": "held a mouse button",
    "keyboard_press": "pressed keys",
    "keyboard_hold": "held keys",
    "touch_tap": "tapped",
    "touch_swipe": "swiped",
    "touch_pinch": "pinched",
    "element_act": "used a control",
    "window_focus": "focused a window",
    "window_place": "moved a window",
}


def _did(tool: str, bound: inspect.BoundArguments) -> str:
    """What Claude did, in words, for the log (never the text it typed)."""
    if tool == "keyboard_type":
        count = len(str(bound.arguments.get("text", "")))
        chars = f"{count} character" + ("" if count == 1 else "s")
        if bound.arguments.get("into") is None:
            return f"typed {chars}"
        if bound.arguments.get("replace"):
            return f"replaced a field's text with {chars}"
        return f"wrote {chars}"
    return _DID.get(tool, tool)


def _acted(level: str) -> None:
    """After Claude's click, keypress or scroll was delivered, never before: the corner
    notice's ring shows use, so it must not move for a refused or failed action."""
    if covers(level, "control"):
        with contextlib.suppress(OSError):
            state.note_acted()


def _note_pointer(engine: AutomationEngine, bound: inspect.BoundArguments) -> None:
    """Tell the overlay where the agent just moved the pointer, for the label with its name."""
    import json

    args = bound.arguments
    target = args.get("to") or args.get("target") or args.get("center")
    if isinstance(target, Point):
        warden = getattr(engine, "warden", None)
        agent = warden.agent if warden is not None else "Claude"
        point = {"x": target.x, "y": target.y, "t": time.time(), "agent": agent}
        with contextlib.suppress(OSError):
            path = state.runtime_dir() / "pointer"
            path.write_text(json.dumps(point), encoding="utf-8")


def _markup_safe(text: str) -> str:
    """Text for the card's few rich-text lines: Claude's words are shown, never drawn as markup."""
    import html

    return html.escape(text, quote=False)


def _allow_label(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"Allow for {hours} Hour" + ("" if hours == 1 else "s")
    return f"Allow for {minutes} Minutes"


# ── The daemon: stop key, your hands, the frame and the access list ───────────


def ensure_daemon() -> None:
    """Start the warden's daemon if it isn't running (it ends itself when idle)."""
    import subprocess
    import sys

    pid_path = state.runtime_dir() / "daemon.pid"
    with contextlib.suppress(OSError, ValueError):
        pid = int(pid_path.read_text().strip())
        if "kwin_mcp.warden.daemon" in _cmdline(pid):
            return
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env["QT_FORCE_STDERR_LOGGING"] = "1"
    with contextlib.suppress(OSError):
        subprocess.Popen(
            [sys.executable, "-m", "kwin_mcp.warden.daemon"],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


# ── The gated engine ──────────────────────────────────────────────────────────


def _wrap(name: str, rule: Rule) -> Callable[..., Any]:
    original = getattr(AutomationEngine, name)
    signature = inspect.signature(original)

    @functools.wraps(original)
    def guarded(self: WardenEngine, *args: Any, **kwargs: Any) -> Any:
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        return self._guarded_call(name, rule, original, bound)

    guarded.__dict__["__warden_rule__"] = rule
    return guarded


def _your_input(rule: Rule) -> float:
    """When the person last did something that should pause this action.

    Claude's pointer is your pointer, so a pointer action waits for any use of the
    mouse, a nudge included. Typing, key presses, focusing and placing windows don't move
    the pointer, so they wait only for a click or a key press (a mouse
    gets nudged all the time). A focus change is caught on its own, before every piece of
    typing.
    """
    return state.last_hands() if rule.scope == "point" else state.last_press()


class WardenEngine(AutomationEngine):
    """kwin-mcp's engine with the warden in front of every tool."""

    def __init__(self, *args: Any, settings: Settings | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.warden = Warden(self, settings)
        self._screen_watch: threading.Thread | None = None

    @staticmethod
    def _live_accessibility(
        session: Any, dbus_addr: str, *, enable: bool, warnings: list[str]
    ) -> Any:
        """The warden's switch: never flipped by session_connect, only from its card."""
        state_now = AutomationEngine._live_accessibility(
            session, dbus_addr, enable=False, warnings=warnings
        )
        if "Reconnect with enable_accessibility=true" in warnings:
            warnings.remove("Reconnect with enable_accessibility=true")
        if state_now == "off":
            warnings.append(A11Y_OFF_HINT)
        elif enable and state_now == "on":
            pass  # already on: nothing to switch
        return state_now

    def _a11y_off(self) -> bool:
        from kwin_mcp.session import LiveSession

        if not isinstance(self._session, LiveSession):
            return False
        return not a11y.is_on(self._session_env().get("DBUS_SESSION_BUS_ADDRESS", ""))

    def _watch_own_screen(self) -> None:
        """Close Claude's own screen the moment its grant ends, not at Claude's next call.

        Runs off the tool thread, so it never touches the engine: it stops the
        session's process group through kwin-mcp's own registry, as kwin-mcp does
        when its server exits mid-tool. The next call finds the session gone.
        """
        from kwin_mcp.session import Session, process_registry

        while True:
            time.sleep(0.5)
            session = self._session
            if not isinstance(session, Session) or not session.is_running:
                return
            if self.warden.book.own_screen() is None:
                proc = getattr(session, "_process", None)
                if proc is not None:
                    process_registry.terminate_leaders([proc], reap=False)
                    log.write(self.warden.session_name(), "own screen", "closed: its grant ended")
                return

    def _host_wayland_socket(self) -> str:
        """Claude's own screen is a window on your desktop, so you can watch it."""
        name = os.environ.get("WAYLAND_DISPLAY", "")
        if not name or os.environ.get("KWIN_MCP_WARDEN_HEADLESS"):
            return ""
        if os.path.isabs(name):
            return name
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        return os.path.join(runtime, name)

    # Session kind

    def _live(self) -> bool:
        return isinstance(self._session, LiveSession)

    def _virtual(self) -> bool:
        return self._session is not None and not self._live()

    # The one gate every tool passes

    def _guarded_call(
        self, name: str, rule: Rule, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        try:
            return self._guarded_call_inner(name, rule, original, bound)
        except Refused as exc:
            if not exc.logged:
                log.write(self.warden.session_name(), "refused", f"{name}: {exc}")
            raise
        finally:
            # A reason belongs to one call; a refused call must not leave it for the next.
            self.warden.reasons.pop(name, None)

    def _guarded_call_inner(
        self, name: str, rule: Rule, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        w = self.warden
        if self._live():
            ensure_daemon()
        if rule.kind == "own_screen":
            if w.book.own_screen() is None:
                raise Refused(
                    "A screen of your own needs the person's go: call "
                    'access_request(level="own_screen", reason=...) first.'
                )
            result = original(*bound.args, **bound.kwargs)
            if isinstance(self._session, Session):
                self._session.contain_apps = True
            self._screen_watch = threading.Thread(target=self._watch_own_screen, daemon=True)
            self._screen_watch.start()
            return result
        if self._virtual():
            if name == "app_launch":
                self._own_screen_launch(bound)
            if w.book.own_screen() is None and name not in ("session_stop", "session_info"):
                try:
                    AutomationEngine.session_stop(self)
                except Exception as exc:
                    log.write(w.session_name(), "own screen", f"could not close it: {exc}")
                raise Refused("The grant for your own screen ended, so the screen was closed.")
            return original(*bound.args, **bound.kwargs)
        if rule.kind == "free" or not self._live():
            return original(*bound.args, **bound.kwargs)
        # Your screen.
        if rule.kind == "ask":
            return self._ask(name, rule, original, bound)
        return self._level(name, rule, original, bound)

    # Claude's own screen: what it opens there must stay there

    _HOST_ENV = (
        *HOST_DESKTOP_ENV,
        "WAYLAND_DISPLAY",
        "WAYLAND_SOCKET",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "AT_SPI_BUS_ADDRESS",
    )
    _REACHES_OUT = (
        "env",
        "sh",
        "bash",
        "zsh",
        "dash",
        "fish",
        "ksh",
        "sudo",
        "pkexec",
        "su",
        "doas",
        "flatpak-spawn",
        "systemd-run",
        "dbus-send",
        "dbus-launch",
        "gdbus",
        "qdbus",
        "qdbus6",
        "busctl",
        "ydotool",
        "wtype",
        "kdotool",
        "xdotool",
        "dotool",
        "kstart",
        "kstart5",
        "kioclient",
        "xdg-open",
        "konsole",
        "python",
        "python3",
        "perl",
        "node",
    )

    def _own_screen_launch(self, bound: inspect.BoundArguments) -> None:
        """Refuse a launch in Claude's own screen that would reach your desktop or bus."""
        import shlex

        env = bound.arguments.get("env") or {}
        bad = [k for k in env if k.upper() in self._HOST_ENV]
        if bad:
            raise Refused(
                f"In your own screen, apps can't set {', '.join(bad)}: that would "
                "point them at the person's desktop."
            )
        try:
            program = os.path.basename(shlex.split(str(bound.arguments.get("command", "")))[0])
        except (ValueError, IndexError):
            return
        if program in self._REACHES_OUT or program.startswith("python"):
            raise Refused(
                f"{program} can reach outside your own screen, so it isn't "
                "launched there. Launch the app itself."
            )

    # Asks every time

    def _ask(
        self, name: str, rule: Rule, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        w = self.warden
        if state.card_is_open():
            raise Refused("A consent card is on screen; wait for the person to answer it.")
        args = bound.arguments
        if rule.ask == "dbus":
            service = str(args.get("service", ""))
            path = str(args.get("path", ""))
            if (
                service.startswith(":")
                or any(service == s or service.startswith(s + ".") for s in w.settings.never_dbus)
                or any(
                    path == p or path.startswith(p.rstrip("/") + "/")
                    for p in w.settings.never_dbus_paths
                )
            ):
                log.write(w.session_name(), "never", f"dbus_call to {service} {path}")
                raise Refused(
                    f"dbus_call to {service} {path} is never allowed (unique names, the "
                    "warden, shortcuts, KWin's script and effect loaders, the lock "
                    "screen and password stores)."
                )
            w.ask_once(
                name,
                f"{w.agent} asks to call a D-Bus method",
                _markup_safe(
                    f"{args.get('interface')}.{args.get('method')} on {service} {args.get('path')}."
                ),
                "D-Bus can reach almost anything in your session.",
                hard=True,
            )
        elif rule.ask == "clipboard_read":
            w.ask_once(
                name,
                f"{w.agent} asks to read your clipboard",
                "Whatever you copied last, once.",
                "Your clipboard may hold a password or other private text.",
            )
        elif rule.ask == "clipboard_write":
            w.ask_once(
                name, f"{w.agent} asks to set your clipboard", "It replaces what you copied last."
            )
        elif rule.ask == "launch":
            command = str(args.get("command", ""))
            program = command.split()[0] if command.split() else command
            display, icon = apps.describe(os.path.basename(program))
            env = args.get("env") or {}
            runs = "Runs: " + _markup_safe(command)
            if env:
                runs += " with " + _markup_safe(", ".join(f"{k}={v}" for k, v in env.items()))
            w.ask_once(
                name,
                f"{w.agent} asks to open {display}",
                runs,
                w.warning(os.path.basename(program)),
                allow="Open It",
                deny="Don't Open",
                icon=icon,
            )
        return original(*bound.args, **bound.kwargs)

    # Levels

    def _level(
        self, name: str, rule: Rule, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        w = self.warden
        level = rule.level or "control"
        into = name == "keyboard_type" and bound.arguments.get("into") is not None
        if into:
            # Written straight into a text field over AT-SPI: the grant is checked on the
            # field's own window, not on whichever window has your focus.
            rule = Rule("level", level, "element")
        if rule.scope == "filter":
            return self._filtered(name, original, bound)
        if rule.scope == "capture":
            self._needs_some(level)
            before = self._stack()
            frame = original(*bound.args, **bound.kwargs)
            self._mask([frame], before)
            return frame
        if state.card_is_open():
            raise Refused(
                "A consent card is on screen; nothing of yours moves until it's answered."
            )
        if self._screen_locked():
            raise Refused("The screen is locked; the lock screen is never granted.")
        stack_before = self._stack()
        if rule.scope == "element":
            self._pin_strict(name, bound, level)
        windows = self._targets(rule, bound)
        grants = [self._allowed(win, level) for win in windows]
        # Focus or a moved window would take your typing too. Writing into a field touches
        # neither your focus nor your pointer, so it never waits for your hands.
        since = time.time()
        if covers(level, "arrange") and not into:
            since = self._hands_first(rule, bound)
        between, before_press = self._mid_action_checks(rule, level, grants, since)
        with input_checks(between, before_press):
            if name == "keyboard_type" and bound.arguments.get("method") == "keys" and not into:
                result = self._type_in_pieces(rule, original, bound)
            else:
                try:
                    result = original(*bound.args, **bound.kwargs)
                except (RuntimeError, ValueError) as exc:
                    if (into or name == "element_act") and self._a11y_off():
                        raise type(exc)(f"{exc}\n{A11Y_OFF_HINT}") from exc
                    raise
                _acted(level)
        if rule.scope == "point":
            _note_pointer(self, bound)
        for grant in {g.id: g for g in grants}.values():
            where = f"a {grant.app_name} window" if grant.kind == "window" else "your desktop"
            log.write(w.session_name(), "did", f"{_did(name, bound)} in {where}")
        frames = getattr(result, "frames", None)
        if frames:
            self._mask(frames, stack_before)
        return result

    def _mid_action_checks(
        self, rule: Rule, level: str, grants: list[Grant], since: float
    ) -> tuple[Callable[[], None] | None, Callable[[int, int], None] | None]:
        """Checks that run inside the action itself (kwin_mcp.input.input_checks).

        Between the steps of a drag, swipe, pinch, long-press or multi-step scroll: your
        hands, a consent card and the stop key end it at once, and the gesture lets go of
        what it pressed. Right before a button or touch goes down: the window under that
        point, looked up again after the pointer got there, must still be granted.
        """
        if not covers(level, "control"):
            return None, None
        # ``since``: when the hands were last seen quiet before the action, so a touch
        # while the window was checked again after a pause counts as during the action.
        started = since
        held = list({g.id: g for g in grants}.values())

        def between() -> None:
            if _your_input(rule) > started:
                log.write(self.warden.session_name(), "stopped", "you used the mouse or keyboard")
                raise Refused(
                    "The person used the mouse or keyboard, so you stopped in the "
                    "middle and let go. Try again once they're done."
                )
            if state.card_is_open():
                raise Refused("A consent card came up, so you stopped in the middle and let go.")
            if any(not g.alive() for g in held):
                raise Refused("The grant ended, so you stopped in the middle and let go.")

        def before_press(x: int, y: int) -> None:
            stack = self._stack()
            by_id = {str(s["id"]): s for s in stack}
            self._allowed(self._top_at(stack, x, y), level, by_id)

        return between, before_press if rule.scope == "point" else None

    def _screen_locked(self) -> bool:
        """Whether the session's screen locker is up (org.freedesktop.ScreenSaver.GetActive)."""
        import subprocess

        try:
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
                timeout=3,
                env=self._session_env(),
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return "true" in out

    def _needs_some(self, level: str) -> None:
        if not [g for g in self.warden.book.alive() if g.kind in ("window", "full_desktop")]:
            raise Refused(f"Nothing on this screen is granted yet. {ASK_FIRST}")

    def _stack(self) -> list[dict[str, Any]]:
        resp = self._run_kwin_query({"op": "stack"})
        if not resp.get("ok"):
            raise Refused(f"The warden couldn't read the window stack: {resp.get('error')}")
        return list(resp["result"])

    def _never(self, window: dict[str, Any]) -> bool:
        pid = int(window.get("pid") or 0)
        return (
            "kwin-mcp-warden" in str(window.get("caption", "")).lower()
            or app_matches(str(window.get("app", "")), self.warden.settings.never)
            or app_matches(str(window.get("desktopFile", "")), self.warden.settings.never)
            or (pid > 0 and _is_warden_process(pid))
        )

    def _allowed(
        self,
        window: dict[str, Any] | None,
        level: str,
        by_id: dict[str, dict[str, Any]] | None = None,
    ) -> Grant:
        """The grant that covers ``level`` work in ``window``, or a refusal.

        A window grant covers that one window and its own dialogs and menus; ``by_id``
        (the stack by id) lets a dialog be traced to its window, and is read when not given.
        """
        w = self.warden
        if window is None:
            full = w.book.full_desktop()
            if full is not None and covers("full_desktop", level):
                return full
            raise Refused(f"That spot isn't over any app's window. {ASK_FIRST}")
        app = str(window.get("app") or window.get("desktopFile") or "")
        if self._never(window):
            log.write(w.session_name(), "never", f"{level} in {app or 'a warden window'}")
            raise Refused(
                f"{app or 'That window'} is never granted (password prompts, the lock "
                "screen and the warden's own windows)."
            )
        if not window.get("managed") and w.book.full_desktop() is None:
            raise Refused(f"That spot is on a panel or overlay, not an app's window. {ASK_FIRST}")
        if by_id is None:
            by_id = {str(s["id"]): s for s in self._stack()}
        lineage = _lineage(window, by_id)
        grant = (
            None if any(self._never(x) for x in lineage[1:]) else w.book.for_window(lineage, level)
        )
        if grant is None:
            shown = apps.describe(app)[0] if app else "that window"
            log.write(w.session_name(), "refused", f"{level} in {shown} (not granted)")
            refusal = Refused(
                f"{level.replace('_', ' ').capitalize()} in that {shown} window ({app}) isn't "
                f"granted. {ASK_FIRST}"
            )
            refusal.logged = True
            raise refusal
        return grant

    @staticmethod
    def _see_through(window: dict[str, Any]) -> bool:
        """The warden's own click-through frame layer: no input, nothing hidden by it."""
        pid = int(window.get("pid") or 0)
        return pid > 0 and "overlay.qml -- frames" in _cmdline(pid)

    def _top_at(self, stack: list[dict[str, Any]], x: float, y: float) -> dict[str, Any] | None:
        for window in reversed(stack):
            if not window.get("visible") or self._see_through(window):
                continue
            fx, fy, fw, fh = window["frame"]
            if fx <= x < fx + fw and fy <= y < fy + fh and not window.get("desktop"):
                return window
        return None

    def _targets(self, rule: Rule, bound: inspect.BoundArguments) -> list[dict[str, Any] | None]:
        """The windows this call would act in."""
        args = bound.arguments
        stack = self._stack()
        if rule.scope == "window":
            selector = args.get("window")
            if not isinstance(selector, WindowSelector):
                raise Refused("The warden needs one window to check.")
            from kwin_mcp.geometry import select_one

            try:
                target = select_one(self._windows(), selector)
            except (RuntimeError, ValueError) as exc:
                raise Refused(self._selector_problem(selector, exc)) from None
            return [
                next(
                    (s for s in stack if s["id"] == target.id),
                    {"id": target.id, "app": target.app_id, "managed": True, "pid": 0},
                )
            ]
        if rule.scope == "focus":
            active = next((s for s in stack if s.get("active")), None)
            if active is None:
                raise Refused("No window has keyboard focus, so there is nothing to type into.")
            return [active]
        if rule.scope == "element":
            return self._element_windows(args.get("element") or args.get("into"), stack)
        if rule.scope == "point":
            targets: list[Any] = [
                args[k]
                for k in ("target", "from_", "to", "center")
                if isinstance(args.get(k), (Point, ElementPoint))
            ]
            for waypoint in args.get("waypoints") or []:
                targets.append(Point(x=waypoint.x, y=waypoint.y))
            if not targets:
                raise Refused("The warden found no point to check in this call.")
            points = [p for p, _ in self._resolve_targets(*targets)]
            return [self._top_at(stack, p.x, p.y) for p in points]
        return [None]

    def _element_allowed(self, element: Any, by_id: dict[str, Any], level: str) -> bool:
        """Whether ``element`` (from element_query) sits in a window granted for ``level``."""
        window = by_id.get(str(element.window_id)) if element.window_id else None
        try:
            self._allowed(window, level, by_id)
        except Refused:
            return False
        return True

    def _pin_strict(self, name: str, bound: inspect.BoundArguments, level: str) -> None:
        """Turn a strict locator into the ref of the one element it means, found only in
        windows granted for ``level``; the engine then acts on that element alone.

        Matches in other windows are left out before anything is decided, so a refusal
        reads the same whether or not something matched there (no reading names out of
        windows that aren't granted one guess at a time), and nothing that appears between
        this check and the action can be acted on, counted or listed.
        """
        key = "into" if bound.arguments.get("into") is not None else "element"
        locator = getattr(bound.arguments.get(key), "strict", None)
        if locator is None:
            return
        depth = 64 if name == "element_act" else 15
        found = AutomationEngine.element_query(self, locator=locator, max_depth=depth)
        by_id = {s["id"]: s for s in self._stack()}
        usable = [el for el in found.elements if self._element_allowed(el, by_id, level)]
        if name == "element_act":
            asked = bound.arguments.get("action")
            names = asked.split("|") if isinstance(asked, str) else list(asked or [])
            wanted = {str(n).strip().lower() for n in names}
            acting = [el for el in usable if wanted & {a.lower() for a in el.actions}]
            usable = acting or usable
        elif name == "keyboard_type":
            usable = [el for el in usable if "editable" in el.states] or usable
        desc = locator.describe()
        if not usable:
            self._explain_hollow(locator, by_id, level, desc)
            raise Refused(f"Nothing in a window you're granted matches {desc}. {ASK_FIRST}")
        if len(usable) > 1:
            listed = "; ".join(f'[{el.role}] "{el.name}" ({el.ref})' for el in usable[:10])
            raise Refused(
                f"{len(usable)} elements in windows you're granted match {desc}: {listed}. "
                "Narrow the locator or use one ref."
            )
        bound.arguments[key] = RefAddr(ref=str(usable[0].ref))

    def _explain_hollow(
        self, locator: Locator, by_id: dict[str, Any], level: str, desc: str
    ) -> None:
        """When the granted windows of the app ``locator`` names show nothing inside
        to accessibility (Chrome and Electron apps unless started with
        --force-renderer-accessibility or with Orca running), say so."""
        if not locator.app:
            return
        named = AutomationEngine.element_query(self, locator=Locator(app=locator.app), max_depth=2)
        granted = {
            str(el.window_id)
            for el in named.elements
            if el.depth == 1 and self._element_allowed(el, by_id, level)
        }
        if not granted:
            return
        if any(el.depth == 2 and str(el.window_id) in granted for el in named.elements):
            return
        raise RuntimeError(
            f"No editable text element matches {desc}: the app shows its windows to "
            "accessibility but nothing inside them. Chrome and Electron apps fill them in only "
            "when started with --force-renderer-accessibility or while the Orca screen reader "
            "runs, and even then their text can't be written over AT-SPI. Click the field, "
            "then keyboard_type without into."
        )

    def _element_windows(
        self, element: Any, stack: list[dict[str, Any]]
    ) -> list[dict[str, Any] | None]:
        ref = getattr(element, "ref", None)
        if ref:
            self._check_ref(ref)
            resp = self._run_atspi("resolve", refs=[ref])
            entries = self._atspi_result(resp)
            window_ids = [
                str((e.get("element") or {}).get("window_id")) for e in entries if e.get("ok")
            ]
        else:
            locator = getattr(element, "strict", None)
            if locator is None:
                raise Refused(
                    "The warden couldn't tell which element that is, so it can't check the grant."
                )
            found = AutomationEngine.element_query(self, locator=locator, max_depth=15)
            window_ids = [str(el.window_id) for el in found.elements]
        if not window_ids or any(wid in ("None", "") for wid in window_ids):
            raise Refused(
                "The warden couldn't tell which window that element is in, so it can't "
                "check the grant."
            )
        by_id = {s["id"]: s for s in stack}
        return [by_id.get(wid) for wid in window_ids]

    # Reads cut down to what's granted

    def _see_apps(self) -> tuple[bool, list[Grant]]:
        alive = self.warden.book.alive()
        return any(g.kind == "full_desktop" for g in alive), [
            g for g in alive if g.kind == "window"
        ]

    def _sees(self, window: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
        """Whether Claude may see ``window``: a granted window, or a dialog or menu of one
        (never a never-granted one, even under Full desktop)."""
        lineage = _lineage(window, by_id)
        if any(self._never(x) for x in lineage):
            return False
        return self.warden.book.for_window(lineage, "see") is not None

    def _filtered(
        self, name: str, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        """Reads cut down to what's granted; never-granted windows stay hidden even under
        Full desktop, and a caption filter only ever matches titles Claude may see."""
        full, _ = self._see_apps()
        if name == "window_list":
            wanted = bound.arguments.get("filter")
            caption = getattr(wanted, "caption", None) if wanted is not None else None
            if caption and wanted is not None:
                # Filter by caption here, after the titles Claude can't see are blanked,
                # so a filter can't confirm what a hidden title says.
                bound.arguments["filter"] = wanted.model_copy(update={"caption": None})
            result = original(*bound.args, **bound.kwargs)
            if not isinstance(result, WindowList):
                return result
            stack = {s["id"]: s for s in self._stack()}
            shown = []
            for win in result.windows:
                info = stack.get(win.id)
                if info is None:
                    continue  # not in KWin's stack now: nothing to check it by
                if not self._sees(info, stack):
                    if not full:
                        continue  # a window Claude may not see isn't listed at all
                    win = win.model_copy(update={"caption": ""})  # never-granted, under Full
                if caption and caption.lower() not in win.caption.lower():
                    continue
                shown.append(win)
            return result.model_copy(update={"windows": shown})
        result = original(*bound.args, **bound.kwargs)
        if name == "element_query":
            if not full:
                self._needs_some("see")
            stack = {s["id"]: s for s in self._stack()}
            keep = [
                el
                for el in result.elements
                if el.window_id in stack and self._sees(stack[el.window_id], stack)
            ]
            return result.model_copy(update={"elements": keep})
        return result

    def _mask(self, frames: Iterable[Any], before: list[dict[str, Any]] | None = None) -> None:
        """Black out everything in each picture that isn't a granted app's visible window.

        Uses the window stack from before the picture (when the caller has it) and after,
        and shows only what both allow, so a window that moved in between can't show
        through. Under Full desktop, never-granted windows are still blacked out.
        """
        from PIL import Image, ImageChops, ImageDraw

        full, _ = self._see_apps()
        stacks = [s for s in (before, self._stack()) if s is not None]
        for frame in frames:
            path = getattr(frame, "path", None)
            mapping = getattr(frame, "mapping", None)
            if not path:
                continue
            if mapping is None:
                with contextlib.suppress(OSError):
                    os.unlink(path)
                raise Refused(
                    "A picture without a coordinate mapping can't be cut to the granted "
                    "windows, so it was deleted."
                )
            with Image.open(path) as img:
                img.load()
                combined = None
                for stack in stacks:
                    by_id = {w["id"]: w for w in stack}
                    mask = Image.new("L", img.size, 255 if full else 0)
                    draw = ImageDraw.Draw(mask)
                    ox, oy, sc = mapping.origin.x, mapping.origin.y, mapping.scale
                    for window in stack:
                        if not window.get("visible") or self._see_through(window):
                            continue
                        fx, fy, fw, fh = window["frame"]
                        box = (
                            (fx - ox) * sc,
                            (fy - oy) * sc,
                            (fx + fw - ox) * sc - 1,
                            (fy + fh - oy) * sc - 1,
                        )
                        show = self._sees(window, by_id)
                        draw.rectangle(box, fill=255 if show else 0)
                    combined = mask if combined is None else ImageChops.darker(combined, mask)
                if combined is None:  # nothing to go by: show nothing
                    combined = Image.new("L", img.size, 0)
                black = Image.new(img.mode, img.size)
                Image.composite(img, black, combined).save(path)

    # Your hands come first

    TYPE_PIECE = 8  # characters typed between two looks at your hands

    def _type_in_pieces(
        self, rule: Rule, original: Callable[..., Any], bound: inspect.BoundArguments
    ) -> Any:
        """Type per-key text a few characters at a time, pausing mid-way for your input.

        Between pieces the warden looks at your hands again and, after a pause, at the
        window with focus, so Claude stops mid-word when you click or press a key.
        """
        from kwin_mcp.models import text_echo

        text = str(bound.arguments.get("text", ""))
        if len(text) <= self.TYPE_PIECE:
            result = original(*bound.args, **bound.kwargs)
            _acted(rule.level or "control")
            return result
        pieces = [text[i : i + self.TYPE_PIECE] for i in range(0, len(text), self.TYPE_PIECE)]
        result = None
        for i, piece in enumerate(pieces):
            if i:
                self._hands_first(rule, bound)
                # Every piece: no card up, and the window with focus is still granted
                # (a password prompt that takes focus mid-word gets nothing).
                if state.card_is_open():
                    raise Refused("A consent card came up while you typed; you stopped there.")
                for window in self._targets(rule, bound):
                    self._allowed(window, rule.level or "control")
            args = dict(bound.arguments)
            args["text"] = piece
            if i < len(pieces) - 1:
                args["capture"] = None
            args.pop("self", None)
            result = original(self, **args)
            _acted(rule.level or "control")  # each piece is one ring, like a run of keypresses
        assert result is not None  # more than one piece, so at least one was typed
        return result.model_copy(update={"chars": len(text), "echo": text_echo(text)})

    def _hands_first(self, rule: Rule, bound: inspect.BoundArguments) -> float:
        """Wait until the person's hands are quiet; returns the wall time they were last
        seen quiet, from which any use of them stops the action midway."""
        w = self.warden
        if not state.hands_known():
            ensure_daemon()
            deadline = time.monotonic() + 3
            while not state.hands_known() and time.monotonic() < deadline:
                time.sleep(0.1)
        if not state.hands_known():
            raise Refused(
                "The warden can't see the person's mouse and keyboard (it needs read access to "
                "/dev/input, the input group), so it can't pause you for them. Arranging and "
                "controlling windows on their screen stay off until it can."
            )
        quiet = w.settings.pause_seconds
        started = time.monotonic()
        paused = False
        while True:
            checked = time.time()
            if checked - _your_input(rule) >= quiet:
                if not paused:
                    break
                # After a pause: only once the window it holds is the one under the target
                # or with focus again, and no card came up while it waited. A touch during
                # that check means waiting again, so nothing starts with input already past
                # ``checked``.
                if state.card_is_open():
                    raise Refused("A consent card came up while you waited for the person.")
                for window in self._targets(rule, bound):
                    self._allowed(window, rule.level or "control")
                if _your_input(rule) <= checked:
                    break
            if not paused:
                paused = True
                state.note_paused("pointer" if rule.scope == "point" else "presses")
                log.write(
                    w.session_name(),
                    "paused",
                    "you used the mouse or keyboard"
                    if rule.scope == "point"
                    else "you clicked or pressed a key",
                )
            if time.monotonic() - started > w.settings.pause_limit_seconds:
                raise Refused(
                    "The person kept using the mouse or keyboard, so you waited and "
                    "then stopped. Try again later."
                )
            time.sleep(0.1)
        return checked

    # window_close: our card replaces kwin-mcp's own question

    def _close(self, window: WindowSelector, capture: Any, consent: Any) -> WindowCloseResult:
        if not self._live():
            if self._virtual() and self.warden.book.own_screen() is None:
                raise Refused("The grant for your own screen ended.")
            return AutomationEngine._close(self, window, capture, consent)
        if state.card_is_open():
            raise Refused("A consent card is on screen; wait for the person to answer it.")
        target = self._close_target(window)
        if target is None:
            return AutomationEngine._close(self, window, capture, "not_required")
        stack = {s["id"]: s for s in self._stack()}
        info = stack.get(target.window_id, {"app": target.app_id, "pid": 0, "managed": True})
        if self._never(info):
            raise Refused(f"{target.app_id} is never granted.")
        display, icon = apps.describe(target.app_id)
        caption = str(info.get("caption", ""))
        unsaved = caption.startswith("*") or "[modified]" in caption.lower() or " * " in caption
        document = caption.split(" \u2014 ")[0].split(" - ")[0].replace("*", "").strip()
        risk = ""
        if unsaved:
            risk = (
                f"{document} has unsaved changes. Closing the window loses them."
                if document
                else "This window has unsaved changes. Closing it loses them."
            )
        self.warden.ask_once(
            "window_close",
            f"{self.warden.agent} asks to close a {display} window",
            "",
            risk,
            allow="Close It",
            deny="Don't Close",
            icon=icon,
        )
        before = self._stack()
        result = AutomationEngine._close(
            self, WindowSelector(id=target.window_id), capture, "accepted"
        )
        if getattr(result, "frames", None):
            self._mask(result.frames, before)
        return result

    def _windows(self) -> list[Any]:
        """kwin-mcp's window list cut to the windows Claude may see, so a selector can't
        find, or tell apart, a window that isn't granted. Under Full desktop every window
        is there, never-granted ones without their titles."""
        windows = AutomationEngine._windows(self)
        if not self._live():
            return windows
        full, _ = self._see_apps()
        stack = {s["id"]: s for s in self._stack()}
        kept = []
        for w in windows:
            info = stack.get(w.id)
            if info is not None and self._sees(info, stack):
                kept.append(w)
            elif full:
                kept.append(w.model_copy(update={"caption": ""}))
        return kept

    def _close_target(self, window: WindowSelector) -> Any:
        """kwin-mcp's lookup; its "several windows match" message cut to what Claude may see."""
        try:
            return AutomationEngine._close_target(self, window)
        except (RuntimeError, ValueError) as exc:
            if not self._live():
                raise
            raise Refused(self._selector_problem(window, exc)) from None

    def _selector_problem(self, selector: WindowSelector, exc: Exception) -> str:
        """A selector error without the titles of windows Claude may not see."""
        from kwin_mcp.geometry import select_windows

        matches = select_windows(self._windows(), selector)
        rows = [
            f"{w.id} ({w.app_id}" + (f', "{w.caption}"' if w.caption else "") + ")" for w in matches
        ]
        if not matches:
            return "No window matches that selector."
        return f"{len(matches)} windows match that selector; name one by id: " + "; ".join(rows)

    def _note_reason(self, tool: str, reason: str | None) -> None:
        """Remember the reason Claude gave for an ask-every-time tool."""
        if reason:
            self.warden.reasons[tool] = reason


def _closed(name: str) -> Callable[..., Any]:
    def refuse(self: WardenEngine, *args: Any, **kwargs: Any) -> Any:
        raise Refused(f"{name} isn't in the warden's levels table yet, so it stays closed.")

    refuse.__dict__["__warden_rule__"] = None
    return refuse


for _name, _rule in RULES.items():
    if hasattr(AutomationEngine, _name) and _name != "window_close":
        setattr(WardenEngine, _name, _wrap(_name, _rule))
# Every other public engine method is a tool the table doesn't place (a new upstream
# tool, say): closed until someone adds its rule.
for _name in dir(AutomationEngine):
    if (
        not _name.startswith("_")
        and callable(getattr(AutomationEngine, _name))
        and _name not in RULES
    ):
        setattr(WardenEngine, _name, _closed(_name))
# window_close goes through _close (the server calls it directly), so both are guarded.
_name = "window_close"
setattr(WardenEngine, _name, _wrap(_name, RULES[_name]))
