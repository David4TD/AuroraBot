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

    async def _board_ac(self, interaction: discord.Interaction, current: str):
        """Tournaments this server has actually predicted on, plus All-time."""
        rows = await self.bot.db.tournaments_with_predictions(interaction.guild_id)
        query = (current or "").lower()
        choices = [app_commands.Choice(name="🏆 All-time (every tournament)", value="all")]
        for r in rows:
            name = r["tournament_name"] or f"Tournament {r['tournament_id']}"
            if query and query not in name.lower():
                continue
            choices.append(
                app_commands.Choice(
                    name=f"{name} · {r['picks']} picks"[:100],
                    value=str(int(r["tournament_id"])),
                )
            )
        return choices[:25]

    @app_commands.command(
        name="leaderboard", description="Top predictors for a tournament."
    )
    @app_commands.describe(
        tournament="Which board. Defaults to what this channel follows."
    )
    @app_commands.autocomplete(tournament=_board_ac)
    @app_commands.guild_only()
    async def leaderboard(
        self, interaction: discord.Interaction, tournament: str | None = None
    ) -> None:
        target_id, title = await self._resolve_board(interaction, tournament)
        rows = await self.bot.db.tournament_leaderboard(
            interaction.guild_id, target_id, limit=TOP_N
        )
        if not rows:
            await interaction.response.send_message(
                f"No one's on the **{title}** board yet — a pick only scores "
                f"once the match finishes. Make one with `/predict`, or tap a "
                f"team on a match reminder."
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
            title=f"🏅 {title}",
            description="\n".join(lines),
            color=BRAND,
        )
        embed.set_footer(
            text="Points earned in this server · "
            "underdog calls pay more · /leaderboard tournament:… for another board"
        )
        await interaction.response.send_message(embed=embed)

    async def _resolve_board(
        self, interaction: discord.Interaction, tournament: str | None
    ) -> tuple[int | None, str]:
        """Which board to show, and what to call it.

        With nothing specified, the channel's own alert subscription decides —
        so `/leaderboard` in #lck shows LCK without anyone naming it. Falls back
        to the server's all-time board when the channel follows nothing.
        """
        if tournament == "all":
            return None, "All-time leaderboard"

        if tournament:
            if tournament.isdigit():
                target = int(tournament)
            else:
                # Typed text rather than a picked option: match on name.
                rows = await self.bot.db.tournaments_with_predictions(
                    interaction.guild_id, limit=100
                )
                match = next(
                    (r for r in rows
                     if tournament.lower() in str(r["tournament_name"] or "").lower()),
                    None,
                )
                if match is None:
                    return None, "All-time leaderboard"
                target = int(match["tournament_id"])
            return target, await self._name_for(interaction.guild_id, target)

        subs = [
            s for s in await self.bot.db.list_subscriptions(interaction.guild_id)
            if int(s["channel_id"]) == interaction.channel_id
            and s["tournament_id"] is not None
        ]
        if subs:
            target = int(subs[0]["tournament_id"])
            return target, subs[0]["tournament_name"] or "Tournament"
        return None, "All-time leaderboard"

    async def _name_for(self, guild_id: int, tournament_id: int) -> str:
        rows = await self.bot.db.tournaments_with_predictions(guild_id, limit=100)
        for r in rows:
            if int(r["tournament_id"]) == tournament_id:
                return r["tournament_name"] or f"Tournament {tournament_id}"
        return f"Tournament {tournament_id}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
