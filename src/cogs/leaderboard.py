"""Prediction leaderboard, scored per server.

Points are earned in the server the pick was made in, not pooled globally: a
board that mixed every server the bot is in would rank your members by activity
nobody here can see, and would print strangers' names into the channel.

``users.points`` is still kept as a lifetime total across servers — that's what
`/profile` falls back to in a DM, where there is no server to score against.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import BRAND

TOP_N = 10


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="leaderboard", description="Top predictors in this server."
    )
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.guild_leaderboard(interaction.guild_id, limit=TOP_N)
        if not rows:
            await interaction.response.send_message(
                "No one's on the board yet — a pick only scores once the match "
                "finishes. Make one with `/predict`, or tap a team on a match "
                "reminder."
            )
            return

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f"`#{i + 1}`"
            won, lost = int(r["won"] or 0), int(r["lost"] or 0)
            settled = won + lost
            acc = f"{(won / settled * 100):.0f}%" if settled else "—"
            lines.append(
                f"{rank} <@{r['discord_id']}> — **{int(r['points'])}** pts · "
                f"{won}W/{lost}L ({acc})"
            )

        embed = discord.Embed(
            title="🏅 Prediction leaderboard",
            description="\n".join(lines),
            color=BRAND,
        )
        embed.set_footer(text="Points earned in this server · earn more with /predict")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
