"""The whole upcoming slate, as one embed.

`/upcoming` answers "what's on and when". The first version of this list gave
each match two lines — the matchup, then an indented line repeating the event
and the format — which meant five matches filled a screen and "LCK Playoffs"
appeared five times in a row for a list that was usually one event deep.

Now each match is exactly one line, and anything the whole list has in common
is printed once in the footer instead of on every row. League and stage are
judged separately, because one league across two stages is still one league —
lumping them together was what kept the repetition alive.

Matches that pay more carry their multiplier, since a final being worth double
is now a thing worth planning a token around.

Times are Discord timestamps, so every viewer sees their own clock: the exact
kick-off and the countdown, side by side, because "18:00" answers a different
question from "in 2 hours". The day headings can't be per-viewer — they're a
single server-side grouping, so they follow the guild's digest timezone, the
same one that decides what "today" means for the daily schedule.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import discord

from .embeds import BRAND
from .matches import opponents
from .regions import event_flag
from .scoring import FINAL_WEIGHT, PLAYOFF_WEIGHT, stage_weight
from .tournaments import parse_dt

# One line each now, so more of them fit before a field hits its 1024-character
# cap or the embed stops being scannable.
MAX_MATCHES = 12
MAX_PER_DAY = 8


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    if day < today + timedelta(days=7):
        return day.strftime("%A")
    return day.strftime("%a %d %b")


def _league_of(match: dict) -> str:
    return (match.get("league") or {}).get("name") or ""


def _stage_of(match: dict) -> str:
    return (match.get("tournament") or {}).get("name") or ""


def _weight_tag(match: dict) -> str:
    """Flag the matches that pay more, since that's worth planning around.

    Says *stage* explicitly. A bare "×2" is the same number a double down
    pays, and the two are entirely different things — this one applies to
    everybody automatically, costs nothing and risks nothing.
    """
    weight = stage_weight(match)
    if weight >= FINAL_WEIGHT:
        return f"🏆 stage ×{FINAL_WEIGHT:g}"
    if weight >= PLAYOFF_WEIGHT:
        return f"stage ×{PLAYOFF_WEIGHT:g}"
    return ""


def _line(match: dict, icons, show_league: bool, show_stage: bool) -> str:
    teams = opponents(match)
    begin = parse_dt(match.get("begin_at"))
    when = (
        f"<t:{int(begin.timestamp())}:t> · <t:{int(begin.timestamp())}:R>"
        if begin else "time TBD"
    )

    if len(teams) >= 2:
        a, b = teams[0], teams[1]
        matchup = (
            f"{icons.icon(a)} **{a['name']}** vs **{b['name']}** {icons.icon(b)}"
        ).strip()
    else:
        # An unconfirmed bracket slot: say so rather than inventing an opponent.
        only = teams[0]["name"] if teams else "TBD"
        matchup = f"**{only}** vs _TBD_"

    best_of = match.get("number_of_games")
    tail = [when]
    if best_of:
        tail.append(f"Bo{best_of}")
    tag = _weight_tag(match)
    if tag:
        tail.append(tag)
    # Whatever isn't already said once in the footer. Always on the same line:
    # a wrapped long line still reads as one match, an indented second line
    # reads as two.
    where = []
    if show_league:
        flag = event_flag(match) or ""
        where.append(f"{flag} {_league_of(match)}".strip())
    if show_stage and _stage_of(match):
        where.append(_stage_of(match))
    if where:
        tail.append(" ".join(where))
    return f"{matchup} — {' · '.join(tail)}"


def build_upcoming_list(
    bot, matches: list[dict], heading: str, tz=timezone.utc
) -> discord.Embed:
    """Every upcoming match in scope, grouped by day, soonest first."""
    embed = discord.Embed(title=f"🗓️ Upcoming · {heading}"[:256], color=BRAND)

    shown = matches[:MAX_MATCHES]
    # Whatever the whole list has in common belongs in the footer once, not as
    # a suffix on every line. Judged separately for the league and the stage,
    # because one league across two stages is still one league.
    leagues = {_league_of(m) for m in shown if _league_of(m)}
    stages = {_stage_of(m) for m in shown if _stage_of(m)}
    show_league = len(leagues) > 1
    show_stage = len(stages) > 1

    today = datetime.now(timezone.utc).astimezone(tz).date()
    grouped: dict[date, list[dict]] = {}
    undated: list[dict] = []
    for match in shown:
        begin = parse_dt(match.get("begin_at"))
        if begin is None:
            undated.append(match)
            continue
        grouped.setdefault(begin.astimezone(tz).date(), []).append(match)

    for day in sorted(grouped):
        entries = grouped[day][:MAX_PER_DAY]
        hidden = len(grouped[day]) - len(entries)
        value = "\n".join(_line(m, bot.icons, show_league, show_stage) for m in entries)
        if hidden > 0:
            value += f"\n_+{hidden} more_"
        embed.add_field(name=_day_label(day, today), value=value[:1024],
                        inline=False)

    if undated:
        embed.add_field(
            name="Date to be confirmed",
            value="\n".join(
                _line(m, bot.icons, show_league, show_stage) for m in undated[:MAX_PER_DAY]
            )[:1024],
            inline=False,
        )

    footer = []
    common = []
    if not show_league and leagues:
        flag = event_flag(shown[0]) or ""
        common.append(f"{flag} {leagues.pop()}".strip())
    if not show_stage and stages:
        common.append(stages.pop())
    if common:
        footer.append(" ".join(common))
    if any(_weight_tag(m) for m in shown):
        footer.append("stage bonus is automatic, on top of a double down")
    footer.append("/predict to call one · /lineup for rosters")
    embed.set_footer(text=" · ".join(footer)[:2048])
    return embed
