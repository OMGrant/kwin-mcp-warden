#!/usr/bin/env bash
# Install kwin-mcp-warden for this user and register it with Claude Code.
#
# 1. Builds a wheel from this folder and installs it into its own venv in
#    ~/.local/opt/kwin-mcp-warden (PyGObject, dbus-python and Pillow come from the
#    system; nothing from ~/.local's own Python packages).
# 2. Registers the MCP server "kwin-mcp-warden" with Claude Code at user scope, so
#    every Claude session can use it. Claude still needs a card answered for anything
#    on your desktop.
# Run it again to update. uninstall.sh takes it all back out.
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)"
opt="${KWIN_MCP_WARDEN_PREFIX:-$HOME/.local/opt/kwin-mcp-warden}"
export PYTHONNOUSERSITE=1 PIP_DISABLE_PIP_VERSION_CHECK=1

say() { printf '%s\n' "$*"; }
need() { command -v "$1" >/dev/null || { say "kwin-mcp-warden needs $1: $2"; exit 1; }; }
need python3 "Python 3.12 or newer"
need kwin_wayland "KDE Plasma 6 on Wayland"
python3 -c 'import gi, dbus, PIL' 2>/dev/null || {
    say "kwin-mcp-warden needs PyGObject, dbus-python and Pillow from your system's packages"
    say "(Fedora: python3-gobject python3-dbus python3-pillow)."; exit 1; }
for qml in /usr/lib64/qt6/bin/qml /usr/lib/qt6/bin/qml /usr/lib/x86_64-linux-gnu/qt6/bin/qml \
           /usr/lib/aarch64-linux-gnu/qt6/bin/qml /usr/bin/qml6 /usr/bin/qml; do
    [ -x "$qml" ] && break
done
[ -x "$qml" ] || { say "kwin-mcp-warden needs Qt's qml program (qt6-qtdeclarative) for its consent card."; exit 1; }
id -nG | tr ' ' '\n' | grep -qx input || say "Note: you're not in the input group, so the warden can't see your mouse and keyboard; Claude won't be able to arrange or control windows on your screen."

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
say "Building kwin-mcp-warden"
python3 -m venv "$work/build"
"$work/build/bin/pip" -q wheel --no-deps -w "$work/wheel" "$src"
say "Installing into $opt"
rm -rf "$opt.new"
python3 -m venv --system-site-packages "$opt.new"
"$opt.new/bin/pip" -q install "$work"/wheel/*.whl
rm -rf "$opt.old"
[ -d "$opt" ] && mv "$opt" "$opt.old"
mv "$opt.new" "$opt"
rm -rf "$opt.old"
# The venv's scripts name their interpreter by path, so rewrite them for the final folder.
sed -i "1s|^#!.*/bin/python3\?$|#!$opt/bin/python3|" "$opt"/bin/kwin-mcp-warden "$opt"/bin/kwin-mcp-warden-cli
"$opt/bin/kwin-mcp-warden" --help >/dev/null
# On PATH too, for the Claude Code and OpenCode plugins (integrations/), which start
# the command by name. A file there that isn't ours is left alone.
bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
mkdir -p "$bin"
for cmd in kwin-mcp-warden kwin-mcp-warden-cli; do
    if [ -e "$bin/$cmd" ] && [ "$(readlink "$bin/$cmd")" != "$opt/bin/$cmd" ]; then
        say "Left $bin/$cmd alone: it isn't kwin-mcp-warden's."
    else
        ln -sfn "$opt/bin/$cmd" "$bin/$cmd"
    fi
done
# At login: if the warden turned accessibility on and a crash or a reboot kept it from
# turning it back off (the desktop saves the switch), it goes back off here.
autostart="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
mkdir -p "$autostart"
cat >"$autostart/kwin-mcp-warden-accessibility.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=kwin-mcp-warden: accessibility back off
Comment=Turns accessibility back off at login if kwin-mcp-warden turned it on and couldn't turn it off
Exec=$opt/bin/python3 -m kwin_mcp.warden.a11y --after-login
NoDisplay=true
X-KDE-autostart-phase=2
EOF

# A running daemon keeps drawing the old overlay until every session has ended: stop it
# (checked by its command line first), and the next warden call, or a session's keeper
# within two seconds, starts the new one.
pidfile="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/kwin-mcp-warden/daemon.pid"
if [ -r "$pidfile" ]; then
    pid=$(cat "$pidfile")
    case "$pid" in
    '' | *[!0-9]*) ;;
    *)
        if tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null | grep -q -- "-m kwin_mcp.warden.daemon"; then
            kill -INT "$pid" && say "Restarting the running overlay on the new version"
        fi
        ;;
    esac
fi

if command -v claude >/dev/null; then
    say "Registering the MCP server with Claude Code (user scope)"
    claude mcp remove --scope user kwin-mcp-warden >/dev/null 2>&1 || true
    claude mcp add --scope user kwin-mcp-warden -- "$opt/bin/kwin-mcp-warden" >/dev/null
else
    say "Claude Code isn't installed; register $opt/bin/kwin-mcp-warden as an MCP server yourself."
fi
say "Done. New Claude sessions get kwin-mcp-warden; the stop key is Meta+Alt+Esc."
