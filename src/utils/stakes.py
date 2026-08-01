"""The stake picker shown after a pick.

Deliberately *optional*. Tapping a team is still one tap and stakes the default
automatically — this panel rides along on the ephemeral confirmation for anyone
who wants to commit more. Members who ignore it play a complete game; members
who use it get the triage that makes a budget interesting.

The panel is short-lived and private, so it needs no persistence: re-tapping the
team button brings it back.
"""
from __future__ import annotations

import logging

import discord

from .predictions import is_open
from .scoring import TOURNAMENT_BUDGET, allowed_stakes

log = logging.getLogger("aurorabot.stakes")


class StakeSelect(discord.ui.Select):
    def __init__(self, prediction_id: int, current: int, options: list[int]) -> None:
        self.prediction_id = prediction_id
        super().__init__(
            placeholder="Change your stake…",
            options=[
                discord.SelectOption(
                    label=f"{s} points",
                    value=str(s),
                    default=s == current,
                    description="Your current stake" if s == current else None,
                )
                for s in options
            ],
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        stake = int(self.values[0])
        row = await db.get_prediction_by_id(self.prediction_id)
        if row is None or row["status"] != "open":
            await interaction.response.edit_message(
                content="That pick is already settled — the stake is locked.",
                view=None,
            )
            return
        if not is_open(row["match_starts_at"]):
            await interaction.response.edit_message(
                content="That match has started — the stake is locked. 🔒", view=None
            )
            return

        # Re-check affordability at click time: another pick may have been made
        # since the panel was drawn.
        spent = await db.tournament_spend(
            int(row["discord_id"]), row["guild_id"], row["tournament_id"]
        )
        spent_elsewhere = spent - int(row["stake"])
        if stake not in allowed_stakes(spent_elsewhere):
            await interaction.response.edit_message(
                content=f"You can't afford **{stake}** any more — "
                f"**{max(0, TOURNAMENT_BUDGET - spent_elsewhere)}** left.",
                view=None,
            )
            return

        await db.set_stake(self.prediction_id, stake)
        left = max(0, TOURNAMENT_BUDGET - spent_elsewhere - stake)
        await interaction.response.edit_message(
            content=f"💰 Staked **{stake}** on **{row['predicted_team_name']}** · "
            f"**{left}** left this tournament.",
            view=None,
        )


class StakeView(discord.ui.View):
    def __init__(self, prediction_id: int, current: int, spent_elsewhere: int) -> None:
        super().__init__(timeout=120)
        options = [s for s in allowed_stakes(spent_elsewhere) if s != current]
        if options:
            self.add_item(StakeSelect(prediction_id, current, options + [current]))


async def stake_panel(db, user_id: int, match_id: int) -> StakeView | None:
    """A stake picker for this member's pick on this match, if it's adjustable."""
    try:
        row = await db.get_prediction(user_id, match_id)
        if row is None or row["status"] != "open" or row["tournament_id"] is None:
            return None
        spent = await db.tournament_spend(
            user_id, row["guild_id"], row["tournament_id"]
        )
        view = StakeView(int(row["id"]), int(row["stake"]), spent - int(row["stake"]))
    except Exception:  # noqa: BLE001 - the pick is already saved; this is extra
        log.exception("could not build the stake panel for %s", user_id)
        return None
    return view if view.children else None
