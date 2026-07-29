"""League standings commands."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, standings_embed
from ..utils.games import resolve_slug

log = logging.getLogger("aurorabot.cogs.standings")


class TournamentSelect(discord.ui.Select):
    def __init__(self, cog: "Standings", tournaments: list[dict]) -> None:
        self.cog = cog
        options = []
        for t in tournaments[:25]:
            league = (t.get("league") or {}).get("name", "")
            name = t.get("name", "Stage")
            label = f"{league} · {name}".strip(" ·")[:100] or "Tournament"
            options.append(
                discord.SelectOption(label=label[:100], value=str(t["id"]))
            )
        super().__init__(
            placeholder="Pick a tournament to view its standings…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        tid = int(self.values[0])
        label = next((o.label for o in self.options if o.value == self.values[0]), "Tournament")
        try:
            standings = await self.cog.bot.api.tournament_standings(tid)
        except PandaScoreError:
            await interaction.followup.send("Couldn't load standings for that tournament.", ephemeral=True)
            return
        if not standings:
            await interaction.followup.send(
                "PandaScore has no standings table for that tournament (common for bracket-only events).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=standings_embed(label, standings))


class TournamentView(discord.ui.View):
    def __init__(self, cog: "Standings", tournaments: list[dict]) -> None:
        super().__init__(timeout=120)
        self.add_item(TournamentSelect(cog, tournaments))


class Standings(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="standings", description="Look up standings for a league.")
    @app_commands.describe(
        league="League name, e.g. 'LEC', 'LCK', 'VCT', 'RLCS'.",
        game="Game the league belongs to.",
    )
    @app_commands.choices(game=GAME_CHOICES)
    async def standings(
        self,
        interaction: discord.Interaction,
        league: str,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug) if game else None
        try:
            leagues = await self.bot.api.search_leagues(league, slug=slug, per_page=5)
        except PandaScoreError:
            await interaction.followup.send("The standings service is unavailable right now.")
            return
        if not leagues:
            await interaction.followup.send(
                f"No league matched **{league}**. Try a shorter name like `LEC` or `VCT`."
            )
            return

        target = leagues[0]
        try:
            tournaments = await self.bot.api.league_tournaments(target["id"], per_page=25)
        except PandaScoreError:
            await interaction.followup.send("Couldn't load that league's tournaments.")
            return
        if not tournaments:
            await interaction.followup.send(
                f"Found **{target.get('name')}** but it has no tournaments listed."
            )
            return

        embed = discord.Embed(
            title=f"🏆 {target.get('name')}",
            description="Choose a season / split below to see its standings table.",
            color=BRAND,
        )
        if target.get("image_url"):
            embed.set_thumbnail(url=target["image_url"])
        await interaction.followup.send(
            embed=embed, view=TournamentView(self, tournaments)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Standings(bot))
