"""Per-server preferences, falling back to the deployment defaults.

The digest hour, its timezone and the alert lead used to be env-only, so every
server the bot served shared one operator's midnight. These read a guild's
override when there is one and the ``Settings`` value otherwise, so a fresh
server behaves exactly as before.

Kept tiny and synchronous on purpose: the loops that need these run across every
guild each tick, so they fetch all overrides once and resolve from the dict.
"""
from __future__ import annotations

from .schedule import load_zone

# Bounds are enforced here rather than at the call site, so a bad row in the
# database can't push the digest to hour 47 or make alerts fire a week early.
MIN_LEAD_MINUTES = 5
MAX_LEAD_MINUTES = 180
LEAD_CHOICES = (5, 10, 15, 30, 45, 60, 90, 120)


def digest_hour(settings, overrides: dict | None) -> int:
    value = (overrides or {}).get("digest_hour")
    if value is None:
        return settings.digest_hour
    return max(0, min(23, int(value)))


def digest_tz_name(settings, overrides: dict | None) -> str:
    return (overrides or {}).get("digest_tz") or settings.digest_tz_name


def digest_tz(settings, overrides: dict | None):
    name = digest_tz_name(settings, overrides)
    # load_zone falls back to UTC on an unknown name rather than raising, so a
    # timezone that stops existing degrades instead of killing the sweep.
    return load_zone(name)


def alert_lead(settings, overrides: dict | None) -> int:
    value = (overrides or {}).get("alert_lead_minutes")
    if value is None:
        return settings.alert_lead_minutes
    return max(MIN_LEAD_MINUTES, min(MAX_LEAD_MINUTES, int(value)))


def describe(settings, overrides: dict | None) -> dict[str, str]:
    """Human-readable current values, and whether each is a server override."""
    over = overrides or {}
    return {
        "digest_hour": f"{digest_hour(settings, over):02d}:00"
        + ("" if "digest_hour" in over else " (default)"),
        "digest_tz": digest_tz_name(settings, over)
        + ("" if "digest_tz" in over else " (default)"),
        "alert_lead_minutes": f"{alert_lead(settings, over)} min"
        + ("" if "alert_lead_minutes" in over else " (default)"),
    }
