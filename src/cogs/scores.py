"""Live & recent scores commands."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import RED, match_embed
from ..utils.games import resolve_slug

log = logging.getLogger("aurorabot.cogs.scores")


class Scores(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _slug(self, game_key: str) -> str:
        return resolve_slug(game_key, self.bot.settings.cs_slug)

    @app_commands.command(name="live", description="Show matches happening right now.")
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def live(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        slug = self._slug(game.value) if game else None
        try:
            matches = await self.bot.api.running_matches(slug=slug, per_page=8)
        except PandaScoreError as exc:
            log.warning("live failed: %s", exc)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Couldn't reach the scores service",
                    description="PandaScore didn't respond. Try again shortly.",
                    color=RED,
                )
            )
            return

        if not matches:
            await interaction.followup.send(
                "No live matches right now. Try `/upcoming` to see what's next. 🎮"
            )
            return

        embeds = [match_embed(m, game.value if game else None) for m in matches[:5]]
        await interaction.followup.send(
            content=f"🔴 **{len(matches)} live match(es)**", embeds=embeds
        )

    @app_commands.command(name="upcoming", description="Show upcoming matches.")
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def upcoming(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        slug = self._slug(game.value) if game else None
        try:
            matches = await self.bot.api.upcoming_matches(slug=slug, per_page=8)
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send("Nothing scheduled that I can see yet.")
            return
        embeds = [match_embed(m, game.value if game else None) for m in matches[:5]]
        await interaction.followup.send(content="🗓️ **Upcoming**", embeds=embeds)

    @app_commands.command(name="results", description="Show recent finished matches.")
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def results(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        slug = self._slug(game.value) if game else None
        try:
            matches = await self.bot.api.past_matches(slug=slug, per_page=8)
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send("No recent results found.")
            return
        embeds = [match_embed(m, game.value if game else None) for m in matches[:5]]
        await interaction.followup.send(content="✅ **Recent results**", embeds=embeds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scores(bot))
