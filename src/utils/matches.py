"""Small readers for PandaScore match payloads.

Several cogs need the same two things out of a match — who's playing and which
team IDs are involved — so the shape-juggling lives here rather than being
re-implemented per cog.
"""
from __future__ import annotations

from .games import key_for_slug


def game_key_of(match: dict, cs_override: str | None = None) -> str | None:
    """Which catalogue game a match belongs to, from its ``videogame`` block.

    ``None`` when PandaScore returns a title AuroraBot doesn't catalogue — such
    matches are left alone by the per-guild toggles, since there's no switch to
    turn them off with.
    """
    videogame = match.get("videogame") or {}
    return key_for_slug(videogame.get("slug"), cs_override)


def opponents(match: dict) -> list[dict]:
    """Return ``[{"id": ..., "name": ...}, ...]`` for the confirmed teams.

    Matches with an unconfirmed side (a bracket slot that hasn't been filled)
    yield fewer than two entries — callers should check the length before
    treating it as a head-to-head.
    """
    out: list[dict] = []
    for entry in match.get("opponents") or []:
        team = entry.get("opponent") or {}
        if team.get("id"):
            out.append({"id": int(team["id"]), "name": team.get("name", "Team")})
    return out


def team_ids(match: dict) -> set[int]:
    return {team["id"] for team in opponents(match)}


def league_id(match: dict) -> int | None:
    """The league a match belongs to.

    PandaScore nests League > Serie > Tournament, and a league's stages are
    separate tournaments — LCK's "Legend Group" and "Rise Group" run in
    parallel. Subscribing at the league level catches all of them.
    """
    value = (match.get("league") or {}).get("id")
    return int(value) if value else None


def tournament_id(match: dict) -> int | None:
    tournament = match.get("tournament") or {}
    value = tournament.get("id")
    return int(value) if value else None


def is_head_to_head(match: dict) -> bool:
    return len(opponents(match)) >= 2
