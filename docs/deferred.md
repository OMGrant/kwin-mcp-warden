# kwin-mcp-warden: deferred, stubbed and known gaps

Every report starts with this list. Each item names the file it lives in.

## Decided

1. **The stop key is Meta+Alt+Esc.** The design said Meta+Esc,
   on the claim that it was free; stock Plasma gives Meta+Esc to System Monitor
   (`X-KDE-Shortcuts=Meta+Esc` in `org.kde.plasma-systemmonitor.desktop`).
   The daemon refuses any key another app holds and logs it (`warden/daemon.py`,
   `register_stop_key`; test `test_a_stop_key_another_app_holds_is_refused`).

## Deferred

2. ~~Mid-action pause covers typing only.~~ Fixed 2026-11-14: a drag, swipe, pinch,
   long-press or multi-step scroll already under way stops and lets go the moment you touch
   the mouse or keys, a consent card comes up, or the stop key ends the grant (upstream's
   gesture waits call the warden's check, `input.py` `input_checks`; `gate.py`
   `_mid_action_checks`; test `test_your_input_stops_a_drag_midway`, plant
   `gestures-unchecked`). Typing still pauses and goes on, as before. Unicode typing that
   pastes is one action, so it has no middle to stop in.
3. **Clicks are checked as the button goes down, not inside KWin.** Narrowed 2026-11-14: the
   window under a click, drag start or touch is looked up again after the pointer reaches the
   spot, right before the button or touch goes down (`gate.py`, `_mid_action_checks`; test
   `test_a_window_raised_before_the_press_is_checked`, plant `press-unchecked`). What's left
   is the few milliseconds between that look and the press. Closing it fully needs an input
   filter inside KWin (an out-of-tree C++ KWin plugin built against kwin-devel, rebuilt on
   every KWin update); that needs the owner's decision (a new build dependency, and a crash in it
   takes the compositor down).
4. ~~The frame follows windows by polling.~~ Done: a KWin script loaded once
   (`ui/stack-watch.js`) pushes the stack to the daemon on every change; the poll is only a
   fallback, and the daemon logs when it falls back (`test_the_frame_follows_the_window`).
5. ~~Menus of a granted app are black in pictures.~~ Wrong when written: KWin 6 reports a
   Wayland menu as a managed window of its app, so it already showed. Kept as a guarded
   behavior (`test_a_granted_apps_menu_shows_in_the_picture`), and unmanaged menus (X11
   override-redirect) now follow their parent window (`gate.py`, `_shows_in_picture`).
6. ~~The card says "Google Chrome" where the mock says "Chrome".~~ Decided 2026-11-14: the
   warden names an app the way its desktop file does, which is what the app launcher and the
   task manager show (`warden/apps.py`); the mock now says "Google Chrome" too.
7. ~~Go to Session has no boundary test.~~ Done: `test_go_to_session_brings_the_card_back`
   (plant `go-answers`).
8. ~~The tray icon is untested.~~ Tested 2026-11-14: a stand-in for Plasma's tray on the
   test's private bus (`tests/warden/fake_tray_host.py`) lets the test reach the icon the way
   the panel does; it checks the tooltip and that clicking opens and closes the access list
   (`test_the_tray_icon_opens_the_access_list`; hand plant: an icon whose click does nothing
   fails it).
