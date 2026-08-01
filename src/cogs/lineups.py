"""Pre-match lineup cards: who's playing, by role.

`/lineup` narrows upcoming matches with the usual game → tournament → team
cascade, then renders both sides' rosters side by side, ordered by lane so the
two columns read across.

**What this is not.** PandaScore exposes a confirmed per-match lineup only via
``expected_roster``, which comes back empty on this plan, so the card shows each
team's *current* roster — substitutes included, and not a guarantee of who
starts. The footer says so rather than implying certainty the data doesn't have.

Player photos exist in the API but can't appear here: Discord renders images
inline only as custom emoji, and minting one per player would burn the 2000-emoji
budget that team logos use. Nationality flags carry the same information for a
fraction of the cost.
"""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..services.tourneys import match_has_team, match_in_target
from ..utils.choices import GAME_CHOICES
from ..utils.games import ALL_GAME_KEYS, label_for, resolve_slug
from ..utils.guildgames import blocked_message, filter_enabled, no_games_message
from ..utils.lineupcard import MAX_PLAYERS, build_card, roster_lines  # noqa: F401
from ..utils.matches import opponents
from ..utils.pickers import game_of, team_choices, tournament_choices
from ..utils.regions import event_flag
from ..utils.tiers import filter_for
from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.cogs.lineups")

FETCH_SIZE = 50
MAX_OPTIONS = 25


class MatchSelect(discord.ui.Select):
    def __init__(self, cog: "Lineups", matches: list[dict], game_key: str) -> None:
        self.cog = cog
        self.game_key = game_key
        self._matches = {str(m["id"]): m for m in matches[:MAX_OPTIONS]}
        options = []
        for m in matches[:MAX_OPTIONS]:
            teams = opponents(m)
            starts = parse_dt(m.get("begin_at"))
            options.append(
                discord.SelectOption(
                    label=f"{teams[0]['name']} vs {teams[1]['name']}"[:100],
                    value=str(m["id"]),
                    description=(
                        f"{starts:%d %b %H:%M} UTC · "
                        f"{(m.get('tournament') or {}).get('name', '')}"[:100]
                        if starts else (m.get("tournament") or {}).get("name", "")[:100]
                    ) or None,
                    emoji=event_flag(m),
                )
            )
        super().__init__(placeholder="Pick a match to see the lineups…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        match = self._matches[self.values[0]]
        embed = await self.cog.build_card(match, self.game_key)
        await interaction.followup.send(embed=embed)


class Lineups(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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

    async def build_card(self, match: dict, game_key: str) -> discord.Embed:
        """Render the shared pre-match card. See ``utils.lineupcard``."""
        return await build_card(self.bot, match, game_key)

    @app_commands.command(
        name="lineup", description="Pre-match rosters for an upcoming Tier 1 match."
    )
    @app_commands.describe(
        game="Which game. Defaults to your `/setgame` pick.",
        tournament="Narrow to a league or one of its stages.",
        team="Narrow to one team in that tournament.",
    )
    @app_commands.choices(game=GAME_CHOICES)
    @app_commands.autocomplete(tournament=_tournament_ac, team=_team_ac)
    async def lineup(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
        tournament: str | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)

        enabled = (
            await self.bot.db.enabled_games(interaction.guild_id)
            if interaction.guild_id
            else set(ALL_GAME_KEYS)
        )
        if not enabled:
            await interaction.followup.send(no_games_message())
            return

        if game is not None:
            game_key = game.value
            if game_key not in enabled:
                await interaction.followup.send(blocked_message(game_key))
                return
        else:
            game_key = await self.bot.db.favorite_game(interaction.user.id)
            if game_key is None or game_key not in enabled:
                await interaction.followup.send(
                    "Pick a game — or set a default once with `/setgame`."
                )
                return

        target = None
        if tournament:
            target = await self.bot.tourneys.resolve(tournament, game_key)
            if target is None:
                await interaction.followup.send(
                    f"Couldn't match **{tournament}** to a current "
                    f"{label_for(game_key)} league or tournament."
                )
                return

        picked = None
        if team:
            picked = await self.bot.tourneys.resolve_team(team, game_key, target)
            if picked is None:
                await interaction.followup.send(
                    f"Couldn't find a team called **{team}**."
                )
                return

        try:
            raw = await self.bot.api.upcoming_matches(
                slug=resolve_slug(game_key, self.bot.settings.cs_slug),
                per_page=FETCH_SIZE,
            )
        except PandaScoreError:
            await interaction.followup.send("The match service is unavailable right now.")
            return

        matches = filter_enabled(
            filter_for(self.bot.settings, raw), enabled, self.bot.settings.cs_slug
        )
        matches = [
            m for m in matches
            if match_in_target(m, target)
            and match_has_team(m, picked)
            and len(opponents(m)) >= 2      # both sides must be known
        ]
        matches.sort(key=lambda m: m.get("begin_at") or "")

        scope = " · ".join(
            x for x in (label_for(game_key),
                        target["name"] if target else None,
                        picked["name"] if picked else None) if x
        )
        if not matches:
            await interaction.followup.send(
                f"No upcoming matches with both teams confirmed for **{scope}**."
            )
            return

        if len(matches) == 1:
            await interaction.followup.send(
                embed=await self.build_card(matches[0], game_key)
            )
            return

        view = discord.ui.View(timeout=180)
        view.add_item(MatchSelect(self, matches, game_key))
        await interaction.followup.send(
            f"**{len(matches)}** upcoming match(es) · {scope}", view=view
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Lineups(bot))
