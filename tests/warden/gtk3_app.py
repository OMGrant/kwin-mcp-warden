"""A made-up GTK 3 window with a one-line entry and a text view, for typing into fields.

``gtk3_app.py --dialog`` also opens a dialog over it ("GTK 3 dialog", transient for the
window), for the warden's test that a window's grant covers its own dialogs.

GTK 3 reaches AT-SPI through ATK and at-spi2-atk, not GTK's own AT-SPI code (GTK 4), so
it gets a test of its own. Its AT-SPI name and its Wayland app id (GTK 3 sends the
program name) are both ``gtk3-fields``.
"""

import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

GLib.set_prgname("gtk3-fields")
GLib.set_application_name("gtk3-fields")
app = Gtk.Application(
    application_id="org.example.Gtk3Fields", flags=Gio.ApplicationFlags.NON_UNIQUE
)


def _activate(application):
    window = Gtk.ApplicationWindow(application=application, title="GTK 3 fields")
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.pack_start(Gtk.Entry(), False, False, 0)
    view = Gtk.TextView()
    view.set_size_request(300, 120)
    box.pack_start(view, True, True, 0)
    window.add(box)
    window.set_default_size(360, 220)
    window.show_all()
    if "--dialog" in sys.argv[1:]:
        dialog = Gtk.Window(title="GTK 3 dialog", transient_for=window)
        dialog.add(Gtk.Button(label="OK"))
        dialog.set_default_size(240, 120)
        dialog.show_all()


app.connect("activate", _activate)
sys.exit(app.run([sys.argv[0]]))
