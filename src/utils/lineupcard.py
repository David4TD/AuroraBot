"""The pre-match lineup card, shared by `/lineup` and `/upcoming`.

Lives here rather than on a cog so both can render an identical card without
either reaching into the other — a cross-cog reach is what silently broke
`/games` when a method moved.

**What this is not.** PandaScore has no starting-lineup field and marks every
rostered player ``active: true``, so the card shows each team's *current* roster
and does not claim who starts. A squad listing six or seven players shows all of
them; the footer says as much rather than implying certainty the data lacks.

Player photos exist in the API but can't appear here: Discord renders images
inline only as custom emoji, and minting one per player would burn the
2000-emoji budget team logos use. Nationality flags carry the same information
for a fraction of the cost.
"""
from __future__ import annotations

import asyncio

import discord

from ..services.tourneys import ROLE_LABEL, ROLE_ORDER
from .embeds import BRAND
from .matches import opponents
from .regions import event_flag, flag_for_country
from .tournaments import parse_dt

# A five-player squad plus a couple of extras. Embed fields cap at 1024
# characters, and a very long list stops the two columns reading across.
MAX_PLAYERS = 7


def _line(player: dict) -> str:
    flag = flag_for_country(player.get("nationality")) or "🏳️"
    role = ROLE_LABEL.get((player.get("role") or "").lower(), "")
    suffix = f" · {role}" if role else ""
    return f"{flag} **{player.get('name') or '?'}**{suffix}"


def roster_lines(players: list[dict]) -> str:
    """One line per player: flag, handle, position.

    Ordered by position so the two columns read across — Top opposite Top,
    Support opposite Support.

    **No starter/substitute split.** An earlier version put the first player in
    each lane up top and the rest under a "Bench" heading, which looked
    authoritative but was invented: PandaScore marks every rostered player
    ``active: true`` and offers no starting-lineup field, so a team listing two
    top laners genuinely doesn't say which one plays. Both are simply listed,
    and a squad larger than five is visible as extra rows rather than being
    silently reclassified.
    """
    if not players:
        return "_Roster not listed_"

    ordered = sorted(
        players,
        key=lambda p: (
            ROLE_ORDER.get((p.get("role") or "").lower(), 99),
            (p.get("name") or "").lower(),
        ),
    )
    lines = [_line(p) for p in ordered[:MAX_PLAYERS]]
    if len(ordered) > MAX_PLAYERS:
        lines.append(f"_+{len(ordered) - MAX_PLAYERS} more_")
    return "\n".join(lines)


async def build_card(bot, match: dict, game_key: str) -> discord.Embed:
    """Render both teams' rosters as two aligned columns."""
    teams = opponents(match)
    a, b = teams[0], teams[1]

    # Concurrent, and cached for six hours — the same teams recur across a
    # tournament, so a busy split settles into no calls at all. Fetching in
    # parallel matters for /upcoming, which builds several cards at once.
    roster_a, roster_b = await asyncio.gather(
        bot.tourneys.roster(a["id"]), bot.tourneys.roster(b["id"])
    )

    icons = bot.icons
    flag = event_flag(match)
    league = (match.get("league") or {}).get("name") or ""
    serie = (match.get("serie") or {}).get("full_name") or ""
    stage = (match.get("tournament") or {}).get("name") or ""
    context = " · ".join(x for x in (league, serie, stage) if x)

    embed = discord.Embed(title=f"🆚 {a['name']} vs {b['name']}"[:256], color=BRAND)

    starts = parse_dt(match.get("begin_at"))
    when = (
        f"<t:{int(starts.timestamp())}:F> (<t:{int(starts.timestamp())}:R>)"
        if starts
        else "TBD"
    )
    header = [f"{flag + ' ' if flag else ''}{context}", f"🕒 {when}"]

    bo = match.get("number_of_games")
    if bo:
        header.append(f"🎯 Best of {bo}")
    patch = (match.get("videogame_version") or {}).get("name")
    if patch:
        header.append(f"🔧 Patch {patch}")
    embed.description = "\n".join(header)

    embed.add_field(
        name=f"{icons.icon(a)} {a['name']}".strip()[:256],
        value=roster_lines(roster_a),
        inline=True,
    )
    embed.add_field(
        name=f"{icons.icon(b)} {b['name']}".strip()[:256],
        value=roster_lines(roster_b),
        inline=True,
    )

    streams = [s for s in (match.get("streams_list") or []) if s.get("raw_url")]
    if streams:
        # Official streams first, then whatever else is listed.
        streams.sort(key=lambda s: (not s.get("official"), not s.get("main")))
        links = ", ".join(
            f"[{(s.get('language') or 'watch').upper()}]({s['raw_url']})"
            for s in streams[:4]
        )
        embed.add_field(name="Streams", value=links, inline=False)

    embed.set_footer(
        text="Current rosters — PandaScore doesn't publish starting lineups, "
        "so a squad may list more players than start."
    )
    return embed
