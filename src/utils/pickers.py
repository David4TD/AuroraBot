"""Shared autocomplete builders for the game → tournament → team cascade.

Every command that filters uses these, so the pickers look and behave the same
everywhere: whole leagues first, their stages indented beneath, then teams
scoped to whatever was chosen above.

Each builder answers within Discord's deadline or returns a "still loading"
placeholder — never an exception, which Discord renders as
"loading options failed".
"""
from __future__ import annotations

import functools
import logging

from discord import app_commands

from ..services.tourneys import (
    AUTOCOMPLETE_BUDGET,
    LEAGUE_PREFIX,
    LOADING_SENTINEL,
    MAX_CHOICES,
    TOURNAMENT_PREFIX,
)
from .regions import event_flag, region_flag
from .tournaments import tournament_label

log = logging.getLogger("aurorabot.pickers")

LOADING_CHOICE = app_commands.Choice(
    name="⏳ Loading… type another character", value=LOADING_SENTINEL
)


def _never_fails(builder):
    """Turn any error into the loading placeholder.

    Discord renders an exception from an autocomplete as "loading options
    failed", which tells the user nothing and hides the cause. A placeholder
    plus a logged traceback is strictly better: the picker stays usable and the
    real fault is recoverable from the container log.
    """

    @functools.wraps(builder)
    async def wrapper(*args, **kwargs):
        try:
            return await builder(*args, **kwargs)
        except Exception:  # noqa: BLE001 - an autocomplete must always answer
            log.exception("autocomplete %s failed", builder.__name__)
            return [LOADING_CHOICE]

    return wrapper


def game_of(interaction, fallback: str | None = None) -> str | None:
    """The game chosen on this interaction, so pickers can scope to it."""
    game = getattr(interaction.namespace, "game", None)
    if hasattr(game, "value"):        # app_commands.Choice
        game = game.value
    return game or fallback


@_never_fails
async def tournament_choices(
    directory, game_key: str | None, current: str
) -> list[app_commands.Choice[str]]:
    """Leagues first, then the stages inside them.

    A league's stages are separate tournaments that often run in parallel —
    LCK splits into a Legend Group and a Rise Group — so offering the league
    itself is what most people actually want.
    """
    if not game_key:
        return []

    tournaments = await directory.current(game_key, wait=AUTOCOMPLETE_BUDGET)
    if tournaments is None:
        return [LOADING_CHOICE]

    query = (current or "").strip().lower()

    leagues: dict[int, dict] = {}
    for t in tournaments:
        lg = t.get("league") or {}
        if not lg.get("id"):
            continue
        entry = leagues.setdefault(
            int(lg["id"]), {"name": lg.get("name") or "League", "stages": 0}
        )
        entry["stages"] += 1

    choices: list[app_commands.Choice[str]] = []
    for lid, info in leagues.items():
        name = info["name"]
        if query and query not in name.lower():
            continue
        flag = region_flag(name) or ""
        suffix = (
            f" — all {info['stages']} stages" if info["stages"] > 1
            else " — whole league"
        )
        choices.append(
            app_commands.Choice(
                name=f"{flag} {name}{suffix}".strip()[:100],
                value=f"{LEAGUE_PREFIX}{lid}",
            )
        )
        if len(choices) == MAX_CHOICES:
            return choices

    for t in tournaments:
        label = tournament_label(t)
        if query and query not in label.lower():
            continue
        flag = event_flag(t)
        choices.append(
            app_commands.Choice(
                name=(f"↳ {flag} {label}" if flag else f"↳ {label}")[:100],
                value=f"{TOURNAMENT_PREFIX}{t['id']}",
            )
        )
        if len(choices) == MAX_CHOICES:
            break
    return choices


@_never_fails
async def team_choices(
    directory, game_key: str | None, tournament_value: str | None, current: str
) -> list[app_commands.Choice[str]]:
    """Teams in scope — narrowed to the chosen tournament when there is one.

    Rosters come from the tournament payload, so this needs no extra API call.
    """
    if not game_key:
        return []

    # Don't block on resolving the tournament: if its list isn't cached yet,
    # fall back to every team in the game rather than stalling the picker.
    cached = await directory.current(game_key, wait=AUTOCOMPLETE_BUDGET)
    if cached is None:
        return [LOADING_CHOICE]

    target = await directory.resolve(tournament_value, game_key)
    roster = await directory.teams(game_key, target)

    query = (current or "").strip().lower()
    choices = []
    for team in roster:
        if query and query not in team["name"].lower():
            continue
        choices.append(
            app_commands.Choice(name=team["name"][:100], value=str(team["id"]))
        )
        if len(choices) == MAX_CHOICES:
            break
    return choices
