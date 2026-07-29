"""Async SQLite data-access layer built on aiosqlite.

A single ``Database`` instance is shared across all cogs (attached to the bot).
Every public method opens no long-lived cursor state beyond the shared
connection, and all writes are committed immediately for durability on the
Unraid appdata volume.
"""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

log = logging.getLogger("aurorabot.db")

SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        await self._run_schema()
        log.info("SQLite ready at %s", self.path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _run_schema(self) -> None:
        assert self._conn is not None
        script = SCHEMA_FILE.read_text(encoding="utf-8")
        await self._conn.executescript(script)
        await self._conn.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited yet")
        return self._conn

    # ── users ────────────────────────────────────────────────────────────────
    async def ensure_user(self, discord_id: int, display_name: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (discord_id, display_name)
            VALUES (?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at   = datetime('now')
            """,
            (str(discord_id), display_name),
        )
        await self.conn.commit()

    async def get_user(self, discord_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE discord_id = ?", (str(discord_id),)
        )
        return await cur.fetchone()

    async def set_favorite_game(self, discord_id: int, game: str) -> None:
        await self.conn.execute(
            "UPDATE users SET favorite_game = ?, updated_at = datetime('now') "
            "WHERE discord_id = ?",
            (game, str(discord_id)),
        )
        await self.conn.commit()

    async def add_points(self, discord_id: int, delta: int) -> None:
        await self.conn.execute(
            "UPDATE users SET points = points + ?, updated_at = datetime('now') "
            "WHERE discord_id = ?",
            (delta, str(discord_id)),
        )
        await self.conn.commit()

    # ── followed teams ───────────────────────────────────────────────────────
    async def follow_team(
        self, discord_id: int, team_id: int, team_name: str, game: str | None
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO followed_teams (discord_id, team_id, team_name, game)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(discord_id, team_id) DO UPDATE SET
                team_name = excluded.team_name, game = excluded.game
            """,
            (str(discord_id), team_id, team_name, game),
        )
        await self.conn.commit()

    async def unfollow_team(self, discord_id: int, team_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM followed_teams WHERE discord_id = ? AND team_id = ?",
            (str(discord_id), team_id),
        )
        await self.conn.commit()
        return cur.rowcount

    async def list_followed_teams(self, discord_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM followed_teams WHERE discord_id = ? ORDER BY team_name",
            (str(discord_id),),
        )
        return list(await cur.fetchall())

    # ── alert subscriptions ──────────────────────────────────────────────────
    async def add_subscription(
        self,
        guild_id: int,
        channel_id: int,
        game: str,
        team_id: int | None,
        team_name: str | None,
        created_by: int,
    ) -> bool:
        try:
            await self.conn.execute(
                """
                INSERT INTO alert_subscriptions
                    (guild_id, channel_id, team_id, team_name, game, created_by)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(channel_id),
                    team_id,
                    team_name,
                    game,
                    str(created_by),
                ),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # duplicate subscription

    async def remove_subscription(self, sub_id: int, guild_id: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM alert_subscriptions WHERE id = ? AND guild_id = ?",
            (sub_id, str(guild_id)),
        )
        await self.conn.commit()
        return cur.rowcount

    async def list_subscriptions(self, guild_id: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM alert_subscriptions WHERE guild_id = ? ORDER BY game, team_name",
            (str(guild_id),),
        )
        return list(await cur.fetchall())

    async def all_subscriptions(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM alert_subscriptions")
        return list(await cur.fetchall())

    async def was_alerted(self, match_id: int, state: str, channel_id: int) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM alerted_matches WHERE match_id = ? AND state = ? AND channel_id = ?",
            (match_id, state, str(channel_id)),
        )
        return (await cur.fetchone()) is not None

    async def mark_alerted(self, match_id: int, state: str, channel_id: int) -> None:
        await self.conn.execute(
            "INSERT OR IGNORE INTO alerted_matches (match_id, state, channel_id) "
            "VALUES (?, ?, ?)",
            (match_id, state, str(channel_id)),
        )
        await self.conn.commit()

    # ── predictions ──────────────────────────────────────────────────────────
    async def create_prediction(
        self,
        discord_id: int,
        match_id: int,
        game: str | None,
        predicted_team_id: int,
        predicted_team_name: str,
        opponent_team_name: str | None,
        match_starts_at: str | None,
        stake: int,
    ) -> bool:
        try:
            await self.conn.execute(
                """
                INSERT INTO predictions
                    (discord_id, match_id, game, predicted_team_id,
                     predicted_team_name, opponent_team_name, match_starts_at, stake)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(discord_id),
                    match_id,
                    game,
                    predicted_team_id,
                    predicted_team_name,
                    opponent_team_name,
                    match_starts_at,
                    stake,
                ),
            )
            await self.conn.execute(
                "UPDATE users SET predictions_total = predictions_total + 1 "
                "WHERE discord_id = ?",
                (str(discord_id),),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # already predicted this match

    async def get_open_predictions(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE status = 'open'"
        )
        return list(await cur.fetchall())

    async def resolve_prediction(
        self, prediction_id: int, status: str, points_delta: int, discord_id: str
    ) -> None:
        await self.conn.execute(
            "UPDATE predictions SET status = ?, resolved_at = datetime('now') "
            "WHERE id = ?",
            (status, prediction_id),
        )
        if status == "won":
            await self.conn.execute(
                "UPDATE users SET points = points + ?, predictions_won = predictions_won + 1 "
                "WHERE discord_id = ?",
                (points_delta, discord_id),
            )
        await self.conn.commit()

    async def list_user_predictions(
        self, discord_id: int, limit: int = 10
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE discord_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (str(discord_id), limit),
        )
        return list(await cur.fetchall())

    # ── leaderboard ──────────────────────────────────────────────────────────
    async def leaderboard(self, limit: int = 10) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM users ORDER BY points DESC, predictions_won DESC LIMIT ?",
            (limit,),
        )
        return list(await cur.fetchall())
