"""The card on a match reminder, half an hour before kick-off.

The third of the three states a match passes through, and the last one still
rendering the generic embed `/live` and `/results` were lifted off. That embed
spent fields on *Format* (PandaScore's raw ``best_of`` enum), *Best of* (the
same fact again) and *Tier* (always Tier 1, because nothing else is shown), and
closed with an internal match id.

A reminder is the only moment the bot asks for something rather than reporting:
the buttons underneath it are the whole point, and they are live for about
thirty minutes. So the card leads with the countdown, then what the match is
worth — which is what decides whether it's worth a conviction token — and says
nothing about how the server has voted. That stays hidden until kick-off; a
tally printed while the book is open would anchor everyone who reads it.
"""
from __future__ import annotations

import discord

from .embeds import AMBER
from .matches import opponents
from .regions import event_flag
from .scoring import BASE_POINTS, FINAL_WEIGHT, MAX_MULTIPLIER, PLAYOFF_WEIGHT, stage_weight
from .tournaments import parse_dt


def _stream(match: dict) -> str | None:
    streams = match.get("streams_list") or []
    main = next((s for s in streams if s.get("main") and s.get("raw_url")), None)
    fallback = next((s for s in streams if s.get("raw_url")), None)
    chosen = main or fallback
    return chosen["raw_url"] if chosen else None


def worth_line(match: dict) -> str:
    """What a correct call on this match pays, before the room is counted."""
    weight = stage_weight(match)
    low = int(BASE_POINTS * weight)
    high = int(BASE_POINTS * MAX_MULTIPLIER * weight)
    if weight >= FINAL_WEIGHT:
        stage = f" — the final, so everything here counts **×{FINAL_WEIGHT:g}**"
    elif weight >= PLAYOFF_WEIGHT:
        stage = f" — bracket match, counts **×{PLAYOFF_WEIGHT:g}**"
    else:
        stage = ""
    return f"**{low}** for calling it, up to **{high}** against the room{stage}."


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
        ts = int(begin.timestamp())
        detail.append(f"🕒 <t:{ts}:R> · <t:{ts}:t>")
    best_of = match.get("number_of_games")
    if best_of and int(best_of) > 1:
        detail.append(f"Bo{best_of}")
    weight = stage_weight(match)
    if weight >= FINAL_WEIGHT:
        detail.append(f"🏆 stage ×{FINAL_WEIGHT:g}")
    elif weight >= PLAYOFF_WEIGHT:
        detail.append(f"stage ×{PLAYOFF_WEIGHT:g}")
    if detail:
        lines.append(" · ".join(detail))

    stream = _stream(match)
    if stream:
        lines.append(f"▶ [Watch]({stream})")
    embed.description = "\n".join(lines)

    if predictable:
        embed.add_field(
            name="🎲 Call it",
            value=(
                f"{worth_line(match)}\n"
                f"💥 A double down pays twice that, or costs "
                f"**{BASE_POINTS}** if you're wrong — `/scoring`."
            )[:1024],
            inline=False,
        )

    if game_key:
        from .games import label_for
        embed.set_footer(text=f"{label_for(game_key)} · predictions close at kick-off")
    else:
        embed.set_footer(text="Predictions close at kick-off")
    return embed
