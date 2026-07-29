"""Enforcement helpers for the per-server game toggles (`/games`).

Two shapes are needed and they behave differently on purpose:

* :func:`blocked_message` — the user named a muted game explicitly. Say so,
  rather than returning an empty list that looks like an outage.
* :func:`filter_enabled` — an unfiltered feed spanning every title. Silently
  drop the muted ones; that's the whole point of the toggle.

Titles outside the catalogue (:data:`utils.games.GAMES`) are never dropped —
there's no switch to turn them off with, so filtering them would be a setting
the user can't see or undo.
"""
from __future__ import annotations

from .games import label_for
from .matches import game_key_of


def blocked_message(game_key: str) -> str:
    return (
        f"**{label_for(game_key)}** is muted on this server. "
        f"A member with *Manage Server* can re-enable it with `/games`."
    )


def filter_enabled(
    matches: list[dict], disabled: set[str], cs_override: str | None = None
) -> list[dict]:
    """Drop matches belonging to a muted game."""
    if not disabled:
        return matches
    return [
        m for m in matches if game_key_of(m, cs_override) not in disabled
    ]
