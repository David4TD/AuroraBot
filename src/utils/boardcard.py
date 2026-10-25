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
MAX_PER_DAY = 10
# How many matchdays the board shows. A week of Worlds is mostly rounds that
# haven't been drawn, so the back half of it was five near-identical lines
# saying nothing is known yet — and it pushed the matches you can actually
# predict off the top of the card.
#
# Counted in *days that have fixtures*, not in hours: events take rest days,
# and "the next two matchdays" is what someone means by two days of an event.
BOARD_DAYS = 2
# A guard against an event with a strange calendar, not a limit anyone meets.
MAX_DAYS = 12
# Discord allows five action rows of five buttons. A match takes two buttons,
# and keeping a pair on its own row is what makes the choice readable — four
# buttons side by side stop reading as two matches. So five matches is one
# message's worth, and the fifth row is given up to the controls whenever
# there are four or fewer.
TAPPABLE_PER_PART = 5
CONTROLS_FIT_AT = 4
# A board spans two messages when one can't hold a button for every open
# match: eight games on a Saturday is an ordinary Worlds day, and making
# someone open a dropdown to pick from them is worse than a second message.
MAX_PARTS = 2
MAX_TAPPABLE = TAPPABLE_PER_PART * MAX_PARTS

# Discord caps an embed at 6000 characters across every part of it and at 25
# fields. Stopping short of both leaves room for a day to grow between edits
# without the next render being rejected outright.
EMBED_BUDGET = 5200
MAX_FIELDS = 20


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


def days_shown(
    matches: list[dict], tz=UTC, days: int = BOARD_DAYS,
    now: datetime | None = None,
) -> tuple[list[dict], int]:
    """Trim to the next *days* matchdays. Returns ``(kept, days_left_out)``.

    Everything downstream reads from this — the fields, the open count, the
    buttons — so a fixture the board doesn't show is also one it doesn't offer
    a button for. A board that listed a week but could only be tapped for the
    first few matches would be the worst of both.
    """
    by_day: dict[date, list[dict]] = {}
    for match in matches:
        begin = parse_dt(match.get("begin_at"))
        if begin is None:
            continue
        by_day.setdefault(begin.astimezone(tz).date(), []).append(match)
    ordered = sorted(by_day)
    keep = ordered[:max(1, days)]
    kept = [m for day in keep for m in by_day[day]]
    return kept, len(ordered) - len(keep)


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
    return f"_{count} {what}{where} not drawn yet_"


def _embed_len(embed: discord.Embed) -> int:
    """What Discord counts against the 6000-character limit."""
    total = len(embed.title or "") + len(embed.description or "")
    total += len(embed.footer.text or "") if embed.footer else 0
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def _day_fields(
    bot, matches: list[dict], tz, backers: dict[int, int], now: datetime
) -> list[tuple[str, str]]:
    """One ``(heading, body)`` per day, chronological."""
    icons = bot.icons
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

    out: list[tuple[str, str]] = []
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
        out.append((name[:256], "\n".join(lines)[:1024]))
    return out


FOOTER = "Updated continuously · each match closes at its own kick-off · /scoring"


def button_slices(matches: list[dict], *, now: datetime | None = None):
    """Open matches split into one bank of buttons per message.

    Five to a message, soonest first, because that is what fits as one match
    per row. Eight games on a Saturday is an ordinary Worlds day, so one
    message cannot offer a button for each of them — and a dropdown for a
    normal matchday is a worse answer than a second message.
    """
    live = open_matches(matches, now=now)[:MAX_TAPPABLE]
    return [live[i:i + TAPPABLE_PER_PART]
            for i in range(0, len(live), TAPPABLE_PER_PART)] or [[]]


