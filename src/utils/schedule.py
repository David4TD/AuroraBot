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


def local_hhmm(moment: datetime, tz) -> str:
    return moment.astimezone(tz).strftime("%H:%M")


def iso_date(day: date) -> str:
    return day.isoformat()
