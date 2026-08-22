"""Live & recent scores, filtered game → tournament → team.

Each level's options are scoped by the one above it, so picking LCK narrows the
team list to LCK's rosters. Only the game is required — and even that falls back
to a personal `/setgame` default — so a quick "what's on in LoL" still works
without naming a tournament.

Every feed is limited to **Tier 1** tournaments (PandaScore S- and A-tier by
default): the API is over-fetched and filtered client-side, because PandaScore
has no server-side tier filter on the match endpoints.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..services.tourneys import match_has_team, match_in_target
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import RED
from ..utils.games import ALL_GAME_KEYS, label_for, resolve_slug
from ..utils.guildgames import blocked_message, filter_enabled, no_games_message
from ..utils.guildprefs import digest_tz
from ..utils.livecard import build_live_card
from ..utils.upcominglist import build_upcoming_list
from ..utils.resultcard import build_result_card
from ..utils.pickers import game_of, team_choices, tournament_choices
from ..utils.tiers import NO_TOP_TIER_MATCHES, filter_for

log = logging.getLogger("aurorabot.cogs.scores")

# How many raw matches to pull before filtering, and how many survivors to
# render (Discord caps a message at 10 embeds).
FETCH_SIZE = 50
SHOW = 5

DESCRIBE = {
    "game": "Which game. Defaults to your `/setgame` pick.",
    "tournament": "Narrow to a league or one of its stages.",
    "team": "Narrow to one team in that tournament.",
}


class Filters:
    """The resolved game → tournament → team selection for one invocation."""

    def __init__(self, game_key, enabled, target=None, team=None):
        self.game_key = game_key
        self.enabled = enabled
        self.target = target
        self.team = team

    def apply(self, matches: list[dict]) -> list[dict]:
        return [
            m for m in matches
            if match_in_target(m, self.target) and match_has_team(m, self.team)
        ]

    def describe(self) -> str:
        parts = [label_for(self.game_key)] if self.game_key else []
        if self.target:
            parts.append(self.target["name"])
        if self.team:
            parts.append(self.team["name"])
        return " · ".join(parts)


class Scores(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _slug(self, game_key: str) -> str:
        return resolve_slug(game_key, self.bot.settings.cs_slug)

    async def _tournament_ac(self, interaction, current):
        return await tournament_choices(
            self.bot.tourneys, game_of(interaction), current
        )

    async def _team_ac(self, interaction, current):
        return await team_choices(
            self.bot.tourneys,
            game_of(interaction),
            getattr(interaction.namespace, "tournament", None),
            current,
        )

    async def _resolve(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None,
        tournament: str | None,
        team: str | None,
    ) -> Filters | None:
        """Resolve the cascade, or reply explaining why it can't and return None.

        Order matters: the server's game selection gates everything, the game
        scopes the tournament, and the tournament scopes the team.
        """
        # A DM has no server selection to honour, and no channel to keep tidy.
        enabled = (
            await self.bot.db.enabled_games(interaction.guild_id)
            if interaction.guild_id
            else set(ALL_GAME_KEYS)
        )
        if not enabled:
            await interaction.followup.send(no_games_message())
            return None

        if game is not None:
            game_key = game.value
            if game_key not in enabled:
                await interaction.followup.send(blocked_message(game_key))
                return None
        else:
            # `/setgame` exists to be the default here, so an explicit game is
            # only *required* when there's no default to fall back on.
            game_key = await self.bot.db.favorite_game(interaction.user.id)
            if game_key is None or game_key not in enabled:
                await interaction.followup.send(
                    "Pick a game — or set a default once with `/setgame` and "
                    "these commands will use it from then on."
                )
                return None

        target = None
        if tournament:
            target = await self.bot.tourneys.resolve(tournament, game_key)
            if target is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current "
                    f"{label_for(game_key)} league or tournament. Start typing "
                    "and pick one from the list."
                )
                return None

        picked = None
        if team:
            picked = await self.bot.tourneys.resolve_team(team, game_key, target)
            if picked is None:
                where = f" in {target['name']}" if target else ""
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}**{where}."
                )
                return None

        return Filters(game_key, enabled, target, picked)

    async def _feed(self, fetch, filters: Filters) -> list[dict]:
        raw = await fetch(slug=self._slug(filters.game_key), per_page=FETCH_SIZE)
        matches = filter_for(self.bot.settings, raw)
        matches = filter_enabled(matches, filters.enabled, self.bot.settings.cs_slug)
        return filters.apply(matches)

    def _empty(self, filters: Filters, what: str) -> str:
        scope = filters.describe()
        return f"No {what} for **{scope}** right now."

    # ── commands ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="live", description="Show Tier 1 matches happening right now."
    )
    @app_commands.describe(**DESCRIBE)
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac, team=_team_ac)
    async def live(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        filters = await self._resolve(interaction, game, tournament, team)
        if filters is None:
            return
        try:
            matches = await self._feed(self.bot.api.running_matches, filters)
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
                f"{self._empty(filters, 'live matches')} "
                "Try `/upcoming` to see what's next. 🎮"
            )
            return

        embeds = await asyncio.gather(
            *(build_live_card(self.bot, m, filters.game_key, interaction.guild_id)
              for m in matches[:SHOW])
        )
        await interaction.followup.send(
            content=f"🔴 **{len(matches)} live** · {filters.describe()}",
            embeds=list(embeds),
        )

    @app_commands.command(name="upcoming", description="Show upcoming Tier 1 matches.")
    @app_commands.describe(**DESCRIBE)
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac, team=_team_ac)
    async def upcoming(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        filters = await self._resolve(interaction, game, tournament, team)
        if filters is None:
            return
        try:
            matches = await self._feed(self.bot.api.upcoming_matches, filters)
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send(
                self._empty(filters, "upcoming matches") + f"\n{NO_TOP_TIER_MATCHES}"
            )
            return
        # One list, one shape. This used to render the soonest three as full
        # lineup cards and the rest as compact embeds, which buried the short
        # half under the tall one; rosters are what /lineup is for.
        tz = digest_tz(
            self.bot.settings,
            await self.bot.db.guild_settings(interaction.guild_id)
            if interaction.guild_id else None,
        )
        embed = build_upcoming_list(
            self.bot, matches, filters.describe() or "all games", tz
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(
        name="results", description="Show recent finished Tier 1 matches."
    )
    @app_commands.describe(**DESCRIBE)
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac, team=_team_ac)
    async def results(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        filters = await self._resolve(interaction, game, tournament, team)
        if filters is None:
            return
        try:
            matches = await self._finished(filters)
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send(self._empty(filters, "recent results"))
            return
        # A summary per match rather than a scoreline: /results is the one
        # command where what happened *is* the content.
        embeds = await asyncio.gather(
            *(build_result_card(self.bot, m, filters.game_key, interaction.guild_id)
              for m in matches[:SHOW])
        )
        await interaction.followup.send(
            content=f"✅ **Recent results** · {filters.describe()}",
            embeds=list(embeds),
        )

    async def _finished(self, filters: Filters) -> list[dict]:
        """Recent Tier 1 results, newest first.

        Sourced from the current Tier 1 tournaments rather than
        ``/matches/past``. PandaScore has no server-side tier filter, so the
        generic feed has to be filtered client-side over one page — and for
        Counter-Strike that page is *entirely* tier C/D, because the volume of
        ESEA/CCT matches buries every event anyone would ask about. Measured:
        the past feed yielded 0 Tier 1 CS matches out of 100, while BLAST
        Bounty's playoffs had five finished that same week.

        The generic feed stays as a fallback for the case where no Tier 1
        tournament is currently listed — an off-season week still shows the
        last thing that happened.
        """
        matches = await self.bot.tourneys.matches(
            filters.game_key, filters.target, status="finished"
        )
        matches = filter_enabled(
            filter_for(self.bot.settings, matches),
            filters.enabled,
            self.bot.settings.cs_slug,
        )
        matches = filters.apply(matches)

        if not matches:
            matches = await self._feed(self.bot.api.past_matches, filters)

        # end_at is often unset — PandaScore leaves it null on whole games —
        # so fall back to the kick-off time rather than sorting everything
        # unset to one end.
        matches.sort(
            key=lambda m: str(m.get("end_at") or m.get("begin_at") or ""),
            reverse=True,
        )
        return matches


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scores(bot))
