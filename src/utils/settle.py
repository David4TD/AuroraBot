"""Paying out a finished match.

Lives here because two things need it: the background resolve loop, and the
alert that announces a result. The alert has to settle *before* it renders, or
the leaderboard on the card would be missing the very match it's announcing —
the loop only runs every few minutes.

Safe to call twice. Only predictions still ``open`` are touched, so whichever
caller gets there first wins and the other becomes a no-op.
"""
from __future__ import annotations

import logging

from .scoring import MIN_PERFECT_DAY_MATCHES, PERFECT_DAY_BONUS, payout_for

log = logging.getLogger("aurorabot.settle")


async def settle_match(bot, match_id: int, winner_id: int) -> int:
    """Resolve every open prediction on one match. Returns how many settled.

    The underdog multiplier is priced **per server**, so the crowd a pick is
    measured against is the one that saw it — a server where everyone backed
    the favourite pays differently from one that was split.
    """
    db = bot.db
    preds = await db.open_predictions_for_match(match_id)
    if not preds:
        return 0

    by_guild: dict[str | None, list] = {}
    for p in preds:
        by_guild.setdefault(p["guild_id"], []).append(p)

    settled = 0
    for guild_id, group in by_guild.items():
        # Every pick on this match in that server, not just the open ones, so a
        # re-run can't reprice the match differently.
        all_picks = await db.match_predictions(match_id, guild_id)
        voters = len(all_picks) or len(group)
        backers = sum(
            1 for x in all_picks if int(x["predicted_team_id"]) == winner_id
        ) or sum(1 for x in group if int(x["predicted_team_id"]) == winner_id)

        for p in group:
            won = int(p["predicted_team_id"]) == winner_id
            if won:
                # Streak is read before this result lands, so it counts the run
                # *leading into* this pick rather than including it.
                streak = await db.current_streak(int(p["discord_id"]), guild_id)
                points = payout_for(int(p["stake"]), backers, voters, streak).points
            else:
                points = 0
            await db.resolve_prediction(
                prediction_id=p["id"],
                status="won" if won else "lost",
                points_delta=points,
                discord_id=p["discord_id"],
            )
            settled += 1
            log.info(
                "Resolved #%s (match %s, guild %s): %s for %d pts "
                "[stake %s, %d/%d backers]",
                p["id"], match_id, guild_id, "won" if won else "lost",
                points, p["stake"], backers, voters,
            )
            if won:
                await _maybe_perfect_day(bot, p, guild_id)
    return settled


async def _maybe_perfect_day(bot, prediction, guild_id) -> None:
    """Award the bonus if this completes a clean sweep of the match day.

    Checked on every win rather than on a schedule: the last result to land is
    the one that completes the day, and it's a cheap indexed lookup.
    """
    starts = prediction["match_starts_at"]
    if not starts:
        return
    day = str(starts)[:10]
    same_day = await bot.db.day_predictions(
        int(prediction["discord_id"]), guild_id, day
    )
    if len(same_day) < MIN_PERFECT_DAY_MATCHES:
        return
    if any(row["status"] != "won" for row in same_day):
        return
    await bot.db.award_bonus(
        int(prediction["discord_id"]), int(prediction["id"]), PERFECT_DAY_BONUS
    )
    log.info(
        "Perfect day for %s in guild %s on %s: +%d",
        prediction["discord_id"], guild_id, day, PERFECT_DAY_BONUS,
    )
