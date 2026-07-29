"""Deep team analytics commands."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import analytics_embed
from ..utils.games import rank_by_name, resolve_slug

log = logging.getLogger("aurorabot.cogs.analytics")


class Analytics(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="team", description="Deep analytics for a team: form, win rate, roster."
    )
    @app_commands.describe(
        name="Team name, e.g. 'G2 Esports', 'FaZe', 'Sentinels'.",
        game="Game the team plays (improves matching).",
    )
    @app_commands.choices(game=GAME_CHOICES)
    async def team(
        self,
        interaction: discord.Interaction,
        name: str,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug) if game else None
        try:
            teams = await self.bot.api.search_teams(name, slug=slug, per_page=5)
        except PandaScoreError:
            await interaction.followup.send("The analytics service is unavailable right now.")
            return
        if not teams:
            await interaction.followup.send(f"No team matched **{name}**.")
            return

        team = rank_by_name(teams, name)[0]
        try:
            full = await self.bot.api.get_team(team["id"])
            recent = await self.bot.api.team_matches(team["id"], per_page=12)
        except PandaScoreError:
            full, recent = team, []

        # Only keep finished matches for form/record.
        finished = [m for m in recent if (m.get("status") or "").lower() == "finished"]
        embed = analytics_embed(full or team, finished, game.value if game else None)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Analytics(bot))
