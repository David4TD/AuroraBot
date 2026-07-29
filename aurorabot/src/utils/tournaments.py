"""Tournament helpers: "is this running right now?" and display labels.

Standings and alert subscriptions both care about *current* tournaments only —
a picker full of splits from three years ago is noise. PandaScore exposes
``/tournaments/running``, but ``/leagues/{id}/tournaments`` returns the full
history, so the window check also runs client-side.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Grace either side of the published window: PandaScore's end_at is often the
# scheduled finish, and a split that starts tomorrow is still worth showing.
_START_GRACE = timedelta(days=7)
_END_GRACE = timedelta(days=2)


def parse_dt(value: str | None) -> datetime | None:
    """Parse a PandaScore ISO-8601 timestamp into an aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_current(tournament: dict, *, now: datetime | None = None) -> bool:
    """True when a tournament is running now (or starts within a week).

    Tournaments with no ``begin_at`` are treated as current — PandaScore leaves
    it unset on some freshly announced events, and hiding those is worse than
    showing one extra row.
    """
    now = now or datetime.now(timezone.utc)
    begin = parse_dt(tournament.get("begin_at"))
    end = parse_dt(tournament.get("end_at"))

    if begin is not None and now < begin - _START_GRACE:
        return False
    if end is not None and now > end + _END_GRACE:
        return False
    return True


def current_tournaments(
    tournaments: list[dict], *, now: datetime | None = None
) -> list[dict]:
    """Keep only current tournaments, soonest-ending first."""
    now = now or datetime.now(timezone.utc)
    live = [t for t in tournaments if is_current(t, now=now)]

    def sort_key(t: dict) -> tuple[int, float]:
        begin = parse_dt(t.get("begin_at"))
        if begin is None:
            return (1, 0.0)
        # Already under way sorts above "starting soon".
        return (0 if begin <= now else 1, abs((begin - now).total_seconds()))

    return sorted(live, key=sort_key)


def tournament_label(tournament: dict, *, max_len: int = 100) -> str:
    """``LEC Summer 2026 · Playoffs`` — league, serie context, then the stage.

    The league name is what tells LEC apart from LCS, so it always leads. Its
    serie ``full_name`` is usually only the split ("Summer 2026"), which on a
    cross-league list like the ``/alerts add`` autocomplete leaves several
    tournaments sharing one label. Series that already name their league
    ("LEC Summer 2026") are left alone rather than stuttering.
    """
    serie = (tournament.get("serie") or {}).get("full_name") or ""
    league = (tournament.get("league") or {}).get("name") or ""
    name = tournament.get("name") or "Stage"

    head = serie
    if league and league.casefold() not in serie.casefold():
        head = f"{league} {serie}".strip()

    label = f"{head} · {name}".strip(" ·") if head else str(name)
    return label[:max_len] or "Tournament"
