"""A made-up GTK 4 app (a GApplication), for the warden's open-and-control tests.

``dbus_app.py ID``: opens a window, or, when an instance of ID already runs, asks that
instance to (a single-instance app: the window comes from the other process).
``dbus_app.py ID --service``: an instance that opens no window until it's activated over
D-Bus. The window's app id is the application id.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402

service = "--service" in sys.argv[2:]
flags = Gio.ApplicationFlags.IS_SERVICE if service else Gio.ApplicationFlags.DEFAULT_FLAGS
app = Gtk.Application(application_id=sys.argv[1], flags=flags)
if service:
    app.set_inactivity_timeout(60_000)


def _activate(application):
    window = Gtk.ApplicationWindow(application=application, title="Activated")
    window.set_default_size(320, 200)
    window.present()


app.connect("activate", _activate)
sys.exit(app.run([sys.argv[0]]))
