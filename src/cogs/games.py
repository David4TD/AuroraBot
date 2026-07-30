"""Per-server game selection.

**Games are opt-in.** A fresh server follows nothing, so AuroraBot stays silent
until someone runs `/games` and chooses — no surprise posts about titles the
server doesn't care about.

`/games` shows the current selection. Members with **Manage Server** also get a
multi-select: everything ticked is followed, everything cleared is not, applied
in one interaction.

A followed game appears in the unfiltered feeds and its alerts fire. An
unfollowed one is dropped from those feeds and its alert poll is skipped
entirely, so it costs no API call. Asking for it explicitly still answers,
saying the server doesn't follow it.

Only followed games are stored; see ``schema.sql``. Subscriptions for a game
that's later removed are kept and resume if it's re-added.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import AMBER, BRAND
from ..utils.games import GAMES

log = logging.getLogger("aurorabot.cogs.games")

ALL_KEYS = {g.key for g in GAMES.values()}


def can_manage(user: discord.abc.User | discord.Member) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def status_embed(enabled: set[str], *, editable: bool) -> discord.Embed:
    following = [g for g in GAMES.values() if g.key in enabled]
    ignoring = [g for g in GAMES.values() if g.key not in enabled]

    embed = discord.Embed(
        title="🎮 Games followed by this server",
        color=BRAND if following else AMBER,
    )
    if not following:
        # The default state, so lead with it rather than showing two lists that
        # look like something has gone wrong.
        embed.description = (
            "**Nothing selected yet — AuroraBot is idle here.**\n"
            "Games are opt-in: pick the ones this server cares about below and "
            "scores, alerts and predictions start working for them."
            if editable
            else "**Nothing selected yet — AuroraBot is idle here.**\n"
            "A member with *Manage Server* can choose games with `/games`."
        )
    embed.add_field(
        name=f"✅ Following ({len(following)})",
        value="\n".join(f"{g.emoji} {g.label}".strip() for g in following) or "_None_",
        inline=True,
    )
    embed.add_field(
        name=f"➖ Not following ({len(ignoring)})",
        value="\n".join(f"{g.emoji} {g.label}".strip() for g in ignoring) or "_None_",
        inline=True,
    )
    embed.set_footer(
        text=(
            "Tick the games to follow, then click away to apply."
            if editable
            else "A member with Manage Server can change this with /games."
        )
    )
    return embed


def _prewarm(cog: "Games", enabled: set[str]) -> None:
    """Warm the tournament cache for a new selection.

    Done here rather than lazily so the first `/alerts add` after choosing games
    already has its autocomplete data.
    """
    alerts = cog.bot.get_cog("Alerts")
    if alerts is not None:
        alerts.prewarm(enabled)


class GameSelect(discord.ui.Select):
    def __init__(self, cog: "Games", enabled: set[str]) -> None:
        self.cog = cog
        options = [
            discord.SelectOption(
                label=g.label,
                value=g.key,
                emoji=g.emoji or None,
                default=g.key in enabled,
            )
            for g in GAMES.values()
        ]
        super().__init__(
            placeholder="Choose the games this server follows…",
            options=options,
            min_values=0,               # clearing everything is a valid choice
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Re-check: anyone can click a component someone else's command posted.
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to change this.",
                ephemeral=True,
            )
            return

        enabled = set(self.values)
        await self.cog.bot.db.set_games(
            guild_id=interaction.guild_id,
            enabled=enabled,
            known=ALL_KEYS,
            updated_by=interaction.user.id,
        )
        log.info(
            "Guild %s now follows %d game(s), set by %s",
            interaction.guild_id, len(enabled), interaction.user.id,
        )
        _prewarm(self.cog, enabled)
        await interaction.response.edit_message(
            embed=status_embed(enabled, editable=True),
            view=GamesView(self.cog, enabled),
        )


class FollowAllButton(discord.ui.Button):
    def __init__(self, cog: "Games", enabled: set[str]) -> None:
        super().__init__(
            label="Follow all games",
            style=discord.ButtonStyle.secondary,
            emoji="✅",
            disabled=enabled >= ALL_KEYS,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to change this.",
                ephemeral=True,
            )
            return
        await self.cog.bot.db.set_games(
            guild_id=interaction.guild_id,
            enabled=set(ALL_KEYS),
            known=ALL_KEYS,
            updated_by=interaction.user.id,
        )
        _prewarm(self.cog, set(ALL_KEYS))
        await interaction.response.edit_message(
            embed=status_embed(set(ALL_KEYS), editable=True),
            view=GamesView(self.cog, set(ALL_KEYS)),
        )


class FollowNoneButton(discord.ui.Button):
    def __init__(self, cog: "Games", enabled: set[str]) -> None:
        super().__init__(
            label="Follow none",
            style=discord.ButtonStyle.secondary,
            emoji="➖",
            disabled=not enabled,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to change this.",
                ephemeral=True,
            )
            return
        await self.cog.bot.db.clear_games(interaction.guild_id)
        await interaction.response.edit_message(
            embed=status_embed(set(), editable=True), view=GamesView(self.cog, set())
        )


class GamesView(discord.ui.View):
    def __init__(self, cog: "Games", enabled: set[str]) -> None:
        super().__init__(timeout=180)
        self.add_item(GameSelect(cog, enabled))
        self.add_item(FollowAllButton(cog, enabled))
        self.add_item(FollowNoneButton(cog, enabled))


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="games", description="See or choose which games this server follows."
    )
    @app_commands.guild_only()
    async def games(self, interaction: discord.Interaction) -> None:
        enabled = await self.bot.db.enabled_games(interaction.guild_id)
        editable = can_manage(interaction.user)
        await interaction.response.send_message(
            embed=status_embed(enabled, editable=editable),
            view=GamesView(self, enabled) if editable else None,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))
