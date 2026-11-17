// kwin-mcp-warden's stack watcher: a KWin script loaded once by the warden's daemon while
// Claude holds a grant. It sends the window stack to the daemon whenever it changes, so the
// orange frame follows a window without the daemon asking KWin several times a second.
// It only reports; the warden's own checks always read the stack fresh.
// The daemon replaces __SERVICE__ before loading it.

function windowInfo(w) {
    var current = workspace.currentDesktop;
    if (workspace.currentDesktopForScreen && w.output) {
        current = workspace.currentDesktopForScreen(w.output) || current;
    }
    var onDesktop = w.onAllDesktops || w.desktops.length === 0;
    for (var k = 0; k < w.desktops.length && !onDesktop; k++) {
        if (w.desktops[k] === current) {
            onDesktop = true;
        }
    }
    var f = w.frameGeometry;
    return {
        id: String(w.internalId), pid: w.pid, app: String(w.resourceClass),
        visible: onDesktop && !w.minimized && !w.hidden,
        active: w === workspace.activeWindow,
        managed: w.managed, popup: w.popupWindow,
        frame: [Math.round(f.x), Math.round(f.y), Math.round(f.width), Math.round(f.height)]
    };
}

function send() {
    var out = [];
    var st = workspace.stackingOrder;
    for (var i = 0; i < st.length; i++) {
        if (!st[i].deleted) {
            out.push(windowInfo(st[i]));
        }
    }
    callDBus("__SERVICE__", "/Stack", "__SERVICE__", "Report", JSON.stringify(out));
}

function watch(w) {
    w.frameGeometryChanged.connect(send);
    w.minimizedChanged.connect(send);
    w.desktopsChanged.connect(send);
}

var all = workspace.windowList();
for (var i = 0; i < all.length; i++) {
    watch(all[i]);
}
workspace.windowAdded.connect(function (w) { watch(w); send(); });
workspace.windowRemoved.connect(send);
workspace.windowActivated.connect(send);
workspace.currentDesktopChanged.connect(send);
if (workspace.stackingOrderChanged) {
    workspace.stackingOrderChanged.connect(send);
}
send();
