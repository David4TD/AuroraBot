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
| **Live scores** | `/live`, `/upcoming` (with lineups), `/results` |
| **Standings** | `/standings <league>` — current splits only |
| **Analytics** | `/team <name>` — form, win rate, roster, stats links |
| **Lineups** | `/lineup` — who's playing an upcoming match, by role |
| **Alerts** | `/alerts add`, `/alerts list`, `/alerts remove` |
| **Schedule** | `/schedule` — post today's matches now |
| **Predictions** | `/predict`, `/mypredictions`, `/leaderboard` |
| **Profile** | `/profile`, `/follow`, `/unfollow`, `/setgame` |
| **Settings** | `/settings` — server preferences and resets |
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

League and tournament subscriptions get a message at local midnight with a
**lineup card per match** — the same card `/lineup` shows — each with its own
pair of team buttons for predictions. Busy days run across a few messages.
`/schedule` posts today's immediately.

The first message also carries the **server leaderboard** and the last few
settled predictions with who called them right.

## 🎲 Predictions

Pick a winner with `/predict`, or tap a team button on a match reminder or the
daily schedule. You can change your mind until kick-off, then it locks.

```
points = stake × underdog × streak
```

**Underdog.** Priced off your own server's votes, not a bookmaker. If eight
people back the favourite and two call the upset, the upset pays **3×** and the
favourite pays **1.25×**. Capped at 3×, and it stays flat until at least three
people have voted — two people disagreeing isn't a market.

**Streak.** +10% per consecutive correct call before this one, up to **1.5×**.
A miss resets it.

**Stakes.** Every pick is staked from a **500-point budget per tournament**.
The default is 10, and the ephemeral confirmation lets you raise it to 25 or 50 —
so one tap is still a complete play, and picking *which* matches to commit to is
the actual skill. Run the budget dry and picks still count at a free 5; nobody
gets locked out.

**Perfect day.** Call every one of a day's matches right (two or more) for a
**+50** bonus.

Boards are **per tournament, per server**. `/leaderboard` shows whatever the
channel follows; `/leaderboard tournament:…` picks another, including all-time.
`/profile` shows your record here, with your lifetime total in the footer.

When a match goes live the alert shows the split *and* the multiplier each side
was playing for.

## ⚙️ Per-server settings

`/settings` shows what this server is configured to do and lets *Manage Server*
change it:

- **Daily schedule time** — the hour and the timezone the digest treats as
  "today". These used to be bot-wide, so every server got the operator's
  midnight; now each server picks its own.
- **Reminder lead** — how long before kick-off alerts fire.

Anything you haven't set shows *(default)* and follows the deployment's
configuration, so an untouched server behaves exactly as before.

### Resetting

The same panel has **Reset**, scoped to the current server: alerts, games,
predictions, preferences, or everything. It names the exact number of rows it
will delete and asks a second time — there's no undo.

Nobody's prediction history in *other* servers is touched, and lifetime points
survive a reset.

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

## 🧑‍🤝‍🧑 Lineup cards

`/lineup`, `/upcoming` and the daily digest all render the same pre-match card:
both teams side by side, one row per lane so the columns read across, with
nationality flags and kick-off time.

`/upcoming` cards the soonest **three** matches and lists the rest as
one-liners — a card is several times taller than a scoreline. `/live` and
`/results` keep the compact format, since there a score is what you're after.

Cards show each team's **current roster**, ordered by position — Top, Jungle,
Mid, Bot, Support for LoL; Carry through Hard support for Dota; Tank/DPS/Support
for Overwatch. CS, Valorant and R6 have no position data, so those list names
only.

PandaScore publishes no starting lineup and marks every rostered player active,
so the card doesn't guess: a squad carrying seven players shows all seven rather
than labelling anyone a substitute.

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
| `ALERT_LEAD_MINUTES` | | `30` | Default reminder lead (per-server via `/settings`) |
| `ALERT_POLL_SECONDS` | | `60` | How often to check for matches |
| `TOP_TIER_ONLY` | | `true` | Restrict to top-tier tournaments |
| `TIERS` | | `s,a` | Grades counting as Tier 1 |
| `DIGEST_TZ` | | `Australia/Sydney` | Default timezone (per-server via `/settings`) |
| `DIGEST_HOUR` | | `0` | Default digest hour (per-server via `/settings`) |
| `HEALTH_PORT` | | `8080` | Health server port |
| `LOG_LEVEL` | | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `PANDASCORE_CS_SLUG` | | `csgo` | Route segment for Counter-Strike |

## 📝 Troubleshooting

- **Nothing happens at all** — run `/games` first.
- **Empty feeds** — usually the tier filter; try `TOP_TIER_ONLY=false` to confirm.
- **No standings** — bracket-only stages have no table; try the group stage.
- **New commands missing** — global sync takes up to an hour; set `DEV_GUILD_IDS`.
- **Commands appear twice, or old ones linger** — Discord stores slash commands
  on its side, per scope, so they stay in the picker even with the container
  stopped. A global sync never touches guild-scoped registrations, so a bot that
  once ran with `DEV_GUILD_IDS` and later without it left a duplicate set behind.
  Startup now clears those automatically; to inspect or purge without starting
  the bot:
  ```bash
  python scripts/purge_commands.py
  ```
  It lists what Discord currently holds. Add `--purge-global`, `--purge-guild
  <id>` or `--purge-all-guilds` to delete. If the stale commands show a
  *different* bot's name in the picker, it's another application — kick that bot
  from the server instead.
- **Alerts reset on update** — the `/appdata` Path mapping is missing (see above).

`GET /health` returns `200` when everything's running. `LOG_LEVEL=DEBUG` is safe
to use.

## 📄 License

MIT — see [LICENSE](LICENSE).
