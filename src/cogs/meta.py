"""Help & diagnostics."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import BRAND
from ..utils.tiers import describe


class Meta(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="What can AuroraBot do?")
    async def help(self, interaction: discord.Interaction) -> None:
        lead = self.bot.settings.alert_lead_minutes
        embed = discord.Embed(
            title="🌌 AuroraBot — your eSports companion",
            description="Follow live scores, standings, analytics and predictions "
            "across LoL, CS2, Valorant, Dota 2, Rocket League and more.\n"
            f"**Everything is filtered to Tier 1 tournaments** "
            f"({describe(self.bot.settings)}).",
            color=BRAND,
        )
        embed.add_field(
            name="📡 Scores",
            value="`/live` · `/upcoming` · `/results`",
            inline=False,
        )
        embed.add_field(
            name="📊 Data",
            value="`/standings` (current tournaments) · `/team` (deep analytics)\n"
            "`/lineup` — pre-match rosters by role\n"
            "`/tournament` — event info and its next matches",
            inline=False,
        )
        embed.add_field(
            name="👤 Profile",
            value="`/profile` · `/follow` · `/unfollow`\n"
            "`/setgame` — your default game for the commands above",
            inline=False,
        )
        embed.add_field(
            name="🎲 Play",
            value="`/predict` · `/mypredictions` · `/leaderboard`\n"
            "Or just click a team button on a match reminder to pick a winner.",
            inline=False,
        )
        embed.add_field(
            name="🎮 Games (start here)",
            value="`/games` — pick which titles this server follows.\n"
            "Games are opt-in: nothing works until a mod chooses some.",
            inline=False,
        )
        embed.add_field(
            name="🔔 Alerts (mods)",
            value="`/alerts add` — by **team**, by **tournament**, or a whole game\n"
            f"`/alerts list` · `/alerts remove`\n"
            f"Pings {lead} min before kick-off and again when the match goes live.",
            inline=False,
        )
        embed.set_footer(text="AuroraBot · Tier 1 only · powered by PandaScore")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"🏓 Pong! `{round(self.bot.latency * 1000)}ms`", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
