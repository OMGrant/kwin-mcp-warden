#!/usr/bin/env bash
# Take kwin-mcp-warden back out: the Claude Code registration, its login entry and
# ~/.local/opt/kwin-mcp-warden.
# Your settings (~/.config/kwin-mcp-warden) and the log (~/.local/state/kwin-mcp-warden) stay;
# delete them yourself if you want them gone.
set -euo pipefail
opt="${KWIN_MCP_WARDEN_PREFIX:-$HOME/.local/opt/kwin-mcp-warden}"
if command -v claude >/dev/null; then
    claude mcp remove --scope user kwin-mcp-warden >/dev/null 2>&1 || true
fi
bin="${XDG_BIN_HOME:-$HOME/.local/bin}"
for cmd in kwin-mcp-warden kwin-mcp-warden-cli; do
    [ "$(readlink "$bin/$cmd" 2>/dev/null)" = "$opt/bin/$cmd" ] && rm -f "$bin/$cmd"
done
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/kwin-mcp-warden-accessibility.desktop"
rm -rf "$opt"
echo "kwin-mcp-warden removed. Settings and log were left in place."
