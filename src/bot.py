"""AuroraBot entrypoint.

Wires together configuration, the SQLite database, the PandaScore client, the
health server and all cogs, then runs the Discord gateway connection.

Run locally with:   python -m src.bot
In Docker the CMD is: python -u -m src.bot
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from .config import Settings, load_settings
from .database.db import Database
from .services.emojis import TeamIconStore
from .services.health import HealthServer, HealthState
from .services.pandascore import PandaScoreClient

COGS = [
    "src.cogs.meta",
    "src.cogs.games",
    "src.cogs.scores",
    "src.cogs.standings",
    "src.cogs.analytics",
    "src.cogs.profiles",
    "src.cogs.predictions",
    "src.cogs.leaderboard",
    "src.cogs.alerts",
]


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s │ %(levelname)-7s │ %(name)s │ %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # discord.py is chatty at INFO for the gateway; keep it at WARNING.
    logging.getLogger("discord").setLevel(logging.WARNING)
    # At DEBUG these two drown out AuroraBot's own lines — aiosqlite echoes
    # every statement (including the full schema script on startup) and
    # aiohttp.access logs each healthcheck poll. Pin them one level up so
    # LOG_LEVEL=DEBUG stays usable for diagnosing the bot itself.
    logging.getLogger("aiosqlite").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


class AuroraBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = False  # slash-command only bot
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.settings = settings
        self.db = Database(settings.database_path)
        self.api = PandaScoreClient(settings.pandascore_key, cs_slug=settings.cs_slug)
        self.health = HealthState()
        self.health_server = HealthServer(self.health, settings.health_port)
        # NB: `icons`, not `emojis` — Client.emojis is already a property.
        self.icons = TeamIconStore(self, self.db)
        self._icons_started = False
        self.log = logging.getLogger("aurorabot")

    async def setup_hook(self) -> None:
        await self.db.connect()
        await self.api.start()
        await self.health_server.start()

        for ext in COGS:
            await self.load_extension(ext)
            self.log.info("Loaded %s", ext)

        # Command sync: instant per-guild in dev, global otherwise.
        #
        # The two scopes are additive in Discord: a command registered both
        # globally and on a guild shows up TWICE in that guild's picker. So
        # whichever scope we're using, the other one has to be emptied.
        if self.settings.dev_guild_ids:
            for gid in self.settings.dev_guild_ids:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                self.log.info("Synced %d commands to dev guild %s", len(synced), gid)

            # Drop any global registrations left over from a previous run that
            # booted without DEV_GUILD_IDS, which would otherwise duplicate
            # every command. Clearing the local tree then syncing pushes an
            # empty global set; the cogs repopulate it on the next startup.
            self.tree.clear_commands(guild=None)
            await self.tree.sync()
            self.log.info("Cleared global commands (dev guild sync is active)")
        else:
            synced = await self.tree.sync()
            self.log.info("Synced %d global commands", len(synced))

    async def on_ready(self) -> None:
        self.health.ready = True
        self.health.beat()

        # Application emojis need application_id, which only arrives with
        # READY — setup_hook runs before that. Guarded so a gateway resume
        # doesn't restart the worker.
        if not self._icons_started:
            self._icons_started = True
            try:
                await self.icons.start()
            except Exception:  # noqa: BLE001 - icons are cosmetic, never fatal
                self.log.exception("Team icon store failed to start; continuing without")

        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="the servers | /help"
            )
        )
        self.log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")

    async def close(self) -> None:
        self.log.info("Shutting down…")
        self.health.ready = False
        try:
            await self.icons.close()
            await self.api.close()
            await self.db.close()
            await self.health_server.stop()
        finally:
            await super().close()


async def _amain() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    bot = AuroraBot(settings)

    # Graceful shutdown on SIGTERM (Docker stop) / SIGINT.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal() -> None:
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    async with bot:
        bot_task = asyncio.create_task(bot.start(settings.discord_token))
        stop_task = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            await bot.close()
        # surface any startup exception
        if bot_task in done:
            bot_task.result()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
