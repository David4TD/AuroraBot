"""Async SQLite data-access layer built on aiosqlite.

A single ``Database`` instance is shared across all cogs (attached to the bot).
Every public method opens no long-lived cursor state beyond the shared
connection, and all writes are committed immediately for durability on the
Unraid appdata volume.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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
        # Column changes first, because schema.sql indexes columns a pre-1.1
        # database doesn't have yet. Data migrations run afterwards, once every
        # table they touch is guaranteed to exist.
        await self._migrate()
        await self._run_schema()
        await self._migrate_data()
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

        # Step 3: predictions learn which server they were made in, so a live
        # alert can name this server's voters without exposing anyone from
        # another. Existing rows keep NULL and are simply never attributed.
        prediction_columns = await self._table_columns("predictions")
        if prediction_columns and "guild_id" not in prediction_columns:
            log.info("Adding guild_id to predictions…")
            await self._conn.execute("ALTER TABLE predictions ADD COLUMN guild_id TEXT")
            await self._conn.commit()

        # Step 4: points move from a global counter on users to the prediction
        # that earned them, so they can be totalled per server.
        if prediction_columns and "points_awarded" not in prediction_columns:
            log.info("Adding points_awarded to predictions…")
            await self._conn.execute(
                "ALTER TABLE predictions ADD COLUMN points_awarded INTEGER NOT NULL DEFAULT 0"
            )
            # Backfill history with the reward that was actually paid at the
            # time — deliberately the literal 25 rather than WIN_REWARD, so
            # changing the reward later doesn't rewrite what people already won.
            await self._conn.execute(
                "UPDATE predictions SET points_awarded = 25 WHERE status = 'won'"
            )
            await self._conn.commit()

        # Step 5: leaderboards become per tournament, so a pick — and the alert
        # message a pick can be made from — must remember which one it belongs
        # to. Existing rows stay NULL and appear only on the all-time board.
        if prediction_columns and "tournament_id" not in prediction_columns:
            log.info("Adding tournament columns to predictions…")
            for ddl in (
                "ALTER TABLE predictions ADD COLUMN tournament_id INTEGER",
                "ALTER TABLE predictions ADD COLUMN tournament_name TEXT",
            ):
                await self._conn.execute(ddl)
            await self._conn.commit()

        # Step 6: predictions become opt-out per subscription. Existing rows
        # default to 1, so nothing that was already running goes quiet.
        if "predictions" not in columns:
            log.info("Adding predictions toggle to alert_subscriptions…")
            await self._conn.execute(
                "ALTER TABLE alert_subscriptions "
                "ADD COLUMN predictions INTEGER NOT NULL DEFAULT 1"
            )
            await self._conn.commit()

        # Step 7: a digest row records the match's own tournament. Without it,
        # votes cast from a league digest were filed under the *league* id while
        # votes on the same match from a reminder used the tournament id, so the
        # two landed on different leaderboards.
        digest_match_columns = await self._table_columns("digest_matches")
        if digest_match_columns and "tournament_id" not in digest_match_columns:
            log.info("Adding tournament columns to digest_matches…")
            for ddl in (
                "ALTER TABLE digest_matches ADD COLUMN tournament_id INTEGER",
                "ALTER TABLE digest_matches ADD COLUMN tournament_name TEXT",
            ):
                await self._conn.execute(ddl)
            await self._conn.commit()

        # Step 8: conviction tokens. Existing picks were made under the old
        # variable-stake model and never had one, so 0 is the honest default.
        if prediction_columns and "doubled" not in prediction_columns:
            log.info("Adding the conviction token column to predictions…")
            await self._conn.execute(
                "ALTER TABLE predictions ADD COLUMN doubled INTEGER NOT NULL DEFAULT 0"
            )
            await self._conn.commit()

        # Step 9: playoff matches count for more. Existing rows keep 1 — the
        # weight they were actually settled at.
        if prediction_columns and "weight" not in prediction_columns:
            log.info("Adding the stage weight column to predictions…")
            await self._conn.execute(
                "ALTER TABLE predictions ADD COLUMN weight REAL NOT NULL DEFAULT 1"
            )
            await self._conn.commit()

        alert_message_columns = await self._table_columns("alert_messages")
        if alert_message_columns and "tournament_id" not in alert_message_columns:
            log.info("Adding tournament columns to alert_messages…")
            for ddl in (
                "ALTER TABLE alert_messages ADD COLUMN tournament_id INTEGER",
                "ALTER TABLE alert_messages ADD COLUMN tournament_name TEXT",
            ):
                await self._conn.execute(ddl)
            await self._conn.commit()

    async def _migrate_data(self) -> None:
        """Data migrations, run after schema.sql so every table exists."""
        assert self._conn is not None
        await self._migrate_games_to_optin()
        await self._repair_digest_vote_tournaments()
        await self._rescore_onto_fixed_stake()

    async def _rescore_onto_fixed_stake(self) -> int:
        """Reprice every settled pick under the fixed-stake model. Runs once.

        Points used to be a variable stake (10/25/50 from a per-tournament
        budget) times the multiplier. They're now a flat base times the
        multiplier, so old and new totals aren't the same currency — a board
        holding both would be meaningless. This recomputes the lot.

        Nothing is re-fetched: a settled row already says whether it won, and a
        win means it backed the winner, so the crowd each match was priced
        against is recoverable by counting rows. Perfect-day bonuses are
        recomputed the same way, since rescoring overwrites the row they were
        added to.
        """
        from ..utils.scoring import (
            MIN_PERFECT_DAY_MATCHES, PERFECT_DAY_BONUS, payout_for,
        )

        if await self.meta_get("scoring_model") == "fixed_stake":
            return 0

        cur = await self.conn.execute(
            """
            SELECT match_id, guild_id,
                   COUNT(*) AS voters,
                   SUM(status = 'won') AS backers
            FROM predictions
            WHERE status IN ('won', 'lost')
            GROUP BY match_id, guild_id
            """
        )
        crowds = {
            (row["match_id"], row["guild_id"]): (int(row["backers"] or 0),
                                                int(row["voters"] or 0))
            for row in await cur.fetchall()
        }
        if not crowds:
            await self.meta_set("scoring_model", "fixed_stake")
            return 0

        cur = await self.conn.execute(
            "SELECT id, match_id, guild_id, status, doubled, weight "
            "FROM predictions WHERE status IN ('won', 'lost')"
        )
        rows = list(await cur.fetchall())
        for row in rows:
            if row["status"] != "won":
                await self.conn.execute(
                    "UPDATE predictions SET points_awarded = 0 WHERE id = ?",
                    (row["id"],),
                )
                continue
            backers, voters = crowds[(row["match_id"], row["guild_id"])]
            points = payout_for(
                backers, voters, doubled=bool(row["doubled"]),
                weight=float(row["weight"] or 1.0),
            ).points
            await self.conn.execute(
                "UPDATE predictions SET points_awarded = ? WHERE id = ?",
                (points, row["id"]),
            )

        # Perfect days, re-awarded onto the last pick of each clean sweep.
        cur = await self.conn.execute(
            """
            SELECT discord_id, guild_id, substr(match_starts_at, 1, 10) AS day,
                   COUNT(*) AS picks, SUM(status = 'won') AS wins, MAX(id) AS last_id
            FROM predictions
            WHERE status IN ('won', 'lost') AND match_starts_at IS NOT NULL
              AND guild_id IS NOT NULL
            GROUP BY discord_id, guild_id, day
            HAVING picks >= ? AND wins = picks
            """,
            (MIN_PERFECT_DAY_MATCHES,),
        )
        sweeps = list(await cur.fetchall())
        for row in sweeps:
            await self.conn.execute(
                "UPDATE predictions SET points_awarded = points_awarded + ? "
                "WHERE id = ?",
                (PERFECT_DAY_BONUS, row["last_id"]),
            )

        # users.points is the lifetime cross-server total; rebuild it from the
        # rows rather than trying to adjust it by a delta.
        await self.conn.execute(
            """
            UPDATE users SET points = COALESCE((
                SELECT SUM(points_awarded) FROM predictions p
                WHERE p.discord_id = users.discord_id
            ), 0)
            """
        )
        await self.meta_set("scoring_model", "fixed_stake")
        await self.conn.commit()
        log.info(
            "Rescored %d settled prediction(s) onto the fixed stake, "
            "re-awarding %d perfect day(s)",
            len(rows), len(sweeps),
        )
        return len(rows)

    async def _repair_digest_vote_tournaments(self) -> int:
        """Realign votes that were filed under a league id instead of a stage.

        Votes cast from a *league* digest recorded the league's id as their
        tournament, while votes on the same match from a match reminder used the
        real tournament id. The two then sat on different leaderboards, so a
        match four people predicted showed one of them.

        ``alert_messages`` and ``digest_matches`` both hold the authoritative
        match → tournament mapping, and either will do. Both are consulted
        because they're pruned on different schedules (3 days and 14) and a
        match that only ever appeared in a digest was never in the other.

        Older votes than that keep counting on the all-time board either way.
        Safe to re-run: rows already correct don't match.
        """
        # The mapping, preferring a reminder's copy and falling back to a
        # digest's. Repeated in the WHERE because SQLite has no way to name it.
        source_id = """COALESCE(
                (SELECT am.tournament_id FROM alert_messages am
                 WHERE am.match_id = p.match_id
                   AND am.tournament_id IS NOT NULL LIMIT 1),
                (SELECT dm.tournament_id FROM digest_matches dm
                 WHERE dm.match_id = p.match_id
                   AND dm.tournament_id IS NOT NULL LIMIT 1))"""
        source_name = """COALESCE(
                (SELECT am.tournament_name FROM alert_messages am
                 WHERE am.match_id = p.match_id
                   AND am.tournament_id IS NOT NULL LIMIT 1),
                (SELECT dm.tournament_name FROM digest_matches dm
                 WHERE dm.match_id = p.match_id
                   AND dm.tournament_id IS NOT NULL LIMIT 1),
                p.tournament_name)"""
        cur = await self.conn.execute(
            f"""
            UPDATE predictions AS p
            SET tournament_id = {source_id},
                tournament_name = {source_name}
            WHERE {source_id} IS NOT NULL
              AND {source_id} IS NOT p.tournament_id
            """  # noqa: S608 - the fragments are literals, not input
        )
        await self.conn.commit()
        if cur.rowcount and cur.rowcount > 0:
            log.info(
                "Realigned %d prediction(s) onto their match's tournament",
                cur.rowcount,
            )
        return cur.rowcount or 0

    async def _migrate_games_to_optin(self) -> None:
        """Flip /games from opt-out to opt-in without silencing existing servers.

        The old model stored *disabled* games and treated a missing row as
        enabled, so a server that never ran /games followed everything. Under
        opt-in a missing row means "not following", so applying the new rule
        blindly would mute every existing server until someone noticed.

        So: drop the enabled=0 rows (absence now carries that meaning), and for
        any guild that was actively using alerts but never curated its games,
        write explicit rows for the whole catalogue — preserving exactly what it
        followed before. Guilds that *had* run /games already have enabled=1
        rows, which mean the right thing unchanged.

        Guarded by a meta flag because "no rows" is indistinguishable between a
        fresh guild and a migrated one.
        """
        from ..utils.games import GAMES

        if await self.meta_get("games_optin") == "1":
            return

        cur = await self._conn.execute(
            "SELECT COUNT(*) AS n FROM guild_games WHERE enabled = 0"
        )
        stale = int((await cur.fetchone())["n"])

        cur = await self._conn.execute(
            """
            SELECT DISTINCT guild_id FROM alert_subscriptions
            WHERE guild_id IS NOT NULL
              AND guild_id NOT IN (
                  SELECT guild_id FROM guild_games WHERE enabled = 1
              )
            """
        )
        inherit = [row["guild_id"] for row in await cur.fetchall()]

        if stale:
            await self._conn.execute("DELETE FROM guild_games WHERE enabled = 0")
        if inherit:
            await self._conn.executemany(
                "INSERT OR IGNORE INTO guild_games (guild_id, game, enabled) "
                "VALUES (?, ?, 1)",
                [(gid, key) for gid in inherit for key in GAMES],
            )
        await self._conn.commit()
        await self.meta_set("games_optin", "1")

        if stale or inherit:
            log.info(
                "Migrated /games to opt-in: cleared %d disabled rows, "
                "carried existing selections for %d guild(s)",
                stale, len(inherit),
            )

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() has not been awaited yet")
        return self._conn

    # ── schema metadata ──────────────────────────────────────────────────────
    async def meta_get(self, key: str) -> str | None:
        cur = await self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def meta_set(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

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

    # ── per-guild game selection (opt-in) ────────────────────────────────────
    async def enabled_games(self, guild_id: int | None) -> set[str]:
        """Games this server has opted into. Empty means "follows nothing".

        DMs have no guild, so they get the empty set and every guild-scoped
        feature declines politely rather than guessing.
        """
        if guild_id is None:
            return set()
        cur = await self.conn.execute(
            "SELECT game FROM guild_games WHERE guild_id = ? AND enabled = 1",
            (str(guild_id),),
        )
        return {row["game"] for row in await cur.fetchall()}

    async def all_enabled_games(self) -> dict[str, set[str]]:
        """``{guild_id: {games}}`` — one query for the background loops."""
        cur = await self.conn.execute(
            "SELECT guild_id, game FROM guild_games WHERE enabled = 1"
        )
        out: dict[str, set[str]] = {}
        for row in await cur.fetchall():
            out.setdefault(row["guild_id"], set()).add(row["game"])
        return out

    async def distinct_enabled_games(self) -> set[str]:
        """Every game any guild follows — what the tournament pre-warm needs."""
        cur = await self.conn.execute(
            "SELECT DISTINCT game FROM guild_games WHERE enabled = 1"
        )
        return {row["game"] for row in await cur.fetchall()}

    async def set_games(
        self, guild_id: int, enabled: set[str], known: set[str], updated_by: int
    ) -> None:
        """Replace a guild's selection from the /games panel.

        Enabled games get a row; the rest have theirs removed, so absence is the
        single source of truth for "not following". Games outside *known* are
        left alone, so a stale panel can't wipe an unrecognised row.
        """
        keep = [(str(guild_id), g, str(updated_by)) for g in sorted(enabled & known)]
        if keep:
            await self.conn.executemany(
                """
                INSERT INTO guild_games (guild_id, game, enabled, updated_by)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(guild_id, game) DO UPDATE SET
                    enabled    = 1,
                    updated_by = excluded.updated_by,
                    updated_at = datetime('now')
                """,
                keep,
            )
        drop = sorted(known - enabled)
        if drop:
            placeholders = ",".join("?" * len(drop))
            await self.conn.execute(
                f"DELETE FROM guild_games WHERE guild_id = ? AND game IN ({placeholders})",
                (str(guild_id), *drop),
            )
        await self.conn.commit()

    async def clear_games(self, guild_id: int) -> int:
        """Drop every selection, returning the guild to following nothing."""
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
        predictions: bool = True,
    ) -> int | None:
        """Insert a subscription. Returns its id, or None if it already exists.

        The id is what lets the confirmation offer a toggle without a second
        lookup; autoincrement starts at 1, so callers testing truthiness still
        read correctly.
        """
        try:
            cur = await self.conn.execute(
                """
                INSERT INTO alert_subscriptions
                    (guild_id, channel_id, game, scope, team_id, team_name,
                     league_id, league_name, tournament_id, tournament_name,
                     predictions, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if predictions else 0,
                    str(created_by),
                ),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None  # duplicate subscription

    async def set_subscription_predictions(
        self, sub_id: int, guild_id: int, on: bool
    ) -> int:
        """Turn vote buttons on or off for one subscription.

        Guild-scoped in the WHERE clause: the id arrives from a Discord
        component and can't be trusted to belong to the clicking server.
        """
        cur = await self.conn.execute(
            "UPDATE alert_subscriptions SET predictions = ? "
            "WHERE id = ? AND guild_id = ?",
            (1 if on else 0, sub_id, str(guild_id)),
        )
        await self.conn.commit()
        return cur.rowcount

    # ── tournament cache ─────────────────────────────────────────────────────
    async def save_tournaments(self, game: str, rows: list[dict]) -> None:
        """Persist a game's tournament list so a restart starts warm."""
        await self.conn.execute(
            """
            INSERT INTO tournament_cache (game, payload, fetched_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(game) DO UPDATE SET
                payload    = excluded.payload,
                fetched_at = excluded.fetched_at
            """,
            (game, json.dumps(rows)),
        )
        await self.conn.commit()

    async def load_tournaments(self) -> dict[str, tuple[datetime, list[dict]]]:
        """Every cached tournament list, as ``{game: (fetched_at, rows)}``.

        A row that won't parse is skipped rather than raising — a corrupt cache
        should cost one cold fetch, not a failed startup.
        """
        cur = await self.conn.execute(
            "SELECT game, payload, fetched_at FROM tournament_cache"
        )
        out: dict[str, tuple[datetime, list[dict]]] = {}
        for row in await cur.fetchall():
            try:
                rows = json.loads(row["payload"])
                stamp = datetime.fromisoformat(row["fetched_at"]).replace(
                    tzinfo=timezone.utc
                )
            except (ValueError, TypeError):
                log.warning("discarding unreadable tournament cache for %s", row["game"])
                continue
            out[row["game"]] = (stamp, rows)
        return out

    # ── per-guild preferences ────────────────────────────────────────────────
    async def guild_settings(self, guild_id: int | None) -> dict:
        """A server's overrides. Missing keys mean "use the env default"."""
        if guild_id is None:
            return {}
        cur = await self.conn.execute(
            "SELECT digest_hour, digest_tz, alert_lead_minutes "
            "FROM guild_settings WHERE guild_id = ?",
            (str(guild_id),),
        )
        row = await cur.fetchone()
        if row is None:
            return {}
        return {k: row[k] for k in row.keys() if row[k] is not None}

    async def set_guild_setting(
        self, guild_id: int, key: str, value, updated_by: int
    ) -> None:
        """Set one override. ``None`` clears it back to the deployment default."""
        if key not in {"digest_hour", "digest_tz", "alert_lead_minutes"}:
            raise ValueError(f"unknown guild setting {key!r}")
        await self.conn.execute(
            f"""
            INSERT INTO guild_settings (guild_id, {key}, updated_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                {key}      = excluded.{key},
                updated_by = excluded.updated_by,
                updated_at = datetime('now')
            """,  # noqa: S608 - key is checked against a literal allowlist above
            (str(guild_id), value, str(updated_by)),
        )
        await self.conn.commit()

    async def all_guild_settings(self) -> dict[str, dict]:
        """Every server's overrides, for the loops that run across guilds."""
        cur = await self.conn.execute("SELECT * FROM guild_settings")
        out: dict[str, dict] = {}
        for row in await cur.fetchall():
            out[row["guild_id"]] = {
                k: row[k]
                for k in ("digest_hour", "digest_tz", "alert_lead_minutes")
                if row[k] is not None
            }
        return out

    # ── resets ───────────────────────────────────────────────────────────────
    async def reset_guild(self, guild_id: int, *, what: str) -> dict[str, int]:
        """Wipe part of a server's data. Returns what was removed, per table.

        Every statement is scoped by ``guild_id``: a moderator resetting their
        own server must never be able to touch another's, and these are driven
        from a Discord component whose values can't be trusted on their own.
        """
        gid = str(guild_id)
        removed: dict[str, int] = {}

        async def wipe(label: str, sql: str, params=(gid,)) -> None:
            cur = await self.conn.execute(sql, params)
            if cur.rowcount and cur.rowcount > 0:
                removed[label] = cur.rowcount

        if what in {"alerts", "all"}:
            await wipe("alerts", "DELETE FROM alert_subscriptions WHERE guild_id = ?")
            await wipe("alert messages", "DELETE FROM alert_messages WHERE guild_id = ?")
            await wipe("digests", "DELETE FROM match_digests WHERE guild_id = ?")
        if what in {"games", "all"}:
            await wipe("games", "DELETE FROM guild_games WHERE guild_id = ?")
        if what in {"predictions", "all"}:
            # Only this server's picks. A member's history elsewhere, and their
            # lifetime total on users, are deliberately untouched.
            await wipe("predictions", "DELETE FROM predictions WHERE guild_id = ?")
        if what in {"settings", "all"}:
            await wipe("preferences", "DELETE FROM guild_settings WHERE guild_id = ?")

        # digest_matches hangs off match_digests; the FK cascade needs the
        # pragma, so clean up explicitly rather than relying on it.
        await self.conn.execute(
            "DELETE FROM digest_matches WHERE digest_id NOT IN "
            "(SELECT id FROM match_digests)"
        )
        await self.conn.commit()
        return removed

    async def guild_summary(self, guild_id: int) -> dict[str, int]:
        """Row counts behind a reset confirmation, so it says what it'll take."""
        gid = str(guild_id)
        out = {}
        for label, sql in (
            ("games", "SELECT COUNT(*) AS n FROM guild_games WHERE guild_id = ?"),
            ("alerts", "SELECT COUNT(*) AS n FROM alert_subscriptions WHERE guild_id = ?"),
            ("predictions", "SELECT COUNT(*) AS n FROM predictions WHERE guild_id = ?"),
            ("digests", "SELECT COUNT(*) AS n FROM match_digests WHERE guild_id = ?"),
        ):
            cur = await self.conn.execute(sql, (gid,))
            out[label] = int((await cur.fetchone())["n"])
        return out

    async def remove_subscriptions(self, sub_ids, guild_id: int) -> int:
        """Delete several subscriptions at once. Returns how many went.

        ``guild_id`` is part of the WHERE clause, not a pre-check: the ids come
        back from a Discord component, so they must never be trusted to belong
        to the server whose moderator is clicking.
        """
        ids = [int(i) for i in sub_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        cur = await self.conn.execute(
            f"DELETE FROM alert_subscriptions "  # noqa: S608 - placeholders only
            f"WHERE id IN ({placeholders}) AND guild_id = ?",
            (*ids, str(guild_id)),
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

    async def pending_result_alerts(self, limit: int = 20) -> list[aiosqlite.Row]:
        """Matches a channel saw go live but hasn't seen a result for.

        This is the whole basis for auto-posting results: a finished match is
        in neither the running nor the upcoming feed, so there's nothing to
        poll. What we *do* know is which matches we announced as live, and
        those are exactly the ones worth checking for a final score.

        Oldest first, and capped — a backlog after downtime should drain over
        several polls rather than firing a burst of requests at once.
        """
        cur = await self.conn.execute(
            """
            SELECT DISTINCT a.match_id, a.channel_id
            FROM alerted_matches a
            WHERE a.state = 'live'
              AND NOT EXISTS (
                  SELECT 1 FROM alerted_matches f
                  WHERE f.match_id = a.match_id
                    AND f.channel_id = a.channel_id
                    AND f.state = 'finished'
              )
            ORDER BY a.alerted_at
            LIMIT ?
            """,
            (limit,),
        )
        return list(await cur.fetchall())

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
        tournament_id: int | None = None,
        tournament_name: str | None = None,
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
                 team_a_id, team_a_name, team_b_id, team_b_name, begin_at,
                 tournament_id, tournament_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                tournament_id,
                tournament_name,
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
                 team_a_id, team_a_name, team_b_id, team_b_name,
                 tournament_id, tournament_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    m.get("tournament_id"),
                    m.get("tournament_name"),
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
        guild_id: int | None = None,
        tournament_id: int | None = None,
        tournament_name: str | None = None,
    ) -> bool:
        try:
            await self.conn.execute(
                """
                INSERT INTO predictions
                    (discord_id, guild_id, match_id, game, tournament_id,
                     tournament_name, predicted_team_id, predicted_team_name,
                     opponent_team_name, match_starts_at, stake)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(discord_id),
                    str(guild_id) if guild_id else None,
                    match_id,
                    game,
                    tournament_id,
                    tournament_name,
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

    async def open_predictions_for_match(self, match_id: int) -> list[aiosqlite.Row]:
        """Unsettled picks on one match, across every server."""
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE match_id = ? AND status = 'open'",
            (match_id,),
        )
        return list(await cur.fetchall())

    async def get_prediction_by_id(self, prediction_id: int) -> aiosqlite.Row | None:
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE id = ?", (prediction_id,)
        )
        return await cur.fetchone()

    async def get_open_predictions(self) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM predictions WHERE status = 'open'"
        )
        return list(await cur.fetchall())

    async def resolve_prediction(
        self, prediction_id: int, status: str, points_delta: int, discord_id: str
    ) -> None:
        awarded = points_delta if status == "won" else 0
        await self.conn.execute(
            "UPDATE predictions SET status = ?, points_awarded = ?, "
            "resolved_at = datetime('now') WHERE id = ?",
            (status, awarded, prediction_id),
        )
        if status == "won":
            # users.points stays as the lifetime total across every server,
            # which is what /profile shows in a DM. Per-server totals come from
            # summing predictions.points_awarded instead.
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

    async def align_match_tournament(
        self, match_id: int, tournament_id: int | None, tournament_name: str | None
    ) -> int:
        """File every pick on a match under the match's own tournament.

        A prediction records the tournament it counts towards at the moment
        it's made, from whatever posted the buttons. If any of those sources
        ever disagrees — a league-wide digest naming the league rather than the
        stage, say — the picks scatter across two leaderboards and the result
        card shows a fraction of the people who actually voted.

        Called when the match settles, with the tournament straight off the
        match, which is the one answer that can't be wrong.
        """
        if tournament_id is None:
            return 0
        cur = await self.conn.execute(
            """
            UPDATE predictions
            SET tournament_id = ?,
                tournament_name = COALESCE(?, tournament_name)
            WHERE match_id = ?
              AND (tournament_id IS NULL OR tournament_id != ?)
            """,
            (int(tournament_id), tournament_name, int(match_id), int(tournament_id)),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def set_match_weight(self, match_id: int, weight: float) -> int:
        """Stamp how much a match's stage counts for onto every pick on it.

        Only rows still open are touched: a settled pick was already paid at
        the weight it was settled with, and moving the goalposts afterwards
        would silently rewrite a finished board.
        """
        cur = await self.conn.execute(
            "UPDATE predictions SET weight = ? "
            "WHERE match_id = ? AND status = 'open'",
            (float(weight), int(match_id)),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def match_predictions(
        self, match_id: int, guild_id: int | None
    ) -> list[aiosqlite.Row]:
        """Who picked whom for this match, in this server.

        Scoped to the guild on purpose: predictions are stored per user, not
        per server, so an unscoped list would print names from every other
        server the bot is in into this channel.
        """
        if guild_id is None:
            return []
        cur = await self.conn.execute(
            """
            SELECT p.discord_id, p.predicted_team_id, p.predicted_team_name,
                   p.doubled,
                   COALESCE(u.display_name, 'Someone') AS display_name
            FROM predictions p
            LEFT JOIN users u ON u.discord_id = p.discord_id
            WHERE p.match_id = ? AND p.guild_id = ?
            ORDER BY p.created_at
            """,
            (match_id, str(guild_id)),
        )
        return list(await cur.fetchall())

    # ── leaderboard ──────────────────────────────────────────────────────────
    async def guild_leaderboard(
        self, guild_id: int, limit: int = 5
    ) -> list[aiosqlite.Row]:
        """Top predictors *in this server*, by points earned there.

        ``users.points`` is a lifetime total across every server the bot is in,
        so it can't answer "who is winning here". Summing the server's own
        settled picks can.
        """
        cur = await self.conn.execute(
            """
            SELECT p.discord_id,
                   COALESCE(u.display_name, 'Someone') AS display_name,
                   COALESCE(SUM(p.points_awarded), 0) AS points,
                   SUM(p.status = 'won')  AS won,
                   SUM(p.status = 'lost') AS lost
            FROM predictions p
            LEFT JOIN users u ON u.discord_id = p.discord_id
            WHERE p.guild_id = ? AND p.status IN ('won', 'lost')
            GROUP BY p.discord_id
            HAVING won + lost > 0
            ORDER BY points DESC, won DESC, (won + lost) ASC, display_name
            LIMIT ?
            """,
            (str(guild_id), limit),
        )
        return list(await cur.fetchall())

    # ── stakes, streaks, tournament boards ───────────────────────────────────
    async def tokens_spent(
        self, discord_id: int, guild_id: int | None, tournament_id: int | None
    ) -> int:
        """Conviction tokens this member has committed to this tournament.

        Counts open picks too: a token is spent the moment it's declared, or
        someone could park three on early matches and reclaim them by switching
        teams later.
        """
        if guild_id is None or tournament_id is None:
            return 0
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS spent FROM predictions "
            "WHERE discord_id = ? AND guild_id = ? AND tournament_id = ? "
            "AND doubled = 1",
            (str(discord_id), str(guild_id), tournament_id),
        )
        row = await cur.fetchone()
        return int(row["spent"] or 0)

    async def set_doubled(self, prediction_id: int, doubled: bool) -> bool:
        """Spend or reclaim a token. Only while the pick is still open.

        Returns whether anything changed, so a click that raced the match
        starting can be reported honestly rather than silently ignored.
        """
        cur = await self.conn.execute(
            "UPDATE predictions SET doubled = ? WHERE id = ? AND status = 'open'",
            (1 if doubled else 0, prediction_id),
        )
        await self.conn.commit()
        return bool(cur.rowcount)

    async def current_streak(self, discord_id: int, guild_id: int | None) -> int:
        """Consecutive wins ending at this member's most recent settled pick.

        Walks back through settled predictions in resolution order and stops at
        the first loss, so a streak is genuinely consecutive rather than a
        count of wins.
        """
        if guild_id is None:
            return 0
        cur = await self.conn.execute(
            "SELECT status FROM predictions "
            "WHERE discord_id = ? AND guild_id = ? AND status IN ('won', 'lost') "
            "ORDER BY resolved_at DESC, id DESC LIMIT 50",
            (str(discord_id), str(guild_id)),
        )
        streak = 0
        for row in await cur.fetchall():
            if row["status"] != "won":
                break
            streak += 1
        return streak

    async def tournament_leaderboard(
        self, guild_id: int, tournament_id: int | None, limit: int = 10
    ) -> list[aiosqlite.Row]:
        """Top predictors for one tournament, or the whole server if None."""
        where = "p.guild_id = ? AND p.status IN ('won', 'lost')"
        params: list = [str(guild_id)]
        if tournament_id is not None:
            where += " AND p.tournament_id = ?"
            params.append(tournament_id)
        params.append(limit)
        cur = await self.conn.execute(
            f"""
            SELECT p.discord_id,
                   COALESCE(u.display_name, 'Someone') AS display_name,
                   COALESCE(SUM(p.points_awarded), 0) AS points,
                   SUM(p.status = 'won')  AS won,
                   SUM(p.status = 'lost') AS lost
            FROM predictions p
            LEFT JOIN users u ON u.discord_id = p.discord_id
            WHERE {where}
            GROUP BY p.discord_id
            HAVING won + lost > 0
            ORDER BY points DESC, won DESC, (won + lost) ASC, display_name
            LIMIT ?
            """,  # noqa: S608 - `where` is built from literals only
            params,
        )
        return list(await cur.fetchall())

    # ── champions ────────────────────────────────────────────────────────────
    async def crown_champion(
        self, guild_id: int, tournament_id: int, tournament_name: str | None,
        discord_id: str, points: int, won: int, lost: int,
    ) -> bool:
        """Record an event's winner. False if this event was already crowned.

        The insert is the lock: two pollers racing on the same finished
        tournament can't both announce it, because only one row can exist.
        """
        cur = await self.conn.execute(
            """
            INSERT OR IGNORE INTO tournament_champions
                (guild_id, tournament_id, tournament_name, discord_id,
                 points, won, lost)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(guild_id), int(tournament_id), tournament_name,
             str(discord_id), int(points), int(won), int(lost)),
        )
        await self.conn.commit()
        return bool(cur.rowcount)

    async def crowned_tournaments(self, guild_id: int) -> set[int]:
        cur = await self.conn.execute(
            "SELECT tournament_id FROM tournament_champions WHERE guild_id = ?",
            (str(guild_id),),
        )
        return {int(r["tournament_id"]) for r in await cur.fetchall()}

    async def champion_counts(self, guild_id: int) -> dict[str, int]:
        """How many events each member has won here — the crown count."""
        cur = await self.conn.execute(
            "SELECT discord_id, COUNT(*) AS titles FROM tournament_champions "
            "WHERE guild_id = ? GROUP BY discord_id",
            (str(guild_id),),
        )
        return {str(r["discord_id"]): int(r["titles"]) for r in await cur.fetchall()}

    async def recent_champions(
        self, guild_id: int, limit: int = 5
    ) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(
            "SELECT * FROM tournament_champions WHERE guild_id = ? "
            "ORDER BY crowned_at DESC LIMIT ?",
            (str(guild_id), limit),
        )
        return list(await cur.fetchall())

    async def open_predictions_in_tournament(
        self, guild_id: int, tournament_id: int
    ) -> int:
        """Picks still waiting on a result. A board isn't final until zero."""
        cur = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM predictions "
            "WHERE guild_id = ? AND tournament_id = ? AND status = 'open'",
            (str(guild_id), int(tournament_id)),
        )
        row = await cur.fetchone()
        return int(row["n"] or 0)

    async def guilds_with_predictions(self) -> list[str]:
        cur = await self.conn.execute(
            "SELECT DISTINCT guild_id FROM predictions WHERE guild_id IS NOT NULL"
        )
        return [str(r["guild_id"]) for r in await cur.fetchall()]

    async def channels_for_tournament(
        self, guild_id: int, tournament_id: int
    ) -> list[int]:
        """Where this event was actually talked about, so a crowning lands there.

        Reminders and digests both record the channel they went to; a standing
        subscription covers an event whose recent posts have been pruned.
        """
        cur = await self.conn.execute(
            """
            SELECT DISTINCT channel_id FROM (
                SELECT channel_id FROM alert_messages
                WHERE guild_id = ? AND tournament_id = ?
                UNION
                SELECT d.channel_id FROM match_digests d
                JOIN digest_matches m ON m.digest_id = d.id
                WHERE d.guild_id = ? AND m.tournament_id = ?
                UNION
                SELECT channel_id FROM alert_subscriptions
                WHERE guild_id = ? AND tournament_id = ?
            )
            """,
            (str(guild_id), int(tournament_id)) * 3,
        )
        return [int(r["channel_id"]) for r in await cur.fetchall()]

    async def tournaments_with_predictions(
        self, guild_id: int, limit: int = 25
    ) -> list[aiosqlite.Row]:
        """Tournaments this server has actually predicted on, busiest first."""
        cur = await self.conn.execute(
            """
            SELECT tournament_id, tournament_name, COUNT(*) AS picks
            FROM predictions
            WHERE guild_id = ? AND tournament_id IS NOT NULL
            GROUP BY tournament_id
            ORDER BY MAX(created_at) DESC
            LIMIT ?
            """,
            (str(guild_id), limit),
        )
        return list(await cur.fetchall())

    async def day_predictions(
        self, discord_id: int, guild_id: int | None, day: str
    ) -> list[aiosqlite.Row]:
        """A member's picks for matches starting on *day* (YYYY-MM-DD, UTC).

        Used for the perfect-day bonus. ``match_starts_at`` is the match's own
        kick-off, so the day is the match day rather than when they clicked.
        """
        if guild_id is None:
            return []
        cur = await self.conn.execute(
            "SELECT id, status FROM predictions "
            "WHERE discord_id = ? AND guild_id = ? AND substr(match_starts_at, 1, 10) = ?",
            (str(discord_id), str(guild_id), day),
        )
        return list(await cur.fetchall())

    async def award_bonus(self, discord_id: int, prediction_id: int, points: int) -> None:
        """Add bonus points onto an already-settled prediction."""
        await self.conn.execute(
            "UPDATE predictions SET points_awarded = points_awarded + ? WHERE id = ?",
            (points, prediction_id),
        )
        await self.conn.execute(
            "UPDATE users SET points = points + ? WHERE discord_id = ?",
            (points, str(discord_id)),
        )
        await self.conn.commit()

    async def guild_stats(self, discord_id: int, guild_id: int | None) -> dict:
        """One member's record in one server: points, wins, settled, open."""
        empty = {"points": 0, "won": 0, "lost": 0, "settled": 0, "open": 0}
        if guild_id is None:
            return empty
        cur = await self.conn.execute(
            """
            SELECT COALESCE(SUM(points_awarded), 0) AS points,
                   SUM(status = 'won')  AS won,
                   SUM(status = 'lost') AS lost,
                   SUM(status = 'open') AS still_open
            FROM predictions
            WHERE discord_id = ? AND guild_id = ?
            """,
            (str(discord_id), str(guild_id)),
        )
        row = await cur.fetchone()
        if row is None:
            return empty
        won, lost = int(row["won"] or 0), int(row["lost"] or 0)
        return {
            "points": int(row["points"] or 0),
            "won": won,
            "lost": lost,
            "settled": won + lost,
            "open": int(row["still_open"] or 0),
        }

    async def recent_results(
        self, guild_id: int, limit: int = 5
    ) -> list[aiosqlite.Row]:
        """This server's most recently settled predictions, newest first."""
        cur = await self.conn.execute(
            """
            SELECT p.discord_id, p.status, p.predicted_team_name,
                   p.opponent_team_name, p.resolved_at,
                   COALESCE(u.display_name, 'Someone') AS display_name
            FROM predictions p
            LEFT JOIN users u ON u.discord_id = p.discord_id
            WHERE p.guild_id = ? AND p.status IN ('won', 'lost')
            ORDER BY p.resolved_at DESC, p.id DESC
            LIMIT ?
            """,
            (str(guild_id), limit),
        )
        return list(await cur.fetchall())

