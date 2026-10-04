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
RECENT_TITLES = 5

# How long to let the tournament directory warm before giving up on knowing
# which of a channel's events is still running. A cached list — even a stale
# one — comes back instantly, so this only ever costs anything on a cold start.
DIRECTORY_WAIT = 0.5

# How current an event is, as far as choosing a default board goes.
ENDED, UNKNOWN, RUNNING = 0, 1, 2


class Leaderboard(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _board_ac(self, interaction: discord.Interaction, current: str):
        """Tournaments this server has actually predicted on, plus All-time."""
        rows = await self.bot.db.tournaments_with_predictions(interaction.guild_id)
        crowned = await self.bot.db.crowned_tournaments(interaction.guild_id)
        query = (current or "").lower()
        choices = [app_commands.Choice(name="🏆 Season table (every tournament)", value="all")]
        for r in rows:
            name = r["tournament_name"] or f"Tournament {r['tournament_id']}"
            if query and query not in name.lower():
                continue
            # A crowned event is a closed book: worth saying so in the picker,
            # since a finished board and a live one otherwise look identical.
            done = " · 🏁 done" if int(r["tournament_id"]) in crowned else ""
            choices.append(
                app_commands.Choice(
                    name=f"{name} · {r['picks']} picks{done}"[:100],
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

        titles = await self.bot.db.champion_counts(interaction.guild_id)
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows):
            rank = medals[i] if i < 3 else f"`#{i + 1}`"
            won, lost = int(r["won"] or 0), int(r["lost"] or 0)
            settled = won + lost
            acc = f"{(won / settled * 100):.0f}%" if settled else "—"
            # A crown outranks a points total: it's the thing you can't get by
            # simply predicting more matches than everyone else.
            crowns = titles.get(str(r["discord_id"]), 0)
            crown = f" {'👑' * min(crowns, 3)}" if crowns else ""
            if crowns > 3:
                crown = f" 👑×{crowns}"
            lines.append(
                f"{rank} <@{r['discord_id']}>{crown} — **{int(r['points'])}** pts · "
                f"{won}W/{lost}L ({acc})"
            )

        embed = discord.Embed(
            title=f"🏅 {title}",
            description="\n".join(lines),
            color=BRAND,
        )
        if target_id is None:
            await self._add_champions(embed, interaction.guild_id)
        embed.set_footer(
            text="Points earned in this server · underdog calls pay more · "
            "playoffs count double · /leaderboard tournament:… for another board"
        )
        await interaction.response.send_message(embed=embed)

    async def _add_champions(self, embed: discord.Embed, guild_id: int) -> None:
        """Recent title-holders, under the season table they can't be read off.

        The cumulative board rewards turning up; this rewards finishing first.
        Shown only on the all-time view — under a single event's board the
        champion is either the person at the top or nobody yet.
        """
        try:
            recent = await self.bot.db.recent_champions(guild_id, limit=RECENT_TITLES)
        except Exception:  # noqa: BLE001 - the board matters more than the extra
            return
        if not recent:
            return
        embed.add_field(
            name="👑 Champions",
            value="\n".join(
                f"<@{r['discord_id']}> — {r['tournament_name'] or 'an event'}"
                for r in recent
            )[:1024],
            inline=False,
        )

    async def _resolve_board(
        self, interaction: discord.Interaction, tournament: str | None
    ) -> tuple[int | None, str]:
        """Which board to show, and what to call it.

        With nothing specified, the channel's own alert subscription decides —
        so `/leaderboard` in #lck shows LCK without anyone naming it. Falls back
        to the server's season table when the channel follows nothing, or when
        everything it follows has already finished.
        """
        if tournament == "all":
            return None, "Season table"

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
                    return None, "Season table"
                target = int(match["tournament_id"])
            return target, await self._name_for(interaction.guild_id, target)

        subs = [
            s for s in await self.bot.db.list_subscriptions(interaction.guild_id)
            if int(s["channel_id"]) == interaction.channel_id
            and s["tournament_id"] is not None
        ]
        sub = await self._channel_board(interaction.guild_id, subs)
        if sub is not None:
            return int(sub["tournament_id"]), sub["tournament_name"] or "Tournament"
        return None, "Season table"

    async def _channel_board(self, guild_id: int, subs: list) -> object | None:
        """Which of a channel's events to open on, or None for the season table.

        A channel usually follows more than one, and the subscription list comes
        back sorted by name — so taking the first one meant the board opened on
        whichever event was alphabetically first, long after it had finished.

        What makes an event the right default is that it's *on*: still in the
        tournament directory (which drops anything two days past its end), not
        yet crowned, and with picks waiting on a result. Only once all of that
        ties does recency break it. If nothing the channel follows is still
        running, no single event is the obvious answer and the season table is
        the honest one.
        """
        if not subs:
            return None
        ids = [int(s["tournament_id"]) for s in subs]
        activity = await self.bot.db.tournament_activity(guild_id, ids)
        crowned = await self.bot.db.crowned_tournaments(guild_id)

        running: dict[str, set[int] | None] = {}
        ranked = []
        for sub in subs:
            game = sub["game"]
            if game not in running:
                running[game] = await self._running_ids(game)
            tid = int(sub["tournament_id"])
            known = running[game]
            if known is None:
                state = UNKNOWN          # directory cold; don't call it dead
            else:
                state = RUNNING if tid in known else ENDED
            seen = activity.get(tid) or {}
            ranked.append((
                state,
                0 if tid in crowned else 1,
                1 if seen.get("open_picks") else 0,
                seen.get("last_seen") or "",
                seen.get("picks", 0),
                sub,
            ))

        ranked.sort(key=lambda row: row[:5], reverse=True)
        best = ranked[0]
        return None if best[0] == ENDED else best[5]

    async def _running_ids(self, game: str | None) -> set[int] | None:
        """Tournament ids currently on for a game, or None if we can't tell.

        None and an empty set mean different things here: "ask again later"
        versus "nothing is on". Only the first should leave a stale board as
        the default, so an empty result is reported as None too — a channel
        whose game has no current events gets the same treatment as a cold
        cache rather than a surprise jump to the season table.
        """
        if not game:
            return None
        try:
            rows = await self.bot.tourneys.current(game, wait=DIRECTORY_WAIT)
        except Exception:  # noqa: BLE001 - a board beats no board
            return None
        if not rows:
            return None
        return {int(t["id"]) for t in rows if t.get("id")}

    async def _name_for(self, guild_id: int, tournament_id: int) -> str:
        rows = await self.bot.db.tournaments_with_predictions(guild_id, limit=100)
        for r in rows:
            if int(r["tournament_id"]) == tournament_id:
                return r["tournament_name"] or f"Tournament {tournament_id}"
        return f"Tournament {tournament_id}"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leaderboard(bot))
