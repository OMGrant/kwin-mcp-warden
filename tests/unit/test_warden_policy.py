"""kwin-mcp-warden: every tool is placed in the levels table, and nothing unplaced runs.

The tool names come from the server's own registry and the engine's own methods,
never from a list typed here, so a new upstream tool fails this until it is placed.
"""

from __future__ import annotations

import pytest

from kwin_mcp import server
from kwin_mcp.core import AutomationEngine
from kwin_mcp.warden import policy
from kwin_mcp.warden.gate import Refused, WardenEngine


def _registry_names() -> list[str]:
    return [getattr(spec.body, "__name__", "") for spec in server._TOOL_SPECS]


def test_every_server_tool_has_a_rule() -> None:
    missing = [name for name in _registry_names() if policy.rule_for(name) is None]
    assert not missing, f"tools with no rule in warden/policy.py: {missing}"


def test_every_engine_tool_goes_through_the_warden() -> None:
    public = [
        n
        for n in dir(AutomationEngine)
        if not n.startswith("_") and callable(getattr(AutomationEngine, n))
    ]
    for name in public:
        assert hasattr(getattr(WardenEngine, name), "__warden_rule__"), (
            f"{name} bypasses the warden"
        )
    # window_close's MCP body calls _close directly; that path is the warden's too.
    assert WardenEngine._close is not AutomationEngine._close


def test_a_tool_without_a_rule_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planted: a new upstream tool the table doesn't know is closed, not open."""
    import importlib

    from kwin_mcp.warden import gate

    monkeypatch.setattr(AutomationEngine, "brand_new_tool", lambda self: "ran", raising=False)
    monkeypatch.delitem(policy.RULES, "wayland_list")
    reloaded = importlib.reload(gate)
    try:
        engine = reloaded.WardenEngine.__new__(reloaded.WardenEngine)
        with pytest.raises(reloaded.Refused, match="stays closed"):
            engine.brand_new_tool()
        with pytest.raises(reloaded.Refused, match="stays closed"):
            engine.wayland_list()
    finally:
        monkeypatch.undo()
        importlib.reload(gate)


def test_the_server_and_cli_build_the_warden() -> None:
    from kwin_mcp.cli import KwinMcpShell

    assert isinstance(server.build_server().engine, WardenEngine)
    assert isinstance(KwinMcpShell().engine, WardenEngine)


def test_refused_is_a_runtime_error() -> None:
    # The server and CLI turn RuntimeError into a tool error with its text.
    assert issubclass(Refused, RuntimeError)
