#!/usr/bin/env bash
# The stranger check: build a wheel, install it into a fresh venv outside the source
# folder, and run the boundary tests against the installed copy (fresh HOME, config and
# runtime folder come from the tests' own nested KWin). Usage: tests/warden/stranger.sh
set -eu
src="$(cd "$(dirname "$0")/../.." && pwd)"
work="$(mktemp -d "${TMPDIR:-/tmp}/kwin-mcp-warden-stranger-XXXXXX")"
trap 'rm -rf "$work"' EXIT
# PyGObject, dbus-python and Pillow come from the system; nothing from the
# person's own ~/.local packages (PYTHONNOUSERSITE), which a stranger wouldn't have.
export PYTHONNOUSERSITE=1
python3 -m venv --system-site-packages "$work/venv"
"$work/venv/bin/pip" wheel -q --no-deps -w "$work/wheel" "$src"
"$work/venv/bin/pip" install -q "$work"/wheel/*.whl pytest pytest-timeout
cp "$src/tests/warden/test_boundaries.py" "$src/tests/warden/conftest.py" \
   "$src/tests/warden/fake_tray_host.py" "$src/tests/warden/run_processes.py" \
   "$src/tests/warden/dbus_app.py" "$src/tests/warden/gtk3_app.py" "$work/"
cd "$work"
echo "installed at: $("$work/venv/bin/python" -c 'import kwin_mcp, os; print(os.path.dirname(kwin_mcp.__file__))')"
QT_FORCE_STDERR_LOGGING=1 timeout 900 "$work/venv/bin/python" -m pytest test_boundaries.py -q -p no:cacheprovider
