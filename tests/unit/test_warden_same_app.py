"""App ids name one app (a full id never matches another vendor's app with the same last
part), and a grant covers one window: itself and its own dialogs and menus, never the
same app's other windows."""

from __future__ import annotations

import time

import pytest

from kwin_mcp.warden import grants as grants_mod
from kwin_mcp.warden.settings import same_app


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("org.kde.kate", "org.kde.kate", True),
        ("org.kde.kate", "ORG.KDE.Kate.desktop", True),
        ("org.kde.kate", "kate", True),  # KWin reports Kate's menus as "kate"
        ("kate", "org.kde.kate", True),
        ("org.kde.kate", "com.example.kate", False),
        ("com.example.kate", "org.kde.kate", False),
        ("google-chrome", "google-chrome", True),
        ("google-chrome", "chrome", False),
        ("org.kde.kate", "", False),
        ("", "", False),
        ("org.kde.kate", "x.kate", False),
    ],
)
def test_same_app(a, b, same):
    assert same_app(a, b) is same


def test_a_grant_covers_its_window_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tmp_path.chmod(0o700)
    book = grants_mod.GrantBook("test")
    book.give(
        "window",
        app="org.kde.kate",
        app_name="Kate",
        level="control",
        reason="t",
        minutes=5,
        window="w1",
        window_title="notes.md",
    )
    granted = {"id": "w1", "app": "org.kde.kate"}
    dialog = {"id": "d1", "app": "org.kde.kate", "parent": "w1"}
    sibling = {"id": "w2", "app": "org.kde.kate"}
    assert book.for_window([granted], "control") is not None
    assert book.for_window([granted], "see") is not None  # control includes see
    assert book.for_window([dialog, granted], "control") is not None  # its own dialog
    assert book.for_window([sibling], "control") is None  # the same app's other window
    assert book.for_window([], "control") is None
    assert time.time() < book.alive()[0].expires


def test_a_see_grant_doesnt_cover_control(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tmp_path.chmod(0o700)
    book = grants_mod.GrantBook("test")
    book.give("window", app="org.kde.kate", level="see", reason="t", minutes=5, window="w1")
    assert book.for_window([{"id": "w1"}], "see") is not None
    assert book.for_window([{"id": "w1"}], "control") is None
