"""An app's name and icon, from its desktop file, for the card."""

from __future__ import annotations

import configparser
import os
from functools import lru_cache
from pathlib import Path

from kwin_mcp.warden.settings import short_app


def _data_dirs() -> list[Path]:
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local/share")
    dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    out = [Path(home)] + [Path(d) for d in dirs.split(":") if d]
    out.append(Path("/var/lib/flatpak/exports/share"))
    out.append(Path(os.path.expanduser("~/.local/share/flatpak/exports/share")))
    return out


def _find(app_id: str) -> tuple[str, configparser.SectionProxy] | None:
    """The app's desktop file id and its [Desktop Entry] group, or None."""
    candidates = [app_id, app_id.lower(), short_app(app_id)]
    for base in _data_dirs():
        folder = base / "applications"
        for cand in candidates:
            path = folder / f"{cand}.desktop"
            if not path.is_file():
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                return cand, parser["Desktop Entry"]
            except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
                continue
    return None


def _entry(app_id: str) -> configparser.SectionProxy | None:
    """The app's desktop entry (its [Desktop Entry] group), or None."""
    found = _find(app_id)
    return found[1] if found else None


@lru_cache(maxsize=128)
def describe(app_id: str) -> tuple[str, str]:
    """(display name, icon name) for an app id; falls back to the id itself."""
    entry = _entry(app_id)
    if entry is None:
        return app_id, app_id
    return entry.get("Name", app_id), entry.get("Icon", app_id)


# The Desktop Entry Specification's field codes ("The Exec key"): an app launcher fills
# them with files or URLs to open; opening the app alone drops them. %% is a plain %.
_FIELD_CODES = {"%f", "%F", "%u", "%U", "%d", "%D", "%n", "%N", "%i", "%c", "%k", "%v", "%m"}


def launch_command(app_id: str) -> list[str] | None:
    """How the app launcher opens the app; None when it can't be opened.

    An entry with ``DBusActivatable=true`` is opened the way the Desktop Entry
    Specification says and KDE's launcher does (KIO ``DBusActivationRunner``): a call to
    ``org.freedesktop.Application.Activate`` on the bus name that is its desktop file id,
    which needs at least two dots. KIO also passes an ``activation-token`` on Wayland so
    the new window takes focus; the warden has none to pass and sends no platform data,
    so the window may open without focus (the grant waits only for it to exist).
    Otherwise its Exec key, without the field codes, when the program is installed.
    """
    import shlex
    import shutil

    found = _find(app_id)
    if found is None:
        return None
    desktop_id, entry = found
    if entry.get("Type", "Application") != "Application":
        return None
    activatable = entry.get("DBusActivatable", "false").strip().lower() == "true"
    if activatable and desktop_id.count(".") >= 2 and shutil.which("gdbus"):
        path = "/" + desktop_id.replace(".", "/").replace("-", "_")
        return [
            "gdbus",
            "call",
            "--session",
            "--dest",
            desktop_id,
            "--object-path",
            path,
            "--method",
            "org.freedesktop.Application.Activate",
            "@a{sv} {}",
        ]
    try:
        argv = shlex.split(entry.get("Exec", ""))
    except ValueError:
        return None
    argv = [a.replace("%%", "%") for a in argv if a not in _FIELD_CODES]
    if not argv or shutil.which(argv[0]) is None:
        return None
    return argv
