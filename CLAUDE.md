# kwin-mcp-warden

A fork of isac322/kwin-mcp (MIT): every kwin-mcp tool, with a warden in front of it.
On the live desktop nothing passes without a grant the person gave on a consent card
(UAC-style: front and center on the primary monitor, every screen dimmed, only a click
allows, Allow waits 1 s, Esc or 2 min = Don't Allow). Grants are per window (with its own dialogs and menus) in four levels
(See, Arrange, Control, Full desktop), or a screen of Claude's own; they last the length the
person picks on the card (1, 4 or 8 hours or until the session ends; Full desktop 30 min or 1 hour; ceilings in settings,
`lengths.py`), and end with the session, on Meta+Alt+Esc, or from the access list.

- Spec: `design/mock/index.html` (its "Accessibility" section too), served with
  `python3 -m http.server 8931 --bind 127.0.0.1` from `design/mock`.
- Code: `src/kwin_mcp/warden/` (policy.py is the levels table; gate.py is WardenEngine, the
  engine the server and CLI build; card.py + ui/card.qml the card; daemon.py + ui/overlay.qml
  the stop key, your-hands watcher, frame, tag and access list). The module stays `kwin_mcp`
  so upstream merges stay clean; the remote `upstream` is isac322/kwin-mcp.
- Deferred list: `docs/deferred.md`. Every report starts with it.
- Boundary tests: `tests/warden/run.sh` (a private nested KWin: own bus, Wayland socket, fresh
  HOME and XDG_RUNTIME_DIR, a pipe standing in for the mouse; never the real desktop).
  conftest.py sandboxes the whole test process before any test (no display, bus or home of
  the person; never restored), and run.sh fails if a test session wrote into the real log
  or took down the person's Flatpak document portal (upstream #132), and if anything the run
  started is still running afterwards (every process carries the run's mark,
  `tests/warden/run_processes.py`; what's left is listed and stopped).
  `tests/warden/run.sh --plants` proves each test catches its planted fault (each test must
  first pass alone). Unit: `.venv/bin/python -m pytest tests/unit` (includes
  test_warden_policy.py: every tool in the server's registry has a rule; unplaced tools are
  closed). Card pictures: `tests/warden/render_card.py OUT.png [req.json] --host-theme`.
- Stranger check: `tests/warden/stranger.sh` (a built wheel installed outside the source folder, boundary tests with a fresh HOME). As another Linux user on another distro: `tests/warden/container.sh` (rootless podman, upstream's Debian E2E image plus `docker/warden.Dockerfile`, user `tester`, no network, no host display, bus or input).
- Upstream's release gate: `scripts/run-e2e-docker.sh -- tests/e2e tests/unit` (podman; the Fedora variant's pinned base image isn't on the registry podman resolves `fedora` to). Its first step is upstream's ruff, ruff format and ty, so those must stay clean (`.venv/bin/ruff`, `.venv/bin/ty`, the image's versions: ruff 0.17.5, ty 0.0.104).
- Older Qt: the card and notice must load on Qt 6.8 and layer-shell-qt without `activateOnShow` (Debian 13); newer-only types go through a Loader (`ui/Shadow.qml`).
- Every layer-shell window sits at its own monitor's origin, set again right before it shows (deferred 20): Qt moves a window to the monitor its geometry is on, and layer-shell-qt makes the surface there.
- Dev env: `python3 -m venv --system-site-packages .venv && PYTHONNOUSERSITE=1 .venv/bin/pip install -e .` (without PYTHONNOUSERSITE, pip takes pydantic from ~/.local and the venv breaks under another HOME)
  (PyGObject, dbus-python and Pillow come from the system; uv is not used here).
- Never run tests/e2e or anything that injects input against the real desktop.

---

# Claude Code Working Guidelines

## Project Overview

kwin-mcp is an MCP (Model Context Protocol) server for Linux desktop GUI automation on KDE Plasma 6 Wayland.
**Purpose**: Enable AI agents (Claude Code and other MCP clients) to launch, interact with, and observe any Wayland application — either in a fully isolated virtual KWin session for reproducible GUI testing, or by connecting to a live KDE Plasma desktop (real session or container) for collaborative live desktop automation workflows.

## Toolchain

- **Package Manager**: `uv` (Astral). Always use uv instead of pip/poetry.
- **Lint + Format**: `ruff` (Astral). Config in `pyproject.toml` under `[tool.ruff]`.
- **Type Check**: `ty` (Astral). Config in `pyproject.toml` under `[tool.ty]`.
- **Build**: `uv build` (uv_build backend)

### Common Commands

```bash
uv sync                   # Install/sync dependencies
uv add <pkg>              # Add dependency
uv add --dev <pkg>        # Add dev dependency
uv run ruff check .       # Lint
uv run ruff format .      # Format
uv run ty check           # Type check
uv run python -m kwin_mcp # Run server
```

## Code Style

- Python 3.12+
- ruff rules: E, F, W, I, N, UP, B, A, SIM, TCH, RUF
- Line length: 100
- Quotes: double quote
- Type hints required
- All documents (.md files), code comments, and docstrings must be written in English

## Architecture

Key modules in `src/kwin_mcp/`:
- `models.py`: Shared argument types and result models; one `render()` per result
- `core.py`: `AutomationEngine` — MCP-independent automation logic for all 29 tools
- `server.py`: Thin MCP wrappers delegating to `AutomationEngine`
- `cli.py`: Interactive REPL + pipe mode via `cmd.Cmd` (`kwin-mcp-cli` entry point)
- `session.py`: Virtual sessions (`dbus-run-session` + `kwin_wayland --virtual`) and live session attachment (`session_connect` to real desktop or container)
- `screenshot.py`: KWin ScreenShot2 D-Bus, Spectacle, and X11/scrot capture
- `accessibility.py`: AT-SPI2 widget queries, element refs, and actions
- `spoken_name.py`: Unicode normalization and spoken accessible-name matching
- `geometry.py`: Windows, virtual desktops, activation, and output topology via KWin scripting
- `clipboard.py`: Private Wayland data-control helper for Unicode paste
- `dbus_args.py`: `dbus_call` argument parser (`dbus-send` strings and typed JSON)
- `dbus_service_file.py`: Bounded D-Bus service-file parser and direct AT-SPI exec helper
- `input.py`: KWin EIS D-Bus + libei input injection and held-input tracking

## Verification & Sync Checks

1. After code changes, run `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check src tests`, and `uv run pytest tests/unit`.
2. **Verify with CLI, not MCP tools**: After modifying any kwin-mcp code, verify via the CLI (`uv run python -m kwin_mcp.cli`), NOT the running MCP server process (which still has old code loaded in memory). Use `retention='{"artifacts": true}'` in `session_start` to preserve `/tmp/kwin-mcp-screenshots-*` after `session_stop`, and remove it manually when done.
3. **Plugin and documentation sync**: After changing `pyproject.toml`, `src/kwin_mcp/server.py`, or `integrations/claude-code/skills/kwin-desktop-automation/SKILL.md` (the source of truth — never edit the OpenCode mirror directly), run `python3 scripts/sync_plugin_version.py` and `python3 scripts/check_docs_seo.py`.

## Release Gate

Before tagging a release, verify every kwin-mcp tool end to end on the release commit with the Docker E2E suite (`scripts/run-e2e-docker.sh`, once per `--distro` variant).
