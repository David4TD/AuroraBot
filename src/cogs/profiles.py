"""User profile & favourite-team management."""
from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..services.pandascore import PandaScoreError
from ..utils.choices import GAME_CHOICES
from ..utils.embeds import BRAND, GREEN, RED
from ..utils.games import label_for, resolve_slug

log = logging.getLogger("aurorabot.cogs.profiles")


class FollowSelect(discord.ui.Select):
    def __init__(self, cog: "Profiles", teams: list[dict], game_key: str | None) -> None:
        self.cog = cog
        self.game_key = game_key
        self._teams = {str(t["id"]): t for t in teams[:25]}
        options = [
            discord.SelectOption(
                label=t.get("name", "Team")[:100],
                value=str(t["id"]),
                description=(t.get("location") or "")[:100] or None,
            )
            for t in teams[:25]
        ]
        super().__init__(placeholder="Select a team to follow…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        team = self._teams[self.values[0]]
        await self.cog.bot.db.ensure_user(interaction.user.id, interaction.user.display_name)
        await self.cog.bot.db.follow_team(
            interaction.user.id, int(team["id"]), team.get("name", "Team"), self.game_key
        )
        await interaction.response.edit_message(
            content=f"✅ Now following **{team.get('name')}**. You'll see them in `/profile`.",
            view=None,
        )


class FollowView(discord.ui.View):
    def __init__(self, cog: "Profiles", teams: list[dict], game_key: str | None) -> None:
        super().__init__(timeout=120)
        self.add_item(FollowSelect(cog, teams, game_key))


class Profiles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="profile", description="View your eSports profile.")
    @app_commands.describe(member="View someone else's profile (optional).")
    async def profile(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        target = member or interaction.user
        await self.bot.db.ensure_user(target.id, target.display_name)
        user = await self.bot.db.get_user(target.id)
        teams = await self.bot.db.list_followed_teams(target.id)

        embed = discord.Embed(title=f"👤 {target.display_name}", color=BRAND)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Points", value=str(user["points"]), inline=True)
        total = user["predictions_total"] or 0
        won = user["predictions_won"] or 0
        acc = f"{(won / total * 100):.0f}%" if total else "—"
        embed.add_field(name="Predictions", value=f"{won}/{total} ({acc})", inline=True)
        fav = user["favorite_game"]
        embed.add_field(
            name="Default game",
            value=label_for(fav) if fav else "not set — try `/setgame`",
            inline=True,
        )
        if teams:
            embed.add_field(
                name=f"Following ({len(teams)})",
                value=", ".join(t["team_name"] for t in teams[:15]),
                inline=False,
            )
        else:
            embed.add_field(
                name="Following",
                value="Nobody yet — use `/follow` to track a team.",
                inline=False,
            )
        embed.set_footer(text="AuroraBot")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="follow", description="Follow a team to track them.")
    @app_commands.describe(name="Team name to search for.", game="Game the team plays.")
    @app_commands.choices(game=GAME_CHOICES)
    async def follow(
        self,
        interaction: discord.Interaction,
        name: str,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        slug = resolve_slug(game.value, self.bot.settings.cs_slug) if game else None
        try:
            teams = await self.bot.api.search_teams(name, slug=slug, per_page=25)
        except PandaScoreError:
            await interaction.followup.send("Team search is unavailable right now.", ephemeral=True)
            return
        if not teams:
            await interaction.followup.send(f"No team matched **{name}**.", ephemeral=True)
            return
        if len(teams) == 1:
            t = teams[0]
            await self.bot.db.ensure_user(interaction.user.id, interaction.user.display_name)
            await self.bot.db.follow_team(
                interaction.user.id, int(t["id"]), t.get("name", "Team"),
                game.value if game else None,
            )
            await interaction.followup.send(
                f"✅ Now following **{t.get('name')}**.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "Multiple teams matched — pick one:",
            view=FollowView(self, teams, game.value if game else None),
            ephemeral=True,
        )

    @app_commands.command(name="unfollow", description="Stop following a team.")
    async def unfollow(self, interaction: discord.Interaction) -> None:
        teams = await self.bot.db.list_followed_teams(interaction.user.id)
        if not teams:
            await interaction.response.send_message(
                "You're not following anyone yet.", ephemeral=True
            )
            return

        options = [
            discord.SelectOption(label=t["team_name"][:100], value=str(t["team_id"]))
            for t in teams[:25]
        ]

        select = discord.ui.Select(placeholder="Select a team to unfollow…", options=options)

        async def _cb(inter: discord.Interaction) -> None:
            removed = await self.bot.db.unfollow_team(inter.user.id, int(select.values[0]))
            msg = "✅ Unfollowed." if removed else "You weren't following that team."
            await inter.response.edit_message(content=msg, view=None)

        select.callback = _cb  # type: ignore[assignment]
        view = discord.ui.View(timeout=90)
        view.add_item(select)
        await interaction.response.send_message(
            "Choose a team to unfollow:", view=view, ephemeral=True
        )

    @app_commands.command(
        name="setgame",
        description="Set your default game for /live, /upcoming, /results and /predict.",
    )
    @app_commands.describe(game="Leave blank to clear your default.")
    @app_commands.choices(game=GAME_CHOICES)
    async def setgame(
        self,
        interaction: discord.Interaction,
        game: app_commands.Choice[str] | None = None,
    ) -> None:
        await self.bot.db.ensure_user(interaction.user.id, interaction.user.display_name)

        if game is None:
            await self.bot.db.set_favorite_game(interaction.user.id, None)
            await interaction.response.send_message(
                embed=discord.Embed(
                    description="⭐ Default game cleared — those commands now "
                    "show every game again.",
                    color=GREEN,
                ),
                ephemeral=True,
            )
            return

        await self.bot.db.set_favorite_game(interaction.user.id, game.value)
        note = ""
        # A default pointing at a game the server muted would silently do
        # nothing, so say so instead of letting them wonder.
        if game.value in await self.bot.db.disabled_games(interaction.guild_id):
            note = (
                f"\n\n⚠️ Heads up: **{game.name}** is currently muted on this "
                f"server, so your default won't apply here until a mod "
                f"re-enables it with `/games`."
            )
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"⭐ Default game set to **{game.name}**.\n"
                f"`/live`, `/upcoming`, `/results` and `/predict` will use it "
                f"when you don't name a game. Run `/setgame` with no game to clear."
                + note,
                color=GREEN,
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profiles(bot))
