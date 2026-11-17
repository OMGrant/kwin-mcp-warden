"""How long a grant lasts: the lengths a card offers, and their words.

The person picks the length on the card, shorter or longer than the agent asked;
the agent's ask only decides which one is selected when the card opens. The
settings file sets the longest one offered (``max_grant_minutes``,
``max_full_desktop_minutes``); 0 there means a grant may last until the session
ends, and the card then offers "This session" too.
"""

from __future__ import annotations

SESSION = 0  # "This session": until the agent's session ends
# A session-long grant still ends with the session's server, the stop key or the
# access list; its record carries a far-off expiry so every check stays a number.
SESSION_SECONDS = 100 * 365 * 24 * 3600

WINDOW_LADDER = (60, 240, 480, SESSION)
FULL_DESKTOP_LADDER = (30, 60, 240, 480, SESSION)


def offered(ladder: tuple[int, ...], default: int, ceiling: int) -> list[int]:
    """The lengths a card offers, shortest first, "This session" last: the ladder's
    steps up to ``ceiling`` (0 = no ceiling), plus the default and the ceiling
    themselves when they aren't steps."""
    finite = {m for m in ladder if m != SESSION and (ceiling == SESSION or m <= ceiling)}
    if default != SESSION and (ceiling == SESSION or default <= ceiling):
        finite.add(default)
    if ceiling != SESSION:
        finite.add(ceiling)
    out = sorted(finite)
    if ceiling == SESSION:
        out.append(SESSION)
    return out


def pick(asked: int | None, default: int, choices: list[int]) -> int:
    """The length selected when the card opens: the default when the agent didn't ask,
    else the shortest choice that covers its ask (the longest there is when none does)."""
    if not choices:
        return default
    if asked is None:
        return default if default in choices else choices[0]
    finite = [m for m in choices if m != SESSION]
    if asked == SESSION:
        return SESSION if SESSION in choices else max(finite)
    covering = [m for m in finite if m >= asked]
    if covering:
        return covering[0]
    return SESSION if SESSION in choices else max(finite)


def chip(minutes: int) -> str:
    """A choice on the card: "30 min", "1 hour", "4 hours", "This session"."""
    if minutes == SESSION:
        return "This session"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


def allow(minutes: int) -> str:
    """The Allow button for a length: "Allow for 1 Hour", "Allow for This Session"."""
    if minutes == SESSION:
        return "Allow for This Session"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "Allow for 1 Hour" if hours == 1 else f"Allow for {hours} Hours"
    return f"Allow for {minutes} Minutes"


def left(seconds: float) -> str:
    """Time left on a grant, for the tag, the corner notice and the access list."""
    if seconds > SESSION_SECONDS / 2:
        return "until the session ends"
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return f"{minutes} min left"
    hours, rest = divmod(minutes, 60)
    return f"{hours} h {rest} min left" if rest else f"{hours} h left"


def choices_for_card(choices: list[int], selected: int, prefix: str = "") -> dict[str, object]:
    """The card's part of a request: each choice's words and the Allow button for it."""
    return {
        "lengths": [{"minutes": m, "chip": chip(m), "allow": prefix + allow(m)} for m in choices],
        "length": selected,
        "allow": prefix + allow(selected),
    }
