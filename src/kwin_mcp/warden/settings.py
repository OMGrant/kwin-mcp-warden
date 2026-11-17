"""The warden's settings: a plain TOML file you can edit, with the design's defaults.

File: ``$XDG_CONFIG_HOME/kwin-mcp-warden/settings.toml`` (``~/.config`` when unset).
Every key is optional; a missing or broken file gives the defaults below, and a
broken file is reported in the log rather than silently ignored.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

# Apps with a warning on their card (mock, "Apps with a warning on their card").
# Matched against the window's app id, case-insensitive, after dropping a
# reverse-DNS prefix ("org.kde.konsole" -> "konsole").
DEFAULT_WARNINGS: dict[str, str] = {
    "konsole": "Same as running commands on this computer.",
    "yakuake": "Same as running commands on this computer.",
    "kitty": "Same as running commands on this computer.",
    "alacritty": "Same as running commands on this computer.",
    "foot": "Same as running commands on this computer.",
    "wezterm": "Same as running commands on this computer.",
    "ptyxis": "Same as running commands on this computer.",
    "gnome-terminal": "Same as running commands on this computer.",
    "kate": "Same as running commands on this computer.",
    "kwrite": "Same as running commands on this computer.",
    "code": "Same as running commands on this computer.",
    "code-oss": "Same as running commands on this computer.",
    "codium": "Same as running commands on this computer.",
    "zed": "Same as running commands on this computer.",
    "dolphin": "Can open, move and delete any of your files.",
    "systemsettings": "Can change how this computer is set up.",
    "google-chrome": "Signed in to your accounts. {agent} could act as you on any site open in it.",
    "chromium": "Signed in to your accounts. {agent} could act as you on any site open in it.",
    "chromium-browser": "Signed in to your accounts. {agent} could act as you on any site "
    "open in it.",
    "firefox": "Signed in to your accounts. {agent} could act as you on any site open in it.",
}

# Never granted (mock, "Never granted"): password prompts and the lock screen,
# matched like the warnings. The warden's own windows are never granted by
# process, whatever their app id.
DEFAULT_NEVER: tuple[str, ...] = (
    "polkit-kde-authentication-agent-1",
    "polkit-kde-manager",
    "kscreenlocker_greet",
    "kwalletd5",
    "kwalletd6",
    "ksecretd",
    "kwallet-query",
    "pinentry",
    "pinentry-qt",
    "pinentry-qt5",
    "pinentry-gnome3",
    "pinentry-gtk",
    "pinentry-gtk-2",
    "org.kde.polkit-kde-authentication-agent-1",
    "ksshaskpass",
    "org.kde.ksshaskpass",
    "kdesu",
    "kdesudo",
    "gcr-prompter",
    "polkit-gnome-authentication-agent-1",
    "lxqt-policykit-agent",
    "mate-polkit",
    "polkit-mate-authentication-agent-1",
    "ssh-askpass",
    "x11-ssh-askpass",
    "kwin-mcp-warden",
)

# D-Bus services dbus_call may never reach, whatever you answer: the warden's
# own way in, KWin's effect and script loaders (which could switch the warden's
# view off), the lock screen and the password stores.
DEFAULT_NEVER_DBUS: tuple[str, ...] = (
    "org.kde.kwin_mcp_warden",
    "org.kde.screensaver",
    "org.freedesktop.ScreenSaver",
    "org.kde.kwalletd5",
    "org.kde.kwalletd6",
    "org.kde.ksecretd",
    "org.freedesktop.secrets",
    "org.freedesktop.PolicyKit1",
    "org.kde.kglobalaccel",
    "org.kde.KWin.Script",
)

# Object paths dbus_call may never reach on any service. In Plasma 6 the shortcut service
# and the screen saver live inside KWin's own connection, so a service check alone can be
# got around through org.kde.KWin.
DEFAULT_NEVER_DBUS_PATHS: tuple[str, ...] = (
    "/kglobalaccel",
    "/component",
    "/ScreenSaver",
    "/org/freedesktop/ScreenSaver",
    "/Scripting",
    "/Effects",
    "/Plugins",
    "/modules/kwalletd5",
    "/modules/kwalletd6",
    "/org/freedesktop/secrets",
    "/org/freedesktop/PolicyKit1",
)


@dataclass
class Settings:
    """Every setting the warden reads; field defaults are the design's."""

    # How long a grant lasts: the length selected on the card when the agent doesn't
    # ask for one, and the longest the card offers (0 = until the session ends, and the
    # card offers "This session"). The person picks any offered length on the card.
    grant_minutes: int = 60  # "Allow for 1 Hour"
    max_grant_minutes: int = 480  # a window or a screen of its own: up to 8 hours
    full_desktop_minutes: int = 30
    max_full_desktop_minutes: int = 60  # the whole desktop: up to 1 hour
    own_screen_minutes: int = 60
    answer_seconds: int = 120  # unanswered after 2 minutes = Don't Allow
    allow_delay_ms: int = 1000  # Firefox's security.dialog_enable_delay
    pause_seconds: float = 3.0  # Claude continues after 3 s without your input
    pause_limit_seconds: float = 120.0  # then gives up and tells Claude you're busy
    # Free on stock Plasma; Meta+Esc is System Monitor's.
    stop_key: str = "Meta+Alt+Esc"
    dim: float = 0.6
    # The name cards give the agent; empty takes the MCP client's own ("Claude" for
    # Claude Code). Warnings may say {agent} for it.
    agent_name: str = ""
    warnings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_WARNINGS))
    never: list[str] = field(default_factory=lambda: list(DEFAULT_NEVER))
    never_dbus: list[str] = field(default_factory=lambda: list(DEFAULT_NEVER_DBUS))
    never_dbus_paths: list[str] = field(default_factory=lambda: list(DEFAULT_NEVER_DBUS_PATHS))
    problem: str = ""  # why the file was not read, for the log


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "kwin-mcp-warden" / "settings.toml"


