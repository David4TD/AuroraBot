"""Game catalogue.

PandaScore uses **two different vocabularies** for the same game, and conflating
them is a silent-data-loss bug:

* the **route** segment in ``/{route}/matches/...`` — e.g. ``lol``, ``csgo``
* the **videogame** object carried on every payload, with its own ``id`` and
  ``slug`` — e.g. ``id=1, slug="league-of-legends"``

They differ for half the catalogue, and the canonical slug is frequently *not* a
valid route: ``/league-of-legends/matches/running``, ``/cs-go/...``,
``/dota-2/...``, ``/r6-siege/...`` and ``/cod-mw/...`` all 404. Numeric ids
aren't routes either. Verified against the live API on 2026-07-30.

So each game records both, and identification prefers ``api_id``: PandaScore
renames slugs over time (``cs-go`` will presumably become ``cs2``), but ids are
stable.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    key: str        # internal key used in slash-command choices
    label: str      # human label shown in Discord
    route: str      # path segment: /{route}/matches/... — NOT the payload slug
    api_id: int     # videogame.id in payloads; the stable identifier
    api_slug: str   # videogame.slug in payloads
    emoji: str = ""


# Ordered so the most popular titles surface first in dropdowns.
# route / api_id / api_slug all confirmed against /videogames and live requests.
GAMES: dict[str, Game] = {
    "lol":      Game("lol",      "League of Legends", "lol",      1,  "league-of-legends", "🐉"),
    "valorant": Game("valorant", "Valorant",          "valorant", 26, "valorant",          "🎯"),
    "csgo":     Game("csgo",     "Counter-Strike 2",  "csgo",     3,  "cs-go",             "💣"),
    "dota2":    Game("dota2",    "Dota 2",            "dota2",    4,  "dota-2",            "🛡️"),
    "rl":       Game("rl",       "Rocket League",     "rl",       22, "rl",                "🚗"),
    "r6":       Game("r6",       "Rainbow Six Siege", "r6siege",  24, "r6-siege",          "🔫"),
    "codmw":    Game("codmw",    "Call of Duty",      "codmw",    23, "cod-mw",            "🎖️"),
    "ow":       Game("ow",       "Overwatch",         "ow",       14, "ow",                "🦸"),
    "pubg":     Game("pubg",     "PUBG",              "pubg",     20, "pubg",              "🍳"),
    "mlbb":     Game("mlbb",     "Mobile Legends",    "mlbb",     34, "mlbb",              "📱"),
}

ALL_GAME_KEYS: frozenset[str] = frozenset(GAMES)

_BY_API_ID: dict[int, str] = {g.api_id: g.key for g in GAMES.values()}

# External reference sites the user follows, surfaced in analytics embeds.
REFERENCE_SITES: dict[str, str] = {
    "lol": "https://gol.gg",          # LoL stats (RFT/gol.gg style deep stats)
    "valorant": "https://vlr.gg",
    "csgo": "https://hltv.org",
}


def resolve_slug(key: str, cs_override: str | None = None) -> str:
    """Catalogue key → the path segment for ``/{route}/…`` endpoints."""
    game = GAMES.get(key)
    if game is None:
        return key  # allow passing a raw route through
    if key == "csgo" and cs_override:
        return cs_override
    return game.route


def key_for_slug(slug: str | None, cs_override: str | None = None) -> str | None:
    """PandaScore slug → catalogue key, accepting either vocabulary.

    Payload slugs (``league-of-legends``) and route slugs (``lol``) are both
    matched, because callers legitimately hold one or the other.
    """
    if not slug:
        return None
    slug = slug.strip().lower()
    if cs_override and slug == cs_override.strip().lower():
        return "csgo"
    for game in GAMES.values():
        if slug in (game.api_slug, game.route):
            return game.key
    return None


def key_for_videogame(
    videogame: dict | None, cs_override: str | None = None
) -> str | None:
    """Catalogue key for a payload's ``videogame`` block.

    Prefers the numeric id: it survives the slug renames PandaScore does
    periodically, whereas matching on ``slug`` alone silently stops working the
    day ``cs-go`` becomes ``cs2``.
    """
    if not videogame:
        return None
    vg_id = videogame.get("id")
    if vg_id is not None:
        try:
            key = _BY_API_ID.get(int(vg_id))
        except (TypeError, ValueError):
            key = None
        if key:
            return key
    return key_for_slug(videogame.get("slug"), cs_override)


def game_choices() -> list[tuple[str, str]]:
    """Return (label, key) pairs for building app-command choices."""
    return [(g.label, g.key) for g in GAMES.values()]


def label_for(key: str) -> str:
    g = GAMES.get(key)
    return g.label if g else key


def rank_by_name(items: list[dict], query: str, name_key: str = "name") -> list[dict]:
    """Rank API results so the closest name match to *query* comes first.

    Order: exact (case-insensitive) → startswith → contains → other, and within
    each tier the shortest name wins. This stops broad searches like ``LCK``
    from resolving to ``LCK Academy`` when the main ``LCK`` league exists.
    """
    q = (query or "").strip().lower()

    def score(item: dict) -> tuple[int, int]:
        name = str(item.get(name_key) or "").strip().lower()
        if name == q:
            tier = 0
        elif name.startswith(q):
            tier = 1
        elif q in name:
            tier = 2
        else:
            tier = 3
        return (tier, len(name))

    return sorted(items, key=score)
