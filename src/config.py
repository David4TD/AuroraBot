"""Centralised configuration, loaded from environment variables.

All settings are read once at import time. In development a local `.env` file
is loaded automatically; in Docker the variables are injected by compose/Unraid.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    # Optional: only present/needed for local development.
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:  # pragma: no cover - dotenv is optional at runtime
    pass


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(
            f"Required environment variable {name!r} is not set. "
            f"Copy .env.example to .env and fill it in."
        )
    return value or ""


def _parse_guild_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    return ids


@dataclass(frozen=True)
class Settings:
    discord_token: str
    pandascore_key: str
    database_path: str
    dev_guild_ids: list[int] = field(default_factory=list)
    alert_poll_seconds: int = 60
    health_port: int = 8080
    log_level: str = "INFO"
    cs_slug: str = "cs-go"

    @property
    def data_dir(self) -> Path:
        return Path(self.database_path).expanduser().resolve().parent


def load_settings() -> Settings:
    return Settings(
        discord_token=_get("DISCORD_TOKEN", required=True),
        pandascore_key=_get("ESPORTS_API_KEY", required=True),
        database_path=_get("DATABASE_PATH", "/app/data/aurorabot.db"),
        dev_guild_ids=_parse_guild_ids(_get("DEV_GUILD_IDS", "")),
        alert_poll_seconds=int(_get("ALERT_POLL_SECONDS", "60") or 60),
        health_port=int(_get("HEALTH_PORT", "8080") or 8080),
        log_level=_get("LOG_LEVEL", "INFO") or "INFO",
        cs_slug=_get("PANDASCORE_CS_SLUG", "cs-go") or "cs-go",
    )
