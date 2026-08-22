"""Local-calendar helpers for the daily schedule digest.

"Today's matches" only means something relative to a timezone, and the
container runs on UTC, so the digest works in an explicit zone (``DIGEST_TZ``).
A named zone rather than a fixed offset matters: it keeps the post at local
midnight across daylight-saving changes, which a ``+10:00`` offset would not.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("aurorabot.schedule")

UTC = timezone.utc


def load_zone(name: str) -> ZoneInfo | timezone:
    """Resolve a zone name, falling back to UTC rather than failing to boot.

    A typo in an env var shouldn't take the bot down; a warning plus UTC is a
    far better outcome than a crash loop.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        log.warning("Unknown DIGEST_TZ %r — falling back to UTC", name)
        return UTC


def local_now(tz) -> datetime:
    return datetime.now(UTC).astimezone(tz)


def local_today(tz) -> date:
    return local_now(tz).date()


def day_window(day: date, tz) -> tuple[datetime, datetime]:
    """UTC bounds of a local calendar day, as ``[start, end)``.

    Built by adding 24h to the local midnight rather than using the next
    calendar date, so DST transitions produce 23- or 25-hour days correctly
    instead of silently dropping or duplicating an hour.
    """
    start_local = datetime.combine(day, time(0, 0), tzinfo=tz)
    start = start_local.astimezone(UTC)
    end = (start_local + timedelta(days=1)).astimezone(UTC)
    return start, end


def within_day(moment: datetime | None, day: date, tz) -> bool:
    if moment is None:
        return False
    start, end = day_window(day, tz)
    return start <= moment.astimezone(UTC) < end


# A match kicking off before this hour is an overnight fixture: it belongs to
# the *previous* day's digest, which went out a day earlier, rather than to the
# card for the date it happens to fall on. Without it a 00:20 match is listed
# by a midnight digest twenty minutes before it starts, which is a listing
# nobody can act on.
OVERNIGHT_UNTIL = 10

# How far after a digest posts the handover can sit. Only matters for a
# schedule set later than OVERNIGHT_UNTIL, where the two would otherwise
# coincide: a match at exactly the post time would belong to the card going out
# that second, have already started by the time it did, and so be dropped from
# that one and excluded from the one before — listed by neither.
SEAM = timedelta(minutes=15)


def post_at(day: date, tz, hour: int) -> datetime:
    """When *day*'s digest goes out, in local time."""
    return datetime.combine(day, time(hour, 0), tzinfo=tz)


def handover(day: date, tz, digest_hour: int) -> datetime:
    """When *day*'s digest takes over from the one before, in local time.

    Normally :data:`OVERNIGHT_UNTIL`, so everything between midnight and 10am
    rides with the card posted the day before it is played. A schedule set
    later than that hands over shortly after it posts instead — otherwise an
    18:00 digest would own a window opening at 10:00, and every match in
    between would already have started by the time the card went out.
    """
    return max(
        post_at(day, tz, OVERNIGHT_UNTIL),
        post_at(day, tz, int(digest_hour)) + SEAM,
    )


def digest_window(day: date, tz, hour: int = 0) -> tuple[datetime, datetime]:
    """UTC bounds of what one digest covers, as ``[start, end)``.

    A digest day runs handover to handover rather than midnight to midnight,
    so the night's matches sit on the card posted the evening before they are
    played. Consecutive windows abut exactly, which is what makes every match
    fall inside one and only one — no lookahead to tune, no cross-day dedupe,
    and no way for a fixture to be listed twice or missed entirely.
    """
    return (
        handover(day, tz, hour).astimezone(UTC),
        handover(day + timedelta(days=1), tz, hour).astimezone(UTC),
    )


def within_digest_window(
    moment: datetime | None, day: date, tz, hour: int = 0
) -> bool:
    if moment is None:
        return False
    start, end = digest_window(day, tz, hour)
    return start <= moment.astimezone(UTC) < end


def is_after_midnight(moment: datetime | None, day: date, tz) -> bool:
    """True for a match the window reached forward into the next day to catch."""
    if moment is None:
        return False
    return moment.astimezone(UTC) >= day_window(day, tz)[1]


def local_hhmm(moment: datetime, tz) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def iso_date(day: date) -> str:
    return day.isoformat()
