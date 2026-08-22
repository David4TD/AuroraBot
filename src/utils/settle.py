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

from .badges import evaluate as evaluate_badges
from .scoring import (
    MIN_PERFECT_DAY_MATCHES, PERFECT_DAY_BONUS, payout_for, penalty_for,
    stage_weight,
)

log = logging.getLogger("aurorabot.settle")


async def settle_match(bot, match_id: int, winner_id: int, match: dict | None = None
                       ) -> int:
    """Resolve every open prediction on one match. Returns how many settled.

    The underdog multiplier is priced **per server**, so the crowd a pick is
    measured against is the one that saw it — a server where everyone backed
    the favourite pays differently from one that was split.

    Pass *match* and every pick on it is filed under the match's own tournament
    first, so the board the result card prints can't be missing the people the
    same card just listed as calling it.
    """
    db = bot.db
    if match is not None:
        await _align_tournament(db, match_id, match)
        await _stamp_weight(db, match_id, match)
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

        resolved = []
        for p in group:
            won = int(p["predicted_team_id"]) == winner_id
            weight = float(p["weight"] or 1.0)
            payout = payout_for(
                backers, voters, doubled=bool(p["doubled"]), weight=weight
            )
            if won:
                points = payout.points
            else:
                # A doubled miss costs the base back, capped at what they've
                # actually banked in this event. Read before the row settles,
                # so it can't count itself.
                banked = await db.tournament_total(
                    int(p["discord_id"]), guild_id, p["tournament_id"]
                )
                points = penalty_for(bool(p["doubled"]), banked)
            await db.resolve_prediction(
                prediction_id=p["id"],
                status="won" if won else "lost",
                points_delta=points,
                discord_id=p["discord_id"],
                odds=payout.multiplier,
            )
            settled += 1
            resolved.append((p, payout, weight, won))
            log.info(
                "Resolved #%s (match %s, guild %s): %s for %d pts "
                "[%d/%d backers, x%.2f%s]",
                p["id"], match_id, guild_id, "won" if won else "lost",
                points, backers, voters, payout.multiplier,
                ", doubled" if p["doubled"] else "",
            )

        # Bonuses and badges only once the whole room is settled. "Was anyone
        # else right?" cannot be answered halfway through the group -- asked
        # inside the loop above, the first correct pick read as the only one.
        for p, payout, weight, won in resolved:
            if won:
                await _maybe_perfect_day(bot, p, guild_id)
            # After the perfect-day bonus, so the points a badge reads are the
            # final ones for this pick rather than the payout alone.
            await _award_badges(bot, p, guild_id, payout.multiplier, weight, won)
    return settled


async def _award_badges(bot, prediction, guild_id, odds, weight, won) -> list[str]:
    """Hand out anything this pick just earned. Never blocks a payout.

    Badges are decoration on top of a settled result: if working out whether
    someone swept the week fails, the week's points still stand.
    """
    try:
        earned = await evaluate_badges(
            bot.db, prediction, guild_id, odds=odds, weight=weight, won=won
        )
    except Exception:  # noqa: BLE001 - the points matter more than the badge
        log.exception("could not evaluate badges for prediction %s", prediction["id"])
        return []

    new_badges = []
    for key, detail in earned:
        try:
            if await bot.db.award_badge(
                int(guild_id), int(prediction["discord_id"]), key, detail
            ):
                new_badges.append(key)
                log.info(
                    "Badge %s earned by %s in guild %s (%s)",
                    key, prediction["discord_id"], guild_id, detail,
                )
        except Exception:  # noqa: BLE001
            log.exception("could not award badge %s", key)
    return new_badges


async def _align_tournament(db, match_id: int, match: dict) -> None:
    """Never worth failing a payout over: log it and pay out anyway."""
    tournament = match.get("tournament") or {}
    try:
        moved = await db.align_match_tournament(
            match_id, tournament.get("id"), tournament.get("name")
        )
    except Exception:  # noqa: BLE001 - the points matter more than the filing
        log.exception("could not align predictions for match %s", match_id)
        return
    if moved:
        log.info(
            "Filed %d pick(s) on match %s under tournament %s",
            moved, match_id, tournament.get("id"),
        )


async def _stamp_weight(db, match_id: int, match: dict) -> None:
    """Record how much this stage counts for, before anything is priced."""
    try:
        await db.set_match_weight(match_id, stage_weight(match))
    except Exception:  # noqa: BLE001 - a missing weight just means face value
        log.exception("could not stamp the stage weight for match %s", match_id)


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
    await bot.db.award_badge(
        int(guild_id), int(prediction["discord_id"]), "perfect_day", day
    )
    log.info(
        "Perfect day for %s in guild %s on %s: +%d",
        prediction["discord_id"], guild_id, day, PERFECT_DAY_BONUS,
    )
