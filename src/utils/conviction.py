"""The double-down button shown after a pick.

Deliberately *optional*. Tapping a team is still one tap and counts for full
value — this panel rides along on the ephemeral confirmation for anyone who
wants to commit a token. Members who ignore it play a complete game and can
never lose a point; members who use it take the only real risk in the format.

A token doubles the match if the call lands and costs the base back if it
doesn't, so the copy here always names both halves. A button that only
advertises the upside would be lying about what it does.

Reclaimable right up until kick-off, then locked: changing your mind about a
match that hasn't started is reading, not gaming the system. Once it starts,
the token is spent whatever happens.

The panel is short-lived and private, so it needs no persistence: re-tapping
the team button brings it back.
"""
from __future__ import annotations

import logging

import discord

from .predictions import is_open
from .scoring import (
    DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, TOKENS_PER_TOURNAMENT, tokens_left,
)

log = logging.getLogger("aurorabot.conviction")


class DoubleDownButton(discord.ui.Button):
    def __init__(self, prediction_id: int, doubled: bool, left: int) -> None:
        self.prediction_id = prediction_id
        self.doubled = doubled
        super().__init__(
            label=("Take the double down back" if doubled
                   else f"Double down ({left} left)"),
            style=(discord.ButtonStyle.secondary if doubled
                   else discord.ButtonStyle.success),
            emoji="💥",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        row = await db.get_prediction_by_id(self.prediction_id)
        if row is None or row["status"] != "open":
            await interaction.response.edit_message(
                content="That pick is already settled — tokens are locked.",
                view=None,
            )
            return
        if not is_open(row["match_starts_at"]):
            await interaction.response.edit_message(
                content="That match has started — your token is locked in. 🔒",
                view=None,
            )
            return

        want = not self.doubled
        if want:
            # Re-check at click time: another pick may have spent one since the
            # panel was drawn.
            spent = await db.tokens_spent(
                int(row["discord_id"]), row["guild_id"], row["tournament_id"]
            )
            if tokens_left(spent) <= 0:
                await interaction.response.edit_message(
                    content=f"You've spent all **{TOKENS_PER_TOURNAMENT}** double "
                            f"downs for this tournament. They come back next event.",
                    view=None,
                )
                return

        if not await db.set_doubled(self.prediction_id, want):
            await interaction.response.edit_message(
                content="That match just started — your pick is locked. 🔒",
                view=None,
            )
            return

        spent = await db.tokens_spent(
            int(row["discord_id"]), row["guild_id"], row["tournament_id"]
        )
        left = tokens_left(spent)
        if want:
            content = (
                f"💥 Doubled down on **{row['predicted_team_name']}** — "
                f"**{DOUBLE_DOWN}×** if it lands, "
                f"**−{DOUBLE_DOWN_PENALTY}** if it doesn't. "
                f"**{left}** left this tournament."
            )
        else:
            content = f"Token back — **{left}** left this tournament."
        await interaction.response.edit_message(
            content=content, view=await _view(db, row["id"], want, left)
        )


class ConvictionView(discord.ui.View):
    def __init__(self, prediction_id: int, doubled: bool, left: int) -> None:
        super().__init__(timeout=120)
        # Nothing to offer once the tokens are gone and this pick isn't one.
        if doubled or left > 0:
            self.add_item(DoubleDownButton(prediction_id, doubled, left))


async def _view(db, prediction_id: int, doubled: bool, left: int) -> ConvictionView | None:
    view = ConvictionView(int(prediction_id), doubled, left)
    return view if view.children else None


async def pick_panel(db, user_id: int, match_id: int) -> ConvictionView | None:
    """The double-down offer for this member's pick, if it's still available."""
    try:
        row = await db.get_prediction(user_id, match_id)
        if row is None or row["status"] != "open" or row["tournament_id"] is None:
            return None
        if not is_open(row["match_starts_at"]):
            return None
        spent = await db.tokens_spent(
            user_id, row["guild_id"], row["tournament_id"]
        )
        return await _view(db, int(row["id"]), bool(row["doubled"]),
                           tokens_left(spent))
    except Exception:  # noqa: BLE001 - the pick is already saved; this is extra
        log.exception("could not build the conviction panel for %s", user_id)
        return None
