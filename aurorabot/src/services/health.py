"""Lightweight aiohttp health server.

Exposes ``GET /health`` returning 200 when the bot's websocket is connected and
the last alert loop ran recently, else 503. Docker's HEALTHCHECK curls this.
The status flag is updated by the bot as it runs.
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

log = logging.getLogger("aurorabot.health")


class HealthState:
    def __init__(self) -> None:
        self.ready: bool = False
        self.last_heartbeat: float = time.time()

    def beat(self) -> None:
        self.last_heartbeat = time.time()

    def snapshot(self) -> dict:
        age = time.time() - self.last_heartbeat
        healthy = self.ready and age < 300  # 5 min tolerance
        return {"healthy": healthy, "ready": self.ready, "heartbeat_age_s": round(age, 1)}


class HealthServer:
    def __init__(self, state: HealthState, port: int = 8080) -> None:
        self.state = state
        self.port = port
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.Response:
        snap = self.state.snapshot()
        status = 200 if snap["healthy"] else 503
        return web.json_response(snap, status=status)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._handle)
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        log.info("Health server listening on :%d/health", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
