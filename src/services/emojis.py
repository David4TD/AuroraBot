"""Team logo → application emoji cache.

Discord will only render an image *inline* (inside embed text, a dropdown row,
a button) if it is a custom emoji. So each team's ``image_url`` from PandaScore
is uploaded once as an **application emoji** — owned by the bot, usable in
every server, with a 2000 cap — and referenced by id from then on.

Design constraints that shape this:

* **Rendering must never wait on the network.** :meth:`icon` only ever reads
  the cache; a miss enqueues the team and returns an empty string. A background
  worker mints the emoji, so the *next* render shows it. A busy `/standings`
  therefore fills in over a few seconds rather than stalling the interaction.
* **Uploads are throttled.** Emoji creation is rate limited, and one command
  can reference 25 unknown teams. A rolling budget caps uploads per minute and
  a 429 backs the whole worker off.
* **The cap is finite.** Nearing 2000, the least-recently-used icons are
  deleted to make room, so long-tail teams don't permanently squat.
* **Failure is invisible.** Anything that goes wrong — oversized logo, dead
  URL, revoked permission — leaves the caller rendering plain text, which is
  exactly the pre-icon behaviour.

Requires discord.py >= 2.5 for the application-emoji API.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp
import discord

if TYPE_CHECKING:  # pragma: no cover
    from ..database.db import Database

log = logging.getLogger("aurorabot.emojis")

# Discord's application-emoji ceiling is 2000; stop short so a burst of new
# teams can never wedge against the hard limit.
MAX_EMOJIS = 1900
EVICT_BATCH = 25

# Discord rejects emoji images over 256 KiB. We have no image library to
# downscale with, so oversized logos are simply skipped.
MAX_IMAGE_BYTES = 256 * 1024

UPLOADS_PER_WINDOW = 15
WINDOW_SECONDS = 60.0
BACKOFF_SECONDS = 300.0          # after a 429 or repeated failures
QUEUE_LIMIT = 500

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")
_ALLOWED_CONTENT = ("image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp")


def emoji_name_for(team_id: int, team_name: str | None) -> str:
    """A legal, unique, human-recognisable emoji name.

    Discord allows 2–32 chars of ``[a-z0-9_]``. The id suffix guarantees
    uniqueness; the name prefix just makes the developer-portal list readable.
    """
    stem = _NAME_SAFE.sub("_", (team_name or "team").lower()).strip("_")
    suffix = f"_{team_id}"
    stem = stem[: 32 - len(suffix)] or "team"
    return f"{stem}{suffix}"


class TeamIconStore:
    def __init__(self, bot: discord.Client, db: "Database") -> None:
        self.bot = bot
        self.db = db
        self._cache: dict[int, str] = {}          # team_id → "<:name:id>"
        self._emoji_objects: dict[int, discord.Emoji] = {}   # emoji_id → Emoji
        self._queue: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue(
            maxsize=QUEUE_LIMIT
        )
        self._queued: set[int] = set()
        self._session: aiohttp.ClientSession | None = None
        self._worker: asyncio.Task | None = None
        self._uploads: list[float] = []           # timestamps in the window
        self._blocked_until = 0.0
        self.enabled = True

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Adopt the emojis we already own and reconcile them with the cache."""
        if not hasattr(self.bot, "fetch_application_emojis"):
            self.enabled = False
            log.warning(
                "discord.py is too old for application emojis; "
                "team icons disabled (need >= 2.5)"
            )
            return

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        )
        try:
            existing = await self.bot.fetch_application_emojis()
        except discord.HTTPException as exc:
            self.enabled = False
            log.warning("Could not list application emojis; icons disabled: %s", exc)
            return

        self._emoji_objects = {e.id: e for e in existing}
        by_id = set(self._emoji_objects)

        # Rebuild the in-memory cache, dropping rows whose emoji was deleted
        # out from under us (e.g. cleaned up manually in the dev portal).
        stale = 0
        for row in await self.db.all_team_emojis():
            emoji_id = int(row["emoji_id"])
            if emoji_id in by_id:
                self._cache[int(row["team_id"])] = self._markdown(
                    row["emoji_name"], emoji_id, bool(row["animated"])
                )
            else:
                await self.db.drop_team_emoji(int(row["team_id"]))
                stale += 1

        self._worker = asyncio.create_task(self._run(), name="team-icon-minter")
        log.info(
            "Team icons ready: %d cached, %d owned emojis, %d stale rows cleared",
            len(self._cache), len(existing), stale,
        )

    async def close(self) -> None:
        # Await the cancellation rather than just requesting it: otherwise the
        # worker can still be mid-sleep when the loop shuts down, which races
        # with closing the session it is using.
        if self._worker is not None:
            worker, self._worker = self._worker, None
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self._session and not self._session.closed:
            await self._session.close()

    # ── reading ──────────────────────────────────────────────────────────────
    @staticmethod
    def _markdown(name: str, emoji_id: int, animated: bool = False) -> str:
        return f"<{'a' if animated else ''}:{name}:{emoji_id}>"

    def icon(self, team: dict | None) -> str:
        """Inline markdown for a team's logo, or ``""`` if not cached yet.

        Deliberately synchronous and side-effect-light so embed builders can
        call it freely. A miss schedules a mint for next time.
        """
        if not team or not self.enabled:
            return ""
        team_id = team.get("id")
        if not team_id:
            return ""
        team_id = int(team_id)

        cached = self._cache.get(team_id)
        if cached:
            return cached

        image_url = team.get("image_url")
        if image_url:
            self._enqueue(team_id, str(team.get("name") or "team"), str(image_url))
        return ""

    def partial(self, team: dict | None) -> discord.PartialEmoji | None:
        """The same icon as a :class:`PartialEmoji`, for selects and buttons."""
        markdown = self.icon(team)
        if not markdown:
            return None
        try:
            return discord.PartialEmoji.from_str(markdown)
        except (ValueError, TypeError):
            return None

    def prefix(self, team: dict | None, text: str) -> str:
        """``T1`` → ``<:t1:…> T1``, unchanged when there's no icon."""
        markdown = self.icon(team)
        return f"{markdown} {text}" if markdown else text

    def warm(self, teams: list[dict]) -> None:
        """Queue several teams at once, e.g. a whole standings table."""
        for team in teams:
            self.icon(team)

    # ── minting ──────────────────────────────────────────────────────────────
    def _enqueue(self, team_id: int, name: str, image_url: str) -> None:
        if team_id in self._queued or self._queue.full():
            return
        self._queued.add(team_id)
        try:
            self._queue.put_nowait((team_id, name, image_url))
        except asyncio.QueueFull:  # pragma: no cover - guarded above
            self._queued.discard(team_id)

    def _budget_available(self) -> bool:
        now = time.monotonic()
        if now < self._blocked_until:
            return False
        cutoff = now - WINDOW_SECONDS
        self._uploads = [t for t in self._uploads if t > cutoff]
        return len(self._uploads) < UPLOADS_PER_WINDOW

    async def _run(self) -> None:
        while True:
            try:
                team_id, name, image_url = await self._queue.get()
                try:
                    # Wait out the throttle rather than dropping the request;
                    # nothing is blocked on us, so patience is free.
                    while not self._budget_available():
                        await asyncio.sleep(5)
                    await self._mint(team_id, name, image_url)
                finally:
                    self._queued.discard(team_id)
                    self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the worker must outlive failures
                log.exception("team icon minting failed")

    async def _mint(self, team_id: int, name: str, image_url: str) -> None:
        existing = await self.db.get_team_emoji(team_id)
        if existing and existing["image_url"] == image_url:
            return  # another path already cached it

        payload = await self._download(image_url)
        if payload is None:
            return

        if await self.db.count_team_emojis() >= MAX_EMOJIS:
            await self._evict()

        emoji_name = emoji_name_for(team_id, name)
        try:
            emoji = await self.bot.create_application_emoji(
                name=emoji_name, image=payload
            )
        except discord.HTTPException as exc:
            if getattr(exc, "status", None) == 429:
                self._blocked_until = time.monotonic() + BACKOFF_SECONDS
                log.warning("Emoji rate limit hit; pausing icon minting for %ds",
                            int(BACKOFF_SECONDS))
            else:
                log.warning("Could not create emoji for team %s: %s", team_id, exc)
            return

        self._uploads.append(time.monotonic())
        self._emoji_objects[emoji.id] = emoji

        # Replace a rebranded logo's emoji only once the new one exists.
        if existing:
            await self._delete_emoji(int(existing["emoji_id"]))

        await self.db.put_team_emoji(
            team_id=team_id,
            emoji_id=emoji.id,
            emoji_name=emoji.name,
            image_url=image_url,
            animated=emoji.animated,
        )
        self._cache[team_id] = self._markdown(emoji.name, emoji.id, emoji.animated)
        log.debug("Minted icon for team %s (%s)", team_id, emoji.name)

    async def _download(self, url: str) -> bytes | None:
        if self._session is None or self._session.closed:
            return None
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    log.debug("Logo fetch %s returned %s", url, resp.status)
                    return None
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0]
                if content_type.lower() not in _ALLOWED_CONTENT:
                    log.debug("Logo %s has unsupported type %s", url, content_type)
                    return None
                # Guard on the header when present, then again on the body,
                # since Content-Length is advisory.
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_IMAGE_BYTES:
                    return None
                data = await resp.content.read(MAX_IMAGE_BYTES + 1)
                if len(data) > MAX_IMAGE_BYTES:
                    log.debug("Logo %s exceeds %d bytes", url, MAX_IMAGE_BYTES)
                    return None
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("Logo fetch failed for %s: %s", url, exc)
            return None

    async def _evict(self) -> None:
        """Delete the coldest icons to free room under the cap."""
        for row in await self.db.coldest_team_emojis(EVICT_BATCH):
            await self._delete_emoji(int(row["emoji_id"]))
            await self.db.drop_team_emoji(int(row["team_id"]))
            self._cache.pop(int(row["team_id"]), None)
        log.info("Evicted up to %d least-recently-used team icons", EVICT_BATCH)

    async def _delete_emoji(self, emoji_id: int) -> None:
        emoji = self._emoji_objects.pop(emoji_id, None)
        if emoji is None:
            return
        try:
            await emoji.delete()
        except discord.HTTPException as exc:
            log.debug("Could not delete emoji %s: %s", emoji_id, exc)
