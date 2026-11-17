"""Render the consent card inside a private virtual KWin and save a picture.

Never touches your desktop: kwin-mcp's own virtual session (its own D-Bus,
its own compositor, no real input devices), started with a runtime folder and a
home of its own. With your real runtime folder, the private desktop's document
portal takes over $XDG_RUNTIME_DIR/doc and yours exits (upstream issue #132; it
happened twice on 2026-11-14 before this script was sandboxed). --host-theme
copies only your kdeglobals into the private home. Usage:
  .venv/bin/python tests/warden/render_card.py OUT.png [request.json]
      [--click allow|deny|go] [--key Escape]
Prints the card's exit code when a click or key was sent.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

# A runtime folder and a home of the script's own, before kwin-mcp reads either.
_sandbox = Path(tempfile.mkdtemp(prefix="kwin-mcp-warden-render-"))
atexit.register(shutil.rmtree, _sandbox, ignore_errors=True)
(_sandbox / "run").mkdir(mode=0o700)
(_sandbox / "home" / ".config").mkdir(parents=True)
_kdeglobals = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "kdeglobals"
if "--host-theme" in sys.argv and _kdeglobals.exists():
    shutil.copy(_kdeglobals, _sandbox / "home" / ".config" / "kdeglobals")
os.environ.update(
    {
        "XDG_RUNTIME_DIR": str(_sandbox / "run"),
        "HOME": str(_sandbox / "home"),
        "XDG_CONFIG_HOME": str(_sandbox / "home" / ".config"),
        "XDG_DATA_HOME": str(_sandbox / "home" / ".local" / "share"),
        "XDG_CACHE_HOME": str(_sandbox / "home" / ".cache"),
        "XDG_STATE_HOME": str(_sandbox / "home" / ".local" / "state"),
    }
)
for _name in ("DBUS_SESSION_BUS_ADDRESS", "WAYLAND_DISPLAY", "DISPLAY"):
    os.environ.pop(_name, None)

from kwin_mcp.core import AutomationEngine  # noqa: E402
from kwin_mcp.models import Point  # noqa: E402
from kwin_mcp.warden import card as cardmod  # noqa: E402

out = Path(sys.argv[1])
req_path = next((a for a in sys.argv[2:] if a.endswith(".json")), None)
req = (
    json.loads(Path(req_path).read_text())
    if req_path
    else {
        "session": "Website Redesign",
        "desktop": "Desktop 4",
        "icon": "google-chrome",
        "title": "Claude asks to control a Google Chrome window",
        "reason": "Test the sign-in page at localhost:3000: type a test address, press Sign "
        "in, and check that the error message reads right.",
        "level": "control",
        "means": "Claude can click and type in this Google Chrome window, \u201cSign in \u2013 "
        "Mail App\u201d, and pauses while you use your mouse or keyboard.",
        "means_more": "Its own dialogs and menus too. Nothing else.",
        "risk": "Google Chrome is signed in to your accounts. Claude could act as you on "
        "any site open in it.",
        "allow": "Allow for 1 Hour",
    }
)
click = sys.argv[sys.argv.index("--click") + 1] if "--click" in sys.argv else None
key = sys.argv[sys.argv.index("--key") + 1] if "--key" in sys.argv else None
wait_s = float(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 2.5
w, h = 1920, 1080
eng = AutomationEngine()
eng.session_start(
    screen_width=w, screen_height=h, home=("host" if "--host-theme" in sys.argv else "isolated")
)
try:
    launch = eng.app_launch(
        command=" ".join(cardmod.command_line(req, quoted=True)),
        env={"QT_FORCE_STDERR_LOGGING": "1"},
    )
    time.sleep(wait_s)
    frame = eng.screen_capture()
    shutil.copy(frame.path, out)
    print("saved", out, frame.width, frame.height)
    print("log:", eng.app_log(pid=launch.pid, last_n_lines=30).render()[-2500:])
    if click or key:
        if click:
            # the card's buttons sit on its bottom row; positions come from the test's own probe
            pts = (
                json.loads(Path(out.with_suffix(".buttons.json")).read_text())
                if out.with_suffix(".buttons.json").exists()
                else None
            )
            p = pts[click] if pts else None
            if p is None:
                sys.exit(f"no button positions in {out.with_suffix('.buttons.json')}")
            eng.pointer_click(target=Point(x=p[0], y=p[1]))
        if key:
            eng.keyboard_press(keys=key)
        time.sleep(1.0)
        print("log:", eng.app_log(pid=launch.pid, last_n_lines=20).render()[-600:])
finally:
    eng.session_stop()
