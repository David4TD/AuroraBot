"""Daily schedule digest for tournament alert subscriptions.

At local midnight (``DIGEST_HOUR`` in ``DIGEST_TZ``) every channel subscribed to
a tournament gets one message listing that tournament's matches for the day,
with a dropdown for predicting winners.

Two design points worth knowing:

* **The dropdown is persistent.** It's a :class:`discord.ui.DynamicItem`, so the
  digest keeps working after a restart without re-registering per-message views —
  the interaction is routed by its ``custom_id``, and the matches are re-read
  from ``digest_matches``. Picking a match replies *ephemerally* with two team
  buttons, so voting never clutters the channel.
* **Posting is claimed before it happens.** The ``match_digests`` UNIQUE key on
  (channel, subscription, local date) is taken first, so neither the 5-minute
  catch-up sweep nor a restart can post the same day twice. A crash between
  claim and post costs one digest instead of risking a loop of duplicates.

The sweep runs every 5 minutes rather than firing exactly at midnight so a bot
that was offline at 00:00 still posts when it comes back.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.embeds import BRAND, GREEN
from ..utils.games import label_for, resolve_slug
from ..utils.matches import (
    league_id as match_league_id,
    opponents,
    tournament_id as match_tournament_id,
)
from ..utils.predictions import Outcome, submit_prediction
from ..utils.regions import region_flag
from ..utils.schedule import iso_date, local_hhmm, local_now, local_today, within_day
from ..utils.tiers import filter_for
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.digest")

SWEEP_MINUTES = 5
FETCH_SIZE = 50
MAX_ROWS = 25          # Discord's hard cap on select options
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


class TeamVoteButton(discord.ui.Button):
    """One team on the ephemeral vote prompt."""

    def __init__(self, cog: "Digest", row, team: dict, opponent: dict) -> None:
        super().__init__(label=team["name"][:80], style=discord.ButtonStyle.primary)
        self.cog = cog
        self.row_ = row
        self.team = team
        self.opponent = opponent

    async def callback(self, interaction: discord.Interaction) -> None:
        _, message = await submit_prediction(
            self.cog.bot.db,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            match_id=int(self.row_["match_id"]),
            game=None,
            team=self.team,
            opponent=self.opponent,
            begin_at=self.row_["begin_at"],
        )
        await interaction.response.edit_message(content=message, view=None)


class DigestSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"aurora:digest:(?P<digest_id>[0-9]+)",
):
    """Match picker on a digest message.

    Rebuilt from its ``custom_id`` on every interaction, which is what lets it
    survive restarts. The options Discord already stored on the message are
    enough to render it; only the *selected* value matters on the way back.
    """

    def __init__(self, digest_id: int, options: list[discord.SelectOption] | None = None) -> None:
        self.digest_id = digest_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"aurora:digest:{digest_id}",
                placeholder="Pick a match to predict…",
                options=options
                or [discord.SelectOption(label="No matches", value="none")],
                min_values=1,
                max_values=1,
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Select,
        match: re.Match[str],
    ) -> "DigestSelect":
        return cls(int(match["digest_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.item.values[0]
        if value == "none":
            await interaction.response.send_message(
                "Nothing to predict on this one.", ephemeral=True
            )
            return

        db = interaction.client.db
        row = await db.digest_match(self.digest_id, int(value))
        if row is None:
            await interaction.response.send_message(
                "That match is no longer on this schedule.", ephemeral=True
            )
            return

        team_a = {"id": int(row["team_a_id"]), "name": row["team_a_name"]}
        team_b = {"id": int(row["team_b_id"]), "name": row["team_b_name"]}

        cog = interaction.client.get_cog("Digest")
        view = discord.ui.View(timeout=120)
        view.add_item(TeamVoteButton(cog, row, team_a, team_b))
        view.add_item(TeamVoteButton(cog, row, team_b, team_a))

        starts = parse_dt(row["begin_at"])
        when = f" — starts <t:{int(starts.timestamp())}:t>" if starts else ""
        await interaction.response.send_message(
            f"Who wins **{team_a['name']}** vs **{team_b['name']}**?{when}",
            view=view,
            ephemeral=True,
        )


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
        await interaction.response.defer(ephemeral=True, thinking=True)
        subs = [
            s
            for s in await self.bot.db.list_subscriptions(interaction.guild_id)
            if _digestable(s) and int(s["channel_id"]) == interaction.channel_id
        ]
        if not subs:
            await interaction.followup.send(
                "This channel has no tournament alerts. Add one with "
                "`/alerts add game:… tournament:…`.",
                ephemeral=True,
            )
            return
        posted = 0
        for sub in subs:
            if await self._post_digest(sub, local_today(self.bot.settings.digest_tz),
                                      force=True):
                posted += 1
        await interaction.followup.send(
            f"Posted {posted} schedule message(s)." if posted
            else "Nothing scheduled for today in those tournaments.",
            ephemeral=True,
        )

    # ── daily sweep ──────────────────────────────────────────────────────────
    @tasks.loop(minutes=SWEEP_MINUTES)
    async def sweep(self) -> None:
        try:
            await self._sweep_once()
        except Exception:  # noqa: BLE001 - the loop must outlive failures
            log.exception("digest sweep failed")

    async def _sweep_once(self) -> None:
        settings = self.bot.settings
        tz = settings.digest_tz
        now = local_now(tz)
        if now.hour < settings.digest_hour:
            return  # today's post isn't due yet

        today = local_today(tz)
        subs = [s for s in await self.bot.db.all_subscriptions() if _digestable(s)]
        if not subs:
            return

        # Respect /games mutes, same as the alert poll.
        muted = await self.bot.db.all_disabled_games()
        for sub in subs:
            if sub["game"] in muted.get(str(sub["guild_id"]), frozenset()):
                continue
            await self._post_digest(sub, today)

        await self.bot.db.prune_digests(days=PRUNE_AFTER_DAYS)

    async def _post_digest(self, sub, today, *, force: bool = False) -> bool:
        """Post one subscription's schedule for *today*. True if a message went out."""
        channel_id = int(sub["channel_id"])
        date_key = iso_date(today)

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

        matches = await self._todays_matches(sub, today)
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
            }
            for m, t in matches
        ]
        await self.bot.db.add_digest_matches(digest_id, stored)

        embed = self._build_embed(sub, today, matches)
        view = discord.ui.View(timeout=None)
        view.add_item(DigestSelect(digest_id, self._options(matches)))

        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.warning("digest: missing permission to post in %s", channel_id)
            return False
        except discord.HTTPException as exc:
            log.warning("digest: failed to post in %s: %s", channel_id, exc)
            return False

        await self.bot.db.set_digest_message(digest_id, message.id)
        log.info(
            "Posted digest %s to channel %s (%d matches, %s)",
            digest_id, channel_id, len(matches), date_key,
        )
        return True

    async def _todays_matches(self, sub, today) -> list[tuple[dict, tuple[dict, dict]]]:
        """Tier-1 matches for this subscription's tournament falling on *today*."""
        tz = self.bot.settings.digest_tz
        slug = resolve_slug(sub["game"], self.bot.settings.cs_slug)
        try:
            feed = await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
            feed += await self.bot.api.running_matches(slug=slug, per_page=FETCH_SIZE)
        except PandaScoreError as exc:
            log.warning("digest: could not fetch %s: %s", sub["game"], exc)
            return []

        by_league = (sub["scope"] or "game") == "league"
        wanted = int(sub["league_id"] if by_league else sub["tournament_id"])
        out: list[tuple[dict, tuple[dict, dict]]] = []
        seen: set[int] = set()
        for match in filter_for(self.bot.settings, feed):
            mid = int(match.get("id", 0))
            if mid in seen:
                continue
            got = match_league_id(match) if by_league else match_tournament_id(match)
            if got != wanted:
                continue
            if not within_day(parse_dt(match.get("begin_at")), today, tz):
                continue
            teams = _teams_of(match)
            if teams is None:
                continue          # unconfirmed bracket slot: nothing to vote on
            seen.add(mid)
            out.append((match, teams))

        out.sort(key=lambda pair: parse_dt(pair[0].get("begin_at")) or datetime.max)
        return out[:MAX_ROWS]

    def _build_embed(self, sub, today, matches) -> discord.Embed:
        tz = self.bot.settings.digest_tz
        name = _target_name(sub) or "Tournament"
        flag = region_flag(name) or ""
        icons = self.bot.icons

        lines = []
        for match, (a, b) in matches:
            starts = parse_dt(match.get("begin_at"))
            # Discord renders <t:…:t> in each viewer's own timezone; the local
            # time in brackets anchors it to the digest's day.
            stamp = f"<t:{int(starts.timestamp())}:t>" if starts else "TBD"
            icon_a, icon_b = icons.icon(a), icons.icon(b)
            lines.append(
                f"{stamp} · {icon_a} **{a['name']}** vs {icon_b} **{b['name']}**".replace(
                    "  ", " "
                )
            )

        embed = discord.Embed(
            title=f"📅 Today · {flag} {name}".strip(),
            description="\n".join(lines),
            color=BRAND,
        )
        embed.add_field(name="Matches", value=str(len(matches)), inline=True)
        embed.add_field(name="Game", value=label_for(sub["game"]), inline=True)
        embed.set_footer(
            text=f"{today:%a %d %b} · times in your local zone · "
            f"pick a match below to predict a winner"
        )
        return embed

    def _options(self, matches) -> list[discord.SelectOption]:
        tz = self.bot.settings.digest_tz
        options = []
        for match, (a, b) in matches:
            starts = parse_dt(match.get("begin_at"))
            when = local_hhmm(starts, tz) if starts else "TBD"
            options.append(
                discord.SelectOption(
                    label=f"{when}  {a['name']} vs {b['name']}"[:100],
                    value=str(int(match["id"])),
                    description=(match.get("tournament") or {}).get("name", "")[:100]
                    or None,
                    emoji=self.bot.icons.partial(a),
                )
            )
        return options

    @sweep.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Digest(bot))
