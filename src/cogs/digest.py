"""Daily schedule digest for tournament alert subscriptions.

At local midnight (``DIGEST_HOUR`` in ``DIGEST_TZ``) every channel subscribed to
a tournament gets that tournament's matches for the day as **pre-match lineup
cards** — the same card `/lineup` and `/upcoming` render — each with its own
pair of team buttons for predicting a winner.

Three design points worth knowing:

* **One match per action row.** Discord allows five action rows of five buttons,
  and a match needs two, so a message carries at most **five** matches. A busier
  day is split across follow-up messages and each message's embed lists only the
  matches its own buttons cover — orphaned buttons under a longer list would be
  impossible to map. Matches stay numbered across the split so "3." means the
  same match in the embed and on the button.
* **The buttons are persistent.** Each is a :class:`discord.ui.DynamicItem`
  carrying its digest and match ids in the ``custom_id``, so the digest keeps
  working after a restart with no per-message view to re-register; the teams are
  re-read from ``digest_matches`` at click time. Replies are *ephemeral*, so
  voting neither clutters the channel nor needs a DM.
* **Posting is claimed before it happens.** The ``match_digests`` UNIQUE key on
  (channel, subscription, local date) is taken first, so neither the 5-minute
  catch-up sweep nor a restart can post the same day twice. A crash between
  claim and post costs one digest instead of risking a loop of duplicates.

The sweep runs every 5 minutes rather than firing exactly at midnight so a bot
that was offline at 00:00 still posts when it comes back.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.embeds import BRAND, GREEN
from ..utils.games import label_for, resolve_slug
from ..utils.guildprefs import digest_hour, digest_tz
from ..utils.lineupcard import build_card
from ..utils.matches import (
    league_id as match_league_id,
    opponents,
    tournament_id as match_tournament_id,
)
from ..utils.predictions import Outcome, submit_prediction
from ..utils.conviction import pick_panel
from ..utils.subscriptions import wants_votes
from ..utils.regions import region_flag
from ..utils.schedule import (
    digest_window, is_after_midnight, iso_date, local_now, local_today,
    within_digest_window,
)
from ..utils.tiers import filter_for
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.digest")

SWEEP_MINUTES = 5
FETCH_SIZE = 50
# A view holds 5 action rows; one match per row (two team buttons) is what makes
# the pairing readable, so a message carries 5 matches and the rest spill over.
MATCHES_PER_MESSAGE = 5
MAX_MATCHES = 25       # ceiling on a single day, i.e. at most 5 messages
PRUNE_AFTER_DAYS = 14


def _digestable(sub) -> bool:
    """Subscriptions that get a daily schedule: a league or a single stage.

    Team and whole-game scopes are deliberately excluded — a team plays at most
    once a day, and a whole-game digest would be a wall of text.
    """
    scope = sub["scope"] or "game"
    if scope == "league":
        return sub["league_id"] is not None
    if scope == "tournament":
        return sub["tournament_id"] is not None
    return False


def _target_id(sub) -> int | None:
    if (sub["scope"] or "game") == "league":
        return int(sub["league_id"]) if sub["league_id"] is not None else None
    return int(sub["tournament_id"]) if sub["tournament_id"] is not None else None


def _target_name(sub) -> str | None:
    if (sub["scope"] or "game") == "league":
        return sub["league_name"]
    return sub["tournament_name"]


def _teams_of(match: dict) -> tuple[dict, dict] | None:
    teams = opponents(match)
    return (teams[0], teams[1]) if len(teams) >= 2 else None


class DigestVoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"aurora:dvote:(?P<digest_id>[0-9]+):(?P<match_id>[0-9]+):(?P<side>[01])",
):
    """One team's button for one match on a digest message.

    A digest carries several matches, so — unlike the reminder buttons, which can
    infer everything from the message they hang on — the match has to be encoded
    here. Digest id, match id and side fit comfortably inside the 100-character
    ``custom_id`` budget, and the *teams* are still read back from
    ``digest_matches`` at click time rather than trusted from the label, so a
    renamed team can't corrupt a pick.

    Rebuilt from its ``custom_id`` on every click, so it survives restarts with
    no view to re-register. Replies are **ephemeral**.
    """

    def __init__(
        self,
        digest_id: int,
        match_id: int,
        side: int,
        *,
        label: str = "…",
        emoji=None,
        row: int | None = None,
    ) -> None:
        self.digest_id = digest_id
        self.match_id = match_id
        self.side = side
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"aurora:dvote:{digest_id}:{match_id}:{side}",
                emoji=emoji,
                row=row,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
    ) -> "DigestVoteButton":
        return cls(
            int(match["digest_id"]), int(match["match_id"]), int(match["side"])
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        row = await interaction.client.db.digest_match(self.digest_id, self.match_id)
        if row is None:
            await interaction.response.send_message(
                "That match is no longer on this schedule.", ephemeral=True
            )
            return

        team_a = {"id": int(row["team_a_id"]), "name": row["team_a_name"]}
        team_b = {"id": int(row["team_b_id"]), "name": row["team_b_name"]}
        team, opponent = (team_a, team_b) if self.side == 0 else (team_b, team_a)

        # The match's own tournament, so a vote here lands on the same board as
        # a vote on the same match from a reminder. A league digest covers
        # several tournaments, so the digest's target is the wrong answer.
        tour_id, tour_name = row["tournament_id"], row["tournament_name"]
        if tour_id is None:
            # Written before the column existed: fall back to the digest's
            # target rather than filing the vote nowhere.
            meta = await interaction.client.db.digest_meta(self.digest_id)
            tour_id = meta["tournament_id"] if meta else None
            tour_name = meta["tournament_name"] if meta else None

        _, message = await submit_prediction(
            interaction.client.db,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            match_id=self.match_id,
            game=None,
            team=team,
            opponent=opponent,
            begin_at=row["begin_at"],
            guild_id=interaction.guild_id,
            tournament_id=tour_id,
            tournament_name=tour_name,
        )
        view = await pick_panel(
            interaction.client.db, interaction.user.id, self.match_id
        )
        await interaction.response.send_message(message, view=view, ephemeral=True)


class Digest(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.sweep.start()

    async def cog_unload(self) -> None:
        self.sweep.cancel()

    # ── manual trigger, handy for testing the layout ─────────────────────────
    @app_commands.command(
        name="schedule", description="Post today's schedule for this channel's tournaments."
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def schedule(self, interaction: discord.Interaction) -> None:
        # Public: the digest it posts is public, so a private "posted 2
        # messages" left the person who ran it as the only one who saw a
        # confirmation for something everyone can see.
        await interaction.response.defer(thinking=True)
        subs = [
            s
            for s in await self.bot.db.list_subscriptions(interaction.guild_id)
            if _digestable(s) and int(s["channel_id"]) == interaction.channel_id
        ]
        if not subs:
            await interaction.followup.send(
                "This channel has no tournament alerts. Add one with "
                "`/alerts add game:… tournament:…`."
            )
            return
        posted = 0
        for sub in subs:
            tz = digest_tz(self.bot.settings,
                           await self.bot.db.guild_settings(interaction.guild_id))
            if await self._post_digest(sub, local_today(tz),
                                      force=True):
                posted += 1
        await interaction.followup.send(
            f"Posted {posted} schedule message(s)." if posted
            else "Nothing scheduled for today in those tournaments."
        )

    # ── daily sweep ──────────────────────────────────────────────────────────
    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self) -> None:
        try:
            await self._sweep_once()
        except Exception:  # noqa: BLE001 - the loop must outlive failures
            log.exception("digest sweep failed")

    async def _sweep_once(self) -> None:
        subs = [s for s in await self.bot.db.all_subscriptions() if _digestable(s)]
        if not subs:
            return

        # Respect each server's /games selection, same as the alert poll.
        enabled = await self.bot.db.all_enabled_games()
        # Hour and timezone are per server, so "is it due yet" is answered once
        # per guild rather than once for the whole bot.
        prefs = await self.bot.db.all_guild_settings()
        for sub in subs:
            gid = str(sub["guild_id"])
            if sub["game"] not in enabled.get(gid, frozenset()):
                continue
            over = prefs.get(gid)
            tz = digest_tz(self.bot.settings, over)
            if local_now(tz).hour < digest_hour(self.bot.settings, over):
                continue          # that server's post isn't due yet
            await self._post_digest(sub, local_today(tz))

        await self.bot.db.prune_digests(days=PRUNE_AFTER_DAYS)

    async def _post_digest(self, sub, today, *, force: bool = False) -> bool:
        """Post one subscription's schedule for *today*. True if a message went out."""
        channel_id = int(sub["channel_id"])
        date_key = iso_date(today)
        # The window runs to the moment the *next* digest posts, so it needs
        # this server's own hour and zone, not the deployment defaults.
        over = (
            await self.bot.db.guild_settings(int(sub["guild_id"]))
            if sub["guild_id"] else None
        )
        tz = digest_tz(self.bot.settings, over)
        hour = digest_hour(self.bot.settings, over)

        if not force:
            claimed = await self.bot.db.create_digest(
                guild_id=int(sub["guild_id"]) if sub["guild_id"] else None,
                channel_id=channel_id,
                subscription_id=int(sub["id"]),
                tournament_id=_target_id(sub),
                tournament_name=_target_name(sub),
                game=sub["game"],
                local_date=date_key,
            )
            if claimed is None:
                return False  # already handled for this local day
            digest_id = claimed
        else:
            existing = await self.bot.db.digest_for(channel_id, int(sub["id"]), date_key)
            digest_id = existing["id"] if existing else await self.bot.db.create_digest(
                guild_id=int(sub["guild_id"]) if sub["guild_id"] else None,
                channel_id=channel_id,
                subscription_id=int(sub["id"]),
                tournament_id=_target_id(sub),
                tournament_name=_target_name(sub),
                game=sub["game"],
                local_date=date_key,
            )
            if digest_id is None:
                return False

        matches = await self._todays_matches(sub, today, tz, hour, force=force)
        if not matches:
            # The claim row stays, recording "nothing on today", so the sweep
            # stops re-querying the API for the rest of the day.
            log.debug("digest %s: nothing scheduled for %s", digest_id, date_key)
            return False

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log.warning("digest: channel %s is not visible", channel_id)
            return False

        stored = [
            {
                "match_id": int(m["id"]),
                "begin_at": m.get("begin_at"),
                "team_a": (t[0]["id"], t[0]["name"]),
                "team_b": (t[1]["id"], t[1]["name"]),
                # The match's own tournament, not the subscription's target.
                "tournament_id": match_tournament_id(m),
                "tournament_name": (m.get("tournament") or {}).get("name"),
            }
            for m, t in matches
        ]
        await self.bot.db.add_digest_matches(digest_id, stored)

        chunks = [
            matches[i:i + MATCHES_PER_MESSAGE]
            for i in range(0, len(matches), MATCHES_PER_MESSAGE)
        ]
        # A subscription with predictions off gets the schedule without vote
        # buttons — and without a leaderboard, which would be empty anyway.
        votes = wants_votes(sub)
        first_message = None
        for part, chunk in enumerate(chunks):
            start = part * MATCHES_PER_MESSAGE
            header = self._build_header(
                sub, today, chunk,
                total=len(matches), part=part, parts=len(chunks),
                # The same zone the day's window was measured in, so "after
                # midnight" on the banner means what it meant during selection.
                tz=tz,
            )
            if part == 0 and votes:
                await self._add_standings(
                    header, sub["guild_id"], _target_id(sub), _target_name(sub)
                )
            # The same pre-match card /lineup and /upcoming use, one per match.
            # Rosters are fetched concurrently and cached six hours, so a
            # tournament's second day costs nothing.
            cards = await asyncio.gather(
                *(build_card(self.bot, m, sub["game"]) for m, _ in chunk)
            )
            for n, card in enumerate(cards, start=start + 1):
                # Numbered to match the vote buttons underneath.
                card.title = f"{n}. {card.title}"[:256]
            try:
                message = await channel.send(
                    embeds=[header, *cards],
                    view=(self._vote_view(digest_id, chunk, start)
                          if votes else None),
                )
            except discord.Forbidden:
                log.warning("digest: missing permission to post in %s", channel_id)
                return first_message is not None
            except discord.HTTPException as exc:
                log.warning("digest: failed to post in %s: %s", channel_id, exc)
                return first_message is not None
            if first_message is None:
                first_message = message

        await self.bot.db.set_digest_message(digest_id, first_message.id)
        log.info(
            "Posted digest %s to channel %s (%d matches in %d message(s), %s)",
            digest_id, channel_id, len(matches), len(chunks), date_key,
        )
        return True

    def _vote_view(self, digest_id: int, chunk, start: int) -> discord.ui.View:
        """One action row per match: its two teams, side by side."""
        view = discord.ui.View(timeout=None)
        icons = self.bot.icons
        for row, (match, (a, b)) in enumerate(chunk):
            match_id = int(match["id"])
            number = start + row + 1
            for side, team in ((0, a), (1, b)):
                view.add_item(
                    DigestVoteButton(
                        digest_id,
                        match_id,
                        side,
                        # Numbered to match the embed line, so a team playing
                        # twice in one day still maps to the right row.
                        label=f"{number}. {team['name']}",
                        emoji=icons.partial(team),
                        row=row,
                    )
                )
        return view

    async def _todays_matches(
        self, sub, today, tz=None, hour: int = 0, *, force: bool = False
    ) -> list[tuple[dict, tuple[dict, dict]]]:
        """Tier-1 matches this digest owns: today, up to the next post."""
        tz = tz or self.bot.settings.digest_tz
        slug = resolve_slug(sub["game"], self.bot.settings.cs_slug)
        try:
            feed = await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
            feed += await self.bot.api.running_matches(slug=slug, per_page=FETCH_SIZE)
        except PandaScoreError as exc:
            log.warning("digest: could not fetch %s: %s", sub["game"], exc)
            return []

        by_league = (sub["scope"] or "game") == "league"
        wanted = int(sub["league_id"] if by_league else sub["tournament_id"])
        # Nothing is suppressed for having appeared on an earlier card, and
        # nothing needs to be: each digest owns a window that ends where the
        # next one begins, so a match belongs to exactly one of them.
        out: list[tuple[dict, tuple[dict, dict]]] = []
        seen: set[int] = set()
        for match in filter_for(self.bot.settings, feed):
            mid = int(match.get("id", 0))
            if mid in seen:
                continue
            got = match_league_id(match) if by_league else match_tournament_id(match)
            if got != wanted:
                continue
            begin = parse_dt(match.get("begin_at"))
            if not self._owns(begin, today, tz, hour, force):
                continue
            # A digest is a prediction sheet, and predictions close at kick-off,
            # so a match already under way is a dead row with dead buttons. At
            # the default midnight post nothing has started yet and this never
            # fires; it earns its keep for a server that moves its digest to
            # the evening, where half the day would otherwise be history.
            if begin is not None and begin <= datetime.now(timezone.utc):
                continue
            teams = _teams_of(match)
            if teams is None:
                continue          # unconfirmed bracket slot: nothing to vote on
            seen.add(mid)
            out.append((match, teams))

        out.sort(key=lambda pair: parse_dt(pair[0].get("begin_at")) or datetime.max)
        return out[:MAX_MATCHES]

    @staticmethod
    def _owns(begin, day, tz, hour: int, force: bool) -> bool:
        """Whether this digest is the one that should carry *begin*.

        The scheduled post owns a fixed window so that consecutive days can't
        overlap. `/schedule` is a deliberate catch-up, though, and someone
        running it at noon wants what is left of today — not the window that
        starts at tonight's post — so a forced run reaches back to now.
        """
        if begin is None:
            return False
        if not force:
            return within_digest_window(begin, day, tz, hour)
        _, end = digest_window(day, tz, hour)
        return datetime.now(timezone.utc) <= begin.astimezone(timezone.utc) < end

    def _build_header(
        self, sub, today, chunk, *, total: int, part: int, parts: int, tz
    ) -> discord.Embed:
        """The banner above the day's cards.

        The old flat list of matchups lived here; each match now gets its own
        lineup card underneath, so repeating them would just be duplication.
        """
        name = _target_name(sub) or "Tournament"
        flag = region_flag(name) or ""

        title = f"📅 Today · {flag} {name}".strip()
        if parts > 1:
            title = f"{title} ({part + 1}/{parts})"

        embed = discord.Embed(title=title, color=BRAND)
        embed.add_field(
            name="Matches",
            value=f"{len(chunk)} of {total}" if parts > 1 else str(total),
            inline=True,
        )
        embed.add_field(name="Game", value=label_for(sub["game"]), inline=True)
        early = [m for m, _ in chunk
                 if is_after_midnight(parse_dt(m.get("begin_at")), today, tz)]
        if early:
            # These would otherwise look like a mistake: a card dated tomorrow
            # under a banner that says Today.
            embed.add_field(
                name="Overnight",
                value=f"{len(early)} of these start after midnight — "
                      f"predict them now, not in the morning.",
                inline=False,
            )
        embed.set_footer(
            text=f"{today:%a %d %b} · times in your local zone · "
            f"tap a team to predict the winner"
        )
        return embed

    async def _add_standings(
        self, embed, guild_id, tournament_id=None, tournament_name=None
    ) -> None:
        """Server leaderboard and how the last few picks went.

        Only on the first message of a day — repeating it under every follow-up
        chunk would bury the actual schedule.

        The board is scoped to this server rather than reusing ``users.points``,
        which is a global running total and would rank people by activity in
        servers nobody here can see.
        """
        if not guild_id:
            return
        try:
            board = await self.bot.db.tournament_leaderboard(
                int(guild_id), tournament_id, limit=5
            )
            recent = await self.bot.db.recent_results(int(guild_id), limit=5)
        except Exception:  # noqa: BLE001 - the schedule matters more
            log.exception("could not build digest standings for guild %s", guild_id)
            return

        if board:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for n, row in enumerate(board):
                won, lost = int(row["won"] or 0), int(row["lost"] or 0)
                total = won + lost
                rate = f"{round(100 * won / total)}%" if total else "—"
                mark = medals[n] if n < len(medals) else f"`{n + 1}.`"
                lines.append(
                    f"{mark} <@{row['discord_id']}> — **{int(row['points'])}** pts · "
                    f"{won}W/{lost}L · {rate}"
                )
            embed.add_field(
                name=f"🏆 {tournament_name or 'Leaderboard'}"[:256],
                value="\n".join(lines)[:1024],
                inline=False,
            )

        if recent:
            lines = []
            for row in recent:
                won = row["status"] == "won"
                versus = f" vs {row['opponent_team_name']}" if row["opponent_team_name"] else ""
                lines.append(
                    f"{'✅' if won else '❌'} <@{row['discord_id']}> — "
                    f"{row['predicted_team_name']}{versus}"
                )
            embed.add_field(
                name="🔮 Recent results",
                value="\n".join(lines)[:1024],
                inline=False,
            )

    @sweep.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
