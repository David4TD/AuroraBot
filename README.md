# 🌌 AuroraBot

A production-ready **Discord bot for eSports fans**. Follow live scores,
standings, deep team analytics and real-time match alerts — and challenge your
friends with match predictions and a server leaderboard.

Covers **League of Legends, Counter-Strike 2, Valorant, Dota 2, Rocket League,
Rainbow Six, Call of Duty, Overwatch, PUBG, Mobile Legends** and more, powered
by the [PandaScore API](https://pandascore.co).

Built with **discord.py 2.x** and fully containerised for one-click deployment
on **Unraid** via Docker.

---

## ✨ Features

| Area | Commands |
|------|----------|
| **Live scores** | `/live`, `/upcoming`, `/results` (optionally filtered by game) |
| **Standings** | `/standings <league>` with a season/split picker dropdown |
| **Analytics** | `/team <name>` — recent form, win rate, roster, deep-stats links |
| **Profiles** | `/profile`, `/follow`, `/unfollow`, `/setgame` |
| **Predictions** | `/predict`, `/mypredictions` — earn points for correct calls |
| **Leaderboard** | `/leaderboard` — top predictors in your server |
| **Alerts** | `/alerts add`, `/alerts list`, `/alerts remove` — ping a channel when matches go live |
| **Meta** | `/help`, `/ping` |

Predictions resolve automatically: a background task checks finished matches
every 5 minutes and awards points. Alerts poll for live matches on a
configurable interval (default 60s).

### Deep-stats sources
The analytics embeds link out to the community stat sites you rely on:

- **League of Legends** → [gol.gg](https://gol.gg) (RFT-style deep stats)
- **Valorant** → [vlr.gg](https://vlr.gg)
- **Counter-Strike 2** → [hltv.org](https://hltv.org)

> Core match/team/standings data comes from PandaScore's unified API; these
> links are provided for fans who want to dive deeper on those sites.

---

## 📁 Project structure

```
aurorabot/
├── src/
│   ├── bot.py                 # entrypoint: wires config, DB, API, cogs, health
│   ├── config.py              # env-driven settings
│   ├── database/
│   │   ├── db.py              # aiosqlite data-access layer
│   │   └── schema.sql         # idempotent schema (runs on startup)
│   ├── services/
│   │   ├── pandascore.py      # async PandaScore API client
│   │   └── health.py          # lightweight aiohttp /health server
│   ├── cogs/                  # one module per feature area
│   │   ├── meta.py  scores.py  standings.py  analytics.py
│   │   ├── profiles.py  predictions.py  leaderboard.py  alerts.py
│   └── utils/
│       ├── games.py           # game → PandaScore slug mapping
│       ├── embeds.py          # Discord embed builders
│       └── choices.py         # slash-command choice lists
├── docker/
│   ├── Dockerfile             # multi-stage, non-root, healthcheck
│   ├── docker-compose.yml     # bot service + appdata volume
│   ├── healthcheck.py         # stdlib-only container healthcheck
│   └── aurorabot.xml          # Unraid Community Applications template
├── .github/workflows/
│   └── docker-publish.yml     # build + push image to GHCR
├── requirements.txt
├── .env.example
└── README.md
```

---

## 🔑 Prerequisites

1. **Discord bot token** — create an application at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   add a **Bot**, and copy its token. Under **Bot → Privileged Gateway
   Intents**, no privileged intents are required (AuroraBot is slash-command
   only). Invite it with the `applications.commands` and `bot` scopes and the
   *Send Messages* / *Embed Links* permissions.
2. **PandaScore API key** — sign up at [pandascore.co](https://pandascore.co)
   and grab your key from the dashboard.

---

## 💻 Local development

```bash
git clone https://github.com/YOUR_GITHUB_USER/aurorabot.git
cd aurorabot

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then edit .env with your token + API key
# Tip: set DEV_GUILD_IDS to your test server ID for instant command sync.

python -m src.bot
```

The bot logs in, syncs its slash commands and starts the health server on
`http://localhost:8080/health`.

---
1. In Unraid Docker:
   - Pull ghcr.io/david4td/aurorabot:latest

1. **Create fields** in the Unraid GUI:
   - **Variable DISCORD_TOKEN** — paste your token (field is password-masked).
   - **Varible ESPORTS_API_KEY** — paste your PandaScore API Key (password-masked).
   - **AppData** — leave as `/mnt/user/appdata/aurorabot` (creates on first run).
   - Advanced fields (log level, poll interval, dev guild IDs) are optional.

2. Click **Apply**. Unraid pulls the image and starts the container with
   `restart: unless-stopped`, so it comes back automatically after a reboot.

> **Community Apps note:** if you host the template in a GitHub repo and add it
> to CA's template repositories, it becomes installable directly from the
> **Apps** tab. Otherwise the manual template copy above works immediately.

### Option B — docker-compose (Unraid Compose Manager plugin)

1. Install the **Compose Manager** plugin from Community Apps.
2. Create a new stack, paste in `docker/docker-compose.yml`, and switch the
   service from `build:` to the published image line:
   `image: ghcr.io/YOUR_GITHUB_USER/aurorabot:latest`.
3. Place your `.env` next to the compose file (or inline the env vars).
4. **Compose Up**.

### Where data & logs live

Everything persistent is on the appdata volume:

```
/mnt/user/appdata/aurorabot/
├── aurorabot.db        # SQLite database (users, follows, predictions, alerts)
├── aurorabot.db-wal    # write-ahead log
└── aurorabot.db-shm
```

Container logs are viewable from the Unraid Docker tab (click the container →
**Logs**) or `docker logs aurorabot`.

### Updating the container

When a new image is pushed to GHCR:

- **Unraid GUI:** Docker tab → click **AuroraBot** → **Force Update**
  (or toggle *Advanced View* and hit the update icon). Unraid pulls the new
  image and recreates the container; your appdata volume is untouched.
- **Command line:**
  ```bash
  docker pull ghcr.io/YOUR_GITHUB_USER/aurorabot:latest
  docker compose -f docker/docker-compose.yml up -d
  ```

Because the database lives on the mounted volume, updates never lose data.

---

## ⚙️ Configuration reference

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `DISCORD_TOKEN` | ✅ | — | Discord bot token |
| `ESPORTS_API_KEY` | ✅ | — | PandaScore API key |
| `DATABASE_PATH` | | `/app/data/aurorabot.db` | SQLite file path (inside container) |
| `DEV_GUILD_IDS` | | _(empty)_ | Comma-separated guild IDs for instant command sync |
| `ALERT_POLL_SECONDS` | | `60` | Live-match poll interval |
| `HEALTH_PORT` | | `8080` | Internal health server port |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PANDASCORE_CS_SLUG` | | `cs-go` | PandaScore videogame slug for Counter-Strike |

---

## 🗄️ Database

SQLite via `aiosqlite`, chosen so a single Unraid container needs no separate
database service. The schema (`src/database/schema.sql`) is applied idempotently
on every startup — safe across restarts and updates. Tables: `users`,
`followed_teams`, `alert_subscriptions`, `alerted_matches`, `predictions`.

Prefer Postgres? A commented-out service and guidance are in
`docker/docker-compose.yml`.

---

## 🩺 Health & reliability

- `GET /health` returns `200` when the gateway is connected and the alert loop
  has run recently, else `503`. Docker's `HEALTHCHECK` uses it.
- Background loops (alerts, prediction resolution) swallow and log exceptions so
  a transient PandaScore hiccup never kills the bot.
- Graceful shutdown on `SIGTERM` (Docker stop) closes the DB and HTTP sessions
  cleanly.

---

## 📝 Notes

- PandaScore videogame slugs occasionally change. If a game's commands stop
  returning data, check the [PandaScore docs](https://developers.pandascore.co)
  and adjust the slug in `src/utils/games.py` (or `PANDASCORE_CS_SLUG` for CS).
- Standings tables aren't available for every tournament (bracket-only events
  won't have them) — the bot tells the user when that's the case.

