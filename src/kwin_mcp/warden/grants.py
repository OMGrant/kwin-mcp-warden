"""The grants one session holds, and when they end.

A grant is for one window and one level (See, Arrange, Control), for the whole
desktop, or for a screen of Claude's own. A window's grant covers its own dialogs
and menus (the windows KWin says are transient for it), never the app's other
windows. It ends at the first of: the length the person picked on the card, the end of the
session (this process), the stop key, or an End in the access list.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from kwin_mcp.warden import state
from kwin_mcp.warden.policy import LEVEL_NAMES, covers

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

type GrantKind = Literal["window", "full_desktop", "own_screen"]


@dataclass(frozen=True)
class Grant:
    id: str
    kind: GrantKind
    app: str  # the window's app id ("" for full desktop and own screen)
    app_name: str
    level: str  # see/arrange/control/full_desktop ("" for own screen)
    reason: str
    given: float
    expires: float
    window: str = ""  # KWin's internal id of the granted window ("" for the other kinds)
    window_title: str = ""  # its title when the person allowed it, for the access list

    def alive(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now < self.expires and self.given > state.stop_time() and not state.ended(self.id)

    def describe(self) -> str:
        from kwin_mcp.warden.lengths import left

        remaining = left(self.expires - time.time())
        if self.kind == "own_screen":
            what = "a screen of its own"
        elif self.kind == "full_desktop":
            what = "your whole desktop"
        else:
            what = f"{LEVEL_NAMES[self.level]} in one {self.app_name} window"
        return f"{what} ({remaining})"


class GrantBook:
    """This session's grants; thread-safe; mirrored to the shared runtime directory."""

    def __init__(self, session: str, agent: str = "Claude") -> None:
        self.session = session
        self.agent = agent  # the agent's name, for the overlay (gate.agent_name)
        self._lock = threading.Lock()
        self._grants: list[Grant] = []

    def give(
        self,
        kind: GrantKind,
        *,
        app: str = "",
        app_name: str = "",
        level: str = "",
        reason: str = "",
        minutes: int,
        window: str = "",
        window_title: str = "",
    ) -> Grant:
        from kwin_mcp.warden.lengths import SESSION, SESSION_SECONDS

        now = time.time()
        seconds = SESSION_SECONDS if minutes == SESSION else minutes * 60
        grant = Grant(
            id=secrets.token_hex(6),
            kind=kind,
            app=app,
            app_name=app_name or app,
            level=level,
            reason=reason,
            given=now,
            expires=now + seconds,
            window=window,
            window_title=window_title,
        )
        with self._lock:
            self._grants.append(grant)
        self.publish()
        return grant

    def alive(self) -> list[Grant]:
        with self._lock:
            live = [g for g in self._grants if g.alive()]
            changed = len(live) != len(self._grants)
            self._grants = live
        if changed:
            self.publish()
        return live

    def own_screen(self) -> Grant | None:
        return next((g for g in self.alive() if g.kind == "own_screen"), None)

    def full_desktop(self) -> Grant | None:
        return next((g for g in self.alive() if g.kind == "full_desktop"), None)

    def for_window(self, lineage: Sequence[Mapping[str, Any]], level: str) -> Grant | None:
        """The grant that lets this session do ``level`` work in a window, if any.

        ``lineage`` is the window from KWin's stack, then the window it's transient for,
        and so on up: a dialog or menu is covered by its window's grant. Under Full
        desktop every window is covered (the gate keeps never-granted ones out).
        """
        full = self.full_desktop()
        if full is not None:
            return full
        ids = {str(w.get("id") or "") for w in lineage} - {""}
        for grant in self.alive():
            if grant.kind == "window" and covers(grant.level, level) and grant.window in ids:
                return grant
        return None

    def end(self, grant_id: str) -> None:
        """End one grant (its window closed)."""
        with self._lock:
            self._grants = [g for g in self._grants if g.id != grant_id]
        self.publish()

    def end_all(self) -> None:
        with self._lock:
            self._grants = []
        self.publish()

    def publish(self) -> None:
        with self._lock:
            grants = [asdict(g) for g in self._grants]
        try:
            if grants:
                state.write_grants(
                    os.getpid(), {"session": self.session, "agent": self.agent, "grants": grants}
                )
            else:
                state.clear_grants(os.getpid())
        except OSError:
            pass