9. **Upstream's end-to-end suite isn't adapted to the fork.** Run 2026-11-14 with podman
   (`scripts/run-e2e-docker.sh -- tests/e2e tests/unit`, Debian image). Its first step,
   upstream's ruff, ruff format and ty, now passes (it failed on the warden's code before).
   The suite itself was stopped at 16% so it wouldn't hold the machine for another hour:
   314 passed, 45 failed, 1 skipped. Every failure looked at is the fork's own change,
   not a broken upstream feature: tests that run upstream's command names (`kwin-mcp`,
   `kwin-mcp-cli`; the fork's are `kwin-mcp-warden`, `kwin-mcp-warden-cli`), count 29 tools
   (the fork has 31), or drive the server or CLI expecting no warden (the warden asks for a
   screen of Claude's own first). Most failures were in `test_exit_cleanup_virtual.py`
   (22), `test_cli_surface.py` (8), `test_exit_cleanup_live.py` (5), `test_capture.py` (3)
   and `test_a11y_text_formats.py` (3). Adapting them means reworking a few hundred upstream
   tests for a gated server (answering cards inside the container, as the boundary tests
   do); that scope is the owner's call. The Fedora variant can't start: its pinned base image
   isn't on the registry podman resolves `fedora` to.
10. ~~Plugin manifests are upstream's.~~ Fixed 2026-11-14: every manifest and the OpenCode plugin name `kwin-mcp-warden` and start the bare `kwin-mcp-warden` command (never `uvx kwin-mcp`); versions follow pyproject; `tests/unit/test_plugin_identity.py` guards it. Still open: the OpenCode plugin's `npm run build`, `test:contract` and smoke test were not run (no node_modules; no network install).
11. ~~Not installed anywhere.~~ Installed 2026-11-14: `install.sh` (user-scope MCP server `kwin-mcp-warden`, and since 2026-11-14 the commands on PATH in `~/.local/bin` for the plugins). Tried live 2026-11-14 18:40: See in Konsole, opening Kate and Control in Kate allowed on real cards, typing delivered, a keypress in Konsole refused, typing paused for the person's mouse and went on. Not yet tried live: the stop key, the access list, Don't Allow.
12. ~~Stranger check: not yet as another user.~~ Done 2026-11-14: `tests/warden/container.sh`
    runs the boundary tests as the user `tester` (rootless podman maps it to a host user id
    that isn't the developer's) on Debian 13 (Qt 6.8, KWin 6.3), from an installed wheel, with no
    network and no host display, bus or input: 34 pass, 2 picture tests skip (this KWin's
    virtual backend has no ScreenShot2), 1 is the on-request picture test. It found three
    real faults, now fixed: the card and notice needed Qt 6.9 (`RectangularShadow`, now
    optional through `ui/Shadow.qml`) and a newer layer-shell-qt (`activateOnShow`, now set
    only where it exists), so on an older system no card could ever open; and the stop-key
    clash test relied on System Monitor being installed (it now plants a made-up app).

13. ~~Left open from the Opus review.~~ All fixed and tested 2026-11-14:
    - A daemon that dies comes back within two seconds while a grant is held, without
      waiting for Claude's next call, and its overlays end with it (`gate.py`,
      `Warden._keep_daemon`; `daemon.py`, `_die_with_parent`; test
      `test_the_daemon_comes_back_between_calls`, plant `no-keeper`). In the test the old
      overlays also ended without `_die_with_parent`, so that part is unproven by a plant.
    - A caption selector answers the same for a window Claude may not see as for no window
      (`gate.py`, `_windows` blanks hidden titles; test
      `test_a_caption_selector_cant_find_hidden_windows`, plant `captions-selectable`).
    - Grants are per window (changed 2026-11-17; they were per app): a grant names one
      KWin window by its internal id and covers that window and the windows transient for
      it (its dialogs and menus), never the same app's other windows (`grants.py`,
      `GrantBook.for_window`; `gate.py`, `_lineage`, `_allowed`, `_sees`). Windows Claude
      isn't granted are left out of `window_list`, window selectors and `element_query`,
      and blacked out of pictures. The card names the app's window used last that the
      session doesn't hold yet, by its title (`gate.py`, `_window_to_ask_for`); an app
      that isn't open is granted the new window it opens (`_open_for_grant`). The grant
      ends when its window closes (`daemon.py`, `overlay_state`). Tests:
      `test_a_grant_covers_one_window_not_the_whole_app` (plant `app-wide-grant`),
      `test_window_list_shows_no_window_without_see` (plant `windows-listed`),
      `test_a_granted_windows_dialog_comes_with_it`, `test_a_windows_grant_ends_when_it_closes`,
      `tests/unit/test_warden_same_app.py`. App ids still match as before when choosing
      the window to ask about (`settings.py`, `same_app`; `_full_app_id`).
      Not covered: a dialog an app opens without marking it transient for its window (an
      X11 group transient, or a file dialog from a separate portal process) isn't the
      window's, so it's hidden and needs its own grant; the card names the window by its
      title only, so two windows with the same title can't be told apart on the card.
    - The lock screen: proven against a real locked KWin (a second private session started
      locked); every click and keypress is refused even under Full desktop
      (`test_the_lock_screen_is_never_granted`, plant `lock-ignored`; without the check the
      click reached the lock screen). If `gdbus` or the ScreenSaver service is missing the
      check reads "not locked"; Plasma always has both.
    - kwin-mcp-warden-cli takes `reason="..."` for ask-every-time tools
      (`cli.py`; `tests/unit/test_warden_cli_reason.py`).
    - Without kscreen-doctor the warden asks KWin for the primary monitor
      (`workspace.screenOrder`; `geometry.py` op `primary`; `card.py`; test
      `test_the_primary_monitor_without_kscreen_doctor`, plant `primary-kscreen-only`).

