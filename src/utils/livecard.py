"""The card for a match that is happening right now.

What someone opening `/live` wants is the score, how far through the series
they are, and where to watch — in that order. The old shared match embed spent
three of its fields on *Format* (PandaScore's raw ``best_of`` enum), *Best of*
(the same fact again) and *Tier* (always Tier 1, because the bot only ever
shows Tier 1). None of that is worth a field, and the two things that actually
change minute to minute — which map, how long it's been going — weren't shown
at all.

Everything here comes from the match payload the caller already has. Per-game
detail lives in ``games[]``, which the API fills in as a series progresses, so
map progress costs no extra request.
"""
from __future__ import annotations

import logging

import discord

from .embeds import GREEN
from .matches import opponents
from .regions import event_flag
from .scoring import payout_for
from .tournaments import parse_dt

log = logging.getLogger("aurorabot.livecard")


def _elapsed(match: dict) -> str:
    """How long this has been running, as ``1h 12m``."""
    from datetime import datetime, timezone

    began = parse_dt(match.get("begin_at"))
    if began is None:
        return ""
    seconds = (datetime.now(timezone.utc) - began).total_seconds()
    if seconds < 0:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def map_progress(match: dict) -> str:
    """``Map 4 of 5``, or as much of it as the data supports.

    A series in progress has some games marked finished and the rest not, so
    the one being played is the first unfinished one. Falls back to the
    best-of alone when the API hasn't filled ``games`` in yet.
    """
    games = match.get("games") or []
    best_of = match.get("number_of_games")
    played = sum(
        1 for g in games if (g.get("status") or "").lower() == "finished"
    )
    if games:
        current = min(played + 1, len(games))
        total = int(best_of) if best_of else len(games)
        return f"Map {current} of {total}"
    if best_of and int(best_of) > 1:
        return f"Best of {best_of}"
    return ""


def _scores(match: dict, teams: list[dict]) -> tuple[int, int]:
    by_id = {int(t["id"]): 0 for t in teams}
    for row in match.get("results") or []:
        tid = row.get("team_id")
        if tid is not None and int(tid) in by_id:
            by_id[int(tid)] = int(row.get("score") or 0)
    return by_id[int(teams[0]["id"])], by_id[int(teams[1]["id"])]


def _stream(match: dict) -> str | None:
    streams = match.get("streams_list") or []
    main = next((s for s in streams if s.get("main") and s.get("raw_url")), None)
    fallback = next((s for s in streams if s.get("raw_url")), None)
    chosen = main or fallback
    return chosen["raw_url"] if chosen else None


async def build_live_card(
    bot, match: dict, game_key: str | None = None, guild_id=None
) -> discord.Embed:
    """A match in progress, as one slim card."""
    teams = opponents(match)
    if len(teams) < 2:
        from .embeds import match_embed
        return match_embed(match, game_key, bot.icons)

    icons = bot.icons
    a, b = teams[0], teams[1]
    left_score, right_score = _scores(match, teams)

    flag = event_flag(match) or ""
    league = (match.get("league") or {}).get("name") or ""
    serie = (match.get("serie") or {}).get("full_name") or ""
    stage = (match.get("tournament") or {}).get("name") or ""
    context = " ".join(x for x in (league, serie, stage) if x)
    title = " · ".join(x for x in ("🔴 LIVE", f"{flag} {context}".strip()) if x)

    embed = discord.Embed(title=title[:256], color=GREEN)

    # The scoreline sits in the description, not the title: Discord renders
    # custom emoji (the team logos) in a description and leaks them as raw
    # <:name:id> text in a title, worst of all on mobile.
    icon_a, icon_b = icons.icon(a), icons.icon(b)
    left = f"{icon_a} **{a['name']}**".strip()
    right = f"**{b['name']}** {icon_b}".strip()
    lines = [f"{left}  **{left_score} — {right_score}**  {right}"]

    status = " · ".join(x for x in (map_progress(match), _live_for(match)) if x)
    if status:
        lines.append(status)
    stream = _stream(match)
    if stream:
        lines.append(f"▶ [Watch]({stream})")
    embed.description = "\n".join(lines)

    if guild_id:
        await _add_pick_counts(bot, embed, match, teams, guild_id)

    if game_key:
        from .games import label_for
        embed.set_footer(text=label_for(game_key))
    return embed


def _live_for(match: dict) -> str:
    elapsed = _elapsed(match)
    return f"live {elapsed}" if elapsed else ""


async def _add_pick_counts(bot, embed, match, teams, guild_id) -> None:
    """How the server split, as counts and what each side now pays.

    Counts rather than names: the going-live alert already lists everyone, and
    repeating the roll call here would make `/live` taller than the match it
    describes. What the alert can't show is how the price moved once voting
    closed, which is the one number worth repeating.
    """
    try:
        picks = await bot.db.match_predictions(int(match["id"]), int(guild_id))
    except Exception:  # noqa: BLE001 - the score matters more than the extra
        log.exception("could not read predictions for match %s", match.get("id"))
        return
    if not picks:
        return

    parts = []
    for team in teams[:2]:
        backers = sum(
            1 for p in picks if int(p["predicted_team_id"]) == int(team["id"])
        )
        worth = payout_for(backers, len(picks)).points
        parts.append(f"**{team['name']}** {backers} _({worth} pts)_")
    embed.add_field(
        name="🔮 Server picks",
        value=" · ".join(parts)[:1024],
        inline=False,
    )
