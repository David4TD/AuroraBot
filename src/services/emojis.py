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

# Measured against the logos Discord actually refused: every rejection was
# >= 18.9 KB / >= 106k pixels, while 13.8 KB / 90k pixels went through. The
# documented 256 KiB ceiling is clearly not what's being enforced, so aim well
# under the largest size observed to succeed and shrink until we get there.
TARGET_BYTES = 12 * 1024
FALLBACK_EDGES = (128, 96, 64, 48)

UPLOADS_PER_WINDOW = 15
WINDOW_SECONDS = 60.0
BACKOFF_SECONDS = 300.0          # after a 429 or repeated failures
QUEUE_LIMIT = 500

# A logo Discord refuses will be refused every time, so stop after this many
# attempts instead of re-queueing it on every render.
MAX_ATTEMPTS = 2

_NAME_SAFE = re.compile(r"[^a-z0-9_]+")


def _render(img: "Image.Image", edge: int, quantize: bool) -> bytes:
    """Square-pad and encode at the given edge length.

    Padding rather than stretching keeps wide wordmark logos legible.
    """
    frame = img.copy()
    frame.thumbnail((edge, edge), Image.LANCZOS)

    canvas = Image.new("RGBA", (edge, edge), (0, 0, 0, 0))
    canvas.paste(frame, ((edge - frame.width) // 2, (edge - frame.height) // 2))

    if quantize:
        # Palette PNG with alpha: a big win on flat-colour crests, which is
        # what most team logos are.
        canvas = canvas.quantize(colors=128, method=Image.FASTOCTREE)

    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def normalise_image(raw: bytes) -> bytes | None:
    """Re-encode any logo into a Discord-safe emoji asset.

    Discord answers ``50046 Invalid Asset`` for emoji assets it considers
    oversized — measured empirically, well below the documented 256 KiB — so
    every logo is decoded and re-encoded as a small square RGBA PNG.

    Encoding is attempted at progressively smaller sizes until the result fits
    :data:`TARGET_BYTES`, since a flat crest and a photographic logo of the same
    dimensions compress very differently. Returns ``None`` only if the bytes
    aren't a decodable image at all.
    """
    try:
        with Image.open(io.BytesIO(raw)) as opened:
            # Animated sources collapse to their first frame; a still PNG beats
            # an emoji Discord might reject.
            if getattr(opened, "is_animated", False):
                opened.seek(0)
            img = opened.convert("RGBA")

            best: bytes | None = None
            for edge in FALLBACK_EDGES:
                for quantize in (False, True):
                    data = _render(img, edge, quantize)
                    if best is None or len(data) < len(best):
                        best = data
                    if len(data) <= TARGET_BYTES:
                        return data
    except (UnidentifiedImageError, OSError, ValueError, MemoryError) as exc:
        log.debug("Could not decode logo (%d bytes): %s", len(raw), exc)
        return None

    # Nothing hit the target; ship the smallest we produced if it's at least
    # within Discord's documented ceiling.
    if best is not None and len(best) <= MAX_IMAGE_BYTES:
        log.debug("Logo only compressed to %d bytes (target %d)", len(best), TARGET_BYTES)
        return best
    return None


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
        self._by_name: dict[str, discord.Emoji] = {}         # emoji_name → Emoji
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
        # Emojis live on the application, not in our database, so they outlive
        # a wiped appdata volume. Index by name too, so a missing row can adopt
        # the emoji we already own instead of trying to create a duplicate.
        self._by_name = {e.name: e for e in existing}
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

        emoji_name = emoji_name_for(team_id, name)

        # Adopt an emoji we already own under this name. Happens whenever the
        # database is newer than the application's emoji list — a recreated
        # appdata volume, or a row cleared by reconciliation — and creating it
        # again would fail with 50035 "already exists".
        owned = self._by_name.get(emoji_name)
        if owned is not None and (not existing or int(existing["emoji_id"]) != owned.id):
            await self.db.put_team_emoji(
                team_id=team_id,
                emoji_id=owned.id,
                emoji_name=owned.name,
                image_url=image_url,
                animated=owned.animated,
            )
            self._cache[team_id] = self._markdown(owned.name, owned.id, owned.animated)
            self._attempts.pop(team_id, None)
            log.debug("Adopted existing emoji %s for team %s", owned.name, team_id)
            return

        payload = await self._download(image_url)
        if payload is None:
            self._note_failure(team_id, "logo could not be fetched or decoded")
            return

        if await self.db.count_team_emojis() >= MAX_EMOJIS:
            await self._evict()

        # The logo changed and our own emoji already holds this name. Emoji
        # names must be unique per application and there is no endpoint to
        # replace an emoji's image, so the old one has to be removed *before*
        # the replacement is created — otherwise creation fails with 50035 and
        # a rebranded team can never update.
        if owned is not None:
            log.debug("Replacing emoji %s for team %s (logo changed)",
                      owned.name, team_id)
            await self._delete_emoji(owned.id)

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
        self._by_name[emoji.name] = emoji

        # Clean up a stale emoji the row still pointed at — only if it wasn't
        # the one we just removed to free the name above.
        if existing and (owned is None or int(existing["emoji_id"]) != owned.id):
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

                # Read the whole body by iterating chunks. StreamReader.read(n)
                # reads *up to* n bytes and returns as soon as anything is
                # available, so it silently yields only the first ~16 KB —
                # which produced truncated PNGs that Discord then rejected as
                # "Invalid Asset". Accumulating keeps the cap without
                # truncating.
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.extend(chunk)
                    if len(buf) > MAX_DOWNLOAD_BYTES:
                        log.debug("Logo %s exceeded the download cap", url)
                        return None
                raw = bytes(buf)
                content_type = (resp.headers.get("Content-Type") or "?").split(";")[0]

                if declared and len(raw) != int(declared):
                    log.debug(
                        "Logo %s: got %d bytes but Content-Length said %s",
                        url, len(raw), declared,
                    )
                    return None
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
        self._by_name.pop(emoji.name, None)
        try:
            await emoji.delete()
        except discord.HTTPException as exc:
            log.debug("Could not delete emoji %s: %s", emoji_id, exc)
