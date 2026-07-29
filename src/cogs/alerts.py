"""Match alerts: subscribe a channel to a team, a tournament, or a whole game.

A background loop polls PandaScore for running + upcoming Tier 1 matches and
posts to every subscribed channel:

* a **reminder** ``ALERT_LEAD_MINUTES`` (default 30) before kick-off, carrying
  1️⃣/2️⃣ reactions so anyone can predict the winner without a slash command;
* a **live** alert the moment the match goes live.

Both are deduplicated per (match, state, channel) via ``alerted_matches``, so a
restart mid-window never double-posts. The loop also drives the health
heartbeat and prunes its own bookkeeping tables.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, GREEN, match_embed
from ..utils.games import label_for, rank_by_name, resolve_slug
from ..utils.matches import opponents, team_ids, tournament_id
from ..utils.predictions import Outcome, submit_prediction
from ..utils.tiers import filter_top_tier
from ..utils.tournaments import current_tournaments, parse_dt, tournament_label

log = logging.getLogger("aurorabot.cogs.alerts")

# Reaction → which of the two teams the user is backing.
PICK_EMOJI = ("1️⃣", "2️⃣")

# Autocomplete hits PandaScore on every keystroke; a short cache keeps the
# 3-second interaction budget comfortable and the rate limit happy.
TOURNAMENT_CACHE_SECONDS = 120

FETCH_SIZE = 50
PRUNE_AFTER_DAYS = 3
PRUNE_EVERY_POLLS = 60  # ~hourly at the default 60s poll interval


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # game key → (fetched_at, tournaments)
        self._tournament_cache: dict[str, tuple[datetime, list[dict]]] = {}
        self._polls = 0

    async def cog_load(self) -> None:
        self.poll_loop.change_interval(seconds=self.bot.settings.alert_poll_seconds)
        self.poll_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    def _slug(self, game_key: str) -> str:
        return resolve_slug(game_key, self.bot.settings.cs_slug)

    def _top_tier(self, items: list[dict]) -> list[dict]:
        return filter_top_tier(items, enabled=self.bot.settings.top_tier_only)

    # ── tournament lookup ────────────────────────────────────────────────────
    async def _current_tournaments(self, game_key: str) -> list[dict]:
        """Current Tier 1 tournaments for a game, cached briefly."""
        cached = self._tournament_cache.get(game_key)
        now = datetime.now(timezone.utc)
        if cached and (now - cached[0]).total_seconds() < TOURNAMENT_CACHE_SECONDS:
            return cached[1]

        slug = self._slug(game_key)
        try:
            running = await self.bot.api.running_tournaments(slug=slug, per_page=FETCH_SIZE)
            upcoming = await self.bot.api.upcoming_tournaments(slug=slug, per_page=FETCH_SIZE)
        except PandaScoreError as exc:
            log.warning("tournament lookup failed for %s: %s", game_key, exc)
            return cached[1] if cached else []

        tournaments = current_tournaments(self._top_tier(running + upcoming))
        self._tournament_cache[game_key] = (now, tournaments)
        return tournaments

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
        game = getattr(interaction.namespace, "game", None)
        if not game:
            return []
        tournaments = await self._current_tournaments(game)
        query = (current or "").strip().lower()
        choices = []
        for t in tournaments:
            label = tournament_label(t)
            if query and query not in label.lower():
                continue
            choices.append(app_commands.Choice(name=label, value=str(t["id"])))
            if len(choices) == 25:
                break
        return choices

    @alerts.command(
        name="add",
        description="Alert this channel about matches — by team, by tournament, or all.",
    )
    @app_commands.describe(
        game="Game to watch (required).",
        team="Only alert for this team in that game.",
        tournament="Only alert for this tournament (pick from the list).",
    )
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_autocomplete)
    async def add(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        team: str | None = None,
        tournament: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if team and tournament:
            await interaction.followup.send(
                "Pick either a **team** or a **tournament**, not both — "
                "run `/alerts add` twice if you want both.",
                ephemeral=True,
            )
            return

        scope = "game"
        team_id = team_name = None
        tour_id = tour_name = None

        if team:
            resolved = await self._resolve_team(team, game.value)
            if resolved is None:
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}** in {game.name}.", ephemeral=True
                )
                return
            scope, team_id, team_name = "team", resolved["id"], resolved["name"]

        elif tournament:
            resolved = await self._resolve_tournament(tournament, game.value)
            if resolved is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current {game.name} tournament. "
                    "Start typing and pick one from the list.",
                    ephemeral=True,
                )
                return
            scope, tour_id, tour_name = "tournament", resolved["id"], resolved["name"]

        created = await self.bot.db.add_subscription(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            game=game.value,
            created_by=interaction.user.id,
            scope=scope,
            team_id=team_id,
            team_name=team_name,
            tournament_id=tour_id,
            tournament_name=tour_name,
        )
        if not created:
            await interaction.followup.send(
                "That subscription already exists in this channel.", ephemeral=True
            )
            return

        if scope == "team":
            target = f"**{team_name}** ({game.name})"
        elif scope == "tournament":
            target = f"**{tour_name}**"
        else:
            target = f"all Tier 1 **{game.name}** matches"

        lead = self.bot.settings.alert_lead_minutes
        await interaction.followup.send(
            embed=discord.Embed(
                description=(
                    f"🔔 This channel will be alerted for {target}.\n"
                    f"You'll get a **{lead}-minute reminder** before each match "
                    f"and a ping when it goes **live**."
                ),
                color=GREEN,
            ),
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

    async def _resolve_tournament(self, value: str, game_key: str) -> dict | None:
        """Accept an autocomplete ID, or fall back to matching on name."""
        tournaments = await self._current_tournaments(game_key)

        if value.isdigit():
            wanted = int(value)
            for t in tournaments:
                if int(t["id"]) == wanted:
                    return {"id": wanted, "name": tournament_label(t)}
            # Typed/stale ID: confirm it exists before storing it.
            try:
                fetched = await self.bot.api.get_tournament(wanted)
            except PandaScoreError:
                return None
            if fetched.get("id"):
                return {"id": wanted, "name": tournament_label(fetched)}
            return None

        query = value.strip().lower()
        for t in tournaments:
            if query in tournament_label(t).lower():
                return {"id": int(t["id"]), "name": tournament_label(t)}
        return None

    @alerts.command(name="list", description="List this server's alert subscriptions.")
    async def list_subs(self, interaction: discord.Interaction) -> None:
        subs = await self.bot.db.list_subscriptions(interaction.guild_id)
        if not subs:
            await interaction.response.send_message(
                "No alerts configured. Use `/alerts add`.", ephemeral=True
            )
            return
        icon = {"team": "👥", "tournament": "🏆", "game": "🎮"}
        lines = []
        for s in subs:
            scope = s["scope"] or "game"
            if scope == "team":
                target = s["team_name"] or "a team"
            elif scope == "tournament":
                target = s["tournament_name"] or "a tournament"
            else:
                target = f"all {label_for(s['game'])}"
            lines.append(
                f"`#{s['id']}` {icon.get(scope, '•')} <#{s['channel_id']}> → "
                f"**{target}** · {label_for(s['game'])}"
            )
        embed = discord.Embed(
            title="🔔 Alert subscriptions", description="\n".join(lines), color=BRAND
        )
        embed.set_footer(
            text=f"Reminders fire {self.bot.settings.alert_lead_minutes} min before kick-off."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @alerts.command(name="remove", description="Remove a subscription by its ID.")
    @app_commands.describe(subscription_id="ID from /alerts list (e.g. 3).")
    async def remove(self, interaction: discord.Interaction, subscription_id: int) -> None:
        removed = await self.bot.db.remove_subscription(subscription_id, interaction.guild_id)
        if removed:
            await interaction.response.send_message(
                embed=discord.Embed(description="🗑️ Subscription removed.", color=GREEN),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "No subscription with that ID in this server.", ephemeral=True
            )

    # ── matching ─────────────────────────────────────────────────────────────
    @staticmethod
    def _matches_subscription(sub, match: dict) -> bool:
        scope = sub["scope"] or "game"
        if scope == "team":
            return sub["team_id"] is not None and int(sub["team_id"]) in team_ids(match)
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

        lead = self.bot.settings.alert_lead_minutes
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
                if 0 < minutes_out <= lead:
                    due.append(match)
            feeds[game_key] = (running, due)

        for sub in subs:
            running, due = feeds.get(sub["game"], ([], []))
            for match in due:
                await self._announce(sub, match, state="reminder")
            for match in running:
                await self._announce(sub, match, state="live")

        # Housekeeping is cheap but pointless every minute.
        self._polls += 1
        if self._polls % PRUNE_EVERY_POLLS == 1:
            removed = await self.bot.db.prune_alert_history(days=PRUNE_AFTER_DAYS)
            if removed:
                log.debug("Pruned %d stale alert rows", removed)

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
        predictable = state == "reminder" and len(teams) >= 2
        if state == "reminder":
            lead = self.bot.settings.alert_lead_minutes
            content = f"⏰ **Starting in ~{lead} minutes!**"
            if predictable:
                content += (
                    f"\n🎲 React {PICK_EMOJI[0]} for **{teams[0]['name']}** or "
                    f"{PICK_EMOJI[1]} for **{teams[1]['name']}** to predict the winner."
                )
        else:
            content = "🔴 **A match just went live!**"

        try:
            message = await channel.send(
                content=content, embed=match_embed(match, sub["game"])
            )
        except discord.Forbidden:
            log.warning("Missing permission to post in channel %s", channel_id)
            return
        except discord.HTTPException as exc:
            log.warning("Failed to post alert: %s", exc)
            return

        # Mark first: a failure while adding reactions must not cause a repost.
        await self.bot.db.mark_alerted(match_id, state, channel_id)

        # Reminders double as a prediction poll — reacting picks a winner.
        if predictable:
            await self._attach_prediction_reactions(message, match, teams, sub["game"])

    async def _attach_prediction_reactions(
        self, message: discord.Message, match: dict, teams: list[dict], game_key: str
    ) -> None:
        await self.bot.db.record_alert_message(
            message_id=message.id,
            channel_id=message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            match_id=int(match["id"]),
            game=game_key,
            team_a=(teams[0]["id"], teams[0]["name"]),
            team_b=(teams[1]["id"], teams[1]["name"]),
            begin_at=match.get("begin_at"),
        )
        try:
            for emoji in PICK_EMOJI:
                await message.add_reaction(emoji)
        except discord.Forbidden:
            log.warning(
                "Can't add prediction reactions in channel %s (missing permission)",
                message.channel.id,
            )
        except discord.HTTPException as exc:
            log.warning("Failed to add prediction reactions: %s", exc)

    @poll_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    # ── reaction predictions ─────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Turn a 1️⃣ / 2️⃣ reaction on a match reminder into a prediction.

        Raw events are used deliberately: they fire for messages posted before
        the current process started, so predictions keep working across
        restarts without re-hydrating any views.
        """
        if payload.user_id == (self.bot.user.id if self.bot.user else 0):
            return
        emoji = str(payload.emoji)
        if emoji not in PICK_EMOJI:
            return

        record = await self.bot.db.get_alert_message(payload.message_id)
        if record is None:
            return

        index = PICK_EMOJI.index(emoji)
        picks = [
            {"id": int(record["team_a_id"]), "name": record["team_a_name"]},
            {"id": int(record["team_b_id"]), "name": record["team_b_name"]},
        ]
        team, opponent = picks[index], picks[1 - index]

        member = payload.member
        display_name = member.display_name if member else f"User {payload.user_id}"

        outcome, message = await submit_prediction(
            self.bot.db,
            user_id=payload.user_id,
            display_name=display_name,
            match_id=int(record["match_id"]),
            game=record["game"],
            team=team,
            opponent=opponent,
            begin_at=record["begin_at"],
        )
        if outcome is Outcome.UNCHANGED:
            return  # nothing happened; don't spam a confirmation

        await self._confirm_privately(payload, message)

    async def _confirm_privately(
        self, payload: discord.RawReactionActionEvent, text: str
    ) -> None:
        """DM the reacting user; fall back to a short-lived channel message.

        A reaction has no interaction token, so there's no ephemeral reply
        available — DM first so the channel stays clean.
        """
        user = payload.member or self.bot.get_user(payload.user_id)
        if user is not None:
            try:
                await user.send(text)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass  # DMs closed — fall through to the channel

        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            await channel.send(f"<@{payload.user_id}> {text}", delete_after=20)
        except discord.HTTPException:
            log.debug("Could not confirm prediction for user %s", payload.user_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alerts(bot))
