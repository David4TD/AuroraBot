# 🌌 AuroraBot

A Discord bot for eSports fans: live scores, standings, team analytics, match
alerts and a prediction leaderboard — limited to **Tier 1** tournaments so your
channels stay signal, not noise.

Covers **League of Legends, Counter-Strike 2, Valorant, Dota 2, Rocket League,
Rainbow Six, Call of Duty, Overwatch, PUBG, Mobile Legends** and more, powered by
the [PandaScore API](https://pandascore.co).

Built with **discord.py 2.x**, containerised for one-click **Unraid** deployment.

---

## ✨ Commands

| Area | Commands |
|------|----------|
| **Live scores** | `/live`, `/upcoming`, `/results` |
| **Standings** | `/standings <league>` — current splits only |
| **Analytics** | `/team <name>` — form, win rate, roster, deep-stats links |
| **Profile** | `/profile`, `/follow`, `/unfollow`, `/setgame` |
| **Predictions** | `/predict`, `/mypredictions`, `/leaderboard` |
| **Alerts** | `/alerts add`, `/alerts list`, `/alerts remove` |
| **Schedule** | `/schedule` — post today's matches now |
| **Games** | `/games` — pick which titles this server follows |
| **Meta** | `/help`, `/ping` |

---

## 🔔 Alerts

`/alerts add` subscribes the current channel. Scope it four ways:

```
/alerts add game:LoL                        every Tier 1 LoL match
/alerts add game:LoL team:G2 Esports        one team
/alerts add game:LoL tournament:LCK         a whole league, all stages
/alerts add game:LoL tournament:LCK · …     one specific stage
```

The `tournament` field autocompletes. **Whole leagues are listed first**, because
a league's stages are separate tournaments that often run in parallel — LCK
splits into a Legend Group and a Rise Group — so picking one stage would miss the
other. Pick `LCK` to follow everything, or a `↳` entry for a single stage.

Each subscribed match produces two pings: a **reminder** 30 minutes before
kick-off (`ALERT_LEAD_MINUTES`) and a **live** alert at kick-off. React 1️⃣ / 2️⃣
on the reminder to predict a winner — you can switch until kick-off, then it
locks. Confirmation arrives by DM.

## 📅 Daily schedule

League and tournament subscriptions also get one message at local midnight
listing the day's matches, with a dropdown for predicting winners. Picking a
match replies privately, so voting doesn't clutter the channel.

`/schedule` posts the current day immediately. Both need *Manage Server*.

## 🎮 Per-server games

`/games` shows which titles this server follows; members with *Manage Server*
get a multi-select to change it. Muting a game removes it from the unfiltered
feeds and silences its alerts. Asking for it explicitly still answers, saying
it's muted. Existing subscriptions are kept and resume when you re-enable it.

## 🏆 What "Tier 1" means

PandaScore grades tournaments `S > A > B > C > D`, and reserves `S` for the
majors — so **filtering on `S` alone drops every regional league**:

| Grade | Examples |
|-------|----------|
| `S` | Worlds, MSI, The International, CS Majors / IEM / BLAST, VCT Masters |
| `A` | **LEC, LCK, LCS, LPL**, VCT EMEA / NA, ESL Pro League |
| `B`–`D` | Qualifiers, academy and development circuits |

The default is therefore `TIERS=s,a`. Use `TIERS=s` for majors only, `s,a,b` to
widen, or `TOP_TIER_ONLY=false` to disable filtering.

If a feed looks empty, that's usually the filter working — `/standings` names
what it filtered out and at what grade.

## 🏳️ Flags and logos

Teams show their crest inline (uploaded once as an application emoji) and events
show a region flag — 🇰🇷 LCK, 🇪🇺 LEC, 🌏 VCT Pacific, 🌍 Worlds.

Icons **warm up**: the first view of a new league is mostly plain and fills in
over a few seconds. That's expected, not a fault — rendering never waits on the
network. Anything that fails falls back to plain text.

PandaScore exposes no region field, so event flags come from a table in
`src/utils/regions.py`. Fixing or adding a league is a one-line edit:

```python
"lck": "kr",
```

### Deep-stats links
Analytics embeds link out to [gol.gg](https://gol.gg) (LoL),
[vlr.gg](https://vlr.gg) (Valorant) and [hltv.org](https://hltv.org) (CS2).

---

## 🔑 Prerequisites

1. **Discord bot token** — [Developer Portal](https://discord.com/developers/applications)
   → your app → Bot. No privileged intents needed. Invite with the
   `applications.commands` and `bot` scopes, plus *Send Messages*, *Embed Links*
   and *Add Reactions*.
2. **PandaScore API key** — from the [pandascore.co](https://pandascore.co) dashboard.

## 💻 Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your token + API key
python -m src.bot
```

Set `DEV_GUILD_IDS` to your test server for instant command sync. Health server
runs on `http://localhost:8080/health`.

## 🖥️ Unraid

1. In Unraid Docker, pull `ghcr.io/david4td/aurorabot:latest`.

2. **Create fields** in the GUI:
   - **Variable** `DISCORD_TOKEN` — your token (password-masked)
   - **Variable** `ESPORTS_API_KEY` — your PandaScore key (password-masked)
   - **Path** (⚠️ not a Variable — easy to miss):

     | Field | Value |
     |---|---|
     | Config Type | **Path** |
     | Container Path | `/appdata` |
     | Host Path | `/mnt/user/appdata/aurorabot` |
     | Access Mode | Read/Write |

   - Everything else is optional (see the config table below).

   > **Skip the Path mapping and the bot still runs — but silently loses all
   > data on every Force Update.** The image declares `VOLUME ["/appdata"]`, so
   > Docker creates an *anonymous* volume rather than failing: nothing appears
   > on your share, and each container recreate starts from an empty database.
   > Verify with:
   > ```bash
   > docker inspect aurorabot --format '{{json .Mounts}}'
   > ```
   > You want `"Type":"bind"` with `"Source":"/mnt/user/appdata/aurorabot"`. A
   > `"Type":"volume"` with a hex name means the mapping is missing.
   >
   > If the host directory is root-owned the bot will crash with
   > `unable to open database file` — it runs as uid 1000:
   > ```bash
   > chown -R 1000:1000 /mnt/user/appdata/aurorabot
   > ```

3. **Apply.** `restart: unless-stopped`, so it survives reboots.

**Compose alternative:** install the Compose Manager plugin, paste
`docker/docker-compose.yml`, swap `build:` for
`image: ghcr.io/david4td/aurorabot:latest`, and place your `.env` alongside it.

### Data on disk

```
/mnt/user/appdata/aurorabot/
├── aurorabot.db        # users, follows, predictions, alerts, digests
├── aurorabot.db-wal
├── aurorabot.db-shm
└── logos/              # cached team logos, ~10 KB each
```

`logos/` is a pure cache — deleting it costs one re-download per team. The
database survives image updates; the schema is applied idempotently on startup
and column changes are migrated automatically.

### Updating

Docker tab → **AuroraBot** → **Force Update**. Or:

```bash
docker pull ghcr.io/david4td/aurorabot:latest
docker compose -f docker/docker-compose.yml up -d
```

---

## ⚙️ Configuration

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `DISCORD_TOKEN` | ✅ | — | Discord bot token |
| `ESPORTS_API_KEY` | ✅ | — | PandaScore API key |
| `DATABASE_PATH` | | `/appdata/aurorabot.db` | SQLite path inside the container |
| `DEV_GUILD_IDS` | | — | Comma-separated guild IDs for instant command sync |
| `ALERT_POLL_SECONDS` | | `60` | Match poll interval |
| `ALERT_LEAD_MINUTES` | | `30` | Minutes before kick-off to post the reminder |
| `TOP_TIER_ONLY` | | `true` | Restrict to top-tier tournaments |
| `TIERS` | | `s,a` | Tier grades counting as Tier 1 |
| `DIGEST_TZ` | | `Australia/Sydney` | Timezone the daily schedule treats as "today" |
| `DIGEST_HOUR` | | `0` | Local hour the digest posts |
| `HEALTH_PORT` | | `8080` | Internal health server port |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PANDASCORE_CS_SLUG` | | `cs-go` | PandaScore slug for Counter-Strike |

---

## 📁 Layout

```
src/
├── bot.py              # entrypoint: config, DB, API, cogs, health
├── config.py           # env-driven settings
├── database/           # aiosqlite layer + idempotent schema
├── services/
│   ├── pandascore.py   # async API client
│   ├── emojis.py       # team logo → application emoji cache
│   └── health.py       # /health server
├── cogs/               # one module per feature area
└── utils/              # tiers, regions, schedule, embeds, game catalogue
docker/                 # Dockerfile, compose, Unraid template, icon
```

Icon regenerates with `python docker/make_icon.py` (stdlib only, no Pillow).

## 🩺 Health

`GET /health` returns `200` when the gateway is connected and the alert loop has
run recently, else `503`; Docker's `HEALTHCHECK` uses it. Background loops log
and swallow exceptions so a PandaScore hiccup never kills the bot, and `SIGTERM`
shuts down cleanly.

## 📝 Troubleshooting

- **Empty feeds** — usually the tier filter. Try `TOP_TIER_ONLY=false` to confirm.
- **No standings** — bracket-only stages have no table; try the group stage.
- **Commands appear twice** — a leftover global command set alongside a guild
  one. Restart with `DEV_GUILD_IDS` set; the bot clears the duplicates.
- **New commands missing** — global sync can take up to an hour. Use
  `DEV_GUILD_IDS` for instant sync.
- **Alerts reset on update** — the `/appdata` Path mapping is missing (see above).
- **PandaScore slugs change occasionally.** If one game stops returning data,
  check the [docs](https://developers.pandascore.co) and adjust
  `src/utils/games.py` (or `PANDASCORE_CS_SLUG`).

`LOG_LEVEL=DEBUG` is usable — noisy third-party loggers are pinned down.

## 📄 License

MIT — see [LICENSE](LICENSE).
