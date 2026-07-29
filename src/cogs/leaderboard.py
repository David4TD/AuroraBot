"""Server leaderboard."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..utils.embeds import BRAND


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="leaderboard", description="Top predictors by points.")
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.leaderboard(limit=10)
        if not rows:
            await interaction.response.send_message(
                "No one's on the board yet — make a `/predict` to get started!"
            )
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f"`#{i + 1}`"
            total = r["predictions_total"] or 0
            won = r["predictions_won"] or 0
            acc = f"{(won / total * 100):.0f}%" if total else "—"
            lines.append(
                f"{rank} **{r['display_name']}** — {r['points']} pts · {won}/{total} ({acc})"
            )
        embed = discord.Embed(
            title="🏅 Prediction Leaderboard",
            description="\n".join(lines),
            color=BRAND,
        )
        embed.set_footer(text="AuroraBot · earn points with /predict")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
