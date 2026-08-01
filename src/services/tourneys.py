"""Shared directory of current tournaments, leagues and their rosters.

Four commands now offer the same cascade — **game → tournament → team**, each
level's options scoped by the one before it — so the lookup lives here rather
than in any one cog. One cache serves them all, which also means the warm-up
paid for `/alerts add` benefits `/live` and `/predict` for free.

Everything an autocomplete needs comes from the tournament feed itself:

* **leagues** are derived from each tournament's ``league`` block, and
* **teams** from its ``teams`` block, which PandaScore includes inline.

So the whole cascade costs the same two API calls per game that the tournament
list already cost — no extra request per level.

Autocomplete must answer within Discord's 3-second deadline, so reads never
block on the network: a miss returns ``None`` ("not ready") and warms in the
background. See :meth:`current`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..utils.games import resolve_slug
from ..utils.matches import league_id as match_league_id
from ..utils.matches import team_ids
from ..utils.matches import tournament_id as match_tournament_id
from ..utils.tiers import filter_for
from ..utils.tournaments import current_tournaments, tournament_label
from .pandascore import PandaScoreError

log = logging.getLogger("aurorabot.tourneys")

# Current tournaments change on the order of days, so the cache can be long-
# lived; a background loop refreshes it well before this expires.
CACHE_SECONDS = 30 * 60
FETCH_SIZE = 50

# Rosters change between splits, not between matches, so they can be held far
# longer than the tournament list.
ROSTER_CACHE_SECONDS = 6 * 60 * 60

# A tournament's match list gains a result every few hours at most, and both
# /results and /upcoming read it.
MATCH_CACHE_SECONDS = 5 * 60

# Canonical position order, so both sides of a lineup card read across.
#
# These are the strings PandaScore actually returns, which are **not** the
# obvious ones: League of Legends uses ``jun`` and ``sup``, and Dota 2 uses the
# numeric positions 1-5 rather than names. Assuming "jungle"/"support" meant
# every LoL jungler and support fell through as unrecognised. Values are
# lower-cased before lookup (Overwatch capitalises), and anything unknown sorts
# last rather than being dropped.
ROLE_ORDER = {
    # League of Legends
    "top": 0, "jun": 1, "jungle": 1, "mid": 2, "adc": 3, "bot": 3,
    "sup": 4, "support": 4,
    # Dota 2 — positions, carry through to hard support
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4,
    # Overwatch
    "tank": 0, "dps": 1, "damage": 1,
}
ROLE_LABEL = {
    "top": "Top", "jun": "Jungle", "jungle": "Jungle", "mid": "Mid",
    "adc": "Bot", "bot": "Bot", "sup": "Support", "support": "Support",
    "1": "Carry", "2": "Mid", "3": "Offlane", "4": "Soft support",
    "5": "Hard support",
    "tank": "Tank", "dps": "DPS", "damage": "DPS",
}

# Values are prefixed so a league id can never be mistaken for a tournament id.
LEAGUE_PREFIX = "L:"
TOURNAMENT_PREFIX = "T:"

# Discord discards an autocomplete response after 3s and shows "loading options
# failed"; give up well short of that and offer a placeholder instead.
#
# The 3 seconds are counted from when *Discord* sent the request, so the budget
# has to cover both network hops as well as our own work. 2.0s left only 1.0s
# for that, and a cold League of Legends fetch takes ~2.9s — so the placeholder
# itself arrived too late to render and the picker failed outright. 1.2s leaves
# 1.8s of headroom, which is plenty for a round trip on a home connection.
AUTOCOMPLETE_BUDGET = 1.2
LOADING_SENTINEL = "__loading__"
MAX_CHOICES = 25


class TournamentDirectory:
    def __init__(self, bot) -> None:
        self.bot = bot
        self._cache: dict[str, tuple[datetime, list[dict]]] = {}
        self._warming: dict[str, asyncio.Task] = {}
        self._rosters: dict[int, tuple[datetime, list[dict]]] = {}
        self._tournament_matches_cache: dict[int, tuple[datetime, list[dict]]] = {}

    # ── fetching ─────────────────────────────────────────────────────────────
    async def refresh(self, game_key: str) -> list[dict]:
        """Fetch and cache a game's current Tier 1 tournaments."""
        slug = resolve_slug(game_key, self.bot.settings.cs_slug)
        try:
            # Concurrent: sequentially these are two full round trips, which
            # alone can blow the autocomplete deadline on a cold cache.
            running, upcoming = await asyncio.gather(
                self.bot.api.running_tournaments(slug=slug, per_page=FETCH_SIZE),
                self.bot.api.upcoming_tournaments(slug=slug, per_page=FETCH_SIZE),
            )
        except PandaScoreError as exc:
            log.warning("tournament lookup failed for %s: %s", game_key, exc)
            cached = self._cache.get(game_key)
            return cached[1] if cached else []

        tournaments = current_tournaments(
            filter_for(self.bot.settings, running + upcoming)
        )
        self._cache[game_key] = (datetime.now(timezone.utc), tournaments)

        # Persist so the next restart doesn't pay for this again. Never fatal:
        # a failed write costs one cold fetch after a restart, nothing more.
        try:
            await self.bot.db.save_tournaments(game_key, tournaments)
        except Exception:  # noqa: BLE001 - the cache is an optimisation
            log.exception("could not persist tournament cache for %s", game_key)
        return tournaments

    async def load_persisted(self) -> int:
        """Seed the in-memory cache from disk. Returns how many games loaded.

        Called once at startup, before anything can ask for a picker. Entries
        are loaded regardless of age: a stale list beats an empty one, and the
        background warm replaces it within seconds.
        """
        try:
            stored = await self.bot.db.load_tournaments()
        except Exception:  # noqa: BLE001 - only ever makes startup slower
            log.exception("could not read the persisted tournament cache")
            return 0
        for game_key, (fetched_at, rows) in stored.items():
            self._cache.setdefault(game_key, (fetched_at, rows))
        return len(stored)

    async def current(
        self, game_key: str, *, wait: float | None = None
    ) -> list[dict] | None:
        """Cached tournaments for a game.

        With *wait*, give up after that many seconds and return ``None`` —
        "not ready", distinct from an empty list meaning "fetched, nothing on".
        The in-flight fetch is shared and shielded, so a burst of keystrokes
        causes one request and a caller giving up doesn't cancel the warm-up.
        """
        cached = self._cache.get(game_key)
        fresh = cached is not None and (
            datetime.now(timezone.utc) - cached[0]
        ).total_seconds() < CACHE_SECONDS
        if fresh:
            return cached[1]

        task = self._warming.get(game_key)
        if task is None or task.done():
            task = asyncio.create_task(self.refresh(game_key))
            self._warming[game_key] = task

        if wait is None:
            return await task

        # Stale but present beats waiting: hand back what we have and let the
        # refresh land in the background. Tournament lists change over days, so
        # a few-hours-old list is still correct enough to pick from — and this
        # is what makes the picker instant after a restart, now that the cache
        # is restored from disk.
        if cached is not None:
            return cached[1]

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=wait)
        except (asyncio.TimeoutError, PandaScoreError):
            return None

    def prewarm(self, game_keys) -> None:
        """Start warming these games without waiting on the result."""
        for key in game_keys:
            task = self._warming.get(key)
            if task is not None and not task.done():
                continue
            cached = self._cache.get(key)
            if cached and (
                datetime.now(timezone.utc) - cached[0]
            ).total_seconds() < CACHE_SECONDS / 2:
                continue        # still comfortably fresh
            self._warming[key] = asyncio.create_task(self.refresh(key))

    def cancel(self) -> None:
        for task in self._warming.values():
            task.cancel()
        self._warming.clear()

    # ── resolution ───────────────────────────────────────────────────────────
    async def resolve(self, value: str | None, game_key: str) -> dict | None:
        """Turn an autocomplete value — or typed text — into a filter target.

        Returns ``{"scope": "league"|"tournament", "id": int, "name": str}``.
        Typed text matches league names before stage names, since the league is
        almost always what someone means.
        """
        if not value:
            return None
        value = value.strip()
        if value == LOADING_SENTINEL:
            return None

        tournaments = await self.current(game_key) or []

        if value.startswith(LEAGUE_PREFIX):
            wanted = value[len(LEAGUE_PREFIX):]
            if not wanted.isdigit():
                return None
            wanted = int(wanted)
            for t in tournaments:
                lg = t.get("league") or {}
                if lg.get("id") and int(lg["id"]) == wanted:
                    return {"scope": "league", "id": wanted,
                            "name": lg.get("name") or "League"}
            return None

        if value.startswith(TOURNAMENT_PREFIX):
            value = value[len(TOURNAMENT_PREFIX):]

        if value.isdigit():
            wanted = int(value)
            for t in tournaments:
                if int(t["id"]) == wanted:
                    return {"scope": "tournament", "id": wanted,
                            "name": tournament_label(t)}
            try:
                fetched = await self.bot.api.get_tournament(wanted)
            except PandaScoreError:
                return None
            if fetched.get("id"):
                return {"scope": "tournament", "id": wanted,
                        "name": tournament_label(fetched)}
            return None

        query = value.lower()
        for t in tournaments:                      # league names take priority
            lg = t.get("league") or {}
            if lg.get("id") and query in str(lg.get("name") or "").lower():
                return {"scope": "league", "id": int(lg["id"]),
                        "name": lg.get("name") or "League"}
        for t in tournaments:
            if query in tournament_label(t).lower():
                return {"scope": "tournament", "id": int(t["id"]),
                        "name": tournament_label(t)}
        return None

    async def resolve_team(
        self, value: str | None, game_key: str, target: dict | None
    ) -> dict | None:
        """Resolve a team value against the rosters in scope."""
        if not value:
            return None
        value = value.strip()
        if value == LOADING_SENTINEL:
            return None

        roster = await self.teams(game_key, target)
        if value.isdigit():
            wanted = int(value)
            for team in roster:
                if team["id"] == wanted:
                    return team
            return {"id": wanted, "name": f"Team {wanted}"}

        query = value.lower()
        for team in roster:
            if team["name"].lower() == query:
                return team
        for team in roster:
            if query in team["name"].lower():
                return team
        return None

    # ── rosters ──────────────────────────────────────────────────────────────
    async def roster(self, team_id: int) -> list[dict]:
        """A team's current players, from ``/teams/{id}``.

        The tournament feed's ``teams`` block carries only id/name/logo, so the
        player list needs its own call — cached, because a lineup card asks for
        two teams and the same teams recur across a tournament's matches.

        PandaScore has no confirmed per-match lineup on this plan
        (``expected_roster`` comes back empty), so this is the team's *current*
        roster, including substitutes. Callers should say so.
        """
        cached = self._rosters.get(team_id)
        if cached and (
            datetime.now(timezone.utc) - cached[0]
        ).total_seconds() < ROSTER_CACHE_SECONDS:
            return cached[1]

        try:
            team = await self.bot.api.get_team(team_id)
        except PandaScoreError as exc:
            log.warning("roster lookup failed for team %s: %s", team_id, exc)
            return cached[1] if cached else []

        players = [
            {
                "id": p.get("id"),
                "name": p.get("name") or "?",
                "role": (p.get("role") or "").lower() or None,
                "nationality": p.get("nationality"),
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),
                "age": p.get("age"),
            }
            for p in (team.get("players") or [])
            if p.get("active", True)
        ]
        players.sort(key=lambda p: (ROLE_ORDER.get(p["role"], 99), p["name"].lower()))
        self._rosters[team_id] = (datetime.now(timezone.utc), players)
        return players

    # ── matches from known Tier 1 tournaments ────────────────────────────────
    async def matches(
        self, game_key: str, target: dict | None = None, *, status: str | None = None
    ) -> list[dict]:
        """Matches belonging to the current Tier 1 tournaments in scope.

        The per-game feeds (``/matches/past`` and friends) have no server-side
        tier filter, so they're filtered client-side over a fixed page — and
        that fails badly for results: Counter-Strike runs enough tier C/D
        matches that a full page of 100 recent finishes contains **none** of the
        Tier 1 ones. Asking each known Tier 1 tournament instead can't miss
        them, because the tournament is the thing that was graded.

        Cached briefly: /results and /upcoming both call this, and a tournament's
        match list changes on the order of hours.
        """
        tournaments = await self.current(game_key) or []
        in_scope = [t for t in tournaments if _in_target(t, target)]
        if not in_scope:
            return []

        results = await asyncio.gather(
            *(self._tournament_matches(int(t["id"])) for t in in_scope),
            return_exceptions=True,
        )
        out: dict[int, dict] = {}
        for chunk in results:
            if isinstance(chunk, BaseException):
                continue          # one tournament failing must not empty the rest
            for match in chunk:
                if status and (match.get("status") or "").lower() != status:
                    continue
                out[int(match["id"])] = match
        return list(out.values())

    async def _tournament_matches(self, tournament_id: int) -> list[dict]:
        cached = self._tournament_matches_cache.get(tournament_id)
        if cached and (
            datetime.now(timezone.utc) - cached[0]
        ).total_seconds() < MATCH_CACHE_SECONDS:
            return cached[1]
        try:
            rows = await self.bot.api.tournament_matches(tournament_id)
        except PandaScoreError as exc:
            log.warning("match lookup failed for tournament %s: %s", tournament_id, exc)
            return cached[1] if cached else []
        self._tournament_matches_cache[tournament_id] = (
            datetime.now(timezone.utc), rows
        )
        return rows

    # ── derived views ────────────────────────────────────────────────────────
    async def teams(self, game_key: str, target: dict | None) -> list[dict]:
        """Distinct teams in scope, from the ``teams`` block on each tournament.

        PandaScore includes rosters inline, so narrowing the team list to the
        chosen tournament costs nothing extra.
        """
        tournaments = await self.current(game_key) or []
        out: dict[int, dict] = {}
        for t in tournaments:
            if not _in_target(t, target):
                continue
            for team in t.get("teams") or []:
                if team.get("id") and team["id"] not in out:
                    out[int(team["id"])] = {
                        "id": int(team["id"]),
                        "name": team.get("name") or "Team",
                        "image_url": team.get("image_url"),
                        "location": team.get("location"),
                    }
        return sorted(out.values(), key=lambda t: t["name"].lower())


def _in_target(tournament: dict, target: dict | None) -> bool:
    if target is None:
        return True
    if target["scope"] == "league":
        lg = tournament.get("league") or {}
        return bool(lg.get("id")) and int(lg["id"]) == target["id"]
    return int(tournament.get("id", 0)) == target["id"]


def match_in_target(match: dict, target: dict | None) -> bool:
    """Does a match belong to the chosen league or tournament?"""
    if target is None:
        return True
    if target["scope"] == "league":
        return match_league_id(match) == target["id"]
    return match_tournament_id(match) == target["id"]


def match_has_team(match: dict, team: dict | None) -> bool:
    if team is None:
        return True
    return team["id"] in team_ids(match)
