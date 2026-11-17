"""The cards name the agent that asks, and each level's line ends with "Nothing else."

The copy pattern: "*Agent* can see this KCharSelect window, "Unicode", and read its
text. Its own dialogs and menus too. Nothing else." The name is the MCP client's own
(Claude Code says "claude-code"), never a fixed "Claude" for every client.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from kwin_mcp.warden import apps, card, gate
from kwin_mcp.warden.settings import Settings


@pytest.mark.parametrize(
    ("name", "title", "configured", "shown"),
    [
        ("claude-code", "", "", "Claude"),
        ("claude-code", "Claude Code", "", "Claude"),
        ("claude-ai", "", "", "Claude"),
        ("opencode", "", "", "opencode"),
        ("opencode", "OpenCode", "", "OpenCode"),
        ("mcp", "", "", "Claude"),  # the SDK's default: the client didn't say
        ("", "", "", "Claude"),
        ("opencode", "OpenCode", "Helper", "Helper"),
        ("x" * 80, "", "", "x" * 40),
        ("two\nlines", "", "", "two lines"),
    ],
)
def test_the_agent_name(name, title, configured, shown):
    assert gate.agent_name(name, title, configured) == shown


@pytest.fixture
def warden(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    tmp_path.chmod(0o700)
    shown: list[dict] = []
    w = gate.Warden(cast("Any", object()), Settings())
    monkeypatch.setattr(w, "session_name", lambda: "test")
    window = {"id": "w1", "caption": "Unicode", "managed": True}
    monkeypatch.setattr(w, "_window_to_ask_for", lambda app, level: (window, None))
    monkeypatch.setattr(w, "_full_app_id", lambda app: (app, ""))
    monkeypatch.setattr(apps, "describe", lambda app: ("KCharSelect", ""))

    def fake_card(request):
        shown.append(request)
        return card.Outcome("deny", "")

    monkeypatch.setattr(w, "_card", fake_card)
    return w, shown


@pytest.mark.parametrize(
    ("level", "means"),
    [
        ("see", "{a} can see this KCharSelect window, \u201cUnicode\u201d, and read its text."),
        ("arrange", "{a} can move, resize and focus this KCharSelect window, \u201cUnicode\u201d."),
        (
            "control",
            "{a} can click and type in this KCharSelect window, \u201cUnicode\u201d, and "
            "pauses while you use your mouse or keyboard.",
        ),
    ],
)
@pytest.mark.parametrize("client", ["claude-code", "OpenCode"])
def test_each_level_says_who_and_ends_with_nothing_else(warden, level, means, client):
    warden, shown = warden
    warden.set_client(client)
    agent = gate.agent_name(client)
    warden.request(level, "org.kde.kcharselect", "Test the copy.")
    (request,) = shown
    assert request["title"] == f"{agent} asks to {gate.LEVEL_VERBS[level]} a KCharSelect window"
    assert request["means"] == means.format(a=agent)
    assert request["means_more"] == "Its own dialogs and menus too. Nothing else."


def test_a_window_title_is_never_drawn_as_markup(warden, monkeypatch):
    w, shown = warden
    window = {"id": "w1", "caption": "<b>x</b> & y", "managed": True}
    monkeypatch.setattr(w, "_window_to_ask_for", lambda app, level: (window, None))
    w.request("see", "org.kde.kcharselect", "Test the copy.")
    (request,) = shown
    assert "<b>" not in request["means"]


def test_a_warning_names_the_agent(warden):
    warden, _ = warden
    warden.set_client("opencode", "OpenCode")
    assert warden.warning("google-chrome") == (
        "Signed in to your accounts. OpenCode could act as you on any site open in it."
    )
    assert "{agent}" not in warden.warning("firefox")


def test_an_agent_name_is_never_drawn_as_markup(warden):
    warden, shown = warden
    warden.set_client("<b>x</b>")
    warden.request("see", "org.kde.kcharselect", "Test the copy.")
    (request,) = shown
    assert request["means"].startswith("bx/b can see")
    assert "<" not in request["title"]


@pytest.mark.parametrize(
    ("agents", "said"),
    [
        ([], ""),
        (["Claude"], "Claude"),
        (["Claude", "Claude"], "Claude"),
        (["Claude", "OpenCode"], "Claude and OpenCode"),
        (["Claude", "Codex", "OpenCode"], "Claude, Codex and OpenCode"),
    ],
)
def test_the_overlay_names_every_agent_once(agents, said):
    from kwin_mcp.warden import daemon

    assert daemon.names(agents) == said


def test_two_agents_each_get_their_own_name_on_the_overlay(tmp_path, monkeypatch):
    """Two servers, two agents: the rows say whose each grant is, the list and the tray
    name both, and an old record without a name reads as Claude's."""
    import json
    import os
    import time

    from kwin_mcp.warden import daemon, state

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    tmp_path.chmod(0o700)
    monkeypatch.setattr(daemon, "_stack", list)
    monkeypatch.setattr(daemon, "_primary", lambda: None)
    now = time.time()

    def grant(gid, app):
        return {
            "id": gid,
            "kind": "window",
            "app": app,
            "app_name": app,
            "level": "see",
            "reason": "r",
            "given": now,
            "expires": now + 600,
        }

    # Both records under live pids: this test's own, and its parent's.
    mine = {"session": "a", "agent": "OpenCode", "grants": [grant("g1", "Kate")]}
    state.write_grants(os.getpid(), mine)
    state.write_grants(os.getppid(), {"session": "b", "grants": [grant("g2", "Konsole")]})
    st = daemon.overlay_state(Settings())
    assert {r["who"] for r in st["grants"]} == {"OpenCode · a", "Claude · b"}
    assert st["who"] in ("OpenCode and Claude", "Claude and OpenCode")
    assert st["plural"] is True
    assert st["listTitle"] == "Agents' access"
    json.dumps(st)


def test_no_refusal_names_claude():
    """The refusals go back to whatever agent called, so they say "you", never "Claude".

    Read from the source of every warden module: every string inside a ``Refused(...)``,
    every text handed back in a returned tuple (``request``'s answers), and every
    module-level text constant (hints such as ``A11Y_OFF_HINT`` are added to errors)."""
    import ast
    import pkgutil
    from pathlib import Path

    import kwin_mcp.warden as warden_pkg

    said: list[str] = []
    for mod in pkgutil.iter_modules(warden_pkg.__path__):
        tree = ast.parse(Path(warden_pkg.__path__[0], f"{mod.name}.py").read_text())
        for node in ast.walk(tree):
            texts: list[ast.AST] = []
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "Refused":
                texts = [node]
            elif isinstance(node, ast.Return) and isinstance(node.value, ast.Tuple):
                texts = [node.value]
            elif isinstance(node, ast.Assign) and node in tree.body:
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if names and all(
                    n.isupper() and n.endswith(("HINT", "TEXT", "MESSAGE")) for n in names
                ):
                    texts = [node.value]
            said += [
                c.value
                for t in texts
                for c in ast.walk(t)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            ]
    assert said, "found no refusal texts to check"
    assert not [s for s in said if "Claude" in s]
