"""`/tournament` — what an event is, and what's on next.

Uses the same game → tournament autocomplete as everything else. Naming a
tournament is optional: without one, the game's current Tier 1 events are
offered as a picker, which is usually faster than typing anyway.

The overview and the schedule both come from the shared tournament directory,
so this costs one cached call rather than a fresh fetch per invocation.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.choices import GAME_CHOICES
from ..utils.games import ALL_GAME_KEYS, label_for
from ..utils.guildgames import blocked_message, no_games_message
from ..utils.pickers import game_of, tournament_choices
from ..utils.tournamentcard import build_tournament_card
from ..utils.tournaments import tournament_label

log = logging.getLogger("aurorabot.cogs.tournaments")

MAX_OPTIONS = 25


class TournamentSelect(discord.ui.Select):
    """Pick from the game's current Tier 1 events."""

    def __init__(self, cog: "Tournaments", tournaments: list[dict], game_key: str):
        self.cog = cog
        self.game_key = game_key
        self._by_id = {str(t["id"]): t for t in tournaments[:MAX_OPTIONS]}
        options = []
        for t in tournaments[:MAX_OPTIONS]:
            league = (t.get("league") or {}).get("name") or ""
            options.append(
                discord.SelectOption(
                    label=tournament_label(t)[:100],
                    value=str(t["id"]),
                    description=(t.get("region") or league)[:100] or None,
                )
            )
        super().__init__(placeholder="Pick a tournament…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        tournament = self._by_id[self.values[0]]
        embed = await build_tournament_card(self.cog.bot, tournament, self.game_key)
        await interaction.followup.send(embed=embed)


class Tournaments(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _tournament_ac(self, interaction, current):
        return await tournament_choices(
            self.bot.tourneys, game_of(interaction), current
        )

    @app_commands.command(
        name="tournament",
        description="Tournament info and its next matches.",
    )
    @app_commands.describe(
        game="Which game. Defaults to your `/setgame` pick.",
        tournament="Which event. Leave blank to pick from what's running.",
    )
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac)
    async def tournament(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        enabled = (
            await self.bot.db.enabled_games(interaction.guild_id)
            if interaction.guild_id
            else set(ALL_GAME_KEYS)
        )
        if not enabled:
            await interaction.followup.send(no_games_message())
            return

        if game is not None:
            game_key = game.value
            if game_key not in enabled:
                await interaction.followup.send(blocked_message(game_key))
                return
        else:
            game_key = await self.bot.db.favorite_game(interaction.user.id)
            if game_key is None or game_key not in enabled:
                await interaction.followup.send(
                    "Pick a game — or set a default once with `/setgame`."
                )
                return

        if tournament:
            target = await self.bot.tourneys.resolve(tournament, game_key)
            if target is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current "
                    f"{label_for(game_key)} league or tournament."
                )
                return
            found = await self._find(game_key, target)
            if not found:
                await interaction.followup.send(
                    f"Nothing current under **{target['name']}**."
                )
                return
            if len(found) == 1:
                await interaction.followup.send(
                    embed=await build_tournament_card(self.bot, found[0], game_key)
                )
                return
            # A league can be running several stages at once — LCK splits into
            # a Legend and a Rise group — so offer them rather than guessing.
            view = discord.ui.View(timeout=180)
            view.add_item(TournamentSelect(self, found, game_key))
            await interaction.followup.send(
                f"**{len(found)}** stages running under **{target['name']}**",
                view=view,
            )
            return

        current = await self.bot.tourneys.current(game_key) or []
        if not current:
            await interaction.followup.send(
                f"No current Tier 1 {label_for(game_key)} tournaments."
            )
            return
        if len(current) == 1:
            await interaction.followup.send(
                embed=await build_tournament_card(self.bot, current[0], game_key)
            )
            return
        view = discord.ui.View(timeout=180)
        view.add_item(TournamentSelect(self, current, game_key))
        await interaction.followup.send(
            f"**{len(current)}** current {label_for(game_key)} tournament(s)",
            view=view,
        )

    async def _find(self, game_key: str, target: dict) -> list[dict]:
        """The tournaments a resolved league or stage refers to."""
        current = await self.bot.tourneys.current(game_key) or []
        if target["scope"] == "tournament":
            return [t for t in current if int(t["id"]) == target["id"]]
        return [
            t for t in current
            if int((t.get("league") or {}).get("id") or 0) == target["id"]
        ]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tournaments(bot))
