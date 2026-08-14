"""Crowning the winner of each tournament.

Points reset every event, which is the whole reason a late joiner can still
make a run — but a board that resets and leaves nothing behind is a board
nobody remembers playing. So the moment an event's last result lands, whoever
topped it is recorded permanently, announced in the channels that followed it,
and carries a 👑 on every board from then on.

The record is the point, not the announcement: crowning writes a row first and
posts second, so a channel the bot can't reach, or a restart mid-post, can
never cost someone a title.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.embeds import BRAND
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.champions")

CHECK_EVERY_MINUTES = 30
# How long after an event's scheduled end to wait before crowning. The final's
# result has to have landed and settled, and PandaScore's end_at is the day the
# event ends rather than the minute.
SETTLE_GRACE = timedelta(hours=3)
PODIUM = 3
MAX_TOURNAMENTS_PER_GUILD = 50


class Champions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.crown_loop.start()

    async def cog_unload(self) -> None:
        self.crown_loop.cancel()

    @tasks.loop(minutes=CHECK_EVERY_MINUTES)
    async def crown_loop(self) -> None:
        try:
            await self._crown_finished()
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("crown_loop iteration failed")

    @crown_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _crown_finished(self) -> None:
        guilds = await self.bot.db.guilds_with_predictions()
        if not guilds:
            return
        # One lookup per tournament per pass, however many servers followed it.
        seen: dict[int, dict | None] = {}
        for guild_id in guilds:
            crowned = await self.bot.db.crowned_tournaments(int(guild_id))
            rows = await self.bot.db.tournaments_with_predictions(
                int(guild_id), limit=MAX_TOURNAMENTS_PER_GUILD
            )
            for row in rows:
                tournament_id = row["tournament_id"]
                if tournament_id is None or int(tournament_id) in crowned:
                    continue
                await self._maybe_crown(
                    int(guild_id), int(tournament_id),
                    row["tournament_name"], seen,
                )

    async def _maybe_crown(
        self, guild_id: int, tournament_id: int, name: str | None, seen: dict
    ) -> None:
        # An event with a pick still open isn't over, whatever the calendar
        # says — a postponed final would otherwise crown the wrong person.
        if await self.bot.db.open_predictions_in_tournament(guild_id, tournament_id):
            return

        if tournament_id not in seen:
            try:
                seen[tournament_id] = await self.bot.api.get_tournament(tournament_id)
            except PandaScoreError as exc:
                log.debug("tournament %s lookup failed: %s", tournament_id, exc)
                seen[tournament_id] = None
        tournament = seen[tournament_id]
        if not tournament:
            return

        ends = parse_dt(tournament.get("end_at"))
        if ends is None:
            return                      # still running, or never scheduled an end
        if datetime.now(timezone.utc) - ends < SETTLE_GRACE:
            return

        board = await self.bot.db.tournament_leaderboard(
            guild_id, tournament_id, limit=PODIUM
        )
        if not board:
            return                      # nobody predicted it; nothing to crown

        winner = board[0]
        title = name or tournament.get("name") or f"Tournament {tournament_id}"
        crowned = await self.bot.db.crown_champion(
            guild_id=guild_id,
            tournament_id=tournament_id,
            tournament_name=title,
            discord_id=winner["discord_id"],
            points=int(winner["points"] or 0),
            won=int(winner["won"] or 0),
            lost=int(winner["lost"] or 0),
        )
        if not crowned:
            return                      # another pass got there first
        log.info(
            "Champion of %s in guild %s: %s (%s pts)",
            title, guild_id, winner["discord_id"], winner["points"],
        )
        await self._announce(guild_id, tournament_id, title, board)

    async def _announce(
        self, guild_id: int, tournament_id: int, title: str, board
    ) -> None:
        channels = await self.bot.db.channels_for_tournament(guild_id, tournament_id)
        if not channels:
            log.info("nowhere to announce the %s champion", title)
            return
        titles = await self.bot.db.champion_counts(guild_id)
        embed = _champion_embed(title, board, titles)
        for channel_id in channels:
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                message = await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("could not announce the champion in %s: %s",
                            channel_id, exc)
                continue
            await self._pin(message)

    async def _pin(self, message: discord.Message) -> None:
        """Pin the new champion, retiring the last one from the same channel.

        Pins are a scarce, shared resource — fifty per channel, and they're the
        server's, not ours. Leaving a trail of every event we ever crowned
        would slowly fill it, so the previous crowning steps aside.
        """
        try:
            for pinned in await message.channel.pins():
                if (pinned.author.id == self.bot.user.id
                        and pinned.id != message.id
                        and pinned.embeds
                        and str(pinned.embeds[0].title or "").startswith("👑")):
                    await pinned.unpin()
            await message.pin()
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Pinning needs Manage Messages; the announcement still stands.
            log.info("could not pin the champion post: %s", exc)


def _champion_embed(title: str, board, titles: dict[str, int]) -> discord.Embed:
    winner = board[0]
    count = titles.get(str(winner["discord_id"]), 1)
    embed = discord.Embed(
        title=f"👑 Champion of {title}"[:256],
        description=(
            f"<@{winner['discord_id']}> takes it with **{int(winner['points'])}** "
            f"points — {int(winner['won'] or 0)}W/{int(winner['lost'] or 0)}L."
        ),
        color=BRAND,
    )
    if count > 1:
        embed.description += f"\nThat's **{count}** titles here now. 🏆"

    if len(board) > 1:
        medals = ["🥇", "🥈", "🥉"]
        embed.add_field(
            name="Final standings",
            value="\n".join(
                f"{medals[i]} <@{r['discord_id']}> — **{int(r['points'])}** pts"
                for i, r in enumerate(board[:PODIUM])
            )[:1024],
            inline=False,
        )
    embed.set_footer(
        text="Points reset for the next event · titles are forever · "
        "/leaderboard tournament:All-time for the season table"
    )
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Champions(bot))
