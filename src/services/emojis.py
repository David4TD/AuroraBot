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
import io
import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp
import discord
from PIL import Image, UnidentifiedImageError

if TYPE_CHECKING:  # pragma: no cover
    from ..database.db import Database

log = logging.getLogger("aurorabot.emojis")

# Discord's application-emoji ceiling is 2000; stop short so a burst of new
# teams can never wedge against the hard limit.
MAX_EMOJIS = 1900
EVICT_BATCH = 25

# Discord rejects emoji assets over 256 KiB. Every logo is re-encoded to a
# small PNG first, so the practical output is a few KB and this is only a
# backstop. The *download* cap is generous because the source may be large.
MAX_IMAGE_BYTES = 256 * 1024
MAX_DOWNLOAD_BYTES = 8 * 1024 * 1024

# Emoji render at ~32px; 128 keeps them crisp on hi-dpi without bloating.
EMOJI_EDGE = 128

UPLOADS_PER_WINDOW = 15
WINDOW_SECONDS = 60.0
BACKOFF_SECONDS = 300.0          # after a 429 or repeated failures
QUEUE_LIMIT = 500

# A logo Discord refuses will be refused every time, so stop after this many
# attempts instead of re-queueing it on every render.
MAX_ATTEMPTS = 2

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


def normalise_image(raw: bytes) -> bytes | None:
    """Re-encode any logo into a Discord-safe emoji asset.

    Discord's emoji endpoint answers ``50046 Invalid Asset`` for formats it
    won't take (notably **WebP**, which discord.py's mime sniffer happily
    forwards) and for oversized assets. PandaScore serves a mix, so rather than
    guess, every logo is decoded and re-encoded here as a square RGBA PNG of at
    most :data:`EMOJI_EDGE` px.

    Square-padding rather than stretching keeps wide wordmark logos legible.
    Returns ``None`` if the bytes aren't a decodable image.
    """
    try:
        with Image.open(io.BytesIO(raw)) as img:
            # Animated sources collapse to their first frame: a still PNG is
            # better than an emoji Discord might reject.
            img.seek(0) if getattr(img, "is_animated", False) else None
            img = img.convert("RGBA")

            img.thumbnail((EMOJI_EDGE, EMOJI_EDGE), Image.LANCZOS)

            canvas = Image.new("RGBA", (EMOJI_EDGE, EMOJI_EDGE), (0, 0, 0, 0))
            canvas.paste(
                img,
                ((EMOJI_EDGE - img.width) // 2, (EMOJI_EDGE - img.height) // 2),
            )

            buf = io.BytesIO()
            canvas.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, MemoryError) as exc:
        log.debug("Could not decode logo (%d bytes): %s", len(raw), exc)
        return None

    if len(data) > MAX_IMAGE_BYTES:  # pragma: no cover - 128px PNGs are tiny
        log.debug("Normalised logo still too large: %d bytes", len(data))
        return None
    return data


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
        # team_id → failed attempts. A logo Discord won't take never will, so
        # give up rather than re-queue it on every single render.
        self._attempts: dict[int, int] = {}
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
        if self._attempts.get(team_id, 0) >= MAX_ATTEMPTS:
            return  # known bad logo; stop burning budget and log lines on it
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

    def _note_failure(self, team_id: int, reason: str) -> None:
        """Count a failure, warning once and then falling silent.

        Without this, a logo Discord refuses is re-queued by every render, so
        the same warning repeats forever and eats the upload budget.
        """
        attempts = self._attempts.get(team_id, 0) + 1
        self._attempts[team_id] = attempts
        if attempts >= MAX_ATTEMPTS:
            log.info(
                "Giving up on the icon for team %s after %d attempts (%s); "
                "it will render without one",
                team_id, attempts, reason,
            )
        else:
            log.debug("Icon attempt %d failed for team %s: %s", attempts, team_id, reason)

    async def _mint(self, team_id: int, name: str, image_url: str) -> None:
        existing = await self.db.get_team_emoji(team_id)
        if existing and existing["image_url"] == image_url:
            return  # another path already cached it

        payload = await self._download(image_url)
        if payload is None:
            self._note_failure(team_id, "logo could not be fetched or decoded")
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
                self._note_failure(team_id, str(exc))
            return

        self._attempts.pop(team_id, None)
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
        """Fetch a logo and re-encode it into a Discord-safe PNG.

        The Content-Type is logged but not gatekept: CDNs mislabel assets, and
        :func:`normalise_image` decodes from the bytes anyway, so trusting the
        header would reject perfectly good logos.
        """
        if self._session is None or self._session.closed:
            return None
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    log.debug("Logo fetch %s returned %s", url, resp.status)
                    return None
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    log.debug("Logo %s too large to fetch (%s bytes)", url, declared)
                    return None
                raw = await resp.content.read(MAX_DOWNLOAD_BYTES + 1)
                if len(raw) > MAX_DOWNLOAD_BYTES:
                    log.debug("Logo %s exceeded the download cap", url)
                    return None
                content_type = (resp.headers.get("Content-Type") or "?").split(";")[0]
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.debug("Logo fetch failed for %s: %s", url, exc)
            return None

        data = normalise_image(raw)
        if data is None:
            log.debug("Logo %s (%s) could not be normalised", url, content_type)
        return data

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
