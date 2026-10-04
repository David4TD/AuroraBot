"""The card on a match reminder, half an hour before kick-off.

The third of the three states a match passes through, and the last one still
rendering the generic embed `/live` and `/results` were lifted off. That embed
spent fields on *Format* (PandaScore's raw ``best_of`` enum), *Best of* (the
same fact again) and *Tier* (always Tier 1, because nothing else is shown), and
closed with an internal match id.

A reminder is the only moment the bot asks for something rather than reporting:
the buttons underneath it are the whole point, and they are live for about
thirty minutes. Three lines is the whole card — who, when, and what it pays —
because anyone reading it is deciding whether to tap a button, not studying a
fixture.

It says nothing about how the server has voted. That stays hidden until
kick-off; a tally printed while the book is open would anchor everyone who
reads it.
"""
from __future__ import annotations

import discord

from .embeds import AMBER
from .matches import opponents
from .regions import event_flag
from .scoring import (
    BASE_POINTS, DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, FINAL_WEIGHT,
    MAX_MULTIPLIER, PLAYOFF_WEIGHT, stage_weight,
)
from .tournaments import parse_dt


def _stream(match: dict) -> str | None:
    streams = match.get("streams_list") or []
    main = next((s for s in streams if s.get("main") and s.get("raw_url")), None)
    fallback = next((s for s in streams if s.get("raw_url")), None)
    chosen = main or fallback
    return chosen["raw_url"] if chosen else None


def worth_line(match: dict) -> str:
    """What a correct call pays here, in as few characters as it takes.

    The stage multiplier is already on the line above, so this doesn't explain
    it again — it just shows the number it produces.
    """
    weight = stage_weight(match)
    low = int(BASE_POINTS * weight)
    high = int(BASE_POINTS * MAX_MULTIPLIER * weight)
    return (f"🎲 **{low}–{high}** pts · 💥 double down **×{DOUBLE_DOWN}** "
            f"or **−{DOUBLE_DOWN_PENALTY}**")


def build_reminder_card(
    bot, match: dict, game_key: str | None = None, *, predictable: bool = True
) -> discord.Embed:
    """A fixture about to start, with the case for predicting it."""
    teams = opponents(match)
    if len(teams) < 2:
        from .embeds import match_embed
        return match_embed(match, game_key, bot.icons)

    icons = bot.icons
    a, b = teams[0], teams[1]

    flag = event_flag(match) or ""
    league = (match.get("league") or {}).get("name") or ""
    serie = (match.get("serie") or {}).get("full_name") or ""
    stage = (match.get("tournament") or {}).get("name") or ""
    context = " ".join(x for x in (league, serie, stage) if x)
    title = " · ".join(x for x in ("⏰ Starting soon", f"{flag} {context}".strip()) if x)

    embed = discord.Embed(title=title[:256], color=AMBER)

    # The matchup sits in the description, not the title: Discord renders the
    # custom emoji team logos here and leaks them as raw <:name:id> up there.
    lines = [
        f"{icons.icon(a)} **{a['name']}**  vs  **{b['name']}** {icons.icon(b)}".strip()
    ]

    begin = parse_dt(match.get("begin_at"))
    detail = []
    if begin:
        # The countdown alone: a reminder is thirty minutes wide, so the wall
        # clock beside it was answering a question nobody had.
        detail.append(f"🕒 <t:{int(begin.timestamp())}:R>")
    best_of = match.get("number_of_games")
    if best_of and int(best_of) > 1:
        detail.append(f"Bo{best_of}")
    weight = stage_weight(match)
    if weight >= FINAL_WEIGHT:
        detail.append(f"🏆 stage ×{FINAL_WEIGHT:g}")
    elif weight >= PLAYOFF_WEIGHT:
        detail.append(f"stage ×{PLAYOFF_WEIGHT:g}")
    stream = _stream(match)
    if stream:
        detail.append(f"▶ [Watch]({stream})")
    if detail:
        lines.append(" · ".join(detail))
    if predictable:
        lines.append(worth_line(match))
    embed.description = "\n".join(lines)

    embed.set_footer(text="Predictions close at kick-off · /scoring")
    return embed