14. ~~Accessibility switch: mock awaiting review.~~ Built 2026-11-14 from the approved mock
    (`warden/a11y.py`; `gate.py` `_ask_accessibility`, `WardenEngine._live_accessibility`;
    `daemon.py` turns it off; the access list's row in `ui/overlay.qml`). Claude asks with
    `access_request(level="accessibility")`, only while holding another grant; a no holds
    for the session; `session_connect(enable_accessibility=true)` no longer flips it. Tests
    `test_accessibility_is_asked_for_and_turned_back_off`, `test_keep_it_off_holds_for_the_session`,
    `test_a_switch_already_on_is_left_on`, `test_session_connect_cant_flip_the_switch`,
    plants `a11y-without-a-card`, `a11y-asks-again`, `a11y-connect-flips`, plus two by hand
    (the daemon never turning it off; turning off a switch it didn't turn on).
    - ~~The access list's Turn Off button is untested.~~ Tested 2026-11-14:
      `test_the_access_lists_turn_off_ends_accessibility` clicks it as the person (hand
      plant: a daemon that ignores `A11Y-OFF` fails it). The overlay's buttons are now real
      buttons to a screen reader (`ui/overlay.qml`, `WardenButton`: every one of them only
      takes access away or opens the log; the card, which grants, is unchanged).
    - ~~After a crash, the switch stays on until the daemon starts.~~ Fixed 2026-11-14: at
      login, `install.sh`'s autostart entry
      (`~/.config/autostart/kwin-mcp-warden-accessibility.desktop`, removed by
      `uninstall.sh`) runs `python -m kwin_mcp.warden.a11y --after-login`, and the record
      counts even when it names a bus that's gone (a bus address can change between
      logins; `a11y.py`, `ours`). Test `test_a_switch_left_on_before_a_restart_goes_back_off`
      (hand plants: the old exact-address record; a record of a live bus of another
      desktop taken as ours). A crash with no reboot: fixed 2026-11-16, a guard starts
      with the switch (`a11y.py`, `guard`, `python -m kwin_mcp.warden.a11y --guard`) and,
      once neither the daemon runs nor any live session holds a grant, seen twice a
      second apart, turns it off; it ends when the switch isn't the warden's or its bus or
      runtime folder is gone. Test
      `test_the_switch_goes_off_when_the_warden_dies_without_doing_it` (the daemon
      SIGKILLed, nothing to restart it; registered plant `no-a11y-guard`, a guard that
      holds the lock and never turns it off, caught). One guard at a time (a lock); the
      daemon starts it again whenever the switch is the warden's, and a turn-on that
      failed after the record was written starts it too. A grant record counts only while
      the process that wrote it runs: it carries the process's start time, so a reused pid
      or a zombie holds nothing (`state.py`, `_proc_start`;
      `tests/unit/test_warden_grant_records.py`, plant: no start-time check, caught).
      Left: the person turning the switch on themselves while the warden's record stands
      can't be told apart from the warden's.
    - Apps that joined while it was on stay readable until they restart (Qt keeps its
      bridge; measured 2026-11-14).
15. **Typing into a field (`keyboard_type(into=...)`, built 2026-11-14)**
    (`accessibility.py` `write_text`, `core.py` `_type_into`, `warden/gate.py` `_level`):
    - ~~It only inserts at the caret.~~ `replace=true` (2026-11-14) puts the text in place
      of everything in the field (`accessibility.py`, `_replace_text`: SetTextContents,
      else delete and insert). Test `test_replace_puts_its_text_in_place_of_the_fields`
      (hand plant: replace ignored).
    - Proven in Qt (Kate: `test_typing_into_a_field_needs_no_focus`, with accents and an
      emoji; plants `into-waits-for-hands`, plus wrong-unit lengths and a no-op write
      planted by hand), in a minimized window (`test_typing_into_a_minimized_window`), and
      in GTK 4 (zenity: `test_typing_into_a_gtk_field` and
      `test_typing_into_a_gtk_text_view`; a one-line GTK entry ignores the length, a text
      view counts it in bytes, `gtk/a11y/gtkatspieditabletext.c`; hand plant: Qt's units
      for GTK cut the text view's text). The GTK tests skip where zenity isn't installed
      (the Debian 13 container image). GTK 3 (through ATK and at-spi2-atk) is proven
      since 2026-11-16 with a made-up GTK 3 window (`tests/warden/gtk3_app.py`,
      `test_typing_into_gtk3_fields`: entry and text view; plant: Qt's units for every
      toolkit, both caught). Firefox is tested since 2026-11-16 (Firefox 160 from
      Mozilla's tarball, checksum checked, in ~/.local/opt; the tests take it from PATH or
      $WARDEN_FIREFOX and skip without it, as in the container): its fields show to
      accessibility and typing by keys at the focus works (`test_typing_into_firefox`),
      but Firefox takes an AT-SPI write and drops it, on <input> and <textarea> alike
      (Mozilla bug 1861026, open): `test_writing_into_firefox_fields` is a strict known
      failure that turns green when Firefox fixes it; until then the write's warning
      says to click the field and type (`core.py`). Untested: whether a Firefox already
      open joins when the switch turns on later (the tests start it with the switch on).
      Chrome is tested since 2026-11-16
      (`test_typing_into_chrome`, `test_chrome_fields_cant_be_written_over_accessibility`):
      Chromium has no AT-SPI EditableText (`ax_platform_node_auralinux.cc`), and on Linux
      it shows nothing inside its windows unless Orca runs or it was started with
      --force-renderer-accessibility (`browser_accessibility_state_impl_auralinux.cc`), so
      a write into a Chrome field is refused with that reason and typing goes by keys at
      the focus (accents and an emoji land whole). A write that matches nothing now goes
      on to the engine's answer only when every window of the app it names is granted
      (`gate.py` `_element_windows`; plant no-match-checks-focus, caught). Electron apps
      share Chromium's code but aren't tested; the Chrome tests skip where Chrome isn't
      installed (the container).
    - Upstream's tool-list budget went from 9510 to 9700 (`tests/unit/test_schema_budget.py`,
      `scripts/render_tool_surface.py`).
16. ~~A touchpad tap-to-click isn't a press.~~ Fixed 2026-11-16 (`warden/daemon.py`, `Tap`):
    the kernel reports only a finger landing, so the watcher applies libinput's own tap
    rules (`src/evdev-mt-touchpad-tap.c`: lifted within 180 ms, moved under 1.3 mm, the
    touchpad's resolution read with EVIOCGABS) and counts the tap when the finger lifts,
    when libinput makes the click. Without a resolution only the time counts (a quick
    flick then pauses typing too, never the other way). Tests in
    `tests/unit/test_warden_presses.py` (plants by hand: the watcher ignoring taps; motion
    ignored). The ioctl was checked against this machine's touchscreen (10 units/mm); no
    real touchpad was on hand, so a real tap is untested.

17. ~~Private desktops share their runtime folder with the desktop that started them~~
    (`session.py`; upstream issue #132):
    - ~~A second private desktop's AT-SPI launcher takes over the first's socket.~~ Fixed
      2026-11-14 with the narrower option A from #132's thread: the session's bus, the
      services it activates and the AT-SPI bus get a runtime folder of their own
      (`Session._bus_runtime_dir`, inside the session's `kwin-mcp-bus-*` folder); KWin and
      launched apps keep the host's, and the wrapper script links KWin's socket into the
      private folder for activated services (probed: `wayland-info` reaches KWin through
      the link). Before this, a screen of Claude's own on the person's desktop replaced
      the person's own `$XDG_RUNTIME_DIR/at-spi/bus` (at-spi2-core
      `bus/at-spi-bus-launcher.c` unlinks the path before binding it). Test
      `test_a_second_screen_leaves_the_first_ones_accessibility_alone` (no longer xfail;
      plant `shared-runtime`). Activated services in a private desktop no longer see the
      host's PipeWire or sound sockets (see "Limits by design").
    - ~~Upstream #132: a private desktop's document portal takes down the host's.~~ Fixed
      2026-11-14 for the document portal and the permission store: a private desktop's bus
      finds a stand-in for each that fails to start (`session.py`,
      `_write_private_bus_config`, the narrow workaround from #132), so its portal never
      mounts `$XDG_RUNTIME_DIR/doc`. Proven twice on a real desktop: `tests/warden/render_card.py`,
      which then started its private desktop on the host's runtime folder, took the host's portal down
      on every run (status 21, at 14:13:19 and 19:48:26; restarted both times with
      `systemctl --user restart xdg-document-portal`), and with the stand-ins the same
      run left it up. That is also the path a screen of Claude's own takes on a real desktop.
      Test `test_a_private_desktop_cant_take_the_document_portal`, plant
      `private-bus-starts-portals`. `render_card.py` now runs in a runtime folder and a
      home of its own. `tests/warden/run.sh` fails any run that takes the portal down.
      A Flatpak app inside Claude's own screen has no document portal there.
    - The document portal stand-ins stay (defense in depth: without an isolated home, a
      private portal would share the host's permission store).
    - Fixed 2026-11-14: two private desktops started from one process in the same second got
      the same Wayland socket name, and stopping one deleted the other's socket
      (`session.py`, the name now uses `time.time_ns()`).

18. **The tray icon's first click did nothing at first.** Fixed in `ui/overlay.qml`: focus lost in the list's first 600 ms doesn't close
    it, and the click that just closed it doesn't reopen it (Plasma's own popups guard the
    same way, `AppletPopup::focusOutEvent`; Qt takes Plasma's focus token,
    `qstatusnotifieritemadaptor.cpp`). Test `test_the_tray_icon_opens_the_access_list` models
    focus moving right after the click; with the guard planted out, the list closed. The
    exact cause on the live desktop isn't proven (a D-Bus Activate opened it fine). Confirmed fixed live 2026-11-15 after deferred 20: the list opens on the first click.

19. ~~Test runs leave warden daemons behind.~~ Fixed 2026-11-14. On 2026-11-14, 16 daemons
    from earlier runs were still running, and a whole private test desktop from a run
    stopped by hand at 14:10 was still up at 19:47 (stopped by hand after checking each
    one's runtime folder). Causes and fixes:
    - The daemon counted every kwin-mcp-warden server on the machine as its own (the
      person's live sessions kept test daemons alive), and recreated its runtime folder
      after it was removed. Now it counts only servers sharing its runtime folder, exits
      when that folder is gone, and never recreates `$XDG_RUNTIME_DIR` (`daemon.py`,
      `_servers_alive`, `_runtime_gone`; `state.py`, `runtime_dir`). This matters off the
      test bench too: a logout removes the folder. Tests
      `tests/unit/test_warden_daemon_lifetime.py` (three faults planted by hand).
    - Every process a run starts carries the run's mark; the fixtures stop whatever is
      left (`conftest.py`, `sweep_run`), a run stopped with SIGTERM still tears down
      (pytest's KeyboardInterrupt path; run.sh's timeouts give 30 s before SIGKILL), and
      `run.sh` fails a run that left anything, lists it and stops it
      (`tests/warden/run_processes.py`; KWin hides its environment, so it's found by the
      marked process session it runs in). Hand plants: a stray `sleep`, and a skipped
      teardown that left a whole private desktop (KWin, Kate and the daemon included):
      both listed and stopped.

20. ~~Warden windows on the wrong monitor~~ (a darker rectangle behind the card that wasn't
    fullscreen; the access list opened far from where it should have). Measured on a
    two-monitor desktop: the card's dim layer for one monitor sat centered on the other, over
    that monitor's own dim, and the first monitor went undimmed. Cause: Qt moves a window to
    the monitor its geometry lies on (layer-shell-qt
    MR 89's note: `QWindow::screen()` changes after geometry changes), and every warden
    window sat at Qt's default 0,0; layer-shell-qt makes the surface on that monitor. On
    Qt 6.8 (Debian 13) a size set after the monitor also moved it back. Fixed in
    `ui/card.qml` and `ui/overlay.qml`: each window sits at its own monitor's origin, set
    again in code right before it shows; the list and the notice are placed before they
    show and shown again when their monitor changes (compared by name: the state is read
    four times a second, and each read gave a new screen object, which made the open list
    flash on Debian); the list now opens on the primary monitor. Test
    `test_every_warden_window_sits_on_its_own_monitor` (a second private KWin with two
    outputs; card plant: no origin; list plant: never placed), and the tray test checks the
    open list keeps one window (plant: compare by object; caught on Debian, where it
    flashed). Layer-shell-qt's own `screen` setting can't be used from QML (it takes a
    QScreen, QML has only screen infos). Not proven: why the very first tray click showed
    nothing on the live desktop; the list's wrong monitor is the likely cause. Live, after this fix, the first click opens the list (2026-11-15).

21. ~~The dim zoomed open with the card~~ (the fullscreen background overlay animated with
    the card, a zoom effect it shouldn't have).
    KWin types a layer-shell window by its scope and gave the card's windows, scope
    `kwin-mcp-warden-card`, the "normal" type, so its Scale effect zoomed each whole window
    (the dim and the card in it) open. Fixed in `ui/card.qml`: the scope is `utility`, which
    neither Scale nor Fading Popups animates (kwin `src/layershellv1window.cpp`
    scopeToType; `effects/scale` isScaleWindow; `effects/fadingpopups`), and the card plays
    Scale's own entrance (200 ms, OutCubic, from 80 % and transparent, times Plasma's
    animation speed, passed as `motion` by `card.py`) while the dim only fades. Checked in
    `test_every_warden_window_sits_on_its_own_monitor` (plant: the old scope). The overlay's
    windows (frames, tags, corner notice, access list) still open with KWin's zoom; left as
    they are, since only the card was reported.

22. **Open and control on one card** (built 2026-11-15 from the mock, "Open and
    control": the warden checks what's open before prompting, and if an app isn't open but
    needs to be, it asks to open it and control it at the same time). `gate.py` `Warden.request` checks the live window stack first; an app that
    isn't open but has a desktop entry gets one card ("Claude asks to open Konsole and
    control it", Allow "Open and Allow for 1 Hour"); on Allow, `_open_for_grant` opens it
    with the entry's Exec (`apps.launch_command`, field codes dropped per the Desktop Entry
    Specification) and grants only once a window of that app is on the desktop (30 s);
    neither open nor installed gets no card. Tests: `test_an_app_that_isnt_open_is_opened_and_allowed_on_one_card`,
    `test_dont_allow_opens_nothing`, `test_an_app_that_cant_be_opened_gets_no_card`,
    `test_something_else_opening_grants_nothing` (a made-up entry that opens KCharSelect);
    plants `no-open-card`, `grant-before-window`, plus Don't Allow opening it by hand.
    ~~Not covered: D-Bus activation and single-instance apps.~~ Done 2026-11-16: an entry
    with `DBusActivatable=true` opens the way KDE's launcher does (`apps.launch_command`:
    `org.freedesktop.Application.Activate` on the desktop file id, per the Desktop Entry
    Specification and KIO `DBusActivationRunner`, which also wants two dots in the name),
    Exec otherwise. Tests `test_an_app_that_only_opens_over_dbus_is_opened_that_way` (a
    made-up GTK 4 app named with a dash, no Exec, started by the private bus as a service
    that opens a window only when Activate reaches it; plants: no D-Bus route, and
    `dbus-wrong-path`, both caught), `test_a_single_instance_app_hands_its_window_over` (an
    instance already running with no window takes the open from a second launch, so the
    window comes from another process; the grant follows the app, and still covers the
    window once it's moved to another virtual desktop; hand plant: an app that doesn't
    hand over, caught), `tests/unit/test_warden_launch_route.py`. An instance that already
    has a window, on any desktop, means the app is open, so it gets the plain card and
    nothing is launched. Not sent: KIO's Wayland `activation-token`, so an activated
    window may open without focus.
23. **Every name is the agent's that asks or holds the grant** (a card reads "*Agent* can
    see KCharSelect's window and read its text. Nothing else.", with whatever agent is
    connecting). Done 2026-11-16. Cards: `gate.py` `agent_name`
    (the settings file's `agent_name`, else the MCP client's clientInfo, `claude-code` reads
    "Claude", else "Claude"; `<` and `>` dropped). Overlay: each server's grant record
    carries its agent (`grants.py`, `state.all_grants`; a record without one reads as
    Claude's), and `daemon.overlay_state` names them for the tray tooltip, tag, corner
    notice, access list (its title, and each row once two agents hold grants) and the
    accessibility row; the pointer label reads the agent from the pointer file. The named
    lines now have accessible names (a screen reader read none of the overlay's text).
    Tests: `tests/unit/test_warden_card_copy.py`, `test_the_mcp_route_goes_through_the_warden`
    (client `stranger-agent`), `test_the_overlay_names_the_agent_that_holds_the_grant`.
    Plants by hand, all caught: the server ignoring the client's name; every record read as
    Claude's; the pointer file fixed to Claude. The refusals the agent reads say "you"
    (`gate.py`); `test_no_refusal_names_claude` reads every `Refused(...)` text and every
    text `request` hands back from the source (two plants by hand, both caught). The
    unused `policy.LEVEL_MEANS` table is gone.
24. **The tray test's flake** (`test_the_tray_icon_opens_the_access_list`, failed once in a
    full run on 2026-11-16). Cause, reproduced: the test acted out Plasma's focus shuffle
    with ~360 ms of its own calls (a header click, then a focus change) and judged the list
    by a 600 ms window, so a slow moment in those calls looked like the list closing early
    (a 1 s pause gave the same message). Fixed: the test moves focus at once and judges
    only moves that landed under 400 ms, retrying up to four times; the tooltip check waits
    for the overlay's next read. Planted: no grace at all (`listSettleMs: 0`), caught. The
    overlay now times the grace from the list's first focus after it opens, not from the
    click (`ui/overlay.qml`, `listActiveAt`), for a list that shows late on a busy machine.

25. ~~The drag test's flake~~ (`test_your_input_stops_a_drag_midway`, "the drag ran to
    its end through the person's input", twice in slow runs). Cause, reproduced with a
    1.5 s delay before the drag's checks: the test touched the mouse 1 s after starting
    the drag, which on a slow run was before the drag began, and a touch then rightly
    makes the drag wait, not stop. Fixed: the test touches only once the drag's first
    mid-action check ran, and times the stop from the touch (the same delay now passes).
    Found while fixing it, a real gap: after a pause for the person's hands, the warden
    checks the window again before the drag starts, and a touch during that check was
    missed (the midway checks counted from after it). Now they count from the moment the
    hands were last seen quiet (`gate.py`, `_hands_first` returns it). Test
    `test_a_touch_while_the_window_is_checked_after_a_pause_stops_the_drag` (plant: the
    old start time, caught). Since the review the same day: a touch during that check
    means waiting again, so neither a gesture nor typing starts with input already past
    the quiet moment (test renamed `..._waits_again`; registered plant `pause-gap`).

26. A private desktop (the tests' nested KWin, and Claude's own screen) has no desktop
    portal since 2026-11-16 (`session.py` `_HOST_ONLY_SERVICES`): it can't run without the
    permission store, which stays the person's (upstream #132), and left to start it
    half-started and was started again by every request, so Firefox never opened a window
    (`test_a_private_desktop_has_no_portal_that_half_starts`, plant portal-half-starts).
    Apps there go without portals: no portal file chooser, settings or screenshots.
    Apps there also no longer get the person's session manager (SESSION_MANAGER, XSMP),
    which registered them with the person's real session
    (`test_own_screen_apps_dont_inherit_your_session_manager`, plant
    session-manager-inherited); the test sandbox drops it too.
27. A screen of the agent's own: since 2026-11-16 its apps get the bus's private runtime
    folder and only a short allow-list of the caller's settings (`session.py`
    `CONTAINED_ENV_NAMES`, set on by the warden after session_start), so they aren't told
    where the person's sound server, SSH agent or user services are
    (`test_own_screen_apps_get_a_runtime_folder_of_their_own`, made-up settings stand in
    for what a desktop exports; plant own-screen-not-contained). Before, they kept the
    person's folder and every setting, so `pw-record` could record the microphone and
    `ssh` could sign with the person's agent. Still open: they run as the same user with
    no mount namespace, so an app that names the person's folder by its path
    (/run/user/UID/...) still reaches it, and `systemctl --user` reaches their user
    services over the system bus's user manager. Closing that needs a sandbox such as
    bubblewrap (a new dependency: the owner's word first). The launch list
    `_REACHES_OUT` is typed by hand and misses commands such as `qdbus-qt6`,
    `dbus-monitor`, `systemctl`, `flatpak`, `gio`, `pw-record`, `ssh`; it can't be made
    complete, which is why the folder and the settings are what keep apps in.
28. A strict locator (`{strict: ...}` for keyboard_type into= and element_act) is
    resolved by the warden itself since 2026-11-16, from windows granted for the call
    only, and the engine gets the one element's ref (`gate.py` `_pin_strict`). Before, a
    locator naming no app was told apart by its refusal ("Control in X isn't granted"
    when something matched in a window not granted, another text when nothing did), so
    names could be read out of those windows a guess at a time; and anything that
    appeared between the warden's look and the engine's could be written into or listed
    (`test_a_locator_cant_read_windows_not_granted`, plant hidden-match-told). Several
    granted matches are refused with their refs listed; element_act keeps only those
    with the asked action first. A ref an agent guesses still gets "isn't granted" with
    the app's name for an element in a window not granted (no contents).

29. **A unit test's flake** (`tests/unit/test_warden_daemon_lifetime.py`
    `test_install_restarts_a_running_daemon_and_nothing_else`): twice on 2026-11-16 it
    timed out ("timed out after 10 seconds", "install.sh left the old daemon running")
    in a full unit run while the machine was loaded (load average 6); alone it passed
    every time. Cause unproven: likely the 10 s wait under load.

30. **A flake in the own-screen session-manager test, fixed 2026-11-17**:
    `test_own_screen_apps_dont_inherit_your_session_manager` read the app's
    `/proc/PID/environ` right after launch; under a full run's load the process was still
    in exec (state D, nothing mapped), so the read came back empty. It failed 3 of 7 full
    runs; it now waits for the app's window first, as the runtime-folder test does. Probably
    the unnamed stranger-check failure of 2026-11-16 too (its name wasn't kept, so that's
    unproven).

31. **Grant lengths, built 2026-11-17** at the owner's ask: the card offers lengths up to a
    ceiling set in settings (`max_grant_minutes` 480, `max_full_desktop_minutes` 60; 0 = until
    the session ends, which adds "This session"); the agent's `minutes` only selects one as the
    card opens, and the person picks any offered length, shorter or longer (`lengths.py`;
    `gate.py`, `_lengths`, `_chosen`; `ui/card.qml`, the "For" row; the card prints
    `WARDEN-CARD-LENGTH`, taken only when it's a length the card offered). Tests:
    `test_the_person_picks_how_long_not_the_agent` (real clicks on the card, both directions;
    plant `agent-picks-length`), `tests/unit/test_warden_lengths.py`. Not done: a grant can't
    be lengthened in place; when it runs out the agent asks again and a new card comes up.

## Limits by design (stated, not deferred)

- The warden guards the kwin-mcp route. A Claude session with a shell can still reach the
  desktop by other routes; Claude Code's own permission prompts guard those.
- "Your hands come first" reads real input devices under /dev/input, which needs the `input`
  group. Without it, Control on your screen is refused rather than run without the pause.
- A screen of the agent's own shares no sound or screen casting with the person's
  desktop through its settings: services its private bus starts and the apps launched
  there get their own runtime folder (deferred 17 and 27), so they aren't pointed at the
  host's PipeWire or sound sockets. An app that names the person's runtime folder by its
  path still reaches it (deferred 27). The warden's own pictures of that screen come from
  KWin's ScreenShot2 and don't need either.
