"""How long a grant lasts: the card offers lengths up to the person's ceiling, selects
the agent's ask, and the person's pick is what the grant gets, shorter or longer."""

from __future__ import annotations

import time
from typing import Any, cast

import pytest

from kwin_mcp.warden import apps, card, gate, grants, lengths
from kwin_mcp.warden.settings import Settings, load


def test_the_default_ceiling_offers_up_to_eight_hours():
    assert lengths.offered(lengths.WINDOW_LADDER, 60, 480) == [60, 240, 480]
    assert lengths.offered(lengths.FULL_DESKTOP_LADDER, 30, 60) == [30, 60]


def test_no_ceiling_offers_the_session():
    assert lengths.offered(lengths.WINDOW_LADDER, 60, 0) == [60, 240, 480, lengths.SESSION]


def test_a_ceiling_off_the_ladder_is_offered_itself():
    assert lengths.offered(lengths.WINDOW_LADDER, 60, 120) == [60, 120]
    assert lengths.offered(lengths.WINDOW_LADDER, 20, 30) == [20, 30]


@pytest.mark.parametrize(
    ("asked", "picked"),
    [(None, 60), (30, 60), (60, 60), (90, 240), (480, 480), (999, 480), (0, 480)],
)
def test_the_agents_ask_selects_the_nearest_length_that_covers_it(asked, picked):
    assert lengths.pick(asked, 60, [60, 240, 480]) == picked


def test_an_ask_past_every_length_selects_the_session_when_offered():
    assert lengths.pick(999, 60, [60, 240, 480, 0]) == 0
    assert lengths.pick(0, 60, [60, 240, 480, 0]) == 0


@pytest.mark.parametrize(
    ("minutes", "chip", "allow"),
    [
        (30, "30 min", "Allow for 30 Minutes"),
        (60, "1 hour", "Allow for 1 Hour"),
        (240, "4 hours", "Allow for 4 Hours"),
        (0, "This session", "Allow for This Session"),
    ],
)
def test_words(minutes, chip, allow):
    assert lengths.chip(minutes) == chip
    assert lengths.allow(minutes) == allow


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (59 * 60 + 5, "59 min left"),
        (4 * 3600, "4 h left"),
        (4 * 3600 + 30 * 60, "4 h 30 min left"),
        (lengths.SESSION_SECONDS, "until the session ends"),
    ],
)
def test_time_left(seconds, said):
    assert lengths.left(seconds) == said


def test_the_settings_file_sets_the_ceiling(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text("max_grant_minutes = 0\nmax_full_desktop_minutes = 240\n")
    st = load(path)
    assert (st.max_grant_minutes, st.max_full_desktop_minutes) == (0, 240)
    path.write_text("max_grant_minutes = 120\ngrant_minutes = 240\n")
    st = load(path)
    assert st.grant_minutes == 120, "a default past its ceiling comes down to it"
    assert "past its ceiling" in st.problem


def test_a_session_long_grant_outlasts_any_hour(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    tmp_path.chmod(0o700)
    book = grants.GrantBook("test")
    grant = book.give("window", app="org.kde.kate", level="see", minutes=0, window="w1")
    assert grant.expires - time.time() > 50 * 365 * 24 * 3600
    assert grant.describe().endswith("(until the session ends)")


@pytest.fixture
def warden(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    tmp_path.chmod(0o700)
    w = gate.Warden(cast("Any", object()), Settings())
    monkeypatch.setattr(w, "session_name", lambda: "test")
    monkeypatch.setattr(w, "_full_app_id", lambda app: (app, ""))
    window = {"id": "w1", "caption": "notes.md", "managed": True}
    monkeypatch.setattr(w, "_window_to_ask_for", lambda app, level: (window, None))
    monkeypatch.setattr(apps, "describe", lambda app: ("Kate", ""))
    monkeypatch.setattr(gate, "ensure_daemon", lambda: None)
    monkeypatch.setattr(w, "_keep_daemon", lambda: None)
    return w


def _answer(w, monkeypatch, outcome):
    shown: list[dict] = []

    def fake_card(request):
        shown.append(request)
        return outcome

    monkeypatch.setattr(w, "_card", fake_card)
    return shown


def test_the_card_selects_the_agents_ask(warden, monkeypatch):
    shown = _answer(warden, monkeypatch, card.Outcome("deny"))
    warden.request("control", "org.kde.kate", "Long test run.", minutes=200)
    (request,) = shown
    assert [c["minutes"] for c in request["lengths"]] == [60, 240, 480]
    assert request["length"] == 240
    assert request["allow"] == "Allow for 4 Hours"


@pytest.mark.parametrize("picked", [60, 480])
def test_the_person_can_pick_shorter_or_longer(warden, monkeypatch, picked):
    _answer(warden, monkeypatch, card.Outcome("allow", minutes=picked))
    allowed, _ = warden.request("control", "org.kde.kate", "Long test run.", minutes=240)
    assert allowed
    (grant,) = warden.book.alive()
    assert abs((grant.expires - grant.given) - picked * 60) < 1


def test_a_length_the_card_didnt_offer_counts_as_the_one_it_opened_with(warden, monkeypatch):
    _answer(warden, monkeypatch, card.Outcome("allow", minutes=99_999))
    allowed, _ = warden.request("control", "org.kde.kate", "Test.", minutes=240)
    assert allowed
    (grant,) = warden.book.alive()
    assert abs((grant.expires - grant.given) - 240 * 60) < 1


def test_the_session_is_offered_only_without_a_ceiling(warden, monkeypatch):
    _answer(warden, monkeypatch, card.Outcome("allow", minutes=0))
    warden.request("control", "org.kde.kate", "Test.", minutes=0)
    (grant,) = warden.book.alive()
    assert grant.expires - grant.given == pytest.approx(480 * 60, abs=1)


def test_full_desktop_has_its_own_ceiling(warden, monkeypatch):
    shown = _answer(warden, monkeypatch, card.Outcome("deny"))
    warden.request("full_desktop", "", "Test.", minutes=480)
    (request,) = shown
    assert [c["minutes"] for c in request["lengths"]] == [30, 60]
    assert request["length"] == 60
