"""Shared prediction rules, used by both `/predict` and alert reactions.

Two entry points create predictions — the `/predict` button flow and reacting
to a match-reminder alert — and they must agree on the stake, the reward and
when the book closes. That logic lives here so neither can drift.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from .scoring import (
    DEFAULT_STAKE,
    FREE_STAKE,
    TOURNAMENT_BUDGET,
    clamp_stake,
)
from .tournaments import parse_dt

# Kept for the resolver's fallback path when a match had too few voters to
# price; the live model is in utils.scoring.
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
    tournament_id: int | None = None,
    tournament_name: str | None = None,
) -> tuple[Outcome, str]:
    """Record (or amend) a user's pick. Returns the outcome and a message.

    A new pick opens at the default stake if the tournament budget can afford
    it, otherwise at the largest that fits — down to :data:`FREE_STAKE`, so
    running out of budget never blocks a prediction. The stake can be raised
    afterwards from the ephemeral panel; see ``utils.stakes``.
    """
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
        spent = await db.tournament_spend(user_id, guild_id, tournament_id)
        stake = clamp_stake(DEFAULT_STAKE, spent)
        await db.create_prediction(
            discord_id=user_id,
            match_id=match_id,
            game=game,
            predicted_team_id=team["id"],
            predicted_team_name=team["name"],
            opponent_team_name=opponent_name,
            match_starts_at=begin_at,
            stake=stake,
            guild_id=guild_id,
            tournament_id=tournament_id,
            tournament_name=tournament_name,
        )
        left = max(0, TOURNAMENT_BUDGET - spent - stake)
        note = (
            f"Staked **{stake}** · **{left}** left this tournament."
            if tournament_id
            else f"Staked **{stake}**."
        )
        if stake == FREE_STAKE and tournament_id:
            note = (
                f"Budget's gone, so this rides at the free **{FREE_STAKE}** — "
                f"still counts, just worth less."
            )
        return (
            Outcome.CREATED,
            f"🎲 You backed **{team['name']}**. {note}",
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
        f"🔄 Switched your pick to **{team['name']}** — stake unchanged.",
    )
