"""kwin-mcp-warden-cli passes reason="..." to the warden, as the MCP tools do."""

from __future__ import annotations

from types import SimpleNamespace

from kwin_mcp.cli import KwinMcpShell
from kwin_mcp.warden.gate import WardenEngine


def test_the_cli_hands_the_reason_to_the_warden(monkeypatch):
    engine = WardenEngine()
    noted: list[tuple[str, str]] = []
    called: list[dict] = []
    monkeypatch.setattr(engine, "_note_reason", lambda tool, reason: noted.append((tool, reason)))
    shell = KwinMcpShell(engine=engine)

    def clipboard_read(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(render=lambda: "ok")

    shell._commands["clipboard_read"] = clipboard_read
    shell.default('clipboard_read reason="Copy the tracking link"')
    assert noted == [("clipboard_read", "Copy the tracking link")]
    assert called == [{}]
