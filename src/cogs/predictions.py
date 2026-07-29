"""Match predictions & friendly challenges.

Two ways in, both funnelling through ``utils.predictions.submit_prediction``:

  /predict game:<game>  → dropdown of upcoming Tier 1 matches
                        → two buttons (the two teams)
  reacting 1️⃣ / 2️⃣ to a match reminder alert (see cogs/alerts.py)

A background task resolves open predictions once the match finishes and awards
points to correct predictors.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext import tasks

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND
from ..utils.games import resolve_slug
from ..utils.matches import opponents
from ..utils.predictions import WIN_REWARD, submit_prediction
from ..utils.tiers import filter_top_tier

log = logging.getLogger("aurorabot.cogs.predictions")

FETCH_SIZE = 50


class TeamButton(discord.ui.Button):
    def __init__(self, cog: "Predictions", match: dict, team: dict, game_key: str) -> None:
        super().__init__(label=team["name"][:80], style=discord.ButtonStyle.primary)
        self.cog = cog
        self.match = match
        self.team = team
        self.game_key = game_key

    async def callback(self, interaction: discord.Interaction) -> None:
        opponent = next(
            (t for t in opponents(self.match) if t["id"] != self.team["id"]), None
        )
        _, message = await submit_prediction(
            self.cog.bot.db,
            user_id=interaction.user.id,
            display_name=interaction.user.display_name,
            match_id=int(self.match["id"]),
            game=self.game_key,
            team=self.team,
            opponent=opponent,
            begin_at=self.match.get("begin_at"),
        )
        await interaction.response.edit_message(content=message, view=None)


class MatchSelect(discord.ui.Select):
    def __init__(self, cog: "Predictions", matches: list[dict], game_key: str) -> None:
        self.cog = cog
        self.game_key = game_key
        self._matches = {str(m["id"]): m for m in matches[:25]}
        options = []
        for m in matches[:25]:
            opps = opponents(m)
            if len(opps) < 2:
                continue
            label = f"{opps[0]['name']} vs {opps[1]['name']}"[:100]
            league = (m.get("league") or {}).get("name", "")
            options.append(
                discord.SelectOption(label=label, value=str(m["id"]), description=league[:100] or None)
            )
        super().__init__(placeholder="Pick a match to predict…", options=options or [
            discord.SelectOption(label="No eligible matches", value="none")
        ])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await interaction.response.edit_message(content="No matches to predict.", view=None)
            return
        match = self._matches[self.values[0]]
        opps = opponents(match)
        view = discord.ui.View(timeout=120)
        for team in opps[:2]:
            view.add_item(TeamButton(self.cog, match, team, self.game_key))
        await interaction.response.edit_message(
            content=f"Who wins **{opps[0]['name']}** vs **{opps[1]['name']}**?", view=view
        )


class Predictions(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.resolve_loop.start()

    async def cog_unload(self) -> None:
        self.resolve_loop.cancel()

    @app_commands.command(
        name="predict", description="Predict the winner of an upcoming Tier 1 match."
    )
    @app_commands.choices(game=GAME_CHOICES)
    async def predict(
        self, interaction: discord.Interaction, game: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug)
        try:
            matches = await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
        except PandaScoreError:
            await interaction.followup.send("Predictions are unavailable right now.", ephemeral=True)
            return
        matches = filter_top_tier(matches, enabled=self.bot.settings.top_tier_only)
        eligible = [m for m in matches if len(opponents(m)) >= 2]
        if not eligible:
            await interaction.followup.send(
                "No upcoming Tier 1 matches with confirmed teams to predict yet.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=120)
        view.add_item(MatchSelect(self, eligible, game.value))
        await interaction.followup.send(
            "Choose a match, then pick your winner:", view=view, ephemeral=True
        )

    @app_commands.command(name="mypredictions", description="See your recent predictions.")
    async def mypredictions(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_user_predictions(interaction.user.id, limit=10)
        if not rows:
            await interaction.response.send_message(
                "You haven't made any predictions yet. Try `/predict`.", ephemeral=True
            )
            return
        icon = {"open": "⏳", "won": "✅", "lost": "❌", "void": "➖"}
        lines = [
            f"{icon.get(r['status'], '•')} **{r['predicted_team_name']}** "
            f"vs {r['opponent_team_name'] or '?'} — _{r['status']}_"
            for r in rows
        ]
        embed = discord.Embed(
            title="🎲 Your predictions", description="\n".join(lines), color=BRAND
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── background resolution ────────────────────────────────────────────────
    @tasks.loop(minutes=5)
    async def resolve_loop(self) -> None:
        try:
            await self._resolve_open()
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("resolve_loop iteration failed")

    async def _resolve_open(self) -> None:
        open_preds = await self.bot.db.get_open_predictions()
        if not open_preds:
            return
        # Group by match to minimise API calls.
        by_match: dict[int, list] = {}
        for p in open_preds:
            by_match.setdefault(p["match_id"], []).append(p)

        for match_id, preds in by_match.items():
            try:
                match = await self.bot.api.get_match(match_id)
            except PandaScoreError:
                continue
            if (match.get("status") or "").lower() != "finished":
                continue
            winner_id = (match.get("winner") or {}).get("id")
            if winner_id is None:
                continue
            for p in preds:
                won = int(p["predicted_team_id"]) == int(winner_id)
                await self.bot.db.resolve_prediction(
                    prediction_id=p["id"],
                    status="won" if won else "lost",
                    points_delta=WIN_REWARD,
                    discord_id=p["discord_id"],
                )
                log.info(
                    "Resolved prediction #%s (match %s): %s",
                    p["id"], match_id, "won" if won else "lost",
                )

    @resolve_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Predictions(bot))
