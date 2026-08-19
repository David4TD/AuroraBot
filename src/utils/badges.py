"""Achievement badges: the prizes that aren't the leaderboard.

Most servers have two or three people who will win every tournament, and
everyone else. A board alone tells that second group they're losing, weekly,
forever. Badges give them something that's actually winnable — called one
genuine upset, swept a day, spent all three tokens well — and, being permanent
and per server, they accumulate for people who never finish top three.

Every badge is checked against rows the database already has, so nothing here
needs the API, a clock, or a background sweep. They're evaluated when a match
settles: that's the only moment any of these facts can become true.

Deliberately *not* here: anything needing per-game statistics (a team's 0-3
group exit, a player's KDA). Those live behind endpoints this API plan returns
403 for, and a badge that silently never fires is worse than one that doesn't
exist.
"""
from __future__ import annotations

from dataclasses import dataclass

from .scoring import MAX_MULTIPLIER, TOKENS_PER_TOURNAMENT

# How contrarian a correct call has to be to count as a genuine upset. The
# multiplier caps just under 3, so this is roughly "no more than a third of the
# room was with you".
UPSET_ODDS = 2.5
PERFECT_WEEK_MINIMUM = 5      # a clean week of two picks isn't a week
CENTURION_POINTS = 500
MARATHON_PICKS = 50


@dataclass(frozen=True)
class Badge:
    key: str
    emoji: str
    name: str
    blurb: str


BADGES: dict[str, Badge] = {
    b.key: b for b in (
        Badge("giant_killer", "🗡️", "Giant Killer",
              f"Called an upset at ×{UPSET_ODDS} or better."),
        Badge("perfect_day", "☀️", "Perfect Day",
              "Swept every match on a day."),
        Badge("perfect_week", "🌟", "Perfect Week",
              f"Won every pick across a week — at least {PERFECT_WEEK_MINIMUM}."),
        Badge("champion", "👑", "Champion",
              "Won a tournament outright."),
        Badge("all_in", "💥", "All In",
              f"Won all {TOKENS_PER_TOURNAMENT} double downs in one tournament."),
        Badge("kingmaker", "🎯", "Kingmaker",
              "Called a grand final correctly."),
        Badge("centurion", "💯", "Centurion",
              f"Banked {CENTURION_POINTS} points in this server."),
        Badge("marathon", "🏃", "Marathon",
              f"Made {MARATHON_PICKS} predictions here."),
        Badge("lone_wolf", "🐺", "Lone Wolf",
              "Was the only person to call a match right."),
    )
}


def describe(key: str) -> Badge | None:
    return BADGES.get(key)


def render(key: str, detail: str | None = None) -> str:
    """One line for a badge list."""
    badge = BADGES.get(key)
    if badge is None:
        return f"• {key}"
    tail = f" — _{detail}_" if detail else ""
    return f"{badge.emoji} **{badge.name}** · {badge.blurb}{tail}"


async def evaluate(db, prediction, guild_id, *, odds: float, weight: float,
                   won: bool) -> list[tuple[str, str | None]]:
    """Which badges this settled pick just earned. Never raises on its own data.

    Returns (key, detail) pairs. Awarding is the caller's job, and awarding is
    idempotent, so a badge already held costs one ignored insert.
    """
    if guild_id is None:
        return []                       # a DM pick belongs to no server's ledger

    earned: list[tuple[str, str | None]] = []
    user = int(prediction["discord_id"])
    tournament = prediction["tournament_id"]
    against = prediction["opponent_team_name"] or "the field"
    team = prediction["predicted_team_name"]

    if won and odds >= UPSET_ODDS:
        earned.append(("giant_killer", f"{team} over {against}, ×{odds:.1f}"))
    if won and weight >= 2.0:
        earned.append(("kingmaker", f"{team} in the final"))

    stats = await db.guild_stats(user, guild_id)
    if int(stats["points"]) >= CENTURION_POINTS:
        earned.append(("centurion", f"{int(stats['points'])} points"))
    if int(stats["settled"]) + int(stats["open"]) >= MARATHON_PICKS:
        earned.append(("marathon", None))

    if won and await db.was_only_caller(int(prediction["match_id"]), guild_id, user):
        earned.append(("lone_wolf", f"{team} over {against}"))

    if won and tournament is not None:
        tokens = await db.token_record(user, guild_id, int(tournament))
        if (int(tokens["spent"]) >= TOKENS_PER_TOURNAMENT
                and int(tokens["won"]) >= TOKENS_PER_TOURNAMENT):
            earned.append(("all_in", prediction["tournament_name"]))

    if won and await db.perfect_week(user, guild_id, PERFECT_WEEK_MINIMUM):
        earned.append(("perfect_week", None))

    return earned
