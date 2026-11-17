"""A stand-in for Plasma's system tray on the test's private bus (never the real one).

Owns org.kde.StatusNotifierWatcher, says a tray host is there, and writes each item that
registers (its bus name and object path) to the file named on the command line, one JSON
line each. The boundary tests use it to reach the warden's tray icon the way the panel
does: through the item's org.kde.StatusNotifierItem interface.
"""

from __future__ import annotations

import json
import os
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

WATCHER = "org.kde.StatusNotifierWatcher"


class Watcher(dbus.service.Object):
    def __init__(self, bus: dbus.Bus, out: str) -> None:
        super().__init__(bus, "/StatusNotifierWatcher")
        self.out = out
        self.items: list[str] = []

    @dbus.service.method(WATCHER, in_signature="s", sender_keyword="sender")
    def RegisterStatusNotifierItem(self, service: str, sender: str = "") -> None:  # noqa: N802
        name, path = (
            (sender, service) if service.startswith("/") else (service, "/StatusNotifierItem")
        )
        self.items.append(f"{name}{path}")
        with open(self.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": name, "path": path}) + "\n")
        self.StatusNotifierItemRegistered(f"{name}{path}")

    @dbus.service.method(WATCHER, in_signature="s")
    def RegisterStatusNotifierHost(self, service: str) -> None:  # noqa: N802
        pass

    @dbus.service.signal(WATCHER, signature="s")
    def StatusNotifierItemRegistered(self, service: str) -> None:  # noqa: N802
        pass

    @dbus.service.signal(WATCHER, signature="")
    def StatusNotifierHostRegistered(self) -> None:  # noqa: N802
        pass

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface: str, prop: str):  # noqa: N802
        return self.GetAll(interface)[prop]

    @dbus.service.method(dbus.PROPERTIES_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str):  # noqa: N802
        return {
            "RegisteredStatusNotifierItems": dbus.Array(self.items, signature="s"),
            "IsStatusNotifierHostRegistered": True,
            "ProtocolVersion": dbus.Int32(0),
        }


def main() -> None:
    out = sys.argv[1]
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    if (
        "kwin-mcp-warden-test-" not in out
        or not address
        or f"/run/user/{os.getuid()}/bus" in address
    ):
        sys.exit("fake_tray_host runs only on the warden test's private bus")
    DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    keep = dbus.service.BusName(WATCHER, bus)
    Watcher(bus, out)
    print("ready", flush=True)
    GLib.MainLoop().run()
    del keep


if __name__ == "__main__":
    main()
