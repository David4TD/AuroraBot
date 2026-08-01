"""Shared prediction rules, used by both `/predict` and alert reactions.

Two entry points create predictions — the `/predict` button flow and reacting
to a match-reminder alert — and they must agree on the stake, the reward and
when the book closes. That logic lives here so neither can drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from .tournaments import parse_dt

DEFAULT_STAKE = 10
WIN_REWARD = 25


class Outcome(str, Enum):
    CREATED = "created"      # first pick on this match
    CHANGED = "changed"      # swapped to the other team before kick-off
    UNCHANGED = "unchanged"  # re-picked the team they already had
    CLOSED = "closed"        # match already started or finished


def is_open(begin_at: str | None, *, now: datetime | None = None) -> bool:
    """Predictions close the moment a match is due to start.

    A missing ``begin_at`` is treated as open: PandaScore leaves it unset on
    some scheduled matches, and refusing those would silently break `/predict`.
    """
    begin = parse_dt(begin_at)
    if begin is None:
        return True
    return begin > (now or datetime.now(timezone.utc))


async def submit_prediction(
    db,
    *,
    user_id: int,
    display_name: str,
    match_id: int,
    game: str | None,
    team: dict,
    opponent: dict | None,
    begin_at: str | None,
    guild_id: int | None = None,
) -> tuple[Outcome, str]:
    """Record (or amend) a user's pick. Returns the outcome and a message."""
    existing = await db.get_prediction(user_id, match_id)

    if not is_open(begin_at):
        if existing is not None:
            return (
                Outcome.CLOSED,
                f"That match has started — your pick of "
                f"**{existing['predicted_team_name']}** is locked in. 🔒",
            )
        return Outcome.CLOSED, "That match has already started — predictions are closed. 🔒"

    opponent_name = opponent["name"] if opponent else None

    if existing is None:
        await db.ensure_user(user_id, display_name)
        await db.create_prediction(
            discord_id=user_id,
            match_id=match_id,
            game=game,
            predicted_team_id=team["id"],
            predicted_team_name=team["name"],
            opponent_team_name=opponent_name,
            match_starts_at=begin_at,
            stake=DEFAULT_STAKE,
            guild_id=guild_id,
        )
        return (
            Outcome.CREATED,
            f"🎲 You predicted **{team['name']}** to win. "
            f"Call it right for **+{WIN_REWARD}** points!",
        )

    if int(existing["predicted_team_id"]) == team["id"]:
        return (
            Outcome.UNCHANGED,
            f"You've already got **{team['name']}** for that match. 👍",
        )

    if existing["status"] != "open":
        return (
            Outcome.CLOSED,
            f"That prediction is already settled (**{existing['status']}**).",
        )

    await db.update_prediction_team(
        prediction_id=existing["id"],
        predicted_team_id=team["id"],
        predicted_team_name=team["name"],
        opponent_team_name=opponent_name,
    )
    return (
        Outcome.CHANGED,
        f"🔄 Switched your pick to **{team['name']}**.",
    )
