"""Small readers for PandaScore match payloads.

Several cogs need the same two things out of a match — who's playing and which
team IDs are involved — so the shape-juggling lives here rather than being
re-implemented per cog.
"""
from __future__ import annotations


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


def tournament_id(match: dict) -> int | None:
    tournament = match.get("tournament") or {}
    value = tournament.get("id")
    return int(value) if value else None


def is_head_to_head(match: dict) -> bool:
    return len(opponents(match)) >= 2
