"""The post-match summary card used by `/results`.

A finished match carries far more than a scoreline: PandaScore returns the
per-game breakdown with each map's winner and length, the total duration, and
whether it ended on a forfeit. `/live` and `/upcoming` want the compact form —
there the question is "what's on" — but a result is the whole point of
`/results`, so it gets the detail.

It also reports how the **server's** predictions went and where that leaves the
tournament standings — both only possible because predictions record the guild
they were made in. Those come from the database, never the API.

The caller settles the match *before* rendering, so the standings shown already
include the result being announced.
"""
from __future__ import annotations

import logging

import discord

from .embeds import BRAND
from .matches import opponents
from .regions import event_flag
from .tournaments import parse_dt

log = logging.getLogger("aurorabot.resultcard")

MAX_GAMES_SHOWN = 7        # a Bo5 plus headroom; embed fields cap at 1024
MAX_NAMES = 8              # predictors listed per outcome
LEADERBOARD_SHOWN = 5      # standings rows under a result


def _duration(seconds: int | None) -> str:
    """`3h 2m`, `46m`, `58s` — whichever units actually carry information."""
    if not seconds or seconds < 0:
        return ""
    hours, rest = divmod(int(seconds), 3600)
    minutes = rest // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{int(seconds)}s"


def _scores(match: dict, teams: list[dict]) -> dict[int, int]:
    """Map team id → score, falling back to counting won games.

    ``results`` is normally present on a finished match, but a forfeit or an
    abandoned series can leave it empty; the per-game winners still tell the
    story.
    """
    out = {int(t["id"]): 0 for t in teams}
    rows = match.get("results") or []
    if rows:
        for row in rows:
            tid = row.get("team_id")
            if tid is not None and int(tid) in out:
                out[int(tid)] = int(row.get("score") or 0)
        return out
    for game in match.get("games") or []:
        winner = (game.get("winner") or {}).get("id")
        if winner is not None and int(winner) in out:
            out[int(winner)] += 1
    return out


def _headline(match: dict, teams: list[dict], scores: dict[int, int], icons) -> str:
    a, b = teams[0], teams[1]
    winner_id = match.get("winner_id")
    if match.get("draw"):
        return f"🤝 {a['name']} {scores[int(a['id'])]} – {scores[int(b['id'])]} {b['name']}"

    # Put the winner first: a result reads better as "X beat Y" than in
    # whatever order the fixture happened to list them.
    if winner_id is not None and int(winner_id) == int(b["id"]):
        a, b = b, a
    crest = icons.icon(a)
    lead = f"{crest} " if crest else "🏆 "
    return (
        f"{lead}{a['name']} {scores[int(a['id'])]} – "
        f"{scores[int(b['id'])]} {b['name']}"
    )


