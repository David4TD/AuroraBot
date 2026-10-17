"""Thin async client for the PandaScore eSports API.

Docs: https://developers.pandascore.co

Only the endpoints AuroraBot needs are wrapped. The client is resilient:
network / rate-limit errors are logged and surfaced as ``PandaScoreError`` so
cogs can show a friendly message instead of crashing the interaction.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp

from ..utils.tournaments import parse_dt

log = logging.getLogger("aurorabot.pandascore")

BASE_URL = "https://api.pandascore.co"

# PandaScore's maximum. Worth asking for in full: there is no server-side tier
# filter on the match feeds, so a page is mostly matches the bot will never
# show, and the page size decides how much of the real slate it can even see.
PAGE_SIZE = 100
# A ceiling on a single read, so a game with a pathological feed costs a
# bounded number of requests rather than walking the whole calendar.
MAX_PAGES = 5


class PandaScoreError(Exception):
    """Raised when the API returns a non-success response."""


class PandaScoreClient:
    def __init__(self, api_key: str, cs_slug: str = "csgo") -> None:
        self._api_key = api_key
        self.cs_slug = cs_slug
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(
        self, path: str, params: dict[str, Any] | None = None, *, retries: int = 2
    ) -> list[dict] | dict:
        await self.start()
        assert self._session is not None
        url = f"{BASE_URL}{path}"
        params = {k: v for k, v in (params or {}).items() if v is not None}

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self._session.get(url, params=params) as resp:
                    if resp.status == 429:  # rate limited
                        wait = int(resp.headers.get("Retry-After", "2"))
                        log.warning("Rate limited on %s, waiting %ss", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status >= 400:
                        body = await resp.text()
                        raise PandaScoreError(
                            f"{resp.status} for {path}: {body[:200]}"
                        )
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                log.warning("Request error on %s (attempt %d): %s", path, attempt, exc)
                await asyncio.sleep(1 + attempt)
        raise PandaScoreError(f"Failed to reach PandaScore for {path}: {last_exc}")

    # ── matches ──────────────────────────────────────────────────────────────
    # NOTE: PandaScore has no server-side ``filter[tier]`` on the match
    # endpoints — tier lives on the embedded tournament/serie/league objects.
    # Callers over-fetch here and filter with ``utils.tiers`` afterwards, so
    # these default to a generous page size.
    async def running_matches(
        self, slug: str | None = None, per_page: int = 50, page: int = 1
    ) -> list[dict]:
        path = f"/{slug}/matches/running" if slug else "/matches/running"
        data = await self._get(path, {"per_page": per_page, "page": page})
        return data if isinstance(data, list) else []

    async def upcoming_matches(
        self, slug: str | None = None, per_page: int = 50, page: int = 1
    ) -> list[dict]:
        path = f"/{slug}/matches/upcoming" if slug else "/matches/upcoming"
        data = await self._get(
            path, {"per_page": per_page, "sort": "begin_at", "page": page}
        )
        return data if isinstance(data, list) else []

    async def upcoming_until(
        self, slug: str | None, horizon, *,
        per_page: int = PAGE_SIZE, max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Every upcoming match starting at or before *horizon*, however deep.

        One page of a tier-mixed feed is not the same thing as one page of the
        matches the bot shows. Counter-Strike runs enough lower-tier fixtures
        that the first Tier 1 one sat at index 24 when this was written, so a
        50-row page left half the Tier 1 CS slate invisible to the poll — those
        matches got no reminder and appeared on no digest.

        The feed is ordered by kick-off, so reading on until it passes
        *horizon* cannot miss one. In practice that is a single request: 100
        rows reached more than two days ahead for every game measured. The page
        cap only matters if that ever stops being true.
        """
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            chunk = await self.upcoming_matches(
                slug=slug, per_page=per_page, page=page
            )
            out.extend(chunk)
            if len(chunk) < per_page:
                break                   # the end of the feed
            last = parse_dt(chunk[-1].get("begin_at"))
            if last is None or last > horizon:
                break                   # the feed has run past the window
        return out

    async def running_all(
        self, slug: str | None, *,
        per_page: int = PAGE_SIZE, max_pages: int = MAX_PAGES,
    ) -> list[dict]:
        """Every running match, rather than whichever page one happened on.

        No kick-off order to stop on here, so this reads until a short page.
        Only as many matches are ever live at once as are being played, so one
        request covers it in practice.
        """
        out: list[dict] = []
        for page in range(1, max_pages + 1):
            chunk = await self.running_matches(
                slug=slug, per_page=per_page, page=page
            )
            out.extend(chunk)
            if len(chunk) < per_page:
                break
        return out

    async def past_matches(self, slug: str | None = None, per_page: int = 50) -> list[dict]:
        path = f"/{slug}/matches/past" if slug else "/matches/past"
        data = await self._get(
            path, {"per_page": per_page, "sort": "-end_at"}
        )
        return data if isinstance(data, list) else []

    async def tournament_matches(
        self, tournament_id: int, per_page: int = 50, sort: str = "-begin_at"
    ) -> list[dict]:
        """Every match in one tournament.

        The generic ``/matches/past`` feed can't answer "recent Tier 1 results":
        there is no server-side tier filter, and games like Counter-Strike run
        so many tier C/D matches that a whole page of 100 contains none of the
        events anyone asked about. Asking a known Tier 1 tournament directly
        sidesteps that entirely.
        """
        data = await self._get(
            f"/tournaments/{tournament_id}/matches",
            {"per_page": per_page, "sort": sort},
        )
        return data if isinstance(data, list) else []

    async def get_match(self, match_id: int) -> dict:
        data = await self._get(f"/matches/{match_id}")
        return data if isinstance(data, dict) else {}

    async def team_matches(self, team_id: int, per_page: int = 10) -> list[dict]:
        """A team's matches, most recent first (form reads newest → oldest)."""
        data = await self._get(
            "/matches",
            {"filter[opponent_id]": team_id, "per_page": per_page, "sort": "-begin_at"},
        )
        return data if isinstance(data, list) else []

    # ── teams ────────────────────────────────────────────────────────────────
    async def search_teams(self, name: str, slug: str | None = None, per_page: int = 10) -> list[dict]:
        path = f"/{slug}/teams" if slug else "/teams"
        data = await self._get(
            path, {"search[name]": name, "per_page": per_page}
        )
        return data if isinstance(data, list) else []

    async def get_team(self, team_id: int) -> dict:
        data = await self._get(f"/teams/{team_id}")
        return data if isinstance(data, dict) else {}

    # ── leagues / standings ──────────────────────────────────────────────────
    async def search_leagues(self, name: str, slug: str | None = None, per_page: int = 10) -> list[dict]:
        path = f"/{slug}/leagues" if slug else "/leagues"
        data = await self._get(path, {"search[name]": name, "per_page": per_page})
        return data if isinstance(data, list) else []

    async def league_tournaments(self, league_id: int, per_page: int = 50) -> list[dict]:
        data = await self._get(
            f"/leagues/{league_id}/tournaments",
            {"per_page": per_page, "sort": "-begin_at"},
        )
        return data if isinstance(data, list) else []

    async def tournament_standings(self, tournament_id: int) -> list[dict]:
        data = await self._get(f"/tournaments/{tournament_id}/standings")
        return data if isinstance(data, list) else []

    # ── tournaments ──────────────────────────────────────────────────────────
    async def running_tournaments(
        self, slug: str | None = None, per_page: int = 50
    ) -> list[dict]:
        """Tournaments currently in progress, optionally scoped to one game."""
        path = f"/{slug}/tournaments/running" if slug else "/tournaments/running"
        data = await self._get(path, {"per_page": per_page, "sort": "begin_at"})
        return data if isinstance(data, list) else []

    async def upcoming_tournaments(
        self, slug: str | None = None, per_page: int = 50
    ) -> list[dict]:
        path = f"/{slug}/tournaments/upcoming" if slug else "/tournaments/upcoming"
        data = await self._get(path, {"per_page": per_page, "sort": "begin_at"})
        return data if isinstance(data, list) else []

    async def get_tournament(self, tournament_id: int) -> dict:
        data = await self._get(f"/tournaments/{tournament_id}")
        return data if isinstance(data, dict) else {}
