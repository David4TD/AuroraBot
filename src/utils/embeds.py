"""Embed builders — keeps Discord presentation logic out of the cogs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:  # pragma: no cover
    from ..services.emojis import TeamIconStore

from .games import REFERENCE_SITES, label_for
from .regions import event_flag, event_region, team_flag
from .tiers import tier_label

BRAND = discord.Color.from_rgb(138, 99, 255)  # AuroraBot purple
GREEN = discord.Color.from_rgb(59, 201, 121)
RED = discord.Color.from_rgb(230, 78, 92)
AMBER = discord.Color.from_rgb(240, 176, 64)


def _fmt_dt(value: str | None) -> str:
    if not value:
        return "TBD"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f"<t:{int(dt.timestamp())}:F> (<t:{int(dt.timestamp())}:R>)"
    except (ValueError, TypeError):
        return value


def _opponent_dicts(match: dict) -> list[dict]:
    """The raw team payloads, needed for logos (``image_url``) and flags."""
    return [
        (o.get("opponent") or {})
        for o in (match.get("opponents") or [])[:2]
    ]


def _opponents(match: dict) -> tuple[str, str]:
    opps = match.get("opponents") or []
    names = []
    for o in opps[:2]:
        team = o.get("opponent") or {}
        names.append(team.get("name", "TBD"))
    while len(names) < 2:
        names.append("TBD")
    return names[0], names[1]


def _score_line(match: dict) -> str:
    results = match.get("results") or []
    if len(results) >= 2:
        return f"**{results[0].get('score', 0)}** – **{results[1].get('score', 0)}**"
    return "vs"


def match_embed(
    match: dict, game_key: str | None = None, icons: "TeamIconStore | None" = None
) -> discord.Embed:
    """Render a match.

    Layout note: the matchup sits in the **description**, not the title,
    because Discord does not render custom emoji (the team logos) in embed
    titles — they leak as raw ``<:name:id>`` text, worst of all on mobile. The
    title carries the state and the event, using only unicode emoji and the
    region flag, which do render there.
    """
    a, b = _opponents(match)
    state = (match.get("status") or "").lower()
    color = {"running": GREEN, "not_started": AMBER, "finished": BRAND}.get(state, BRAND)

    league = (match.get("league") or {}).get("name")
    serie = (match.get("serie") or {}).get("full_name")
    tournament = (match.get("tournament") or {}).get("name")
    context = " · ".join(x for x in [league, serie, tournament] if x)
    flag = event_flag(match)

    marker = {"running": "🔴 LIVE", "finished": "✅ Result"}.get(state, "🕒 Upcoming")
    heading = " · ".join(x for x in [marker, f"{flag} {context}" if flag else context] if x)
    embed = discord.Embed(title=heading[:256], color=color)

    # Team logos render inline here. Without a cached icon this degrades to
    # exactly the previous plain-text matchup.
    team_a, team_b = (_opponent_dicts(match) + [{}, {}])[:2]
    icon_a = icons.icon(team_a) if icons else ""
    icon_b = icons.icon(team_b) if icons else ""
    left = f"{icon_a} **{a}**".strip()
    right = f"{icon_b} **{b}**".strip()
    separator = _score_line(match) if state in {"running", "finished"} else "vs"
    embed.description = f"{left}  {separator}  {right}"

    embed.add_field(name="Format", value=match.get("match_type", "—"), inline=True)
    embed.add_field(
        name="Best of", value=str(match.get("number_of_games", "—")), inline=True
    )
    embed.add_field(name="Tier", value=f"🏅 {tier_label(match)}", inline=True)
    if state != "running":
        embed.add_field(name="Starts", value=_fmt_dt(match.get("begin_at")), inline=False)

    stream = None
    for s in match.get("streams_list") or []:
        if s.get("main") and s.get("raw_url"):
            stream = s["raw_url"]
            break
    if stream:
        embed.add_field(name="Stream", value=f"[Watch here]({stream})", inline=False)

    if game_key:
        embed.set_footer(text=f"AuroraBot · {label_for(game_key)} · match #{match.get('id')}")
    else:
        embed.set_footer(text=f"AuroraBot · match #{match.get('id')}")
    embed.timestamp = datetime.now(timezone.utc)
    return embed


def standings_embed(
    tournament_name: str,
    standings: list[dict],
    tournament: dict | None = None,
    icons: "TeamIconStore | None" = None,
) -> discord.Embed:
    flag = event_flag(tournament) if tournament else None
    heading = f"{flag} {tournament_name}" if flag else tournament_name
    embed = discord.Embed(title=f"📊 Standings · {heading}", color=BRAND)

    # Queue every logo up front so one pass of the worker covers the table.
    teams = [(row.get("team") or {}) for row in standings[:20]]
    if icons:
        icons.warm(teams)

    lines = []
    for i, row in enumerate(standings[:20], start=1):
        team = row.get("team") or {}
        name = team.get("name", "—")
        # Prefer the club's own logo; fall back to its country flag (from
        # PandaScore's `location`) so a row is never bare while icons warm up.
        badge = (icons.icon(team) if icons else "") or team_flag(team) or ""
        label = f"{badge} **{name}**".strip()
        wins = row.get("wins", row.get("total_win", "—"))
        losses = row.get("losses", row.get("total_loss", "—"))
        rank = row.get("rank", i)
        lines.append(f"`{rank:>2}` {label} — {wins}W / {losses}L")
    embed.description = "\n".join(lines) or "No standings available."
    embed.set_footer(text="AuroraBot · Tier 1 only · powered by PandaScore")
    return embed


def analytics_embed(team: dict, recent: list[dict], game_key: str | None) -> discord.Embed:
    name = team.get("name", "Unknown")
    flag = team_flag(team)
    embed = discord.Embed(
        title=f"🔬 Deep Analytics · {flag + ' ' if flag else ''}{name}", color=BRAND
    )
    if team.get("image_url"):
        embed.set_thumbnail(url=team["image_url"])

    wins = losses = 0
    form: list[str] = []
    for m in recent:
        winner_id = (m.get("winner") or {}).get("id")
        if winner_id is None:
            continue
        if winner_id == team.get("id"):
            wins += 1
            form.append("🟩")
        else:
            losses += 1
            form.append("🟥")
    total = wins + losses
    winrate = f"{(wins / total * 100):.0f}%" if total else "—"

    embed.add_field(name="Recent record", value=f"{wins}W – {losses}L", inline=True)
    # Form reads left-to-right newest → oldest (see PandaScoreClient.team_matches).
    embed.add_field(name="Win rate", value=winrate, inline=True)
    embed.add_field(name="Form", value=" ".join(form[:8]) or "—", inline=False)

    players = team.get("players") or []
    if players:
        roster = ", ".join(p.get("name", "?") for p in players[:6])
        embed.add_field(name="Roster", value=roster, inline=False)

    if game_key and game_key in REFERENCE_SITES:
        embed.add_field(
            name="Deep stats",
            value=f"[{REFERENCE_SITES[game_key]}]({REFERENCE_SITES[game_key]})",
            inline=False,
        )
    embed.set_footer(text="AuroraBot · Tier 1 form · powered by PandaScore")
    return embed
