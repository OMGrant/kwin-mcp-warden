"""How the warden opens an app the card named: as KDE's launcher does.

DBusActivatable entries go through org.freedesktop.Application.Activate on the bus name
that is their desktop file id (Desktop Entry Specification; KIO DBusActivationRunner,
which also wants at least two dots); every other entry through its Exec key.
"""

from __future__ import annotations

import shutil

import pytest

from kwin_mcp.warden import apps


@pytest.fixture
def entries(tmp_path, monkeypatch):
    folder = tmp_path / "applications"
    folder.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "none"))
    monkeypatch.setattr(apps, "_data_dirs", lambda: [tmp_path])

    def write(desktop_id: str, body: str) -> None:
        (folder / f"{desktop_id}.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=X\n" + body, encoding="utf-8"
        )

    return write


@pytest.mark.skipif(shutil.which("gdbus") is None, reason="needs gdbus")
def test_an_activatable_entry_is_activated_over_dbus(entries):
    entries("org.example.Some-App", "DBusActivatable=true\nExec=true %U\n")
    assert apps.launch_command("org.example.Some-App") == [
        "gdbus",
        "call",
        "--session",
        "--dest",
        "org.example.Some-App",
        "--object-path",
        "/org/example/Some_App",
        "--method",
        "org.freedesktop.Application.Activate",
        "@a{sv} {}",
    ]


def test_an_activatable_entry_without_a_wellformed_name_uses_exec(entries):
    entries("someapp", "DBusActivatable=true\nExec=true %U\n")
    assert apps.launch_command("someapp") == ["true"]


def test_an_ordinary_entry_uses_exec_without_field_codes(entries):
    entries("org.example.plain", "Exec=true --flag %F\n")
    assert apps.launch_command("org.example.plain") == ["true", "--flag"]


def test_no_exec_and_not_activatable_cant_be_opened(entries):
    entries("org.example.empty", "")
    assert apps.launch_command("org.example.empty") is None
