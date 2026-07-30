"""League standings commands.

Flow:
  /standings league:<name> game:<game>
    → rank matching leagues (exact name wins over 'Academy'/'Challengers')
    → if one clear match, jump straight to its tournaments
    → otherwise show a league picker, then a season/split picker
    → render the standings table

The season/split picker only offers **current** tournaments (running now, or
starting within the week) at the configured tiers — historical splits are
filtered out rather than padding the dropdown.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, standings_embed
from ..utils.games import rank_by_name, resolve_slug
from ..utils.guildgames import blocked_message
from ..utils.regions import event_flag, region_flag, region_label
from ..utils.tiers import describe, filter_for, tier_label
from ..utils.tournaments import current_tournaments, parse_dt, tournament_label

log = logging.getLogger("aurorabot.cogs.standings")


async def _show_tournaments(
    cog: "Standings",
    interaction: discord.Interaction,
    league: dict,
    *,
    edit: bool,
) -> None:
    """Fetch a league's *current* tournaments and present the split picker."""
    try:
        tournaments = await cog.bot.api.league_tournaments(league["id"], per_page=50)
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

    # Current tournaments only, and only at the tier AuroraBot covers. The two
    # filters are applied separately so the "nothing found" message can say
    # which one emptied the list.
    settings = cog.bot.settings
    live = current_tournaments(tournaments)
    eligible = filter_for(settings, live)

    log.debug(
        "standings %s: %d tournaments → %d current → %d in %s",
        league.get("name"), len(tournaments), len(live), len(eligible), describe(settings),
    )

    if not eligible:
        if live:
            # Current events exist but sit below the tier bar — name them, so
            # it's obvious this is the filter and not missing data.
            found = ", ".join(
                f"{tournament_label(t, max_len=40)} ({tier_label(t)})" for t in live[:3]
            )
            msg = (
                f"**{league.get('name')}** is running, but nothing matches your tier "
                f"filter (**{describe(settings)}**).\nCurrently on: {found}.\n"
                f"Widen it with `TIERS=s,a,b` or set `TOP_TIER_ONLY=false`."
            )
        else:
            msg = (
                f"**{league.get('name')}** has no tournament running at the moment "
                f"— standings appear once the next split is under way."
            )
        if edit:
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg)
        return
    tournaments = eligible

    name = league.get("name")
    flag = region_flag(name)
    region = region_label(name)
    embed = discord.Embed(
        title=f"🏆 {flag + ' ' if flag else ''}{name}",
        description="Choose a current season / split below to see its standings table.",
        color=BRAND,
    )
    if region:
        embed.add_field(name="Region", value=f"{flag} {region}", inline=True)
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
            name = str(l.get("name", "League"))
            # PandaScore has no region field on leagues (the old l["region"]
            # read here was always None), so derive it from the name.
            flag = region_flag(name)
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(l["id"]),
                    description=region_label(name),
                    emoji=flag,
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
        # Kept so the standings embed can show the event's region flag.
        self._tournaments = {str(t["id"]): t for t in tournaments[:25]}
        now = datetime.now(timezone.utc)
        options = []
        for t in tournaments[:25]:
            begin = parse_dt(t.get("begin_at"))
            if begin is None:
                hint = tier_label(t)
            elif begin <= now:
                hint = f"running now · {tier_label(t)}"
            else:
                hint = f"starts {begin:%d %b} · {tier_label(t)}"
            options.append(
                discord.SelectOption(
                    label=tournament_label(t),
                    value=str(t["id"]),
                    description=hint,
                    emoji=event_flag(t),
                )
            )
        super().__init__(
            placeholder="Pick a current season / split to view standings…",
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
        await interaction.followup.send(
            embed=standings_embed(
                label, standings, self._tournaments.get(self.values[0])
            )
        )


class TournamentView(discord.ui.View):
    def __init__(self, cog: "Standings", tournaments: list[dict]) -> None:
        super().__init__(timeout=120)
        self.add_item(TournamentSelect(cog, tournaments))


class Standings(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="standings", description="Standings for a league's current tournaments."
    )
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
        if game and game.value in await self.bot.db.disabled_games(interaction.guild_id):
            await interaction.followup.send(blocked_message(game.value))
            return
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