def build_board_parts(
    bot,
    matches: list[dict],
    *,
    title: str,
    tz=UTC,
    backers: dict[int, int] | None = None,
    now: datetime | None = None,
    min_parts: int = 1,
    notes: list[str] | None = None,
    more_days: int = 0,
) -> list[discord.Embed]:
    """The event as one card, or two.

    Two reasons to need a second: more open matches than one message can carry
    a button for (the usual one — *min_parts* comes from
    :func:`button_slices`), or a schedule too long for Discord's 6000-character
    embed, which is rejected whole rather than trimmed.

    Days are never split across the two, so a day always reads as one block,
    and the first card holds the nearest fixtures because that is where the
    first bank of buttons goes.
    """
    now = now or datetime.now(UTC)
    backers = backers or {}
    flag = event_flag(matches[0]) if matches else ""
    heading = f"🗓️ {flag} {title}".strip().replace("  ", " ")

    live = open_matches(matches, now=now)
    picks = sum(backers.get(int(m["id"]), 0) for m in live)
    tally = [f"🎲 **{len(live)}** open to predict"]
    if picks:
        tally.append(f"{picks} pick{'' if picks == 1 else 's'} in so far")
    if more_days:
        # Otherwise two days of a three-week event reads as the bot having
        # lost the rest of it.
        tally.append(
            f"{more_days} more day{'' if more_days == 1 else 's'} to come")

    fields = _day_fields(bot, matches, tz, backers, now)

    if not fields:
        empty = discord.Embed(
            title=heading[:256],
            description=(
                "Nothing scheduled yet. This board fills itself in as fixtures "
                "are announced — no need to check back."
            ),
            color=BRAND,
        )
        empty.set_footer(text=FOOTER)
        return [empty]

    parts: list[discord.Embed] = []
    queue = list(fields)
    wanted = max(1, min(min_parts, MAX_PARTS))
    while (queue or len(parts) < wanted) and len(parts) < MAX_PARTS:
        first = not parts
        # A later part says which matches its buttons are for; without that a
        # second bank of buttons under a "continued" heading is a guess.
        note = (notes or [None] * MAX_PARTS)[len(parts)] if notes else None
        embed = discord.Embed(
            title=(heading if first else f"{heading} · continued")[:256],
            description=(" · ".join(tally) if first else note),
            color=BRAND,
        )
        embed.set_footer(text=FOOTER)
        last = len(parts) == MAX_PARTS - 1
        while queue:
            name, value = queue[0]
            if len(embed.fields) >= MAX_FIELDS:
                break
            if (embed.fields
                    and _embed_len(embed) + len(name) + len(value) > EMBED_BUDGET):
                break
            embed.add_field(name=name, value=value, inline=False)
            queue.pop(0)
        if queue and last:
            # Out of room and out of parts: say how much is not shown rather
            # than ending mid-week as though the schedule stopped.
            days = len(queue)
            embed.add_field(
                name="Further ahead",
                value=f"_{days} more day{'' if days == 1 else 's'} scheduled — "
                      f"**🎲 more** lists every open match._",
                inline=False,
            )
            queue.clear()
        parts.append(embed)
    return parts


def build_board(
    bot,
    matches: list[dict],
    *,
    title: str,
    tz=UTC,
    backers: dict[int, int] | None = None,
    now: datetime | None = None,
) -> discord.Embed:
    """The first card of the board. See :func:`build_board_parts`."""
    return build_board_parts(
        bot, matches, title=title, tz=tz, backers=backers, now=now
    )[0]


def signature(embeds, open_ids: list[int]) -> str:
    """What was rendered, as a short digest.

    The sweep runs every few minutes and almost always has nothing new to say.
    Comparing this against the stored one turns those into no API call at all,
    which is what makes a continuously-updated message cheap.

    Covers every part, so a change confined to the second card still triggers
    an edit — and so does a week shrinking from two cards back to one.
    """
    if isinstance(embeds, discord.Embed):
        embeds = [embeds]
    parts: list[str] = [str(len(embeds))]
    for embed in embeds:
        parts.append(embed.title or "")
        parts.append(embed.description or "")
        for field in embed.fields:
            parts.append(field.name or "")
            parts.append(field.value or "")
    parts.append(",".join(str(i) for i in open_ids))
    return hashlib.sha256("\u0000".join(parts).encode("utf-8")).hexdigest()[:32]
