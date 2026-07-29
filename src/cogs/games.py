"""Per-server game toggles.

`/games` shows which titles this server follows. Members with **Manage Server**
also get a multi-select to switch them on and off — everything selected is
enabled, everything cleared is disabled, applied in one interaction.

A disabled game is dropped from the unfiltered feeds (`/live`, `/upcoming`,
`/results`) and skipped by the alert poll, so it can't ping the server. Asking
for it explicitly (`/live game:Dota 2`) still works — the toggle curates the
firehose rather than censoring the catalogue.

Only disabled games are stored; see ``schema.sql``. A server that never runs
this command follows everything, and titles added to the catalogue later
default to on.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import AMBER, BRAND, GREEN
from ..utils.games import GAMES

log = logging.getLogger("aurorabot.cogs.games")

ALL_KEYS = {g.key for g in GAMES.values()}


def can_manage(user: discord.abc.User | discord.Member) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def status_embed(disabled: set[str], *, editable: bool) -> discord.Embed:
    enabled = [g for g in GAMES.values() if g.key not in disabled]
    off = [g for g in GAMES.values() if g.key in disabled]

    embed = discord.Embed(
        title="🎮 Games followed by this server",
        color=BRAND if enabled else AMBER,
    )
    embed.add_field(
        name=f"✅ Following ({len(enabled)})",
        value="\n".join(f"{g.emoji} {g.label}".strip() for g in enabled) or "_None_",
        inline=True,
    )
    embed.add_field(
        name=f"⛔ Muted ({len(off)})",
        value="\n".join(f"{g.emoji} {g.label}".strip() for g in off) or "_None_",
        inline=True,
    )
    if not enabled:
        embed.add_field(
            name="⚠️ Every game is muted",
            value="Feeds and alerts will stay silent until you re-enable one.",
            inline=False,
        )
    embed.set_footer(
        text=(
            "Select the games to follow, then click away to apply."
            if editable
            else "A member with Manage Server can change this with /games."
        )
    )
    return embed


class GameToggleSelect(discord.ui.Select):
    def __init__(self, cog: "Games", disabled: set[str]) -> None:
        self.cog = cog
        options = [
            discord.SelectOption(
                label=g.label,
                value=g.key,
                emoji=g.emoji or None,
                default=g.key not in disabled,
            )
            for g in GAMES.values()
        ]
        super().__init__(
            placeholder="Choose the games this server follows…",
            options=options,
            min_values=0,               # clearing everything mutes the bot
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        # Re-check: anyone can click a button someone else's command posted.
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
        disabled = ALL_KEYS - enabled
        log.info(
            "Guild %s game toggles updated by %s: %d enabled, %d muted",
            interaction.guild_id, interaction.user.id, len(enabled), len(disabled),
        )
        await interaction.response.edit_message(
            embed=status_embed(disabled, editable=True),
            view=GamesView(self.cog, disabled),
        )


class FollowAllButton(discord.ui.Button):
    def __init__(self, cog: "Games", disabled: set[str]) -> None:
        super().__init__(
            label="Follow all games",
            style=discord.ButtonStyle.secondary,
            emoji="↩️",
            disabled=not disabled,      # nothing to undo when all are on
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to change this.",
                ephemeral=True,
            )
            return
        await self.cog.bot.db.reset_games(interaction.guild_id)
        await interaction.response.edit_message(
            embed=status_embed(set(), editable=True), view=GamesView(self.cog, set())
        )


class GamesView(discord.ui.View):
    def __init__(self, cog: "Games", disabled: set[str]) -> None:
        super().__init__(timeout=180)
        self.add_item(GameToggleSelect(cog, disabled))
        self.add_item(FollowAllButton(cog, disabled))


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="games", description="See or change which games this server follows."
    )
    @app_commands.guild_only()
    async def games(self, interaction: discord.Interaction) -> None:
        disabled = await self.bot.db.disabled_games(interaction.guild_id)
        editable = can_manage(interaction.user)
        await interaction.response.send_message(
            embed=status_embed(disabled, editable=editable),
            view=GamesView(self, disabled) if editable else None,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Games(bot))
