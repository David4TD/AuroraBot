"""The `/tournament` overview card.

One embed answering "what is this event, and what's on next": the dates, tier,
region, prize pool and field, followed by the soonest few matches.

Progress is derived rather than reported — PandaScore has no "matches played"
field, so it's counted from the tournament's own match list, which is the same
cached call the schedule uses.
"""
from __future__ import annotations

import logging

import discord

from ..services.tourneys import TOURNAMENT_PREFIX
from .embeds import BRAND
from .matches import opponents
from .regions import event_flag, region_flag
from .tiers import tier_label
from .tournaments import parse_dt, tournament_label

log = logging.getLogger("aurorabot.tournamentcard")

UPCOMING_SHOWN = 5      # what the command promises
MAX_TEAMS = 12          # ESEA fields 65; a wall of names helps nobody


def _teams_line(tournament: dict, icons) -> str:
    teams = [t for t in (tournament.get("teams") or []) if t.get("name")]
    if not teams:
        return "_Not announced_"
    names = []
    for team in teams[:MAX_TEAMS]:
        crest = icons.icon(team)
        names.append(f"{crest} {team['name']}".strip())
    line = " · ".join(names)
    if len(teams) > MAX_TEAMS:
        line += f" _+{len(teams) - MAX_TEAMS} more_"
    return line[:1024]


def _window(tournament: dict) -> str:
    begin = parse_dt(tournament.get("begin_at"))
    end = parse_dt(tournament.get("end_at"))
    if begin and end:
        return f"<t:{int(begin.timestamp())}:D> → <t:{int(end.timestamp())}:D>"
    if begin:
        return f"from <t:{int(begin.timestamp())}:D>"
    if end:
        return f"until <t:{int(end.timestamp())}:D>"
    return "Dates unannounced"


def _match_line(match: dict, icons) -> str:
    teams = opponents(match)
    starts = parse_dt(match.get("begin_at"))
    when = f"<t:{int(starts.timestamp())}:f>" if starts else "TBD"
    if len(teams) >= 2:
        a, b = teams[0], teams[1]
        # Crests are empty until the emoji cache warms, so build each side
        # separately rather than leaving a gap where the icon will go.
        left = f"{icons.icon(a)} **{a['name']}**".strip()
        right = f"{icons.icon(b)} **{b['name']}**".strip()
        matchup = f"{left} vs {right}"
    else:
        # A bracket slot whose feeder hasn't finished; the name usually carries
        # the round, which is more use than an empty matchup.
        matchup = f"**{match.get('name') or 'TBD'}**"
    bo = match.get("number_of_games")
    suffix = f" · Bo{bo}" if bo and int(bo) > 1 else ""
    return f"{when} · {matchup}{suffix}"


async def build_tournament_card(bot, tournament: dict, game_key: str) -> discord.Embed:
    """Overview of one tournament, plus its next few matches."""
    league = (tournament.get("league") or {}).get("name") or ""
    flag = event_flag(tournament) or region_flag(league) or ""
    title = f"🏆 {flag} {tournament_label(tournament)}".replace("  ", " ")

    embed = discord.Embed(title=title[:256], color=BRAND)

    header = [f"📅 {_window(tournament)}"]
    tier = tier_label(tournament)
    if tier:
        header.append(f"🏅 {tier}")
    where = tournament.get("country") or tournament.get("region")
    if where:
        header.append(f"📍 {where}")
    prize = tournament.get("prizepool")
    if prize:
        header.append(f"💰 {prize}")
    embed.description = " · ".join(header)

    # One cached call, shared with /results and the schedule.
    try:
        matches = await bot.tourneys.matches(
            game_key,
            {"scope": "tournament", "id": int(tournament["id"]),
             "name": tournament_label(tournament)},
        )
    except Exception:  # noqa: BLE001 - the overview is still worth showing
        log.exception("could not load matches for tournament %s", tournament.get("id"))
        matches = []

    played = [m for m in matches if (m.get("status") or "").lower() == "finished"]
    live = [m for m in matches if (m.get("status") or "").lower() == "running"]
    upcoming = sorted(
        (m for m in matches if (m.get("status") or "").lower() == "not_started"),
        key=lambda m: m.get("begin_at") or "",
    )

    if matches:
        embed.add_field(
            name="Progress",
            value=f"**{len(played)}** of **{len(matches)}** matches played"
            + (f" · **{len(live)}** live now" if live else ""),
            inline=True,
        )
    teams = tournament.get("teams") or []
    if teams:
        embed.add_field(name="Teams", value=f"**{len(teams)}**", inline=True)

    icons = bot.icons
    if live:
        embed.add_field(
            name="🔴 Live now",
            value="\n".join(_match_line(m, icons) for m in live[:3])[:1024],
            inline=False,
        )

    # Always say something about the schedule. An event announced weeks out has
    # no matches yet, and silently omitting the field reads as a broken card
    # rather than "the bracket isn't published".
    if upcoming:
        embed.add_field(
            name=f"⏭️ Next {min(len(upcoming), UPCOMING_SHOWN)} matches",
            value="\n".join(
                _match_line(m, icons) for m in upcoming[:UPCOMING_SHOWN]
            )[:1024],
            inline=False,
        )
    elif matches:
        embed.add_field(
            name="⏭️ Next matches",
            value="_Nothing left to play — this stage looks finished._",
            inline=False,
        )
    else:
        started = parse_dt(tournament.get("begin_at"))
        embed.add_field(
            name="⏭️ Next matches",
            value=(
                f"_Not published yet — the event starts "
                f"<t:{int(started.timestamp())}:R>._"
                if started
                else "_No schedule published yet._"
            ),
            inline=False,
        )

    if teams:
        embed.add_field(name="Field", value=_teams_line(tournament, icons),
                        inline=False)

    embed.set_footer(
        text=f"/alerts add tournament:{TOURNAMENT_PREFIX}{tournament.get('id')} "
        f"to follow this event"
    )
    url = (tournament.get("league") or {}).get("url")
    if url:
        embed.url = url
    logo = (tournament.get("league") or {}).get("image_url")
    if logo:
        embed.set_thumbnail(url=logo)
    return embed
