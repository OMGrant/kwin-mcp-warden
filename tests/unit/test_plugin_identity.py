"""The plugin manifests and integrations install the fork's gated server, never upstream's.

Every name below is derived from ``pyproject.toml`` (the distribution name and its
console script), so a renamed fork or a new manifest field is checked without editing a
list here. The MCP server command must be the bare console script: no ``uvx``, no
upstream ``kwin-mcp`` from PyPI.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
NAME: str = PYPROJECT["name"]
SERVER_COMMAND: str = next(s for s in PYPROJECT["scripts"] if s == NAME)
OPENCODE_NAME = f"{NAME}-opencode"

MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN = REPO_ROOT / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
MCP_JSON = REPO_ROOT / "integrations" / "claude-code" / ".mcp.json"
OPENCODE_DIR = REPO_ROOT / "integrations" / "opencode"
OPENCODE_PACKAGE = OPENCODE_DIR / "plugin" / "package.json"
OPENCODE_EXAMPLE = OPENCODE_DIR / "opencode.json.example"
OPENCODE_SOURCE = OPENCODE_DIR / "plugin" / "src" / "index.ts"
OPENCODE_TESTS = sorted((OPENCODE_DIR / "plugin" / "test").glob("*.mjs"))
OPENCODE_README = OPENCODE_DIR / "plugin" / "README.md"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_code_manifests_name_the_fork() -> None:
    marketplace = _json(MARKETPLACE)
    assert marketplace["name"] == NAME
    assert [p["name"] for p in marketplace["plugins"]] == [NAME]
    assert _json(PLUGIN)["name"] == NAME


def test_claude_code_plugin_starts_the_gated_server() -> None:
    servers = _json(MCP_JSON)["mcpServers"]
    assert servers == {NAME: {"command": SERVER_COMMAND}}


def test_opencode_package_and_example_name_the_fork() -> None:
    assert _json(OPENCODE_PACKAGE)["name"] == OPENCODE_NAME
    assert _json(OPENCODE_EXAMPLE)["plugins"] == [OPENCODE_NAME]


def test_opencode_plugin_registers_the_gated_server() -> None:
    source = OPENCODE_SOURCE.read_text(encoding="utf-8")
    assert f'MCP_NAME = "{NAME}"' in source
    assert f'command: ["{SERVER_COMMAND}"]' in source
    assert f'id: "{OPENCODE_NAME}"' in source


def test_opencode_tests_expect_the_fork_names() -> None:
    assert OPENCODE_TESTS
    for path in OPENCODE_TESTS:
        text = path.read_text(encoding="utf-8")
        assert f'"{OPENCODE_NAME}"' in text, path.name
        assert "@isac322" not in text, path.name


def test_nothing_launches_upstream_kwin_mcp() -> None:
    launches_upstream = re.compile(
        r"\buvx\b[\s\",\]]*kwin-mcp\b(?!-)|\"command\"\s*:\s*\[?\s*\"uvx\""
    )
    for path in (
        MARKETPLACE,
        PLUGIN,
        MCP_JSON,
        OPENCODE_PACKAGE,
        OPENCODE_EXAMPLE,
        OPENCODE_SOURCE,
    ):
        assert not launches_upstream.search(path.read_text(encoding="utf-8")), path.name
    assert "uvx kwin-mcp" not in OPENCODE_README.read_text(encoding="utf-8")


def test_descriptions_say_what_the_warden_is() -> None:
    for text in (
        _json(PLUGIN)["description"],
        _json(MARKETPLACE)["plugins"][0]["description"],
        _json(OPENCODE_PACKAGE)["description"],
    ):
        assert "consent card" in text and "Meta+Alt+Esc" in text
