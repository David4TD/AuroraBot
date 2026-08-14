"""Match predictions & friendly challenges.

Two ways in, both funnelling through ``utils.predictions.submit_prediction``:

  /predict game:<game>  → dropdown of upcoming Tier 1 matches
                        → two buttons (the two teams)
  clicking a team button on a match reminder (see cogs/alerts.py)

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
from ..utils.games import ALL_GAME_KEYS, label_for, resolve_slug
from ..services.tourneys import match_has_team, match_in_target
from ..utils.guildgames import blocked_message, no_games_message
from ..utils.pickers import game_of, team_choices, tournament_choices
from ..utils.matches import opponents, tournament_id
from ..utils.predictions import submit_prediction
from ..utils.settle import settle_match
from ..utils.conviction import pick_panel
from ..utils.regions import event_flag
from ..utils.tiers import filter_for

log = logging.getLogger("aurorabot.cogs.predictions")

FETCH_SIZE = 50


class TeamButton(discord.ui.Button):
    def __init__(self, cog: "Predictions", match: dict, team: dict, game_key: str) -> None:
        # `team` here is the trimmed {id, name} form, so look the logo up from
        # the match payload where image_url actually lives.
        full = next(
            (
                (o.get("opponent") or {})
                for o in (match.get("opponents") or [])
                if (o.get("opponent") or {}).get("id") == team["id"]
            ),
            team,
        )
        super().__init__(
            label=team["name"][:80],
            style=discord.ButtonStyle.primary,
            emoji=cog.bot.icons.partial(full),
        )
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
            guild_id=interaction.guild_id,
            tournament_id=tournament_id(self.match),
            tournament_name=(self.match.get("tournament") or {}).get("name"),
        )
        view = await pick_panel(
            self.cog.bot.db, interaction.user.id, int(self.match["id"])
        )
        await interaction.response.edit_message(content=message, view=view)


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
                discord.SelectOption(
                    label=label,
                    value=str(m["id"]),
                    description=league[:100] or None,
                    emoji=event_flag(m),
                )
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

    async def _tournament_ac(self, interaction, current):
        return await tournament_choices(
            self.bot.tourneys, game_of(interaction), current
        )

    async def _team_ac(self, interaction, current):
        return await team_choices(
            self.bot.tourneys,
            game_of(interaction),
            getattr(interaction.namespace, "tournament", None),
            current,
        )

    @app_commands.command(
        name="predict", description="Predict the winner of an upcoming Tier 1 match."
    )
    @app_commands.describe(
        game="Game to predict. Defaults to your `/setgame` pick.",
        tournament="Narrow to a league or one of its stages.",
        team="Narrow to one team in that tournament.",
    )
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac, team=_team_ac)
    async def predict(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        enabled = (
            await self.bot.db.enabled_games(interaction.guild_id)
            if interaction.guild_id
            else set(ALL_GAME_KEYS)      # DMs have no server selection
        )
        if not enabled:
            await interaction.followup.send(no_games_message(), ephemeral=True)
            return

        if game is not None:
            game_key = game.value
            if game_key not in enabled:
                await interaction.followup.send(blocked_message(game_key), ephemeral=True)
                return
        else:
            # Unlike the browse commands there's no "all games" fallback here:
            # a prediction needs one concrete match list, so ask for a game
            # rather than guessing.
            game_key = await self.bot.db.favorite_game(interaction.user.id)
            if game_key is None or game_key not in enabled:
                await interaction.followup.send(
                    "Pick a game — or set a default once with `/setgame` and "
                    "`/predict` will use it from then on.",
                    ephemeral=True,
                )
                return

        slug = resolve_slug(game_key, self.bot.settings.cs_slug)
        try:
            matches = await self.bot.api.upcoming_matches(slug=slug, per_page=FETCH_SIZE)
        except PandaScoreError:
            await interaction.followup.send("Predictions are unavailable right now.", ephemeral=True)
            return
        matches = filter_for(self.bot.settings, matches)

        # Same cascade as the browse commands: tournament narrows the match
        # list, team narrows it further.
        target = None
        if tournament:
            target = await self.bot.tourneys.resolve(tournament, game_key)
            if target is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current "
                    f"{label_for(game_key)} league or tournament.", ephemeral=True
                )
                return
            matches = [m for m in matches if match_in_target(m, target)]

        picked = None
        if team:
            picked = await self.bot.tourneys.resolve_team(team, game_key, target)
            if picked is None:
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}**.", ephemeral=True
                )
                return
            matches = [m for m in matches if match_has_team(m, picked)]

        eligible = [m for m in matches if len(opponents(m)) >= 2]
        if not eligible:
            scope = " · ".join(
                x for x in (label_for(game_key),
                            target["name"] if target else None,
                            picked["name"] if picked else None) if x
            )
            await interaction.followup.send(
                f"No upcoming Tier 1 matches with confirmed teams for **{scope}**.",
                ephemeral=True,
            )
            return
        view = discord.ui.View(timeout=120)
        view.add_item(MatchSelect(self, eligible, game_key))
        await interaction.followup.send(
            "Choose a match, then pick your winner:", view=view, ephemeral=True
        )

    @app_commands.command(name="mypredictions", description="See your recent predictions.")
    async def mypredictions(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_user_predictions(interaction.user.id, limit=10)
        if not rows:
            await interaction.response.send_message(
                "You haven't made any predictions yet. Try `/predict`."
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
        await interaction.response.send_message(embed=embed)

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
            await settle_match(self.bot, match_id, int(winner_id), match)

    @resolve_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Predictions(bot))
