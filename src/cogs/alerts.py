"""Match alerts: subscribe channels to teams/games and get pinged on go-live.

A background loop polls PandaScore for running + upcoming matches and posts an
embed to any subscribed channel when a relevant match goes live (deduplicated
via the ``alerted_matches`` table). The loop also updates the health heartbeat.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, GREEN, RED, match_embed
from ..utils.games import label_for, resolve_slug

log = logging.getLogger("aurorabot.cogs.alerts")


def _match_team_ids(match: dict) -> set[int]:
    ids: set[int] = set()
    for o in match.get("opponents") or []:
        team = o.get("opponent") or {}
        if team.get("id"):
            ids.add(int(team["id"]))
    return ids


class Alerts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.poll_loop.change_interval(seconds=self.bot.settings.alert_poll_seconds)
        self.poll_loop.start()

    async def cog_unload(self) -> None:
        self.poll_loop.cancel()

    # ── slash command group ──────────────────────────────────────────────────
    alerts = app_commands.Group(
        name="alerts",
        description="Manage live-match alerts for this channel.",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @alerts.command(name="add", description="Alert this channel when matches go live.")
    @app_commands.describe(
        game="Game to watch.",
        team="Optional: only alert for this team (leave blank for all matches).",
    )
    @app_commands.choices(game=GAME_CHOICES)
    async def add(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str],
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug)
        team_id: int | None = None
        team_name: str | None = None
        if team:
            try:
                matches = await self.bot.api.search_teams(team, slug=slug, per_page=1)
            except PandaScoreError:
                matches = []
            if not matches:
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}**.", ephemeral=True
                )
                return
            team_id = int(matches[0]["id"])
            team_name = matches[0].get("name")

        ok = await self.bot.db.add_subscription(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            game=game.value,
            team_id=team_id,
            team_name=team_name,
            created_by=interaction.user.id,
        )
        if not ok:
            await interaction.followup.send(
                "That subscription already exists here.", ephemeral=True
            )
            return
        scope = f"**{team_name}**" if team_name else f"all **{game.name}** matches"
        await interaction.followup.send(
            embed=discord.Embed(
                description=f"🔔 This channel will now be alerted for {scope}.",
                color=GREEN,
            ),
            ephemeral=True,
        )

    @alerts.command(name="list", description="List this server's alert subscriptions.")
    async def list_subs(self, interaction: discord.Interaction) -> None:
        subs = await self.bot.db.list_subscriptions(interaction.guild_id)
        if not subs:
            await interaction.response.send_message(
                "No alerts configured. Use `/alerts add`.", ephemeral=True
            )
            return
        lines = []
        for s in subs:
            scope = s["team_name"] or f"all {label_for(s['game'])}"
            lines.append(f"`#{s['id']}` <#{s['channel_id']}> → {scope} ({label_for(s['game'])})")
        embed = discord.Embed(
            title="🔔 Alert subscriptions", description="\n".join(lines), color=BRAND
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

        # Which games do we actually need to poll?
        games = {s["game"] for s in subs}
        running_by_game: dict[str, list[dict]] = {}
        for game_key in games:
            slug = resolve_slug(game_key, self.bot.settings.cs_slug)
            try:
                running_by_game[game_key] = await self.bot.api.running_matches(slug=slug, per_page=25)
            except PandaScoreError as exc:
                log.warning("poll: could not fetch running %s: %s", game_key, exc)
                running_by_game[game_key] = []

        for sub in subs:
            matches = running_by_game.get(sub["game"], [])
            for match in matches:
                if sub["team_id"] and int(sub["team_id"]) not in _match_team_ids(match):
                    continue
                channel_id = int(sub["channel_id"])
                if await self.bot.db.was_alerted(int(match["id"]), "live", channel_id):
                    continue
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                try:
                    await channel.send(
                        content="🔴 **A match just went live!**",
                        embed=match_embed(match, sub["game"]),
                    )
                    await self.bot.db.mark_alerted(int(match["id"]), "live", channel_id)
                except discord.Forbidden:
                    log.warning("Missing permission to post in channel %s", channel_id)
                except discord.HTTPException as exc:
                    log.warning("Failed to post alert: %s", exc)

    @poll_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Alerts(bot))
