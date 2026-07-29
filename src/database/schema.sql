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

-- ─── Alert subscriptions (per guild channel) ────────────────────────────────
-- A channel can subscribe to a team and/or a whole game feed.
CREATE TABLE IF NOT EXISTS alert_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    TEXT NOT NULL,
    channel_id  TEXT NOT NULL,
    team_id     INTEGER,                  -- NULL = all matches for the game
    team_name   TEXT,
    game        TEXT NOT NULL,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (channel_id, team_id, game)
);

-- Tracks which match/state combos we've already announced so we don't repeat.
CREATE TABLE IF NOT EXISTS alerted_matches (
    match_id    INTEGER NOT NULL,
    state       TEXT NOT NULL,            -- 'upcoming' | 'live' | 'finished'
    channel_id  TEXT NOT NULL,
    alerted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (match_id, state, channel_id)
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
