"""Match predictions & friendly challenges.

Flow:
  /predict game:<game>  → dropdown of upcoming matches
                        → two buttons (the two teams)
                        → prediction stored (10 pts stake by default)

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
from ..utils.embeds import BRAND, GREEN, RED
from ..utils.games import resolve_slug

log = logging.getLogger("aurorabot.cogs.predictions")

DEFAULT_STAKE = 10
WIN_REWARD = 25


def _opponents(match: dict) -> list[dict]:
    out = []
    for o in match.get("opponents") or []:
        team = o.get("opponent") or {}
        if team.get("id"):
            out.append({"id": team["id"], "name": team.get("name", "Team")})
    return out


class TeamButton(discord.ui.Button):
    def __init__(self, cog: "Predictions", match: dict, team: dict, game_key: str) -> None:
        super().__init__(label=team["name"][:80], style=discord.ButtonStyle.primary)
        self.cog = cog
        self.match = match
        self.team = team
        self.game_key = game_key

    async def callback(self, interaction: discord.Interaction) -> None:
        opps = _opponents(self.match)
        opponent = next((t["name"] for t in opps if t["id"] != self.team["id"]), None)
        await self.cog.bot.db.ensure_user(interaction.user.id, interaction.user.display_name)
        ok = await self.cog.bot.db.create_prediction(
            discord_id=interaction.user.id,
            match_id=int(self.match["id"]),
            game=self.game_key,
            predicted_team_id=int(self.team["id"]),
            predicted_team_name=self.team["name"],
            opponent_team_name=opponent,
            match_starts_at=self.match.get("begin_at"),
            stake=DEFAULT_STAKE,
        )
        if ok:
            await interaction.response.edit_message(
                content=f"🎲 You predicted **{self.team['name']}** to win. "
                f"Win it for **+{WIN_REWARD}** points!",
                view=None,
            )
        else:
            await interaction.response.edit_message(
                content="You've already made a prediction for that match.", view=None
            )


class MatchSelect(discord.ui.Select):
    def __init__(self, cog: "Predictions", matches: list[dict], game_key: str) -> None:
        self.cog = cog
        self.game_key = game_key
        self._matches = {str(m["id"]): m for m in matches[:25]}
        options = []
        for m in matches[:25]:
            opps = _opponents(m)
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
        opps = _opponents(match)
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

    @app_commands.command(name="predict", description="Predict the winner of an upcoming match.")
    @app_commands.choices(game=GAME_CHOICES)
    async def predict(
        self, interaction: discord.Interaction, game: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug)
        try:
            matches = await self.bot.api.upcoming_matches(slug=slug, per_page=25)
        except PandaScoreError:
            await interaction.followup.send("Predictions are unavailable right now.", ephemeral=True)
            return
        eligible = [m for m in matches if len(_opponents(m)) >= 2]
        if not eligible:
            await interaction.followup.send(
                "No upcoming matches with confirmed teams to predict yet.", ephemeral=True
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
