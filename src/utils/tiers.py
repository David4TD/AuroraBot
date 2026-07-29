"""Tournament tier filtering — AuroraBot only surfaces top-flight eSports.

PandaScore grades tournaments ``S > A > B > C > D > unranked``, assigned
in-house from the organiser, the calibre of the field and the prize pool.
Crucially, **their ``S`` is narrower than what the scene calls "Tier 1"**:

* ``S`` — Worlds, MSI, The International, CS Majors / IEM / BLAST, VCT Masters
* ``A`` — LEC, LCK, LCS, LPL, VCT EMEA / NA, ESL Pro League, DPC Division 1

Filtering on ``S`` alone therefore drops every regional league. "Tier 1" in the
sense fans mean it is ``S`` **and** ``A``, which is the default here. Narrow it
with ``TIERS=s`` (majors only) or widen it with ``TIERS=s,a,b``.

Tier lives on the *tournament* object only — not on series or leagues. Match
payloads embed their tournament, so match feeds are filtered client-side after
over-fetching; the tournament endpoints could also use PandaScore's
``filter[tier]``, but filtering in one place keeps the behaviour identical
everywhere.

Ref: https://developers.pandascore.co/docs/tournaments-in-depth
"""
from __future__ import annotations

from typing import Any, Iterable

# What "Tier 1" means by default: PandaScore's top two grades.
DEFAULT_TOP_TIERS: frozenset[str] = frozenset({"s", "a"})

# Some payloads/users write tiers numerically. Map them onto the letter scale
# rather than treating them as separate values.
_TIER_ALIASES: dict[str, str] = {
    "1": "s", "tier 1": "s", "tier1": "s", "premier": "s",
    "2": "a", "tier 2": "a", "tier2": "a",
    "3": "b", "tier 3": "b", "tier3": "b",
    "4": "c", "5": "d",
}

# Where a tier can hide on a match payload, most specific first. PandaScore
# only documents it on the tournament, but the fallbacks cost nothing and
# protect against a payload shape change.
_TIER_PARENTS = ("tournament", "serie", "league")


def normalise_tier(value: Any) -> str | None:
    """Return a lowercase tier letter, or ``None`` if absent.

    Numeric spellings are folded onto the letter scale (``1`` → ``s``).
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    return _TIER_ALIASES.get(text, text)


def parse_tier_list(raw: str) -> frozenset[str]:
    """Parse a ``TIERS`` env value like ``"s,a"`` into a tier set."""
    tiers = {
        norm
        for chunk in (raw or "").replace(";", ",").split(",")
        if (norm := normalise_tier(chunk))
    }
    return frozenset(tiers) or DEFAULT_TOP_TIERS


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


def is_top_tier(obj: dict | None, allowed: frozenset[str] | None = None) -> bool:
    """True when *obj* belongs to a tournament in the allowed tier set.

    Unknown tiers are treated as **not** top tier: quietly letting third-tier
    qualifiers through a "Tier 1" filter would be worse than showing less.
    """
    return tier_of(obj) in (allowed or DEFAULT_TOP_TIERS)


def filter_top_tier(
    items: Iterable[dict],
    *,
    enabled: bool = True,
    allowed: frozenset[str] | None = None,
) -> list[dict]:
    """Keep only top-tier entries. A no-op when *enabled* is False."""
    items = list(items)
    if not enabled:
        return items
    allowed = allowed or DEFAULT_TOP_TIERS
    return [item for item in items if is_top_tier(item, allowed)]


def filter_for(settings, items: Iterable[dict]) -> list[dict]:
    """``filter_top_tier`` wired to the bot's configured tier policy."""
    return filter_top_tier(
        items, enabled=settings.top_tier_only, allowed=settings.tier_allowlist
    )


def describe(settings) -> str:
    """Human summary of the active policy, e.g. ``S-Tier / A-Tier``."""
    if not settings.top_tier_only:
        return "all tiers"
    return " / ".join(
        f"{t.upper()}-Tier" for t in sorted(settings.tier_allowlist)
    )


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
    "**Tier 1** tournaments (PandaScore S- and A-tier)."
)