def load(path: Path | None = None) -> Settings:
    """Read the settings file over the defaults; never raises."""
    settings = Settings()
    path = path or config_path()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return settings
    except (OSError, tomllib.TOMLDecodeError) as exc:
        settings.problem = f"settings file {path} not read: {exc}"
        return settings
    known = {f.name: f for f in fields(Settings) if f.name != "problem"}
    bad: list[str] = []
    for key, value in data.items():
        if key not in known:
            bad.append(f"unknown key {key!r}")
            continue
        current = getattr(settings, key)
        if key == "warnings" and isinstance(value, dict):
            merged = dict(current)
            merged.update({str(k).lower(): str(v) for k, v in value.items()})
            settings.warnings = merged
        elif key in ("never", "never_dbus", "never_dbus_paths") and isinstance(value, list):
            # Your additions join the defaults; the defaults can't be removed here.
            setattr(settings, key, list(dict.fromkeys([*current, *map(str, value)])))
        elif isinstance(current, bool) or not isinstance(value, type(current)):
            if isinstance(current, float) and isinstance(value, int):
                setattr(settings, key, float(value))
            else:
                bad.append(f"{key} should be {type(current).__name__}")
        else:
            setattr(settings, key, value)
    # Bounds: the file can make the warden stricter, never looser than the design; the
    # longest grant is the person's own choice (0 = until the session ends).
    year = 365 * 24 * 60
    limits = {
        "max_grant_minutes": (0, year),
        "max_full_desktop_minutes": (0, year),
        "grant_minutes": (1, year),
        "full_desktop_minutes": (1, year),
        "own_screen_minutes": (1, year),
        "answer_seconds": (10, 280),
        "allow_delay_ms": (1000, 10_000),
        "pause_seconds": (1.0, 60.0),
        "pause_limit_seconds": (5.0, 3600.0),
        "dim": (0.3, 0.9),
    }
    for key, (low, high) in limits.items():
        value = getattr(settings, key)
        clamped = min(max(value, low), high)
        if clamped != value:
            bad.append(f"{key} {value} is outside {low}..{high}, using {clamped}")
            setattr(settings, key, type(value)(clamped))
    # A default length past its ceiling comes down to the ceiling.
    for key, ceiling in (
        ("grant_minutes", settings.max_grant_minutes),
        ("own_screen_minutes", settings.max_grant_minutes),
        ("full_desktop_minutes", settings.max_full_desktop_minutes),
    ):
        if ceiling and getattr(settings, key) > ceiling:
            bad.append(f"{key} {getattr(settings, key)} is past its ceiling, using {ceiling}")
            setattr(settings, key, ceiling)
    if bad:
        settings.problem = f"settings file {path}: " + "; ".join(bad)
    return settings


def short_app(app_id: str) -> str:
    """``org.kde.konsole`` -> ``konsole``; ``google-chrome`` stays; lower case."""
    app = app_id.strip().lower()
    if app.endswith(".desktop"):
        app = app[: -len(".desktop")]
    parts = app.split(".")
    if len(parts) >= 3 and parts[0] in ("org", "com", "io", "net", "dev", "app"):
        return parts[-1]
    return app


_DNS_ROOTS = ("org", "com", "io", "net", "dev", "app")


def _reverse_dns(app: str) -> bool:
    parts = app.split(".")
    return len(parts) >= 3 and parts[0] in _DNS_ROOTS


def same_app(a: str, b: str) -> bool:
    """Whether two app ids name one app, for grants.

    Equal ids match (case and ``.desktop`` aside), and so do a full reverse-DNS id and a
    bare name equal to its last part (``org.kde.kate`` and ``kate``: KWin reports Kate's
    menus as ``kate``). Two different full ids never match, even when they end the same
    way (``com.example.kate`` is not ``org.kde.kate``).
    """
    a, b = a.strip().lower().removesuffix(".desktop"), b.strip().lower().removesuffix(".desktop")
    if not a or not b:
        return False
    if a == b:
        return True
    if _reverse_dns(a) and _reverse_dns(b):
        return False
    if _reverse_dns(a):
        return "." not in b and a.rsplit(".", 1)[-1] == b
    if _reverse_dns(b):
        return "." not in a and b.rsplit(".", 1)[-1] == a
    return False


def app_matches(app_id: str, names: list[str] | tuple[str, ...]) -> bool:
    wanted = {n.lower() for n in names}
    return app_id.lower() in wanted or short_app(app_id) in wanted
