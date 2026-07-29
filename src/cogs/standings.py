"""League standings commands.

Flow:
  /standings league:<name> game:<game>
    → rank matching leagues (exact name wins over 'Academy'/'Challengers')
    → if one clear match, jump straight to its tournaments
    → otherwise show a league picker, then a season/split picker
    → render the standings table
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, standings_embed
from ..utils.games import rank_by_name, resolve_slug

log = logging.getLogger("aurorabot.cogs.standings")


async def _show_tournaments(
    cog: "Standings",
    interaction: discord.Interaction,
    league: dict,
    *,
    edit: bool,
) -> None:
    """Fetch a league's tournaments and present the season/split picker."""
    try:
        tournaments = await cog.bot.api.league_tournaments(league["id"], per_page=25)
    except PandaScoreError:
        msg = "Couldn't load that league's tournaments."
        if edit:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg)
        return

    if not tournaments:
        msg = f"Found **{league.get('name')}** but it has no tournaments listed."
        if edit:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg)
        return

    embed = discord.Embed(
        title=f"🏆 {league.get('name')}",
        description="Choose a season / split below to see its standings table.",
        color=BRAND,
    )
    if league.get("image_url"):
        embed.set_thumbnail(url=league["image_url"])
    view = TournamentView(cog, tournaments)
    if edit:
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view)


class LeagueSelect(discord.ui.Select):
    def __init__(self, cog: "Standings", leagues: list[dict]) -> None:
        self.cog = cog
        self._leagues = {str(l["id"]): l for l in leagues[:25]}
        options = []
        for l in leagues[:25]:
            region = l.get("region") or ""
            options.append(
                discord.SelectOption(
                    label=str(l.get("name", "League"))[:100],
                    value=str(l["id"]),
                    description=region[:100] or None,
                )
            )
        super().__init__(placeholder="Which league did you mean?", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        league = self._leagues[self.values[0]]
        await _show_tournaments(self.cog, interaction, league, edit=True)


class LeagueView(discord.ui.View):
    def __init__(self, cog: "Standings", leagues: list[dict]) -> None:
        super().__init__(timeout=120)
        self.add_item(LeagueSelect(cog, leagues))


class TournamentSelect(discord.ui.Select):
    def __init__(self, cog: "Standings", tournaments: list[dict]) -> None:
        self.cog = cog
        options = []
        for t in tournaments[:25]:
            serie = (t.get("serie") or {}).get("full_name") or ""
            name = t.get("name", "Stage")
            label = f"{serie} · {name}".strip(" ·")[:100] or "Tournament"
            year = (t.get("begin_at") or "")[:4]
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(t["id"]),
                    description=(f"{year}" if year else None),
                )
            )
        super().__init__(
            placeholder="Pick a season / split to view standings…",
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
                "PandaScore has no standings table for that tournament "
                "(common for bracket-only playoff stages — try the group/regular-season stage instead).",
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
            leagues = await self.bot.api.search_leagues(league, slug=slug, per_page=25)
        except PandaScoreError:
            await interaction.followup.send("The standings service is unavailable right now.")
            return
        if not leagues:
            await interaction.followup.send(
                f"No league matched **{league}**. Try a shorter name like `LEC` or `VCT`."
            )
            return

        ranked = rank_by_name(leagues, league)
        top_name = str(ranked[0].get("name") or "").strip().lower()
        exact = top_name == league.strip().lower()

        # One clear/exact hit → skip straight to its tournaments.
        if len(ranked) == 1 or exact:
            await _show_tournaments(self, interaction, ranked[0], edit=False)
            return

        # Ambiguous → let the user pick the right league first.
        embed = discord.Embed(
            title=f"🔎 Leagues matching “{league}”",
            description="Several leagues matched — pick the one you meant.",
            color=BRAND,
        )
        await interaction.followup.send(embed=embed, view=LeagueView(self, ranked))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Standings(bot))
