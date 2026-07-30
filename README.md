# 🌌 AuroraBot

A production-ready **Discord bot for eSports fans**. Follow live scores,
standings, deep team analytics and real-time match alerts — and challenge your
friends with match predictions and a server leaderboard.

Everything AuroraBot surfaces is limited to **Tier 1** tournaments, so your
channels stay signal, not noise.

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
| **Standings** | `/standings <league>` — picker of that league's **current** splits |
| **Analytics** | `/team <name>` — recent form, win rate, roster, deep-stats links |
| **Profiles** | `/profile`, `/follow`, `/unfollow`, `/setgame` |
| **Predictions** | `/predict`, `/mypredictions` — earn points for correct calls |
| **Leaderboard** | `/leaderboard` — top predictors in your server |
| **Alerts** | `/alerts add`, `/alerts list`, `/alerts remove` — by team, by tournament, or a whole game |
| **Games** | `/games` — toggle which titles this server follows |
| **Meta** | `/help`, `/ping` |

Predictions resolve automatically: a background task checks finished matches
every 5 minutes and awards points. Alerts poll PandaScore on a configurable
interval (default 60s).

### 🏆 Tier 1 only

Scores, alerts, predictions, standings and team form all pass through a tier
filter. Unknown/ungraded tiers are excluded rather than let through, so the feed
never quietly fills up with third-tier qualifiers.

**PandaScore's tier scale is narrower than the scene's.** They grade
`S > A > B > C > D > unranked`, and reserve `S` for the majors — so filtering on
`S` alone would drop every regional league:

| Grade | Examples |
|-------|----------|
| `S` | Worlds, MSI, The International, CS Majors / IEM / BLAST, VCT Masters |
| `A` | **LEC, LCK, LCS, LPL**, VCT EMEA / NA, ESL Pro League, DPC Division 1 |
| `B`–`D` | Regional qualifiers, academy and development circuits |

"Tier 1" in the sense fans mean it is therefore **S + A**, which is the default
(`TIERS=s,a`). Narrow it to majors only with `TIERS=s`, widen it with
`TIERS=s,a,b`, or turn the filter off entirely with `TOP_TIER_ONLY=false`.

Tier lives on the *tournament* object only — not on series or leagues — and
PandaScore exposes no server-side tier filter on the match endpoints, so
AuroraBot over-fetches and filters client-side in `src/utils/tiers.py`.

> Sources: [Tournaments in-depth](https://developers.pandascore.co/docs/tournaments-in-depth)

### 🏳️ Team logos (inline icons)

Team logos appear **inline** — in the match line, standings rows, the winner
buttons on `/predict`, and the `/follow` picker.

Discord only renders an image inside text if it is a **custom emoji**, so each
team's `image_url` is uploaded once as an *application emoji* (owned by the bot,
usable in every server, 2000 cap) and referenced by id thereafter. This needs
**discord.py ≥ 2.5**, hence the 2.7.1 pin.

How it behaves, by design:

- **Rendering never waits on the network.** A cache miss returns nothing and
  queues the team; a background worker mints the emoji, so the *next* render
  shows it. The first `/standings` for a new league is therefore mostly
  flagless and fills in within seconds — it is warming, not broken.
- **Uploads are throttled** (15/minute) because one command can reference 25
  unknown teams. A `429` pauses minting for five minutes.
- **The cap is respected.** Nearing 2000, least-recently-used icons are deleted
  so long-tail teams don't squat.
- **Logos are downloaded whole.** `StreamReader.read(n)` reads *up to* n bytes
  and returns as soon as anything is available, so it yielded only the first
  ~16 KB — a truncated PNG that Discord then refused with `50046 Invalid
  Asset`. Logos under one chunk arrived intact and worked, which made it look
  convincingly like a size limit; it never was. The body is now accumulated via
  `iter_chunked`, and a length mismatch against `Content-Length` is treated as a
  failed transfer rather than uploaded.
- **Every logo is re-encoded before upload.** Pillow renders each one as a
  square 128px PNG, stepping down through 96/64/48px with palette quantization
  if needed to stay small. Wide wordmarks are padded onto a transparent square
  rather than stretched. Real logos land at 1.3–11.5 KB from 4.9–89 KB sources.
- **Emojis outlive the database.** They belong to the *application*, not to
  appdata, so a recreated volume leaves them orphaned. Startup indexes owned
  emojis by name and minting adopts a match instead of creating a duplicate
  (`50035`). When a team rebrands, the old emoji is deleted *before* the
  replacement is created, since names must be unique and there is no endpoint
  to swap an emoji's image.