async def build_result_card(bot, match: dict, game_key: str, guild_id=None
                            ) -> discord.Embed:
    """A finished match, summarised."""
    teams = opponents(match)
    if len(teams) < 2:
        # Nothing to summarise; the caller shouldn't reach here, but a walkover
        # with one side listed shouldn't raise.
        from .embeds import match_embed
        return match_embed(match, game_key, bot.icons)

    icons = bot.icons
    scores = _scores(match, teams)

    embed = discord.Embed(
        title=_headline(match, teams, scores, icons)[:256], color=BRAND
    )

    flag = event_flag(match)
    league = (match.get("league") or {}).get("name") or ""
    serie = (match.get("serie") or {}).get("full_name") or ""
    stage = (match.get("tournament") or {}).get("name") or ""
    context = " · ".join(x for x in (league, serie, stage) if x)

    header = [f"{flag + ' ' if flag else ''}{context}".strip()]
    ended = parse_dt(match.get("end_at")) or parse_dt(match.get("begin_at"))
    if ended:
        header.append(f"🕒 Ended <t:{int(ended.timestamp())}:R>")

    began = parse_dt(match.get("begin_at"))
    finished = parse_dt(match.get("end_at"))
    if began and finished and finished > began:
        header.append(f"⏱️ {_duration((finished - began).total_seconds())}")

    bo = match.get("number_of_games")
    if bo and int(bo) > 1:
        header.append(f"🎯 Best of {bo}")
    if match.get("forfeit"):
        header.append("⚠️ Won by forfeit")
    embed.description = "\n".join(x for x in header if x)

    # Per-game breakdown — the part a scoreline can't show.
    games = [g for g in (match.get("games") or [])
             if (g.get("status") or "").lower() == "finished"]
    if len(games) > 1:
        by_id = {int(t["id"]): t["name"] for t in teams}
        lines = []
        for game in sorted(games, key=lambda g: g.get("position") or 0)[:MAX_GAMES_SHOWN]:
            winner = (game.get("winner") or {}).get("id")
            name = by_id.get(int(winner), "?") if winner is not None else "—"
            length = _duration(game.get("length"))
            lines.append(
                f"`{game.get('position', '?')}.` **{name}**"
                + (f" · {length}" if length else "")
            )
        embed.add_field(
            name=f"Game by game ({len(games)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    if guild_id:
        await _add_predictions(bot, embed, match, teams, guild_id)

    embed.set_footer(text="Final result · powered by PandaScore")
    return embed


async def _add_predictions(bot, embed, match, teams, guild_id) -> None:
    """How this server called it. Silent when nobody predicted."""
    try:
        picks = await bot.db.match_predictions(int(match["id"]), int(guild_id))
    except Exception:  # noqa: BLE001 - the result matters more than the extra
        log.exception("could not read predictions for match %s", match.get("id"))
        return
    if not picks:
        return

    winner_id = match.get("winner_id")
    if winner_id is None:
        return
    right = [p["display_name"] for p in picks
             if int(p["predicted_team_id"]) == int(winner_id)]
    wrong = [p["display_name"] for p in picks
             if int(p["predicted_team_id"]) != int(winner_id)]

    def listed(names: list[str]) -> str:
        shown = ", ".join(names[:MAX_NAMES])
        if len(names) > MAX_NAMES:
            shown += f" _+{len(names) - MAX_NAMES}_"
        return shown

    parts = []
    if right:
        parts.append(f"✅ {listed(right)}")
    if wrong:
        parts.append(f"❌ {listed(wrong)}")
    embed.add_field(
        name=f"🔮 Called it: {len(right)}/{len(picks)}",
        value="\n".join(parts)[:1024],
        inline=False,
    )
    await _add_leaderboard(bot, embed, match, guild_id)


async def _add_leaderboard(bot, embed, match, guild_id) -> None:
    """The tournament's standings as they are *after* this match.

    Boards are per tournament, so this is the one the result just changed
    rather than a server-wide total. The caller settles the match first, so the
    points here already include it.
    """
    tournament_id = (match.get("tournament") or {}).get("id")
    try:
        board = await bot.db.tournament_leaderboard(
            int(guild_id), int(tournament_id) if tournament_id else None,
            limit=LEADERBOARD_SHOWN,
        )
    except Exception:  # noqa: BLE001 - the result matters more than the extra
        log.exception("could not read the leaderboard for match %s", match.get("id"))
        return
    if not board:
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for n, row in enumerate(board):
        won, lost = int(row["won"] or 0), int(row["lost"] or 0)
        mark = medals[n] if n < len(medals) else f"`{n + 1}.`"
        lines.append(
            f"{mark} <@{row['discord_id']}> — **{int(row['points'])}** pts "
            f"· {won}W/{lost}L"
        )

    name = (match.get("tournament") or {}).get("name") or "Leaderboard"
    league = (match.get("league") or {}).get("name") or ""
    embed.add_field(
        name=f"🏆 {league} {name}".strip()[:256],
        value="\n".join(lines)[:1024],
        inline=False,
    )
