#!/usr/bin/env bash
# kwin-mcp-warden's boundary tests, inside a private nested KWin (see test_boundaries.py).
#   tests/warden/run.sh [pytest args]     run every boundary test
#   tests/warden/run.sh --plants [NAME...] prove each test catches its planted fault (or only those)
set -u
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
export PYTHONNOUSERSITE=1  # only what the venv and the system provide
export QT_FORCE_STDERR_LOGGING=1
# Guard, checked from outside after the run: no test session may write into the person's
# real warden log, and no real warden folder may appear (conftest.py sandboxes the
# environment and runs from a folder named kwin-mcp-warden-test-*, which marks any
# line a test session would log). Real sessions may keep logging meanwhile.
real_log="$HOME/.local/state/kwin-mcp-warden/log"
real_dirs=("$HOME/.local/state/kwin-mcp-warden" "$HOME/.config/kwin-mcp-warden" "${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/kwin-mcp-warden")
marks() { grep -c "kwin-mcp-warden-test-" "$real_log" 2>/dev/null || true; }
present() { for d in "${real_dirs[@]}"; do [ -e "$d" ] && echo "$d"; done; }
# The person's Flatpak document portal (upstream issue #132: a nested session's portal can
# take over $XDG_RUNTIME_DIR/doc and the host's portal exits): mounted before, mounted after.
real_doc="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/doc"
doc_mounted() { findmnt -n "$real_doc" >/dev/null 2>&1 && echo yes || echo no; }
marks_before="$(marks)"; present_before="$(present)"; doc_before="$(doc_mounted)"
# Every process a run starts inherits this mark (conftest.py): after pytest exits, none may
# be left (a daemon, a nested KWin, a helper). Anything left is stopped, after its own
# environment shows the mark, and the run fails.
export KWIN_MCP_WARDEN_TEST_RUN="run.sh-$$-$(date +%s%N)"
# The lookup (run_processes.py, shared with conftest.py) runs without the mark itself, so
# it never finds its own helpers; it also finds a private desktop's KWin, which hides its
# environment, by the marked session it runs in.
marked() {
    env -u KWIN_MCP_WARDEN_TEST_RUN python3 -I tests/warden/run_processes.py \
        "KWIN_MCP_WARDEN_TEST_RUN=$KWIN_MCP_WARDEN_TEST_RUN"
}
left_behind() {
    local pids pid sig
    pids="$(marked)"
    [ -z "$pids" ] && return 0
    echo "FAILED: the test run left processes behind:"
    for pid in $pids; do echo "        $pid $(tr '\0' ' ' </proc/"$pid"/cmdline 2>/dev/null | cut -c1-150)"; done
    for sig in TERM KILL; do
        pids="$(marked)"
        [ -n "$pids" ] && kill -"$sig" $pids 2>/dev/null
        sleep 2
    done
    return 1
}
check_untouched() {
    if [ "$(marks)" != "$marks_before" ]; then
        echo "FAILED: a test session wrote into the real log $real_log"; return 1
    fi
    if [ "$doc_before" = yes ] && [ "$(doc_mounted)" != yes ]; then
        echo "FAILED: the test run took down the person's document portal ($real_doc)"
        echo "        restore it with: systemctl --user restart xdg-document-portal"; return 1
    fi
    if [ "$(present)" != "$present_before" ]; then
        echo "FAILED: the test run created a real warden folder: $(present)"; return 1
    fi
}
if [ "${1:-}" = "--plants" ]; then
    status=0
    while IFS='|' read -r plant test; do
        if ! timeout -k 30 600 $PY -m pytest tests/warden/test_boundaries.py -q -p no:cacheprovider \
            -k "$test" >/dev/null 2>&1; then
            echo "BROKEN  $plant: $test fails even without the fault"; status=1
            left_behind || status=1; continue
        fi
        left_behind || status=1
        if PLANT="$plant" timeout -k 30 600 $PY -m pytest tests/warden/test_boundaries.py -q -p no:cacheprovider \
            -k "$test" >/dev/null 2>&1; then
            echo "MISSED  $plant: $test passed with the fault planted"; status=1
        else
            echo "caught  $plant: $test failed, as it must"
        fi
        left_behind || status=1
    done < <($PY -c "import sys; sys.path.insert(0, 'tests/warden'); from conftest import PLANTS; only = sys.argv[1:]; [print(f'{k}|{v}') for k, v in PLANTS.items() if not only or k in only]" "${@:2}")
    check_untouched || status=1
    exit $status
fi
timeout -k 30 900 $PY -m pytest tests/warden/test_boundaries.py -q -p no:cacheprovider "$@"
status=$?
left_behind || status=1
check_untouched || status=1
exit $status
