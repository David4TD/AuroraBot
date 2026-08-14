"""Match alerts: subscribe a channel to a team, a tournament, or a whole game.

A background loop polls PandaScore for running + upcoming Tier 1 matches and
posts to every subscribed channel:

* a **reminder** ``ALERT_LEAD_MINUTES`` (default 30) before kick-off, carrying
  a button per team so anyone can predict the winner without a slash command;
* a **live** alert the moment the match goes live;
* a **result** summary when it finishes.

All three are deduplicated per (match, state, channel) via ``alerted_matches``,
so a restart mid-window never double-posts. The loop also drives the health
heartbeat and prunes its own bookkeeping tables.

Results can't come from a feed — a finished match is in neither the running nor
the upcoming list, and the generic past feed carries almost no Tier 1 matches
(see ``Scores._finished``). Instead the matches this channel already saw go
*live* are checked for a final score, which is both targeted and bounded.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, GREEN, match_embed
from ..utils.games import key_for_videogame, label_for, rank_by_name, resolve_slug
from ..utils.guildgames import blocked_message
from ..utils.guildprefs import alert_lead
from ..utils.matches import league_id, opponents, team_ids, tournament_id
from ..utils.predictions import submit_prediction
from ..utils.scoring import payout_for, potential_odds
from ..utils.settle import settle_match
from ..utils.conviction import pick_panel
from ..utils.subscriptions import wants_votes
from ..utils.regions import event_flag, region_flag
from ..utils.resultcard import build_result_card
from ..utils.tiers import filter_for
from ..utils.pickers import game_of, tournament_choices
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.alerts")

# The tournament directory refreshes on a shorter period than its own TTL,
# so the autocomplete is always served warm.
WARM_EVERY_MINUTES = 10

FETCH_SIZE = 50
PRUNE_AFTER_DAYS = 3

# Value prefixes and the autocomplete deadline now live with the shared
# tournament directory, re-exported here so existing importers keep working.
from ..services.tourneys import (  # noqa: E402  (grouped with the constants)
    AUTOCOMPLETE_BUDGET,
    LEAGUE_PREFIX,
    LOADING_SENTINEL,
    TOURNAMENT_PREFIX,
)

PRUNE_EVERY_POLLS = 60  # ~hourly at the default 60s poll interval
# Finished-match lookups per poll. One request each, so this bounds the cost
# of a backlog after downtime rather than firing dozens at once.
RESULTS_PER_POLL = 10


class VoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"aurora:vote:(?P<side>[01])",
):
    """One team's button on a match reminder.

    Persistent by construction: the item is rebuilt from its ``custom_id`` on
    every click, so reminders keep working across restarts without any view
    being re-registered. Only the *side* (0 or 1) is encoded — the match and its
    two teams are read back from ``alert_messages`` using the message the button
    is attached to, which keeps the custom_id short and the teams authoritative.

    Replies are **ephemeral**: only the person who clicked sees the outcome, so
    voting neither clutters the channel nor needs a DM.
    """

    def __init__(self, side: int, label: str = "…", emoji=None) -> None:
        self.side = side
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"aurora:vote:{side}",
                emoji=emoji,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["side"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        row = await db.get_alert_message(interaction.message.id)
        if row is None:
            # The row is written just after the message is sent, so a click in
            # that window finds nothing. Also covers a pruned old reminder.
            await interaction.response.send_message(
                "That match isn't open for predictions any more.", ephemeral=True
            )
            return

        picks = [
            {"id": int(row["team_a_id"]), "name": row["team_a_name"]},
            {"id": int(row["team_b_id"]), "name": row["team_b_name"]},
        ]
        team, opponent = picks[self.side], picks[1 - self.side]

        _, message = await submit_prediction(
            db,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            match_id=int(row["match_id"]),
            game=row["game"],
            team=team,
            opponent=opponent,
            begin_at=row["begin_at"],
            guild_id=interaction.guild_id,
            tournament_id=row["tournament_id"],
            tournament_name=row["tournament_name"],
        )
        view = await pick_panel(db, interaction.user.id, int(row["match_id"]))
        await interaction.response.send_message(message, view=view, ephemeral=True)


def can_manage(user) -> bool:
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


SCOPE_ICON = {"team": "👥", "league": "🏅", "tournament": "🏆", "game": "🎮"}
MAX_PICKER_OPTIONS = 25       # Discord's cap on select options


def describe_target(sub) -> str:
    """What a subscription watches, as plain text (no channel, no game)."""
    scope = sub["scope"] or "game"
    if scope == "team":
        return sub["team_name"] or "a team"
    if scope == "league":
        name = sub["league_name"] or "a league"
        flag = region_flag(name)
        return f"{flag} {name} (all stages)" if flag else f"{name} (all stages)"
    if scope == "tournament":
        name = sub["tournament_name"] or "a tournament"
        flag = region_flag(name)
        return f"{flag} {name}" if flag else name
    return f"all {label_for(sub['game'])}"


def subscription_embed(target: str, lead: int, predictions: bool) -> discord.Embed:
    """What a new (or just-toggled) subscription will do."""
    lines = [
        f"🔔 This channel will be alerted for {target}.",
        f"You'll get a **{lead}-minute reminder** before each match "
        f"and a ping when it goes **live**.",
    ]
    lines.append(
        "🎲 Reminders and the daily schedule carry **vote buttons**."
        if predictions
        else "🔕 **Predictions are off** here — alerts only, no vote buttons."
    )
    return discord.Embed(description="\n".join(lines), color=GREEN)


class PredictionToggleButton(discord.ui.Button):
    """Flip vote buttons for one subscription, from its confirmation."""

    def __init__(self, sub_id: int, predictions: bool) -> None:
        self.sub_id = sub_id
        self.predictions = predictions
        super().__init__(
            label="Turn predictions off" if predictions else "Turn predictions on",
            emoji="🔕" if predictions else "🎲",
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not can_manage(interaction.user):
            await interaction.response.send_message(
                "You need **Manage Server** to change alerts.", ephemeral=True
            )
            return
        wanted = not self.predictions
        changed = await interaction.client.db.set_subscription_predictions(
            self.sub_id, interaction.guild_id, wanted
        )
        if not changed:
            await interaction.response.edit_message(
                content="That subscription no longer exists.", embed=None, view=None
            )
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                description=(
                    "🎲 Predictions are **on** — reminders and the daily "
                    "schedule will carry vote buttons."
                    if wanted
                    else "🔕 Predictions are **off** — alerts only, no vote buttons."
                ),
                color=GREEN,
            ),
            view=PredictionToggleView(self.sub_id, wanted),
        )


class PredictionToggleView(discord.ui.View):
    def __init__(self, sub_id: int, predictions: bool) -> None:
        super().__init__(timeout=180)
        self.add_item(PredictionToggleButton(sub_id, predictions))


class RemoveSelect(discord.ui.Select):
    """Pick subscriptions to delete.

    Options carry the real database id as their value, so nothing depends on
    the numbers shown in ``/alerts list`` — those are per-server ordinals that
    shift the moment anything is removed, and asking someone to retype one was
    the old design's flaw.
    """

    def __init__(self, view_: "RemoveView", subs, bot) -> None:
        self._view = view_
        options = []
        for n, s in enumerate(subs[:MAX_PICKER_OPTIONS], start=1):
            scope = s["scope"] or "game"
            channel = bot.get_channel(int(s["channel_id"]))
            where = f"#{channel.name}" if channel else "deleted channel"
            options.append(
                discord.SelectOption(
                    label=f"{n}. {describe_target(s)}"[:100],
                    value=str(int(s["id"])),
                    description=f"{where} · {label_for(s['game'])}"[:100],
                    emoji=SCOPE_ICON.get(scope, "•"),
                )
            )
        super().__init__(
            placeholder="Select the alerts to remove…",
            options=options,
            min_values=1,
            max_values=len(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        removed = await self._view.bot.db.remove_subscriptions(
            self.values, interaction.guild_id
        )
        self._view.stop()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                description=(
                    f"🗑️ Removed **{removed}** alert{'s' if removed != 1 else ''}."
                    if removed
                    else "Those alerts were already gone."
                ),
                color=GREEN,
            ),
            view=None,
        )


class RemoveAllButton(discord.ui.Button):
    """Clear every alert in the server — behind a second, explicit click."""

    def __init__(self, view_: "RemoveView", count: int) -> None:
        self._view = view_
        self.count = count
        self.armed = False
        super().__init__(label=f"Remove all {count}", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction) -> None:
        if not self.armed:
            # Deleting a server's whole alert config is not something to do on
            # a stray click, and there is no undo.
            self.armed = True
            self.label = f"Click again to confirm ({self.count})"
            await interaction.response.edit_message(view=self._view)
            return

        # Every subscription in scope, not just the 25 the picker could show.
        ids = [int(s["id"]) for s in self._view.subs]
        removed = await self._view.bot.db.remove_subscriptions(ids, interaction.guild_id)
        self._view.stop()
        await interaction.response.edit_message(
            content=None,
            embed=discord.Embed(
                description=(
                    f"🗑️ Removed all **{removed}** {self._view.scope} alerts."
                ),
                color=GREEN,
            ),
            view=None,
        )


class RemoveView(discord.ui.View):
    def __init__(self, bot, subs, scope: str = "") -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.subs = subs
        # "Remove all" clears whatever the command scoped to, so the wording has
        # to follow the filter rather than always claiming the whole server.
        self.scope = scope or "server"
        self.add_item(RemoveSelect(self, subs, bot))
        self.add_item(RemoveAllButton(self, len(subs)))


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._polls = 0

    async def cog_load(self) -> None:
        self.poll_loop.change_interval(seconds=self.bot.settings.alert_poll_seconds)
        self.poll_loop.start()
        self.warm_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()
        self.warm_loop.cancel()

    @tasks.loop(minutes=WARM_EVERY_MINUTES)
    async def warm_loop(self) -> None:
        """Keep the tournament cache warm for every game any server follows.

        Refreshing on a shorter period than the TTL means the autocomplete never
        has to wait on the network. Only *followed* games are warmed, so the
        cost scales with what's actually in use — nothing at all until someone
        runs /games.
        """
        try:
            games = await self.bot.db.distinct_enabled_games()
            if games:
                self.bot.tourneys.prewarm(games)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("tournament pre-warm failed")

    @warm_loop.before_loop
    async def _before_warm(self) -> None:
        await self.bot.wait_until_ready()

    def _slug(self, game_key: str) -> str:
        return resolve_slug(game_key, self.bot.settings.cs_slug)

    def _top_tier(self, items: list[dict]) -> list[dict]:
        return filter_for(self.bot.settings, items)

    # ── slash command group ──────────────────────────────────────────────────
    alerts = app_commands.Group(
        name="alerts",
        description="Manage match alerts for this channel.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    async def _tournament_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return await tournament_choices(
            self.bot.tourneys, game_of(interaction), current
        )

    @alerts.command(
        name="add",
        description="Alert this channel about matches — by team, by tournament, or all.",
    )
    @app_commands.describe(
        game="Game to watch (required).",
        team="Only alert for this team in that game.",
        tournament="A whole league, or one stage of it (pick from the list).",
        predictions="Put vote buttons on this subscription's alerts. Default: yes.",
    )
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_autocomplete)
    async def add(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        team: str | None = None,
        tournament: str | None = None,
        predictions: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if team and tournament:
            await interaction.followup.send(
                "Pick either a **team** or a **tournament**, not both — "
                "run `/alerts add` twice if you want both.",
                ephemeral=True,
            )
            return

        # Refuse up front rather than storing a subscription the poll will skip.
        if game.value not in await self.bot.db.enabled_games(interaction.guild_id):
            await interaction.followup.send(blocked_message(game.value), ephemeral=True)
            return

        scope = "game"
        team_id = team_name = None
        tour_id = tour_name = None
        lg_id = lg_name = None

        if team:
            resolved = await self._resolve_team(team, game.value)
            if resolved is None:
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}** in {game.name}.", ephemeral=True
                )
                return
            scope, team_id, team_name = "team", resolved["id"], resolved["name"]

        elif tournament:
            resolved = await self.bot.tourneys.resolve(tournament, game.value)
            if resolved is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current {game.name} league "
                    "or tournament. Start typing and pick one from the list.",
                    ephemeral=True,
                )
                return
            scope = resolved["scope"]
            if scope == "league":
                lg_id, lg_name = resolved["id"], resolved["name"]
            else:
                tour_id, tour_name = resolved["id"], resolved["name"]

        created = await self.bot.db.add_subscription(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            game=game.value,
            created_by=interaction.user.id,
            scope=scope,
            team_id=team_id,
            team_name=team_name,
            league_id=lg_id,
            league_name=lg_name,
            tournament_id=tour_id,
            tournament_name=tour_name,
            predictions=predictions,
        )
        if not created:
            await interaction.followup.send(
                "That subscription already exists in this channel.", ephemeral=True
            )
            return

        if scope == "team":
            target = f"**{team_name}** ({game.name})"
        elif scope == "league":
            target = f"**{lg_name}** — every stage"
        elif scope == "tournament":
            target = f"**{tour_name}**"
        else:
            target = f"all Tier 1 **{game.name}** matches"

        lead = alert_lead(
            self.bot.settings, await self.bot.db.guild_settings(interaction.guild_id)
        )
        await interaction.followup.send(
            embed=subscription_embed(target, lead, predictions),
            # The toggle rides along so the choice can be changed without
            # removing and re-adding the subscription.
            view=PredictionToggleView(created, predictions),
            ephemeral=True,
        )

    async def _resolve_team(self, query: str, game_key: str) -> dict | None:
        try:
            results = await self.bot.api.search_teams(
                query, slug=self._slug(game_key), per_page=25
            )
        except PandaScoreError:
            return None
        if not results:
            return None
        best = rank_by_name(results, query)[0]  # exact name wins over partials
        return {"id": int(best["id"]), "name": best.get("name", "Team")}

    @alerts.command(name="list", description="List this server's alert subscriptions.")
    @app_commands.describe(game="Only show alerts for this game.")
    @app_commands.choices(game=GAME_CHOICES)
    async def list_subs(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        subs = await self.bot.db.list_subscriptions(interaction.guild_id)
        if game is not None:
            subs = [s for s in subs if s["game"] == game.value]
        if not subs:
            await interaction.response.send_message(
                f"No {game.name} alerts configured." if game
                else "No alerts configured. Use `/alerts add`.",
                ephemeral=True,
            )
            return

        # Grouped by channel, because that's how someone thinks about them —
        # "what does #esports get?" — rather than as one flat list.
        by_channel: dict[str, list] = {}
        for s in subs:
            by_channel.setdefault(str(s["channel_id"]), []).append(s)

        embed = discord.Embed(title="🔔 Alert subscriptions", color=BRAND)
        orphans = 0
        n = 0
        for channel_id, group in by_channel.items():
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                orphans += len(group)
                heading = "⚠️ deleted channel"
            else:
                heading = f"#{channel.name}"
            lines = []
            for s in group:
                n += 1
                scope = s["scope"] or "game"
                quiet = "" if wants_votes(s) else " · 🔕 no predictions"
                lines.append(
                    f"`{n}.` {SCOPE_ICON.get(scope, '•')} **{describe_target(s)}** "
                    f"· {label_for(s['game'])}{quiet}"
                )
            embed.add_field(name=heading, value="\n".join(lines), inline=False)

        footer = (
            f"Reminders fire {alert_lead(self.bot.settings, await self.bot.db.guild_settings(interaction.guild_id))} min before "
            f"kick-off · /alerts remove to clear any of these"
        )
        if orphans:
            footer = (
                f"{orphans} alert(s) point at a deleted channel — "
                f"/alerts remove will clear them. " + footer
            )
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alerts.command(name="remove", description="Remove alerts — pick them from a list.")
    @app_commands.describe(game="Narrow the list to one game.")
    @app_commands.choices(game=GAME_CHOICES)
    async def remove(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        subs = await self.bot.db.list_subscriptions(interaction.guild_id)
        if game is not None:
            subs = [s for s in subs if s["game"] == game.value]
        if not subs:
            await interaction.response.send_message(
                f"No {game.name} alerts to remove." if game
                else "No alerts configured in this server.",
                ephemeral=True,
            )
            return

        content = None
        if len(subs) > MAX_PICKER_OPTIONS:
            content = (
                f"Showing the first **{MAX_PICKER_OPTIONS}** of {len(subs)} alerts — "
                f"run `/alerts remove game:…` to narrow the list."
            )
        await interaction.response.send_message(
            content=content,
            view=RemoveView(self.bot, subs, scope=game.name if game else "server"),
            ephemeral=True,
        )

    # ── matching ─────────────────────────────────────────────────────────────
    @staticmethod
    def _matches_subscription(sub, match: dict) -> bool:
        scope = sub["scope"] or "game"
        if scope == "team":
            return sub["team_id"] is not None and int(sub["team_id"]) in team_ids(match)
        if scope == "league":
            # Covers every stage of the league, however PandaScore splits it.
            return (
                sub["league_id"] is not None
                and league_id(match) == int(sub["league_id"])
            )
        if scope == "tournament":
            return (
                sub["tournament_id"] is not None
                and tournament_id(match) == int(sub["tournament_id"])
            )
        return True  # whole-game feed

    # ── background poll ──────────────────────────────────────────────────────
    @tasks.loop(seconds=60)
    async def poll_loop(self) -> None:
        try:
            await self._poll_once()
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("alert poll iteration failed")
        finally:
            self.bot.health.beat()

    async def _poll_once(self) -> None:
        subs = await self.bot.db.all_subscriptions()
        if not subs:
            return

        # Honour each server's /games selection before picking which feeds to
        # fetch, so an unfollowed game costs no API call at all. Subscriptions
        # for a game later removed stay in the table and simply go quiet — they
        # resume if it's re-added.
        enabled_by_guild = await self.bot.db.all_enabled_games()
        subs = [
            s
            for s in subs
            if s["game"] in enabled_by_guild.get(str(s["guild_id"]), frozenset())
        ]
        if not subs:
            return

        # Lead time is per server, so keep every upcoming match with its
        # distance from now and let each subscription apply its own window.
        prefs = await self.bot.db.all_guild_settings()
        widest = max(
            [alert_lead(self.bot.settings, p) for p in prefs.values()]
            + [self.bot.settings.alert_lead_minutes]
        )
        now = datetime.now(timezone.utc)

        # Fetch each subscribed game once, not once per subscription.
        feeds: dict[str, tuple[list[dict], list[dict]]] = {}
        for game_key in {s["game"] for s in subs}:
            slug = self._slug(game_key)
            try:
                running = self._top_tier(
                    await self.bot.api.running_matches(slug=slug, per_page=FETCH_SIZE)
                )
                upcoming = self._top_tier(
                    await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
                )
            except PandaScoreError as exc:
                log.warning("poll: could not fetch %s: %s", game_key, exc)
                running, upcoming = [], []
            # Only matches inside the reminder window are worth announcing.
            due = []
            for match in upcoming:
                begin = parse_dt(match.get("begin_at"))
                if begin is None:
                    continue
                minutes_out = (begin - now).total_seconds() / 60
                if 0 < minutes_out <= widest:
                    due.append((match, minutes_out))
            feeds[game_key] = (running, due)

        for sub in subs:
            running, due = feeds.get(sub["game"], ([], []))
            lead = alert_lead(self.bot.settings, prefs.get(str(sub["guild_id"])))
            for match, minutes_out in due:
                if minutes_out > lead:
                    continue      # inside another server's window, not this one
                await self._announce(sub, match, state="reminder")
            for match in running:
                await self._announce(sub, match, state="live")

        await self._announce_results()

        # Housekeeping is cheap but pointless every minute.
        self._polls += 1
        if self._polls % PRUNE_EVERY_POLLS == 1:
            removed = await self.bot.db.prune_alert_history(days=PRUNE_AFTER_DAYS)
            if removed:
                log.debug("Pruned %d stale alert rows", removed)

    async def _announce_results(self) -> None:
        """Post the summary for any match a channel saw go live and finish.

        Driven from ``alerted_matches`` rather than a feed: a finished match
        appears in neither the running nor the upcoming list, and the generic
        past feed is useless for Tier 1 (see ``Scores._finished``). The matches
        we announced as live are precisely the ones a channel is waiting on.

        Each pending match costs one ``/matches/{id}`` call, deduplicated
        across channels and capped per poll, so a busy evening or a restart
        backlog drains steadily instead of in a burst.
        """
        try:
            pending = await self.bot.db.pending_result_alerts(limit=RESULTS_PER_POLL)
        except Exception:  # noqa: BLE001 - never take the poll down with us
            log.exception("could not list pending result alerts")
            return
        if not pending:
            return

        by_match: dict[int, list[int]] = {}
        for row in pending:
            by_match.setdefault(int(row["match_id"]), []).append(int(row["channel_id"]))

        for match_id, channels in by_match.items():
            try:
                match = await self.bot.api.get_match(match_id)
            except PandaScoreError as exc:
                log.warning("result lookup failed for match %s: %s", match_id, exc)
                continue
            status = (match.get("status") or "").lower()
            if status != "finished":
                # Still playing, or abandoned without a result — try next poll.
                # The row is pruned after PRUNE_AFTER_DAYS either way, so a
                # match that never finishes stops being asked about.
                continue

            # Settle before rendering, or the leaderboard on the card would be
            # missing the very match it's announcing — the resolve loop only
            # runs every few minutes. Idempotent, so the loop finding it first
            # is fine too.
            winner = match.get("winner_id")
            if winner is not None:
                try:
                    await settle_match(self.bot, match_id, int(winner), match)
                except Exception:  # noqa: BLE001 - the result still goes out
                    log.exception("could not settle match %s", match_id)

            game_key = key_for_videogame(
                match.get("videogame") or {}, self.bot.settings.cs_slug
            )
            for channel_id in channels:
                await self._post_result(match, game_key, channel_id)

    async def _post_result(self, match: dict, game_key, channel_id: int) -> None:
        match_id = int(match["id"])
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            # Channel gone: mark it done so we stop re-fetching this match.
            await self.bot.db.mark_alerted(match_id, "finished", channel_id)
            return

        guild_id = channel.guild.id if getattr(channel, "guild", None) else None
        try:
            embed = await build_result_card(self.bot, match, game_key, guild_id)
            await channel.send(content="🏁 **Full time!**", embed=embed)
        except discord.Forbidden:
            log.warning("Missing permission to post results in %s", channel_id)
        except discord.HTTPException as exc:
            log.warning("Failed to post result in %s: %s", channel_id, exc)
        finally:
            # Marked regardless: a channel we can't post to must not be retried
            # every minute for three days.
            await self.bot.db.mark_alerted(match_id, "finished", channel_id)

    async def _announce(self, sub, match: dict, *, state: str) -> None:
        """Post one alert, unless this channel already saw this match/state."""
        if not self._matches_subscription(sub, match):
            return

        match_id = int(match["id"])
        channel_id = int(sub["channel_id"])
        if await self.bot.db.was_alerted(match_id, state, channel_id):
            return

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        teams = opponents(match)
        # A subscription can opt out of the prediction game entirely: alerts
        # still fire, they just carry no vote buttons.
        votes = wants_votes(sub)
        predictable = state == "reminder" and len(teams) >= 2 and votes
        if state == "reminder":
            lead = alert_lead(
                self.bot.settings,
                await self.bot.db.guild_settings(sub["guild_id"]),
            )
            content = f"⏰ **Starting in ~{lead} minutes!**"
            if predictable:
                content += "\n🎲 Pick the winner below — only you see the result."
        else:
            content = "🔴 **A match just went live!**"

        view = None
        if predictable:
            view = discord.ui.View(timeout=None)
            for side, team in enumerate(teams[:2]):
                view.add_item(
                    VoteButton(side, team["name"], self.bot.icons.partial(team))
                )

        embed = match_embed(match, sub["game"], self.bot.icons)
        if state != "reminder" and len(teams) >= 2:
            # Predictions have closed, so naming who backed whom spoils nothing
            # and gives the channel something to argue about while it plays.
            await self._add_votes(embed, match_id, sub["guild_id"], teams)

        try:
            message = await channel.send(
                content=content,
                embed=embed,
                view=view,
            )
        except discord.Forbidden:
            log.warning("Missing permission to post in channel %s", channel_id)
            return
        except discord.HTTPException as exc:
            log.warning("Failed to post alert: %s", exc)
            return

        # Mark first: a later failure must not cause a repost.
        await self.bot.db.mark_alerted(match_id, state, channel_id)

        # Reminders double as a prediction poll — the buttons pick a winner.
        if predictable:
            await self._attach_vote_buttons(message, match, teams, sub["game"])

    async def _add_votes(self, embed, match_id: int, guild_id, teams) -> None:
        """List this server's predictions on a going-live embed, side by side.

        Scoped to the server the alert is posting in — predictions are stored
        per user, so an unscoped list would print names from every other server
        the bot serves into this channel.
        """
        if not guild_id:
            return
        try:
            picks = await self.bot.db.match_predictions(match_id, int(guild_id))
        except Exception:  # noqa: BLE001 - an alert must still go out
            log.exception("could not read predictions for match %s", match_id)
            return
        if not picks:
            return

        icons = self.bot.icons
        for team in teams[:2]:
            backers = [
                # A doubled-down pick is public the moment it can't be changed:
                # spending a token is a statement, and it should read like one.
                p["display_name"] + (" 💥" if p["doubled"] else "")
                for p in picks
                if int(p["predicted_team_id"]) == int(team["id"])
            ]
            share = f"{len(backers)}/{len(picks)}"
            # Predictions are closed, so the multiplier is settled — showing
            # what this side is now worth is what makes backing the unpopular
            # one feel like a call rather than a coin toss.
            odds = potential_odds(len(backers), len(picks))
            worth = payout_for(len(backers), len(picks)).points
            price = f" · ×{odds:.1f} · {worth} pts" if odds > 1.0 else f" · {worth} pts"
            value = ", ".join(backers[:12]) or "_Nobody_"
            if len(backers) > 12:
                value += f" _+{len(backers) - 12} more_"
            embed.add_field(
                name=f"{icons.icon(team)} {team['name']} · {share}{price}".strip()[:256],
                value=value[:1024],
                inline=True,
            )

    async def _attach_vote_buttons(
        self, message: discord.Message, match: dict, teams: list[dict], game_key: str
    ) -> None:
        """Record which match a reminder announced, so its buttons can resolve.

        The row is written after sending because it's keyed by message id.
        A click in the intervening milliseconds is handled by the button,
        which asks the user to try again rather than erroring.
        """
        await self.bot.db.record_alert_message(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            match_id=int(match["id"]),
            game=game_key,
            team_a=(teams[0]["id"], teams[0]["name"]),
            team_b=(teams[1]["id"], teams[1]["name"]),
            begin_at=match.get("begin_at"),
            tournament_id=tournament_id(match),
            tournament_name=(match.get("tournament") or {}).get("name"),
        )

    @poll_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alerts(bot))
