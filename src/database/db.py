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
        # Migrations first: schema.sql assumes the current column set (it
        # indexes tournament_id, which pre-1.1 databases don't have yet).
        await self._migrate()
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

    async def _table_columns(self, table: str) -> set[str]:
        cur = await self.conn.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in await cur.fetchall()}

    async def _migrate(self) -> None:
        """Bring an existing database up to the current column set.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so new
        columns are added here before schema.sql runs. Each step is guarded by
        a column check, making the whole thing idempotent.
        """
        assert self._conn is not None
        columns = await self._table_columns("alert_subscriptions")
        if not columns:
            return  # fresh database; schema.sql builds it correctly

        # Step 1: team-only table → tournament-aware.
        if "tournament_id" not in columns:
            log.info("Migrating alert_subscriptions to tournament-aware schema…")
            for ddl in (
                "ALTER TABLE alert_subscriptions ADD COLUMN scope TEXT NOT NULL DEFAULT 'game'",
                "ALTER TABLE alert_subscriptions ADD COLUMN tournament_id INTEGER",
                "ALTER TABLE alert_subscriptions ADD COLUMN tournament_name TEXT",
            ):
                await self._conn.execute(ddl)
            # Existing rows are team subs if they named a team, else game-wide.
            await self._conn.execute(
                "UPDATE alert_subscriptions SET scope = 'team' WHERE team_id IS NOT NULL"
            )
            await self._conn.commit()
            # The legacy UNIQUE(channel_id, team_id, game) constraint stays
            # behind on the old table definition; it's strictly weaker than
            # idx_alertsub_unique, so it costs nothing to leave in place.

        # Step 2: add league scope, for subscribing to a whole league whose
        # stages are separate tournaments (LCK's Legend/Rise groups).
        if "league_id" not in columns:
            log.info("Migrating alert_subscriptions to league-aware schema…")
            for ddl in (
                "ALTER TABLE alert_subscriptions ADD COLUMN league_id INTEGER",
                "ALTER TABLE alert_subscriptions ADD COLUMN league_name TEXT",
            ):
                await self._conn.execute(ddl)
            # The uniqueness index has to widen to include league_id. CREATE
            # ... IF NOT EXISTS in schema.sql would silently keep the old
            # narrower definition, so drop it and let schema.sql rebuild it.
            await self._conn.execute("DROP INDEX IF EXISTS idx_alertsub_unique")
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

    async def favorite_game(self, discord_id: int) -> str | None:
        """A user's default game for the browse commands, if they set one."""
        cur = await self.conn.execute(
            "SELECT favorite_game FROM users WHERE discord_id = ?", (str(discord_id),)
        )
        row = await cur.fetchone()
        return row["favorite_game"] if row else None

    async def set_favorite_game(self, discord_id: int, game: str | None) -> None:
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

    # ── team logo emoji cache ────────────────────────────────────────────────
    async def all_team_emojis(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute("SELECT * FROM team_emojis")
        return list(await cur.fetchall())

    async def get_team_emoji(self, team_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM team_emojis WHERE team_id = ?", (team_id,)
        )
        return await cur.fetchone()

    async def put_team_emoji(
        self,
        team_id: int,
        emoji_id: int,
        emoji_name: str,
        image_url: str | None,
        animated: bool = False,
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO team_emojis
                (team_id, emoji_id, emoji_name, image_url, animated, last_used_at)
            VALUES (?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(team_id) DO UPDATE SET
                emoji_id     = excluded.emoji_id,
                emoji_name   = excluded.emoji_name,
                image_url    = excluded.image_url,
                animated     = excluded.animated,
                last_used_at = datetime('now')
            """,
            (team_id, str(emoji_id), emoji_name, image_url, int(animated)),
        )
        await self.conn.commit()

    async def touch_team_emoji(self, team_id: int) -> None:
        """Mark an icon as recently used, so eviction takes the coldest first."""
        await self.conn.execute(
            "UPDATE team_emojis SET last_used_at = datetime('now') WHERE team_id = ?",
            (team_id,),
        )
        await self.conn.commit()

    async def drop_team_emoji(self, team_id: int) -> None:
        await self.conn.execute("DELETE FROM team_emojis WHERE team_id = ?", (team_id,))
        await self.conn.commit()

    async def count_team_emojis(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM team_emojis")
        row = await cur.fetchone()
        return int(row["n"]) if row else 0

    async def coldest_team_emojis(self, limit: int) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM team_emojis ORDER BY last_used_at ASC LIMIT ?", (limit,)
        )
        return list(await cur.fetchall())

    # ── per-guild game toggles ───────────────────────────────────────────────
    async def disabled_games(self, guild_id: int | None) -> set[str]:
        """Games this server has switched off. Empty for DMs / untouched guilds."""
        if guild_id is None:
            return set()
        cur = await self.conn.execute(
            "SELECT game FROM guild_games WHERE guild_id = ? AND enabled = 0",
            (str(guild_id),),
        )
        return {row["game"] for row in await cur.fetchall()}

    async def all_disabled_games(self) -> dict[str, set[str]]:
        """``{guild_id: {disabled games}}`` — one query for the alert poll."""
        cur = await self.conn.execute(
            "SELECT guild_id, game FROM guild_games WHERE enabled = 0"
        )
        out: dict[str, set[str]] = {}
        for row in await cur.fetchall():
            out.setdefault(row["guild_id"], set()).add(row["game"])
        return out

    async def set_games(
        self, guild_id: int, enabled: set[str], known: set[str], updated_by: int
    ) -> None:
        """Replace a guild's toggles in one shot from the /games panel.

        *known* is the full game catalogue; anything in it that isn't in
        *enabled* is written as disabled. Games outside *known* are left alone
        so an unrecognised row can't be clobbered by a stale panel.
        """
        rows = [
            (str(guild_id), game, 1 if game in enabled else 0, str(updated_by))
            for game in known
        ]
        await self.conn.executemany(
            """
            INSERT INTO guild_games (guild_id, game, enabled, updated_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, game) DO UPDATE SET
                enabled    = excluded.enabled,
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,
            rows,
        )
        await self.conn.commit()

    async def reset_games(self, guild_id: int) -> int:
        """Clear all toggles, returning the guild to "every game enabled"."""
        cur = await self.conn.execute(
            "DELETE FROM guild_games WHERE guild_id = ?", (str(guild_id),)
        )
        await self.conn.commit()
        return cur.rowcount

    # ── alert subscriptions ──────────────────────────────────────────────────
    async def add_subscription(
        self,
        guild_id: int,
        channel_id: int,
        game: str,
        created_by: int,
        scope: str = "game",
        team_id: int | None = None,
        team_name: str | None = None,
        league_id: int | None = None,
        league_name: str | None = None,
        tournament_id: int | None = None,
        tournament_name: str | None = None,
    ) -> bool:
        """Insert a subscription. Returns False if this channel already has it."""
        try:
            await self.conn.execute(
                """
                INSERT INTO alert_subscriptions
                    (guild_id, channel_id, game, scope, team_id, team_name,
                     league_id, league_name, tournament_id, tournament_name,
                     created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id),
                    str(channel_id),
                    game,
                    scope,
                    team_id,
                    team_name,
                    league_id,
                    league_name,
                    tournament_id,
                    tournament_name,
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
            "SELECT * FROM alert_subscriptions WHERE guild_id = ? "
            "ORDER BY game, scope, team_name, league_name, tournament_name",
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

    # ── alert messages (reaction predictions) ────────────────────────────────
    async def record_alert_message(
        self,
        message_id: int,
        channel_id: int,
        guild_id: int | None,
        match_id: int,
        game: str | None,
        team_a: tuple[int, str],
        team_b: tuple[int, str],
        begin_at: str | None,
    ) -> None:
        """Remember which match an alert message announced, and its two teams.

        Reactions arrive as raw gateway events with only IDs, and may land
        after a restart, so the mapping has to live in the database rather than
        in an in-memory view.
        """
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO alert_messages
                (message_id, channel_id, guild_id, match_id, game,
                 team_a_id, team_a_name, team_b_id, team_b_name, begin_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(message_id),
                str(channel_id),
                str(guild_id) if guild_id else None,
                match_id,
                game,
                team_a[0],
                team_a[1],
                team_b[0],
                team_b[1],
                begin_at,
            ),
        )
        await self.conn.commit()

    async def get_alert_message(self, message_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM alert_messages WHERE message_id = ?", (str(message_id),)
        )
        return await cur.fetchone()

    async def prune_alert_history(self, days: int = 3) -> int:
        """Drop alert bookkeeping older than *days*; both tables grow forever
        otherwise, and neither is useful once a match is long finished."""
        cur = await self.conn.execute(
            "DELETE FROM alerted_matches WHERE alerted_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        removed = cur.rowcount
        cur = await self.conn.execute(
            "DELETE FROM alert_messages WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        removed += cur.rowcount
        await self.conn.commit()
        return removed

    # ── daily schedule digests ───────────────────────────────────────────────
    async def digest_for(
        self, channel_id: int, subscription_id: int, local_date: str
    ) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM match_digests "
            "WHERE channel_id = ? AND subscription_id = ? AND local_date = ?",
            (str(channel_id), subscription_id, local_date),
        )
        return await cur.fetchone()

    async def create_digest(
        self,
        guild_id: int | None,
        channel_id: int,
        subscription_id: int,
        tournament_id: int | None,
        tournament_name: str | None,
        game: str | None,
        local_date: str,
    ) -> int | None:
        """Claim today's digest slot. ``None`` means another pass already has it.

        Claiming *before* posting means a crash between claim and post costs one
        missed digest rather than risking a duplicate every five minutes.
        """
        try:
            cur = await self.conn.execute(
                """
                INSERT INTO match_digests
                    (guild_id, channel_id, subscription_id, tournament_id,
                     tournament_name, game, local_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(guild_id) if guild_id else None,
                    str(channel_id),
                    subscription_id,
                    tournament_id,
                    tournament_name,
                    game,
                    local_date,
                ),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def set_digest_message(self, digest_id: int, message_id: int) -> None:
        await self.conn.execute(
            "UPDATE match_digests SET message_id = ? WHERE id = ?",
            (str(message_id), digest_id),
        )
        await self.conn.commit()

    async def add_digest_matches(self, digest_id: int, matches: list[dict]) -> None:
        await self.conn.executemany(
            """
            INSERT OR REPLACE INTO digest_matches
                (digest_id, match_id, begin_at,
                 team_a_id, team_a_name, team_b_id, team_b_name)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    digest_id,
                    m["match_id"],
                    m.get("begin_at"),
                    m["team_a"][0],
                    m["team_a"][1],
                    m["team_b"][0],
                    m["team_b"][1],
                )
                for m in matches
            ],
        )
        await self.conn.commit()

    async def digest_match(self, digest_id: int, match_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM digest_matches WHERE digest_id = ? AND match_id = ?",
            (digest_id, match_id),
        )
        return await cur.fetchone()

    async def digest_meta(self, digest_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM match_digests WHERE id = ?", (digest_id,)
        )
        return await cur.fetchone()

    async def prune_digests(self, days: int = 14) -> int:
        cur = await self.conn.execute(
            "DELETE FROM match_digests WHERE created_at < datetime('now', ?)",
            (f"-{days} days",),
        )
        removed = cur.rowcount
        # digest_matches has ON DELETE CASCADE, but only fires with the pragma
        # on — clear orphans explicitly so pruning is reliable either way.
        await self.conn.execute(
            "DELETE FROM digest_matches WHERE digest_id NOT IN "
            "(SELECT id FROM match_digests)"
        )
        await self.conn.commit()
        return removed

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

    async def get_prediction(self, discord_id: int, match_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE discord_id = ? AND match_id = ?",
            (str(discord_id), match_id),
        )
        return await cur.fetchone()

    async def update_prediction_team(
        self,
        prediction_id: int,
        predicted_team_id: int,
        predicted_team_name: str,
        opponent_team_name: str | None,
    ) -> int:
        """Switch an *open* prediction to the other team.

        Lets someone change their mind by swapping their reaction before the
        match starts. Resolved predictions are untouched, so this can't be used
        to rewrite history after a result lands.
        """
        cur = await self.conn.execute(
            """
            UPDATE predictions
               SET predicted_team_id = ?, predicted_team_name = ?, opponent_team_name = ?
             WHERE id = ? AND status = 'open'
            """,
            (predicted_team_id, predicted_team_name, opponent_team_name, prediction_id),
        )
        await self.conn.commit()
        return cur.rowcount

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
