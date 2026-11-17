"""What each kwin-mcp tool needs from the warden, and the words the card uses.

The approved design (design/mock/index.html, "The levels" and "The other asks")
is the source of every rule and every line here. A tool that has no rule is
refused: a new upstream tool stays closed until someone places it in ``RULES``
(tests/unit/test_warden_policy.py takes the tool names from the server's own
registry, so a new tool fails that test until it is placed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type Level = Literal["see", "arrange", "control", "full_desktop"]

# Each level includes the ones before it.
LEVELS: tuple[Level, ...] = ("see", "arrange", "control", "full_desktop")
RANK: dict[str, int] = {level: i for i, level in enumerate(LEVELS)}

LEVEL_NAMES: dict[str, str] = {
    "see": "See",
    "arrange": "Arrange",
    "control": "Control",
    "full_desktop": "Full desktop",
}

# The request's verb names the level (mock, badge 1).
LEVEL_VERBS: dict[str, str] = {
    "see": "see",
    "arrange": "arrange",
    "control": "control",
}

type Kind = Literal[
    "free",  # no grant needed: session status, own launched apps' logs, protocol lists
    "own_screen",  # starting a screen of Claude's own needs that grant
    "level",  # needs a grant of at least ``level`` for the app it touches
    "ask",  # asks every time, with its own card
]

type Scope = Literal[
    "none",  # nothing to scope
    "window",  # one window named by a WindowSelector
    "point",  # the window under one or more pointer or touch targets
    "focus",  # the window with keyboard focus
    "element",  # an accessibility element's window
    "filter",  # a read whose result is cut down to what is granted
    "capture",  # a picture with everything not granted blacked out
]


@dataclass(frozen=True)
class Rule:
    """What one tool needs on your screen (a live session)."""

    kind: Kind
    level: Level | None = None
    scope: Scope = "none"
    ask: str | None = None  # which "asks every time" card


RULES: dict[str, Rule] = {
    # Session
    "session_start": Rule("own_screen"),
    "session_connect": Rule("free"),
    "session_stop": Rule("free"),
    "session_info": Rule("free"),
    # Apps
    "app_launch": Rule("ask", ask="launch"),
    "app_log": Rule("free"),  # only apps launched through this server
    # Screen and elements
    "screen_capture": Rule("level", "see", "capture"),
    "element_query": Rule("level", "see", "filter"),
    "element_act": Rule("level", "control", "element"),
    # Windows and desktops
    "window_list": Rule("level", "see", "filter"),
    "desktop_list": Rule("free"),
    "window_focus": Rule("level", "arrange", "window"),
    "window_place": Rule("level", "arrange", "window"),
    "window_close": Rule("ask", scope="window", ask="close"),
    # Pointer
    "pointer_click": Rule("level", "control", "point"),
    "pointer_move": Rule("level", "control", "point"),
    "pointer_scroll": Rule("level", "control", "point"),
    "pointer_drag": Rule("level", "control", "point"),
    "pointer_hold": Rule("level", "control", "point"),
    # Keyboard
    "keyboard_press": Rule("level", "control", "focus"),
    "keyboard_type": Rule("level", "control", "focus"),
    "keyboard_hold": Rule("level", "control", "focus"),
    # Touch
    "touch_tap": Rule("level", "control", "point"),
    "touch_swipe": Rule("level", "control", "point"),
    "touch_pinch": Rule("level", "control", "point"),
    # Clipboard and escape hatches
    "clipboard_read": Rule("ask", ask="clipboard_read"),
    "clipboard_write": Rule("ask", ask="clipboard_write"),
    "dbus_call": Rule("ask", ask="dbus"),
    "wayland_list": Rule("free"),
    # The warden's own tools
    "access_request": Rule("free"),
    "access_list": Rule("free"),
}


def rule_for(tool: str) -> Rule | None:
    """The tool's rule, or None: a tool without a rule is refused."""
    return RULES.get(tool)


def covers(held: str, wanted: str) -> bool:
    """Whether a grant at ``held`` covers work that needs ``wanted``."""
    return RANK[held] >= RANK[wanted]
