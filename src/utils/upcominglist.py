"""The whole upcoming slate, as one embed.

`/upcoming` used to render the soonest three matches as full lineup cards and
the rest as compact embeds — one list in two shapes, where the tall half pushed
the short half out of view. Rosters are what `/lineup` is for; the question
`/upcoming` answers is "what's on and when", and that reads best as a list you
can take in at a glance.

Times are Discord timestamps, so every viewer sees their own clock. The day
headings can't be: they're a single server-side grouping, so they follow the
guild's digest timezone — the same one that decides what "today" means for the
daily schedule.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import discord

from .embeds import BRAND
from .matches import opponents
from .regions import event_flag
from .tournaments import parse_dt

# Enough to cover a busy day without running into the 1024-character cap on a
# field, which lands at roughly a dozen two-line entries.
MAX_MATCHES = 10
MAX_PER_DAY = 6


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    if day < today + timedelta(days=7):
        return day.strftime("%A")
    return day.strftime("%a %d %b")


def _line(match: dict, icons) -> str:
    teams = opponents(match)
    begin = parse_dt(match.get("begin_at"))
    when = f"<t:{int(begin.timestamp())}:R>" if begin else "TBD"

    if len(teams) >= 2:
        a, b = teams[0], teams[1]
        matchup = (
            f"{icons.icon(a)} **{a['name']}** vs **{b['name']}** {icons.icon(b)}"
        ).strip()
    else:
        # An unconfirmed bracket slot: say so rather than inventing an opponent.
        only = teams[0]["name"] if teams else "TBD"
        matchup = f"**{only}** vs _TBD_"

    flag = event_flag(match) or ""
    stage = (match.get("tournament") or {}).get("name") or ""
    league = (match.get("league") or {}).get("name") or ""
    event = " ".join(x for x in (league, stage) if x)
    best_of = match.get("number_of_games")
    detail = " · ".join(
        x for x in (f"{flag} {event}".strip(), f"Bo{best_of}" if best_of else "")
        if x
    )
    return f"{matchup} — {when}\n╰ {detail}" if detail else f"{matchup} — {when}"


def build_upcoming_list(
    bot, matches: list[dict], heading: str, tz=timezone.utc
) -> discord.Embed:
    """Every upcoming match in scope, grouped by day, newest first."""
    embed = discord.Embed(title=f"🗓️ Upcoming · {heading}"[:256], color=BRAND)

    today = datetime.now(timezone.utc).astimezone(tz).date()
    grouped: dict[date, list[dict]] = {}
    undated: list[dict] = []
    for match in matches[:MAX_MATCHES]:
        begin = parse_dt(match.get("begin_at"))
        if begin is None:
            undated.append(match)
            continue
        grouped.setdefault(begin.astimezone(tz).date(), []).append(match)

    for day in sorted(grouped):
        entries = grouped[day][:MAX_PER_DAY]
        hidden = len(grouped[day]) - len(entries)
        value = "\n".join(_line(m, bot.icons) for m in entries)
        if hidden > 0:
            value += f"\n_+{hidden} more_"
        embed.add_field(name=_day_label(day, today), value=value[:1024],
                        inline=False)

    if undated:
        embed.add_field(
            name="Date to be confirmed",
            value="\n".join(_line(m, bot.icons) for m in undated[:MAX_PER_DAY])[:1024],
            inline=False,
        )

    embed.set_footer(text="Times shown in your local zone · /lineup for rosters")
    return embed
