"""Enforcement helpers for the per-server game selection (`/games`).

**Games are opt-in.** A server follows nothing until someone runs `/games`, so
AuroraBot never posts about titles nobody asked for. That makes the empty set
meaningful rather than a bug, and it's why every helper here takes the *enabled*
set rather than a set of exclusions.

Three shapes are needed, and they differ deliberately:

* :func:`no_games_message` — the server hasn't chosen anything yet. Explain how,
  rather than replying "nothing found" as though the API were empty.
* :func:`blocked_message` — the user named a game the server doesn't follow. Say
  which one, rather than returning an empty list that looks like an outage.
* :func:`filter_enabled` — an unfiltered feed spanning every title. Silently drop
  what isn't followed; that's the whole point.
"""
from __future__ import annotations

from .games import label_for
from .matches import game_key_of

NO_GAMES = (
    "This server isn't following any games yet. A member with *Manage Server* "
    "can pick them with `/games`."
)


def no_games_message() -> str:
    return NO_GAMES


def blocked_message(game_key: str) -> str:
    return (
        f"This server doesn't follow **{label_for(game_key)}**. "
        f"A member with *Manage Server* can add it with `/games`."
    )


def filter_enabled(
    matches: list[dict], enabled: set[str], cs_override: str | None = None
) -> list[dict]:
    """Keep only matches belonging to a game the server follows.

    Titles outside the catalogue can never be in *enabled*, so they're dropped
    too — correct under opt-in, where nothing appears unless it was chosen.
    """
    if not enabled:
        return []
    return [m for m in matches if game_key_of(m, cs_override) in enabled]
