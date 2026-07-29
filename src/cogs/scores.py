"""Live & recent scores commands.

Every feed here is limited to **Tier 1** tournaments (PandaScore S- and
A-tier by default): the API is over-fetched and then filtered client-side (see
``utils.tiers``), because PandaScore has no server-side tier filter on the
match endpoints.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import RED, match_embed
from ..utils.games import resolve_slug
from ..utils.guildgames import blocked_message, filter_enabled
from ..utils.tiers import NO_TOP_TIER_MATCHES, filter_for

log = logging.getLogger("aurorabot.cogs.scores")

# How many raw matches to pull before tier filtering, and how many survivors to
# render (Discord caps a message at 10 embeds).
FETCH_SIZE = 50
SHOW = 5


class Scores(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _slug(self, game_key: str) -> str:
        return resolve_slug(game_key, self.bot.settings.cs_slug)

    def _top_tier(self, matches: list[dict]) -> list[dict]:
        return filter_for(self.bot.settings, matches)

    async def _gate(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None
    ) -> tuple[bool, set[str]]:
        """Apply this server's game toggles.

        Returns ``(allowed, disabled)``. When the user named a muted game the
        caller should bail out — ``_gate`` has already replied explaining why.
        """
        disabled = await self.bot.db.disabled_games(interaction.guild_id)
        if game and game.value in disabled:
            await interaction.followup.send(blocked_message(game.value))
            return False, disabled
        return True, disabled

    def _for_guild(self, matches: list[dict], disabled: set[str]) -> list[dict]:
        return filter_enabled(matches, disabled, self.bot.settings.cs_slug)

    @app_commands.command(
        name="live", description="Show Tier 1 matches happening right now."
    )
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def live(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        allowed, disabled = await self._gate(interaction, game)
        if not allowed:
            return
        slug = self._slug(game.value) if game else None
        try:
            matches = self._for_guild(
                self._top_tier(
                    await self.bot.api.running_matches(slug=slug, per_page=FETCH_SIZE)
                ),
                disabled,
            )
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
                "No live Tier 1 matches right now. Try `/upcoming` to see what's next. 🎮"
            )
            return

        embeds = [match_embed(m, game.value if game else None) for m in matches[:SHOW]]
        await interaction.followup.send(
            content=f"🔴 **{len(matches)} live Tier 1 match(es)**", embeds=embeds
        )

    @app_commands.command(name="upcoming", description="Show upcoming Tier 1 matches.")
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def upcoming(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        allowed, disabled = await self._gate(interaction, game)
        if not allowed:
            return
        slug = self._slug(game.value) if game else None
        try:
            matches = self._for_guild(
                self._top_tier(
                    await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
                ),
                disabled,
            )
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send(NO_TOP_TIER_MATCHES)
            return
        embeds = [match_embed(m, game.value if game else None) for m in matches[:SHOW]]
        await interaction.followup.send(content="🗓️ **Upcoming · Tier 1**", embeds=embeds)

    @app_commands.command(
        name="results", description="Show recent finished Tier 1 matches."
    )
    @app_commands.describe(game="Filter by a specific game (optional).")
    @app_commands.choices(game=GAME_CHOICES)
    async def results(
        self, interaction: discord.Interaction, game: app_commands.Choice[str] | None = None
    ) -> None:
        await interaction.response.defer(thinking=True)
        allowed, disabled = await self._gate(interaction, game)
        if not allowed:
            return
        slug = self._slug(game.value) if game else None
        try:
            matches = self._for_guild(
                self._top_tier(
                    await self.bot.api.past_matches(slug=slug, per_page=FETCH_SIZE)
                ),
                disabled,
            )
        except PandaScoreError:
            await interaction.followup.send("The scores service is unavailable right now.")
            return
        if not matches:
            await interaction.followup.send(NO_TOP_TIER_MATCHES)
            return
        embeds = [match_embed(m, game.value if game else None) for m in matches[:SHOW]]
        await interaction.followup.send(content="✅ **Recent results · Tier 1**", embeds=embeds)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Scores(bot))
