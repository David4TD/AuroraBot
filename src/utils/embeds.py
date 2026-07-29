"""Embed builders — keeps Discord presentation logic out of the cogs."""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from .games import REFERENCE_SITES, label_for

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


def match_embed(match: dict, game_key: str | None = None) -> discord.Embed:
    a, b = _opponents(match)
    state = (match.get("status") or "").lower()
    color = {"running": GREEN, "not_started": AMBER, "finished": BRAND}.get(state, BRAND)

    if state == "running":
        title = f"🔴 LIVE · {a} {_score_line(match)} {b}"
    elif state == "finished":
        title = f"✅ {a} {_score_line(match)} {b}"
    else:
        title = f"🕒 {a} vs {b}"

    embed = discord.Embed(title=title, color=color)
    league = (match.get("league") or {}).get("name")
    serie = (match.get("serie") or {}).get("full_name")
    tournament = (match.get("tournament") or {}).get("name")
    context = " · ".join(x for x in [league, serie, tournament] if x)
    if context:
        embed.add_field(name="Event", value=context, inline=False)

    embed.add_field(name="Format", value=match.get("match_type", "—"), inline=True)
    embed.add_field(
        name="Best of", value=str(match.get("number_of_games", "—")), inline=True
    )
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


def standings_embed(tournament_name: str, standings: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title=f"📊 Standings · {tournament_name}", color=BRAND
    )
    lines = []
    for i, row in enumerate(standings[:20], start=1):
        team = (row.get("team") or {}).get("name", "—")
        wins = row.get("wins", row.get("total_win", "—"))
        losses = row.get("losses", row.get("total_loss", "—"))
        rank = row.get("rank", i)
        lines.append(f"`{rank:>2}` **{team}** — {wins}W / {losses}L")
    embed.description = "\n".join(lines) or "No standings available."
    embed.set_footer(text="AuroraBot · powered by PandaScore")
    return embed


def analytics_embed(team: dict, recent: list[dict], game_key: str | None) -> discord.Embed:
    name = team.get("name", "Unknown")
    embed = discord.Embed(title=f"🔬 Deep Analytics · {name}", color=BRAND)
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
    embed.set_footer(text="AuroraBot · powered by PandaScore")
    return embed
