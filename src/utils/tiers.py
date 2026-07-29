"""Tournament tier filtering — AuroraBot only surfaces top-flight eSports.

PandaScore grades every tournament/serie/league with a ``tier`` field. Its
scale is lettered (``s`` → ``d``, plus ``unranked``) but a few payloads use the
numeric convention instead (``1`` → ``4``). "Tier 1" and "S tier" mean the same
thing — the premier international/regional circuits — so both spellings are
accepted here.

The ``/matches`` endpoints do not expose a server-side ``filter[tier]``, but
every match payload embeds its ``tournament`` / ``serie`` / ``league`` objects
with the tier on board, so filtering happens client-side. Callers should
over-fetch (a larger ``per_page``) and then filter, since a tier-1-only view of
a busy day can still be a small slice of the raw feed.
"""
from __future__ import annotations

from typing import Any, Iterable

# Accepted spellings of the top tier. PandaScore is lowercase-lettered, but be
# forgiving about numeric/verbose variants so a payload change doesn't silently
# filter everything away.
TOP_TIERS: frozenset[str] = frozenset({"s", "1", "tier 1", "tier1", "premier"})

# Where a tier can hide on a match payload, most specific first.
_TIER_PARENTS = ("tournament", "serie", "league")


def normalise_tier(value: Any) -> str | None:
    """Return a lowercase, trimmed tier string, or ``None`` if absent."""
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def tier_of(obj: dict | None) -> str | None:
    """Extract the tier from a match, tournament, serie or league payload.

    A tournament/serie/league carries ``tier`` directly. A match carries it on
    its embedded parents, so those are checked in order of specificity.
    """
    if not obj:
        return None

    direct = normalise_tier(obj.get("tier"))
    if direct:
        return direct

    for key in _TIER_PARENTS:
        parent = obj.get(key)
        if isinstance(parent, dict):
            tier = normalise_tier(parent.get("tier"))
            if tier:
                return tier
    return None


def is_top_tier(obj: dict | None) -> bool:
    """True when *obj* belongs to a Tier 1 / S-tier tournament.

    Unknown tiers are treated as **not** top tier: showing a filtered feed that
    quietly includes tier-3 matches would be worse than showing fewer results.
    """
    return tier_of(obj) in TOP_TIERS


def filter_top_tier(items: Iterable[dict], *, enabled: bool = True) -> list[dict]:
    """Keep only Tier 1 / S-tier entries. A no-op when *enabled* is False."""
    items = list(items)
    if not enabled:
        return items
    return [item for item in items if is_top_tier(item)]


def tier_label(obj: dict | None) -> str:
    """Human-readable tier badge for embeds, e.g. ``S-Tier`` or ``Tier 1``."""
    tier = tier_of(obj)
    if tier is None:
        return "Unranked"
    if tier.isdigit():
        return f"Tier {tier}"
    if len(tier) == 1:
        return f"{tier.upper()}-Tier"
    return tier.title()


NO_TOP_TIER_MATCHES = (
    "Nothing at that level right now — AuroraBot only tracks "
    "**Tier 1 / S-tier** tournaments."
)
