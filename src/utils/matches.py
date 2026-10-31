"""Small readers for PandaScore match payloads.

Several cogs need the same two things out of a match — who's playing and which
team IDs are involved — so the shape-juggling lives here rather than being
re-implemented per cog.
"""
from __future__ import annotations

from .games import key_for_videogame


def game_key_of(match: dict, cs_override: str | None = None) -> str | None:
    """Which catalogue game a match belongs to, from its ``videogame`` block.

    ``None`` when PandaScore returns a title AuroraBot doesn't catalogue — such
    matches are left alone by the per-guild toggles, since there's no switch to
    turn them off with.
    """
    return key_for_videogame(match.get("videogame"), cs_override)


def opponents(match: dict) -> list[dict]:
    """The confirmed teams in a match, with everything needed to draw them.

    Matches with an unconfirmed side (a bracket slot that hasn't been filled)
    yield fewer than two entries — callers should check the length before
    treating it as a head-to-head.

    ``image_url`` matters as much as the name: it is what the icon store mints
    a team's badge from, and this returned only id and name for a long time.
    Every card draws its crests from here, so a team that was only ever seen
    through a match — rather than through a roster picker, which passes the
    whole team object — could never have an icon minted for it at all. That
    looked like a handful of teams Discord simply refused.

    ``location`` comes along for the same reason: it is what the flag
    fallback reads when a team has no badge yet.
    """
    out: list[dict] = []
    for entry in match.get("opponents") or []:
        team = entry.get("opponent") or {}
        if not team.get("id"):
            continue
        out.append({
            "id": int(team["id"]),
            "name": team.get("name", "Team"),
            "image_url": team.get("image_url"),
            "acronym": team.get("acronym"),
            "location": team.get("location"),
        })
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