- **Failure is invisible, and remembered.** A dead URL, an SVG or a revoked
  permission leaves plain text, and the standings table falls back to the
  team's country flag. After two failed attempts a team is given up on, so a
  logo Discord refuses can't be re-queued by every render.
- **Restarts are free.** The `team_emojis` table maps team → emoji, and startup
  reconciles it against the emojis actually owned, dropping stale rows.
- **Blobs are cached on the appdata volume.** Normalised PNGs land in
  `/app/data/logos/` (`/mnt/user/appdata/aurorabot/logos` on Unraid) as
  `{team_id}-{url hash}.png`. A logo is fetched from PandaScore once and never
  again — the `team_emojis` row short-circuits every render, and if an emoji ever
  has to be re-created (LRU eviction, a manual deletion in the developer portal,
  a database restored without its emojis) the bytes come off disk instead of the
  CDN. Writes are atomic, unreadable files are treated as misses so a partial
  write self-heals, superseded versions are pruned on rebrand, and an
  unwritable directory just disables the cache. Budget roughly 10 KB per team.

> Emoji names are `{team}_{id}` (e.g. `t1_11`), so the list stays readable in
> the Discord developer portal.

**Layout note:** the matchup sits in the embed **description**, not the title.
Discord does not render custom emoji in embed titles — they leak as raw
`<:name:id>`, worst of all on mobile. The title carries the state and event,
which is why region flags (plain unicode) work there but logos don't.

### 🌍 Region flags

Events, tournaments and teams carry a region flag — 🇰🇷 LCK, 🇪🇺 LEC, 🇨🇳 LPL,
🌏 VCT Pacific, 🌍 Worlds — in match embeds, the standings title and table, the
league and split pickers, the `/alerts add` autocomplete and the `/predict`
match list.

**Teams** use PandaScore's own `location` field (an ISO-3166 alpha-2 code),
converted arithmetically to a flag, so any country works.

**Events don't**: PandaScore exposes *no* region or country field on leagues or
tournaments (v2.53 — League is id/name/slug/url/image_url/series/videogame;
Tournament adds tier, dates and rosters). So `src/utils/regions.py` derives it
in three passes:

1. an exact match on the league name in `LEAGUE_REGIONS` — accurate, and small
   because only Tier 1 events are ever shown;
2. a keyword scan for region words ("EMEA", "Pacific", "Korea", "Major"),
   catching leagues absent from the table;
3. no flag, rather than a wrong one.

Multi-country regions use a symbol instead of pretending to be one nation:
🇪🇺 EMEA, 🌎 Americas, 🌏 Asia-Pacific, 🌍 International.

**Adding or fixing a league is a one-line edit** to `LEAGUE_REGIONS` in
`src/utils/regions.py`:

```python
"lck": "kr",
"vct pacific": "pacific",
```

### 🎮 Per-server game toggles

`/games` shows which of the ten titles this server follows. Anyone can look;
members with **Manage Server** also get a multi-select — everything selected is
followed, everything cleared is muted, applied in one go. A **Follow all games**
button undoes the lot.

Muting a game:

- removes it from the unfiltered `/live`, `/upcoming` and `/results`, and
- silences the alert poll for it, so muted games can't ping the server (and
  cost no API call).

Asking for it explicitly still works — `/live game:Dota 2` on a server that
muted Dota replies "**Dota 2** is muted on this server" rather than pretending
there's nothing on. The toggle curates the firehose; it doesn't hide the
catalogue.

Existing `/alerts` subscriptions for a muted game are **kept, not deleted** —
they simply go quiet, and resume if you switch the game back on. Only muted
games are stored, so a server that never runs `/games` follows everything, and
titles added to the catalogue later default to on.

Settings are per Discord server; running the bot in several servers gives each
its own list.

### 🔔 Alerts, reminders & reaction predictions

`/alerts add` subscribes the current channel, scoped three ways:

| Command | Alerts on |
|---------|-----------|
| `/alerts add game:Valorant team:Sentinels` | every Sentinels match in that game |
| `/alerts add game:LoL tournament:…` | one tournament (autocompletes to **current** Tier 1 events) |
| `/alerts add game:CS2` | every Tier 1 CS2 match |

Each subscribed match produces **two** pings:

1. a **reminder** `ALERT_LEAD_MINUTES` before kick-off (default **30 min**), and
2. a **live** alert the moment the match starts.

