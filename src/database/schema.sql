-- AuroraBot SQLite schema
-- Idempotent: safe to run on every startup.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── Users ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    discord_id      TEXT PRIMARY KEY,
    display_name    TEXT,
    favorite_game   TEXT,                 -- default game slug for that user
    points          INTEGER NOT NULL DEFAULT 0,
    predictions_won INTEGER NOT NULL DEFAULT 0,
    predictions_total INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── Followed teams (per user) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS followed_teams (
    discord_id  TEXT NOT NULL,
    team_id     INTEGER NOT NULL,         -- PandaScore team id
    team_name   TEXT NOT NULL,
    game        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (discord_id, team_id),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE
);

-- ─── Team logo → application emoji cache ────────────────────────────────────
-- Discord renders images inline only as custom emoji, so each team's logo is
-- uploaded once as an application emoji and referenced by id thereafter.
-- image_url is kept so a rebranded logo can be detected and re-minted;
-- last_used_at drives LRU eviction as the 2000-emoji cap approaches.
CREATE TABLE IF NOT EXISTS team_emojis (
    team_id      INTEGER PRIMARY KEY,   -- PandaScore team id
    emoji_id     TEXT NOT NULL,
    emoji_name   TEXT NOT NULL,
    image_url    TEXT,
    animated     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── Per-guild game toggles ─────────────────────────────────────────────────
-- Only *disabled* games need a row: a missing row means enabled, so a server
-- that never touches /games follows everything, and games added to the
-- catalogue later switch on by default instead of silently staying off.
CREATE TABLE IF NOT EXISTS guild_games (
    guild_id    TEXT NOT NULL,
    game        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (guild_id, game)
);

-- ─── Alert subscriptions (per guild channel) ────────────────────────────────
-- A channel subscribes per game, scoped to a team, a tournament, or the whole
-- (Tier 1) feed for that game. See db.py::_migrate for the upgrade path from
-- the original team-only table.
CREATE TABLE IF NOT EXISTS alert_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT NOT NULL,
    channel_id      TEXT NOT NULL,
    game            TEXT NOT NULL,
    scope           TEXT NOT NULL DEFAULT 'game',  -- game | team | tournament
    team_id         INTEGER,              -- set when scope = 'team'
    team_name       TEXT,
    tournament_id   INTEGER,              -- set when scope = 'tournament'
    tournament_name TEXT,
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- IFNULL() in the index keeps whole-game subscriptions unique too: a plain
-- UNIQUE constraint would treat every NULL team_id as distinct.
CREATE UNIQUE INDEX IF NOT EXISTS idx_alertsub_unique
    ON alert_subscriptions (channel_id, game, IFNULL(team_id, 0), IFNULL(tournament_id, 0));

-- Tracks which match/state combos we've already announced so we don't repeat.
-- 'reminder' is the pre-match ping, 'live' fires when the match goes live.
CREATE TABLE IF NOT EXISTS alerted_matches (
    match_id    INTEGER NOT NULL,
    state       TEXT NOT NULL,            -- 'reminder' | 'live' | 'finished'
    channel_id  TEXT NOT NULL,
    alerted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (match_id, state, channel_id)
);

-- ─── Alert messages (reaction predictions) ──────────────────────────────────
-- Maps a posted alert message to the match it announced, so a reaction added
-- later — possibly after a bot restart — can be resolved back to two teams.
CREATE TABLE IF NOT EXISTS alert_messages (
    message_id  TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL,
    guild_id    TEXT,
    match_id    INTEGER NOT NULL,
    game        TEXT,
    team_a_id   INTEGER NOT NULL,
    team_a_name TEXT NOT NULL,
    team_b_id   INTEGER NOT NULL,
    team_b_name TEXT NOT NULL,
    begin_at    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ─── Daily schedule digests ─────────────────────────────────────────────────
-- One row per (channel, subscription, local date). The UNIQUE key is what stops
-- a restart — or the 5-minute catch-up sweep — from re-posting a day's schedule.
-- A row with message_id NULL records "checked, nothing on today", so we stop
-- re-querying the API for the rest of that day.
CREATE TABLE IF NOT EXISTS match_digests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        TEXT,
    channel_id      TEXT NOT NULL,
    subscription_id INTEGER NOT NULL,
    tournament_id   INTEGER,
    tournament_name TEXT,
    game            TEXT,
    local_date      TEXT NOT NULL,          -- YYYY-MM-DD in DIGEST_TZ
    message_id      TEXT,                   -- NULL = nothing scheduled that day
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (channel_id, subscription_id, local_date)
);

-- The matches a digest listed, so its dropdown can resolve a pick back to two
-- teams after a restart without re-hitting the API.
CREATE TABLE IF NOT EXISTS digest_matches (
    digest_id   INTEGER NOT NULL,
    match_id    INTEGER NOT NULL,
    begin_at    TEXT,
    team_a_id   INTEGER NOT NULL,
    team_a_name TEXT NOT NULL,
    team_b_id   INTEGER NOT NULL,
    team_b_name TEXT NOT NULL,
    PRIMARY KEY (digest_id, match_id),
    FOREIGN KEY (digest_id) REFERENCES match_digests(id) ON DELETE CASCADE
);

-- ─── Predictions ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id      TEXT NOT NULL,
    match_id        INTEGER NOT NULL,
    game            TEXT,
    predicted_team_id   INTEGER NOT NULL,
    predicted_team_name TEXT NOT NULL,
    opponent_team_name  TEXT,
    match_starts_at TEXT,
    stake           INTEGER NOT NULL DEFAULT 10,
    status          TEXT NOT NULL DEFAULT 'open',  -- open | won | lost | void
    resolved_at     TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (discord_id) REFERENCES users(discord_id) ON DELETE CASCADE,
    UNIQUE (discord_id, match_id)
);

CREATE INDEX IF NOT EXISTS idx_predictions_status  ON predictions(status);
CREATE INDEX IF NOT EXISTS idx_predictions_match   ON predictions(match_id);
CREATE INDEX IF NOT EXISTS idx_alertsub_game       ON alert_subscriptions(game);
CREATE INDEX IF NOT EXISTS idx_followed_user       ON followed_teams(discord_id);
CREATE INDEX IF NOT EXISTS idx_alerted_at          ON alerted_matches(alerted_at);
CREATE INDEX IF NOT EXISTS idx_alertmsg_created    ON alert_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_guildgames_disabled ON guild_games(guild_id) WHERE enabled = 0;
CREATE INDEX IF NOT EXISTS idx_teamemoji_lru       ON team_emojis(last_used_at);
CREATE INDEX IF NOT EXISTS idx_digest_date         ON match_digests(local_date);
