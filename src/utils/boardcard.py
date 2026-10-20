"""The event board: one message that is the whole schedule, edited in place.

The daily digest could only ever describe one day, and which day a match
belonged to came from a handover hour — everything before 10am rode with the
previous day's card so an overnight fixture wasn't announced twenty minutes
before kick-off. That works until an event plays straight through the boundary.
Worlds runs 04:00–11:00 in Sydney, so a single Swiss round of eight games was
split across two nights: seven on one card, one lonely game on the next. And
notice swung between eleven and thirty-three hours purely on kick-off hour.

A board has no day to belong to, so none of that applies. It also needs no
bookkeeping for undrawn fixtures: it re-renders from the feed every sweep, so a
bracket slot that fills simply appears, and a round that hasn't been drawn shows
as a promise rather than eight rows of "TBD vs TBD".

**It is one message shared by everyone**, which is the constraint that shapes
it. Discord renders a single body for all viewers, so nothing here can say
"you picked Gen.G" — per-member state lives in the ephemeral reply behind the
buttons. What the board can show is how many people have picked each match,
which is also the honest limit: a vote *split* before kick-off would anchor
everyone who read it, so only the count is shown.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import discord

from .embeds import BRAND
from .matches import opponents
from .regions import event_flag
from .scoring import round_tag
from .tournaments import parse_dt

UTC = timezone.utc

# A day's fixtures go in one field, and a field value caps at 1024 characters.
MAX_DAYS = 6
MAX_PER_DAY = 10
# Rows of buttons, one match each, leaving the fifth for the controls.
TAPPABLE = 4


def _day_label(day: date, today: date) -> str:
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    if day < today + timedelta(days=7):
        return day.strftime("%A %d %b")
    return day.strftime("%a %d %b")


def open_matches(matches: list[dict], *, now: datetime | None = None) -> list[dict]:
    """Drawn fixtures still ahead of kick-off, soonest first.

    These are the ones that can be predicted, so they are also the ones the
    buttons and the picker are built from.
    """
    now = now or datetime.now(UTC)
    out = []
    for match in matches:
        if len(opponents(match)) < 2:
            continue
        begin = parse_dt(match.get("begin_at"))
        if begin is None or begin <= now:
            continue
        out.append(match)
    out.sort(key=lambda m: parse_dt(m.get("begin_at")) or datetime.max.replace(
        tzinfo=UTC))
    return out


def _line(match: dict, icons, tz, backers: int) -> str:
    teams = opponents(match)
    begin = parse_dt(match.get("begin_at"))
    when = f"`{begin.astimezone(tz):%H:%M}`" if begin else "`  ?  `"
    a, b = teams[0], teams[1]
    body = (
        f"{icons.icon(a)} **{a['name']}** vs **{b['name']}** {icons.icon(b)}"
    ).strip()
    bits = [when, body]
    tag = round_tag(match)
    if tag:
        bits.append(tag)
    if backers:
        bits.append(f"🎲 {backers}")
    return " ".join(bits[:2]) + ("  ·  " + " · ".join(bits[2:]) if bits[2:] else "")


def _undrawn_note(count: int, stage: str | None) -> str:
    what = "game" if count == 1 else "games"
    where = f" of {stage}" if stage else ""
    return (
        f"_{count} {what}{where} not drawn yet — they appear here as soon as "
        f"the results that decide them land._"
    )


def build_board(
    bot,
    matches: list[dict],
    *,
    title: str,
    tz=UTC,
    backers: dict[int, int] | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """The whole event as one card: every day, drawn and undrawn alike."""
    now = now or datetime.now(UTC)
    backers = backers or {}
    icons = bot.icons
    flag = event_flag(matches[0]) if matches else ""
    heading = f"🗓️ {flag} {title}".strip().replace("  ", " ")
    embed = discord.Embed(title=heading[:256], color=BRAND)

    today = now.astimezone(tz).date()
    drawn: dict[date, list[dict]] = {}
    undrawn: dict[date, list[dict]] = {}
    for match in matches:
        begin = parse_dt(match.get("begin_at"))
        if begin is None:
            continue
        day = begin.astimezone(tz).date()
        bucket = drawn if len(opponents(match)) >= 2 else undrawn
        bucket.setdefault(day, []).append(match)

    for day in sorted(set(drawn) | set(undrawn))[:MAX_DAYS]:
        games = sorted(drawn.get(day, []),
                       key=lambda m: parse_dt(m.get("begin_at")) or now)
        pending = undrawn.get(day, [])
        total = len(games) + len(pending)
        stage = ""
        if games or pending:
            first = (games or pending)[0]
            stage = (first.get("tournament") or {}).get("name") or ""
        name = f"{_day_label(day, today)} · {total} game{'' if total == 1 else 's'}"
        if stage:
            name = f"{name} · {stage}"

        lines = [_line(m, icons, tz, backers.get(int(m["id"]), 0))
                 for m in games[:MAX_PER_DAY]]
        if len(games) > MAX_PER_DAY:
            lines.append(f"_+{len(games) - MAX_PER_DAY} more_")
        if pending:
            # One promise rather than ten rows of "TBD vs TBD", which is all a
            # Swiss round or a lower bracket looks like until it is drawn.
            lines.append(_undrawn_note(
                len(pending), (pending[0].get("name") or "").split(":")[0] or None))
        embed.add_field(name=name[:256], value="\n".join(lines)[:1024],
                        inline=False)

    live = open_matches(matches, now=now)
    picks = sum(backers.get(int(m["id"]), 0) for m in live)
    tally = [f"🎲 **{len(live)}** open to predict"]
    if picks:
        tally.append(f"{picks} pick{'' if picks == 1 else 's'} in so far")
    embed.description = " · ".join(tally)

    if not embed.fields:
        embed.description = (
            "Nothing scheduled yet. This board fills itself in as fixtures are "
            "announced — no need to check back."
        )
    embed.set_footer(
        text="Updated continuously · each match closes at its own kick-off · "
        "/scoring"
    )
    return embed


def signature(embed: discord.Embed, open_ids: list[int]) -> str:
    """What was rendered, as a short digest.

    The sweep runs every few minutes and almost always has nothing new to say.
    Comparing this against the stored one turns those into no API call at all,
    which is what makes a continuously-updated message cheap.
    """
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    parts.append(",".join(str(i) for i in open_ids))
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()[:32]