The reminder carries 1️⃣ / 2️⃣ reactions — react to predict that team as the
winner, no slash command needed. You can swap your pick right up until kick-off;
once the match starts the pick locks and AuroraBot confirms by DM (falling back
to a self-deleting channel message if your DMs are closed). Reactions are
handled as raw gateway events, so they keep working on alerts posted before the
last bot restart.

Each (match, state, channel) is announced at most once, so restarts mid-window
never double-post.

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
│   │   ├── emojis.py          # team logo → application emoji cache
│   │   └── health.py          # lightweight aiohttp /health server
│   ├── cogs/                  # one module per feature area
│   │   ├── meta.py  scores.py  standings.py  analytics.py
│   │   ├── profiles.py  predictions.py  leaderboard.py  alerts.py
│   │   └── games.py           # /games toggle panel
│   └── utils/
│       ├── games.py           # game ↔ PandaScore slug mapping
│       ├── guildgames.py      # enforcement of the per-server toggles
│       ├── regions.py         # region flags for leagues/events/teams
│       ├── tiers.py           # Tier 1 (S/A grade) filtering
│       ├── tournaments.py     # "is this tournament current?" + labels
│       ├── matches.py         # opponent / tournament readers for payloads
│       ├── predictions.py     # shared stake, reward and lock-in rules
│       ├── embeds.py          # Discord embed builders
│       └── choices.py         # slash-command choice lists
├── docker/
│   ├── Dockerfile             # multi-stage, non-root, healthcheck
│   ├── docker-compose.yml     # bot service + appdata volume
│   ├── healthcheck.py         # stdlib-only container healthcheck
│   ├── aurorabot.xml          # Unraid Community Applications template
│   ├── icon.png               # container icon used by the template
│   └── make_icon.py           # regenerates icon.png (stdlib only)
├── .github/workflows/
│   └── docker-publish.yml     # build + push image to GHCR
├── requirements.txt
├── .env.example
├── LICENSE
└── README.md
```

### 🎨 Regenerating the icon

`docker/icon.png` is committed, but it's generated rather than hand-drawn — no
Pillow or design tool required:

```bash
python docker/make_icon.py --size 256
```

The Unraid template points `<Icon>` at
`https://raw.githubusercontent.com/YOUR_GITHUB_USER/aurorabot/main/docker/icon.png`,
so the icon appears in the Docker tab once the repo is public.

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
├── aurorabot.db-shm
└── logos/              # cached team logo PNGs, ~10 KB each
    ├── 318-4f2a1c9b8e01.png
    └── 2574-9c1d33ef7a20.png
```

`logos/` is a pure cache — deleting it costs one re-download per team, nothing
more.

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
| `ALERT_POLL_SECONDS` | | `60` | Match poll interval |
| `ALERT_LEAD_MINUTES` | | `30` | Minutes before kick-off to post the reminder |
| `TOP_TIER_ONLY` | | `true` | Restrict everything to top-tier tournaments |
| `TIERS` | | `s,a` | PandaScore tier grades counting as Tier 1 (see above) |
| `HEALTH_PORT` | | `8080` | Internal health server port |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PANDASCORE_CS_SLUG` | | `cs-go` | PandaScore videogame slug for Counter-Strike |

---

## 🗄️ Database

SQLite via `aiosqlite`, chosen so a single Unraid container needs no separate
database service. The schema (`src/database/schema.sql`) is applied idempotently
on every startup — safe across restarts and updates. Tables: `users`,
`followed_teams`, `guild_games`, `team_emojis`, `alert_subscriptions`,
`alerted_matches`, `alert_messages`, `predictions`.

Column changes are handled by `Database._migrate()`, which runs before the
schema script and is guarded by `PRAGMA table_info` checks. Upgrading from an
older AuroraBot converts existing alert subscriptions in place — team
subscriptions become `scope='team'`, whole-game ones `scope='game'` — so **no
data is lost and no manual step is needed**; just pull the new image.

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
- Empty feeds are usually the tier filter doing its job, not a bug: outside
  major-league hours there genuinely are no Tier 1 matches. `/standings` names
  the tournaments it filtered out and their grades, so you can see whether the
  bar is set too high — widen `TIERS` or set `TOP_TIER_ONLY=false` to confirm.
  `LOG_LEVEL=DEBUG` logs the tournament counts at each filter step.
- Alert subscriptions are per **channel**, and `/alerts` requires the *Manage
  Server* permission. The bot needs *Add Reactions* in the alert channel for
  reaction predictions; without it the reminder still posts, and a warning is
  logged.

---

## 📄 License

MIT — see [LICENSE](LICENSE).
