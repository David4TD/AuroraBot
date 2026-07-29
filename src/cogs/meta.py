"""Help & diagnostics."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import BRAND


class Meta(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="What can AuroraBot do?")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🌌 AuroraBot — your eSports companion",
            description="Follow live scores, standings, analytics and predictions "
            "across LoL, CS2, Valorant, Dota 2, Rocket League and more.",
            color=BRAND,
        )
        embed.add_field(
            name="📡 Scores",
            value="`/live` · `/upcoming` · `/results`",
            inline=False,
        )
        embed.add_field(
            name="📊 Data",
            value="`/standings` · `/team` (deep analytics)",
            inline=False,
        )
        embed.add_field(
            name="👤 Profile",
            value="`/profile` · `/follow` · `/unfollow` · `/setgame`",
            inline=False,
        )
        embed.add_field(
            name="🎲 Play",
            value="`/predict` · `/mypredictions` · `/leaderboard`",
            inline=False,
        )
        embed.add_field(
            name="🔔 Alerts (mods)",
            value="`/alerts add` · `/alerts list` · `/alerts remove`",
            inline=False,
        )
        embed.set_footer(text="AuroraBot · powered by PandaScore")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"🏓 Pong! `{round(self.bot.latency * 1000)}ms`", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
