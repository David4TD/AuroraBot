"""The prizes that aren't the leaderboard: badges, rivalries, biggest brain.

Three or four people win every tournament. Everyone else needs a reason to keep
clicking, and "you are eleventh" is not one. These are the things a member who
will never top a board can still win:

* **Badges** accumulate permanently and reward being right in a *particular*
  way, not most often — one genuine upset earns Giant Killer forever.
* **Rivalries** shrink the server to one opponent. Being 6-4 up on the person
  you always disagree with is its own scoreboard, and everyone has one.
* **The weekly callout** hands the spotlight to the single longest-odds correct
  call of the week, which is a lottery the sharpest player cannot monopolise.

All three read rows the database already has. Nothing here calls the API.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..utils.badges import BADGES, UPSET_ODDS, render
from ..utils.embeds import BRAND
from ..utils.scoring import DOUBLE_DOWN

log = logging.getLogger("aurorabot.cogs.accolades")

# Hourly, not weekly: a weekly timer would fire once and then be at the mercy
# of every restart. This checks whether *this* server has had its callout for
# the current ISO week and posts if not, which is naturally catch-up safe.
CHECK_EVERY_MINUTES = 60
# Nothing worth calling out below this — at 1.0 everyone agreed, and applauding
# a pick the whole room made reads as sarcasm.
MIN_CALLOUT_ODDS = 1.5
RIVAL_RECENT = 5


def iso_week(when: datetime | None = None) -> str:
    year, week, _ = (when or datetime.now(timezone.utc)).isocalendar()
    return f"{year}-W{week:02d}"


class Accolades(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.callout_loop.start()

    async def cog_unload(self) -> None:
        self.callout_loop.cancel()

    # ── weekly biggest brain ─────────────────────────────────────────────────
    @tasks.loop(minutes=CHECK_EVERY_MINUTES)
    async def callout_loop(self) -> None:
        try:
            await self._post_callouts()
        except Exception:  # noqa: BLE001 - never let the loop die
            log.exception("callout_loop iteration failed")

    @callout_loop.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _post_callouts(self) -> None:
        week = iso_week()
        for guild_id in await self.bot.db.guilds_with_predictions():
            try:
                await self._callout_for(int(guild_id), week)
            except Exception:  # noqa: BLE001 - one server can't break the rest
                log.exception("weekly callout failed for guild %s", guild_id)

    async def _callout_for(self, guild_id: int, week: str) -> bool:
        best = await self.bot.db.best_call_this_week(guild_id)
        if best is None or float(best["odds"] or 1.0) < MIN_CALLOUT_ODDS:
            # Claim the week anyway when there's nothing to say, so a quiet
            # week isn't re-examined every hour for seven days.
            await self.bot.db.claim_weekly_callout(guild_id, week, None, None)
            return False

        if not await self.bot.db.claim_weekly_callout(
            guild_id, week, best["discord_id"], int(best["id"])
        ):
            return False                # already posted this week

        channels = await self.bot.db.channels_with_predictions(guild_id)
        if not channels:
            return False
        embed = callout_embed(best)
        posted = False
        for channel_id in channels[:1]:   # one post, in the busiest channel
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                await channel.send(embed=embed)
                posted = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("could not post the weekly callout in %s: %s",
                            channel_id, exc)
        if posted:
            log.info("Weekly callout for guild %s: %s at x%.2f",
                     guild_id, best["discord_id"], float(best["odds"] or 1))
        return posted

    # ── /badges ──────────────────────────────────────────────────────────────
    @app_commands.command(
        name="badges", description="Achievements earned in this server."
    )
    @app_commands.describe(member="Whose badges to show. Defaults to you.")
    @app_commands.guild_only()
    async def badges(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        who = member or interaction.user
        held = await self.bot.db.badges_for(interaction.guild_id, who.id)
        holders = await self.bot.db.badge_holders(interaction.guild_id)
        earned = {str(r["badge"]): r for r in held}

        lines = [render(str(r["badge"]), r["detail"]) for r in held]
        missing = [k for k in BADGES if k not in earned]

        embed = discord.Embed(
            title=f"🎖️ {who.display_name}",
            description="\n".join(lines) if lines else
            "_No badges yet — they're earned by calling matches, not by "
            "collecting points._",
            color=BRAND,
        )
        if missing:
            embed.add_field(
                name=f"Still out there ({len(missing)})",
                value="\n".join(
                    f"{BADGES[k].emoji} **{BADGES[k].name}** · {BADGES[k].blurb}"
                    for k in missing
                )[:1024],
                inline=False,
            )
        rare = _rarest(earned, holders)
        if rare:
            embed.set_footer(text=rare)
        await interaction.response.send_message(embed=embed)

    # ── /rival ───────────────────────────────────────────────────────────────
    @app_commands.command(
        name="rival", description="Head to head against another predictor."
    )
    @app_commands.describe(
        member="Who to compare against. Defaults to whoever you clash with most."
    )
    @app_commands.guild_only()
    async def rival(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ) -> None:
        me = interaction.user
        if member is None:
            contested = await self.bot.db.most_contested(interaction.guild_id, me.id)
            if not contested:
                await interaction.response.send_message(
                    "You haven't shared a settled match with anyone here yet — "
                    "predict a few and a rival will find you.",
                )
                return
            other_id = int(contested[0]["discord_id"])
            other_name = contested[0]["display_name"]
        else:
            if member.id == me.id:
                await interaction.response.send_message(
                    "You can't be your own rival. Pick someone else.",
                )
                return
            other_id, other_name = member.id, member.display_name

        rows = await self.bot.db.head_to_head(interaction.guild_id, me.id, other_id)
        if not rows:
            await interaction.response.send_message(
                f"You and **{other_name}** haven't both called the same match "
                f"yet. A rivalry needs a disagreement.",
            )
            return
        await interaction.response.send_message(
            embed=rivalry_embed(me.display_name, other_name, other_id, rows)
        )


def _rarest(earned: dict, holders: dict[str, int]) -> str:
    """The scarcest badge they hold, as a footer. Silent if they hold none."""
    if not earned:
        return ""
    key = min(earned, key=lambda k: holders.get(k, 1))
    count = holders.get(key, 1)
    if count > 1:
        return f"Rarest: {BADGES[key].name} — {count} people here have it"
    return f"Rarest: {BADGES[key].name} — nobody else here has it"


def callout_embed(best) -> discord.Embed:
    odds = float(best["odds"] or 1.0)
    against = best["opponent_team_name"] or "the field"
    embed = discord.Embed(
        title="🧠 Biggest brain of the week",
        description=(
            f"<@{best['discord_id']}> called **{best['predicted_team_name']}** "
            f"over **{against}** at **×{odds:.1f}** — worth "
            f"**{int(best['points_awarded'])}** points."
        ),
        color=BRAND,
    )
    if best["tournament_name"]:
        embed.description += f"\n_{best['tournament_name']}_"
    if best["doubled"]:
        embed.description += f"\n💥 And they'd doubled down on it — ×{DOUBLE_DOWN}."
    embed.set_footer(
        text=f"The longest-odds correct call of the last 7 days · "
             f"×{UPSET_ODDS}+ earns the Giant Killer badge"
    )
    return embed


def rivalry_embed(my_name: str, their_name: str, their_id: int, rows) -> discord.Embed:
    """Head to head, counted only over matches both actually called."""
    my_wins = sum(1 for r in rows if r["a_status"] == "won" and r["b_status"] == "lost")
    their_wins = sum(1 for r in rows if r["b_status"] == "won" and r["a_status"] == "lost")
    both = sum(1 for r in rows if r["a_status"] == "won" and r["b_status"] == "won")
    neither = len(rows) - my_wins - their_wins - both

    if my_wins > their_wins:
        verdict = f"**{my_name}** leads it."
    elif their_wins > my_wins:
        verdict = f"**{their_name}** leads it."
    else:
        verdict = "Dead level."

    embed = discord.Embed(
        title=f"⚔️ {my_name} vs {their_name}"[:256],
        description=(
            f"**{my_wins} – {their_wins}** over {len(rows)} shared "
            f"{'match' if len(rows) == 1 else 'matches'}. {verdict}\n"
            f"_Only the {my_wins + their_wins} they disagreed on are scored; "
            f"both right {both}, both wrong {neither}._"
        ),
        color=BRAND,
    )

    # The disagreements are the rivalry; agreeing on a favourite isn't a story.
    splits = [r for r in rows if r["a_status"] != r["b_status"]][:RIVAL_RECENT]
    if splits:
        lines = []
        for r in splits:
            won = r["a_status"] == "won"
            mark = "✅" if won else "❌"
            picked = r["a_team"] if won else r["b_team"]
            lines.append(
                f"{mark} **{picked}** — "
                f"{r['tournament_name'] or 'a match'}"
            )
        embed.add_field(name="Recent splits", value="\n".join(lines)[:1024],
                        inline=False)
    embed.set_footer(text="Counted over matches you both predicted")
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Accolades(bot))
