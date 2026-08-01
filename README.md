# 🌌 AuroraBot

A Discord bot for eSports fans: live scores, standings, lineups, match alerts and
a prediction leaderboard — limited to **Tier 1** tournaments so your channels stay
signal, not noise.

Covers **League of Legends, Counter-Strike 2, Valorant, Dota 2, Rocket League,
Rainbow Six, Call of Duty, Overwatch, PUBG, Mobile Legends** and more, powered by
the [PandaScore API](https://pandascore.co).

---

## 🚀 Start here

**Games are opt-in.** A new server follows nothing until someone with *Manage
Server* runs `/games` and picks some.

## ✨ Commands

| Area | Commands |
|------|----------|
| **Games** | `/games` — pick which titles this server follows (**do this first**) |
| **Live scores** | `/live`, `/upcoming`, `/results` |
| **Standings** | `/standings <league>` — current splits only |
| **Analytics** | `/team <name>` — form, win rate, roster, stats links |
| **Lineups** | `/lineup` — who's playing an upcoming match, by role |
| **Alerts** | `/alerts add`, `/alerts list`, `/alerts remove` |
| **Schedule** | `/schedule` — post today's matches now |
| **Predictions** | `/predict`, `/mypredictions`, `/leaderboard` |
| **Profile** | `/profile`, `/follow`, `/unfollow`, `/setgame` |
| **Meta** | `/help`, `/ping` |

Most commands filter **game → tournament → team**, each step optional after the
first:

```
/live game:LoL                            all Tier 1 LoL
/live game:LoL tournament:LCK             that league
/live game:LoL tournament:LCK team:T1     one team in it
```

Set a default game once with `/setgame` and a bare `/live` works.

## 🔔 Alerts

`/alerts add` subscribes the current channel — to a team, a league, a single
stage, or a whole game. Every match then gets a **reminder 30 minutes before
kick-off** and a **live ping** at kick-off. The reminder carries a button per
team: tap one to predict the winner. Only you see the reply, and you can change
your pick until kick-off.

The tournament picker shows anything **running now or starting within two
months**, so you can subscribe well before an event begins. Picking a whole
league covers every stage it adds later.

`/alerts list` shows what a channel is subscribed to. `/alerts remove` lets you
tick the ones to drop — there's also *Remove all*, which asks twice. Both take an
optional `game:` filter. Alerts need *Manage Server*.

When a match **goes live**, the alert lists who backed which team, with the
split (`3/5`). Predictions have closed by then, so nothing is spoiled.

## 📅 Daily schedule

League and tournament subscriptions get a message at local midnight listing the
day's matches, each with its own pair of team buttons for predictions. Busy days
run across a few messages. `/schedule` posts today's immediately.

The first message also carries a **server leaderboard** (W/L and win rate) and
the last few settled predictions with who called them right. Both count only
picks made in that server — `/leaderboard` is the global one.

## 🏆 What "Tier 1" means

PandaScore reserves its top `S` grade for the majors, so AuroraBot counts **`S`
and `A`** — otherwise every regional league would be filtered out.

| Grade | Examples |
|-------|----------|
| `S` | Worlds, MSI, The International, CS Majors, VCT Masters |
| `A` | LEC, LCK, LCS, LPL, VCT EMEA / NA, ESL Pro League |
| `B`–`D` | Qualifiers, academy and development circuits |

An empty feed is usually the filter doing its job. `TIERS=s,a` is the default;
`TOP_TIER_ONLY=false` turns filtering off.

## 🏳️ Logos and flags

Teams show their crest and events show a region flag. Icons warm up in the
background, so the first view of a new league is plain and fills in over a few
seconds.

Lineup cards show each team's **current roster** — PandaScore doesn't publish
confirmed starters, so substitutes may appear.

---

## 🔑 Setup

You need a **Discord bot token**
([Developer Portal](https://discord.com/developers/applications) → your app →
Bot; no privileged intents) and a **PandaScore API key**
([pandascore.co](https://pandascore.co)).

Invite the bot with the `bot` and `applications.commands` scopes, plus *Send
Messages* and *Embed Links*.

### Unraid

1. Pull `ghcr.io/david4td/aurorabot:latest`.
2. Add two **Variables**: `DISCORD_TOKEN` and `ESPORTS_API_KEY`.
3. Add a **Path** (⚠️ Config Type must be *Path*, not *Variable*):

   | Field | Value |
   |---|---|
   | Container Path | `/appdata` |
   | Host Path | `/mnt/user/appdata/aurorabot` |
   | Access Mode | Read/Write |

> **Don't skip the Path mapping.** Without it the bot still runs but loses all
> data — alerts, predictions, everything — on every Force Update. If it instead
> crashes with `unable to open database file`, the host folder is root-owned:
> ```bash
> chown -R 1000:1000 /mnt/user/appdata/aurorabot
> ```

**Compose alternative:** use `docker/docker-compose.yml` with
`image: ghcr.io/david4td/aurorabot:latest` and an `.env` alongside it.

**Updating:** Docker tab → **AuroraBot** → **Force Update**.

### Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # add your token + API key
python -m src.bot
```

Set `DEV_GUILD_IDS` to your test server for instant command sync.

## ⚙️ Configuration

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `DISCORD_TOKEN` | ✅ | — | Discord bot token |
| `ESPORTS_API_KEY` | ✅ | — | PandaScore API key |
| `DATABASE_PATH` | | `/appdata/aurorabot.db` | Database location |
| `DEV_GUILD_IDS` | | — | Guild IDs for instant command sync |
| `ALERT_LEAD_MINUTES` | | `30` | Minutes before kick-off to remind |
| `ALERT_POLL_SECONDS` | | `60` | How often to check for matches |
| `TOP_TIER_ONLY` | | `true` | Restrict to top-tier tournaments |
| `TIERS` | | `s,a` | Grades counting as Tier 1 |
| `DIGEST_TZ` | | `Australia/Sydney` | Timezone for "today" |
| `DIGEST_HOUR` | | `0` | Hour the daily schedule posts |
| `HEALTH_PORT` | | `8080` | Health server port |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PANDASCORE_CS_SLUG` | | `csgo` | Route segment for Counter-Strike |

## 📝 Troubleshooting

- **Nothing happens at all** — run `/games` first.
- **Empty feeds** — usually the tier filter; try `TOP_TIER_ONLY=false` to confirm.
- **No standings** — bracket-only stages have no table; try the group stage.
- **New commands missing** — global sync takes up to an hour; set `DEV_GUILD_IDS`.
- **Commands appear twice** — restart with `DEV_GUILD_IDS` set and the bot clears
  the duplicates.
- **Alerts reset on update** — the `/appdata` Path mapping is missing (see above).

`GET /health` returns `200` when everything's running. `LOG_LEVEL=DEBUG` is safe
to use.

## 📄 License

MIT — see [LICENSE](LICENSE).
