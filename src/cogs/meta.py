"""Help & diagnostics."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import BRAND
from ..utils.scoring import (
    BASE_POINTS, DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, FINAL_WEIGHT,
    MAX_MULTIPLIER, MIN_VOTERS_FOR_ODDS, PERFECT_DAY_BONUS,
    PLAYOFF_WEIGHT, TOKENS_PER_TOURNAMENT, payout_for,
)
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
            "`/badges` · `/rival` — what you've earned, and who you're chasing\n"
            "Or just click a team button on a match reminder to pick a winner.\n"
            "**`/scoring`** — how points work, and what a double down risks.",
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

    @app_commands.command(
        name="scoring", description="How prediction points work."
    )
    async def scoring(self, interaction: discord.Interaction) -> None:
        """The whole model in one card.

        Written out in full because the rules changed under people who were
        already playing, and a double down can now *cost* points — that is not
        something anyone should have to discover by losing ten.
        """
        upset = payout_for(1, 5).points          # one of five called it
        crowd = payout_for(4, 5).points          # four of five did
        embed = discord.Embed(
            title="🎲 How points work",
            description=(
                f"Every correct call is worth the same **{BASE_POINTS}** before "
                f"your server is taken into account. A wrong one costs nothing "
                f"— unless you doubled down on it.\n\n"
                f"```\n"
                f"right = {BASE_POINTS} × underdog × stage × (2 if doubled)\n"
                f"wrong = 0, or −{DOUBLE_DOWN_PENALTY} if you doubled\n"
                f"```"
            ),
            color=BRAND,
        )
        embed.add_field(
            name="📉 Underdog",
            value=(
                f"Priced off **this server's** votes, not a bookmaker.\n"
                f"Call it with 4 of 5 others → **{crowd} pts**.\n"
                f"Call it when only you did → **{upset} pts**.\n"
                f"Caps just under ×{MAX_MULTIPLIER:g}, and stays flat until "
                f"{MIN_VOTERS_FOR_ODDS} people have voted."
            ),
            inline=False,
        )
        embed.add_field(
            name="🏆 Stage",
            value=(
                f"Grand final **×{FINAL_WEIGHT:g}** · rest of the bracket "
                f"**×{PLAYOFF_WEIGHT:g}** · group stage face value.\n"
                f"Late matches pay more, so an event stays winnable if you "
                f"joined halfway through."
            ),
            inline=False,
        )
        embed.add_field(
            name="💥 Double down — the only way to lose points",
            value=(
                f"**{TOKENS_PER_TOURNAMENT} per tournament.** Declare one on "
                f"the private reply after you pick; take it back any time "
                f"before kick-off, locked once the match starts.\n"
                f"✅ it lands → **×{DOUBLE_DOWN}**\n"
                f"❌ it doesn't → **−{DOUBLE_DOWN_PENALTY}**\n"
                f"Capped at what you've banked this event, so your board can "
                f"dip but never go below zero. The stage multiplies the reward, "
                f"not the risk. Spent is spent — they come back next event."
            ),
            inline=False,
        )
        embed.add_field(
            name="☀️ Perfect day",
            value=f"Call every one of a day's matches right (two or more) "
                  f"for **+{PERFECT_DAY_BONUS}**.",
            inline=False,
        )
        embed.set_footer(
            text="Boards reset every tournament · /leaderboard for this event, "
                 "or the season table · /badges for what's collectable"
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"🏓 Pong! `{round(self.bot.latency * 1000)}ms`", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Meta(bot))
