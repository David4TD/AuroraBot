"""Game catalogue: maps friendly names to PandaScore videogame slugs.

PandaScore exposes per-game endpoints under these slugs, e.g.
``/lol/matches`` or ``/valorant/matches``. The CS slug is configurable via
env because PandaScore has historically used ``cs-go`` for Counter-Strike.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Game:
    key: str        # internal key used in slash-command choices
    label: str      # human label shown in Discord
    slug: str       # PandaScore videogame slug
    emoji: str = ""


# Ordered so the most popular titles surface first in dropdowns.
GAMES: dict[str, Game] = {
    "lol": Game("lol", "League of Legends", "lol", "🐉"),
    "valorant": Game("valorant", "Valorant", "valorant", "🎯"),
    "csgo": Game("csgo", "Counter-Strike 2", "cs-go", "💣"),
    "dota2": Game("dota2", "Dota 2", "dota2", "🛡️"),
    "rl": Game("rl", "Rocket League", "rl", "🚗"),
    "r6": Game("r6", "Rainbow Six Siege", "r6siege", "🔫"),
    "codmw": Game("codmw", "Call of Duty", "cod-mw", "🎖️"),
    "ow": Game("ow", "Overwatch", "ow", "🦸"),
    "pubg": Game("pubg", "PUBG", "pubg", "🍳"),
    "mlbb": Game("mlbb", "Mobile Legends", "mlbb", "📱"),
}

# External reference sites the user follows, surfaced in analytics embeds.
REFERENCE_SITES: dict[str, str] = {
    "lol": "https://gol.gg",          # LoL stats (RFT/gol.gg style deep stats)
    "valorant": "https://vlr.gg",
    "csgo": "https://hltv.org",
}


def resolve_slug(key: str, cs_override: str | None = None) -> str:
    game = GAMES.get(key)
    if game is None:
        return key  # allow passing a raw slug through
    if key == "csgo" and cs_override:
        return cs_override
    return game.slug


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
