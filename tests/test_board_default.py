"""Which board /leaderboard opens on when nobody names one."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

from src.cogs.games import ALL_KEYS                   # noqa: E402
from src.cogs.leaderboard import Leaderboard          # noqa: E402
from src.database.db import Database                  # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
GUILD, CHANNEL = 7, 500

# BLAST sorts before every other name here on purpose: that alphabetical first
# place is exactly what used to decide the default board.
PORTO = (9001, "BLAST Open Porto Fall 2026")
MAJOR = (9002, "IEM Katowice 2026")
LCK = (9003, "LCK 2026 Playoffs")

failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


class Tourneys:
    """Stands in for the tournament directory, which drops ended events."""

    def __init__(self, running: dict[str, list[int] | None]):
        self.running = running
        self.asked: list[str] = []

    async def current(self, game, *, wait=None):
        self.asked.append(game)
        ids = self.running.get(game)
        if ids is None:
            return None                      # cold cache
        return [{"id": i} for i in ids]


class Bot:
    def __init__(self, db, running):
        self.db = db
        self.tourneys = Tourneys(running)


async def fresh():
    db = Database(str(Path(tempfile.mkdtemp()) / "b.db"))
    await db.connect()
    await db.set_games(guild_id=GUILD, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    return db


async def follow(db, tid, name, game="csgo", channel=CHANNEL):
    return await db.add_subscription(
        guild_id=GUILD, channel_id=channel, game=game, created_by=1,
        scope="tournament", tournament_id=tid, tournament_name=name)


async def pick(db, user, tid, name, *, days_ago, status="won", game="csgo"):
    starts = (NOW - timedelta(days=days_ago)).isoformat()
    await db.ensure_user(user, f"user{user}")
    made = await db.create_prediction(
        discord_id=user, match_id=tid * 100 + user, game=game,
        predicted_team_id=1, predicted_team_name="A", opponent_team_name="B",
        match_starts_at=starts, stake=10, guild_id=GUILD,
        tournament_id=tid, tournament_name=name)
    assert made, f"could not record a pick for {user} on {tid}"
    if status != "open":
        row = await db.get_prediction(user, tid * 100 + user)
        await db.resolve_prediction(
            row["id"], status, 10 if status == "won" else 0, str(user))


async def resolve(bot, subs_game_running=None):
    """Run the default path exactly as the command does."""
    cog = Leaderboard.__new__(Leaderboard)
    cog.bot = bot
    subs = [s for s in await bot.db.list_subscriptions(GUILD)
            if int(s["channel_id"]) == CHANNEL and s["tournament_id"] is not None]
    sub = await cog._channel_board(GUILD, subs)
    return None if sub is None else int(sub["tournament_id"])


async def main():
    print("\n[the bug: alphabetical order picked a finished event]")
    db = await fresh()
    await follow(db, *PORTO)
    await follow(db, *MAJOR)
    subs = await db.list_subscriptions(GUILD)
    names = [s["tournament_name"] for s in subs]
    check("the subscription list really is name-ordered",
          names.index(PORTO[1]) < names.index(MAJOR[1]), str(names))
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=40)
    await pick(db, 1, MAJOR[0], MAJOR[1], days_ago=1)
    await pick(db, 2, MAJOR[0], MAJOR[1], days_ago=0, status="open")

    bot = Bot(db, {"csgo": [MAJOR[0]]})          # Porto is gone from the feed
    opened = await resolve(bot)
    check("opens on the event that's actually on", opened == MAJOR[0], str(opened))
    check("not the alphabetically first one", opened != PORTO[0])
    check("one directory lookup per game, not per subscription",
          bot.tourneys.asked.count("csgo") == 1, str(bot.tourneys.asked))
    await db.close()

    print("\n[a crowned event loses to a live one even while both are listed]")
    db = await fresh()
    await follow(db, *PORTO)
    await follow(db, *MAJOR)
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=3)
    await pick(db, 1, MAJOR[0], MAJOR[1], days_ago=3)
    await db.crown_champion(GUILD, PORTO[0], PORTO[1], "1", 10, 1, 0)
    # Both still inside the directory's two-day grace.
    bot = Bot(db, {"csgo": [PORTO[0], MAJOR[0]]})
    check("the uncrowned board wins", await resolve(bot) == MAJOR[0],
          str(await resolve(bot)))
    await db.close()

    print("\n[an open pick outranks a bigger finished board]")
    db = await fresh()
    await follow(db, *PORTO)
    await follow(db, *MAJOR)
    for user in range(1, 6):
        await pick(db, user, PORTO[0], PORTO[1], days_ago=2)
    await pick(db, 1, MAJOR[0], MAJOR[1], days_ago=0, status="open")
    bot = Bot(db, {"csgo": [PORTO[0], MAJOR[0]]})
    check("the one still being played wins", await resolve(bot) == MAJOR[0],
          str(await resolve(bot)))
    await db.close()

    print("\n[recency only breaks a tie]")
    db = await fresh()
    await follow(db, *PORTO)
    await follow(db, *MAJOR)
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=9)
    await pick(db, 1, MAJOR[0], MAJOR[1], days_ago=2)
    bot = Bot(db, {"csgo": [PORTO[0], MAJOR[0]]})
    check("the more recently played board wins",
          await resolve(bot) == MAJOR[0], str(await resolve(bot)))
    await db.close()

    print("\n[when everything the channel follows is over]")
    db = await fresh()
    await follow(db, *PORTO)
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=40)
    bot = Bot(db, {"csgo": [MAJOR[0]]})
    check("falls back to the season table rather than a dead board",
          await resolve(bot) is None, str(await resolve(bot)))
    await db.close()

    print("\n[a cold directory never kills a board]")
    db = await fresh()
    await follow(db, *PORTO)
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=40)
    check("can't tell: keep the channel's event",
          await resolve(Bot(db, {"csgo": None})) == PORTO[0])
    check("nothing on in that game at all: same treatment",
          await resolve(Bot(db, {"csgo": []})) == PORTO[0])

    class Broken(Tourneys):
        async def current(self, game, *, wait=None):
            raise RuntimeError("directory exploded")

    bot = Bot(db, {})
    bot.tourneys = Broken({})
    check("and a directory that raises doesn't take the command with it",
          await resolve(bot) == PORTO[0])
    await db.close()

    print("\n[a channel following one live event still just works]")
    db = await fresh()
    await follow(db, *LCK, game="lol")
    await pick(db, 1, LCK[0], LCK[1], days_ago=1, game="lol")
    bot = Bot(db, {"lol": [LCK[0]]})
    check("opens on it", await resolve(bot) == LCK[0])
    await db.close()

    print("\n[a channel following nothing]")
    db = await fresh()
    bot = Bot(db, {})
    check("season table", await resolve(bot) is None)
    check("and no directory lookups at all", bot.tourneys.asked == [])
    await db.close()

    print("\n[events across two games in one channel]")
    db = await fresh()
    await follow(db, *PORTO, game="csgo")
    await follow(db, *LCK, game="lol")
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=40)
    await pick(db, 1, LCK[0], LCK[1], days_ago=1, game="lol")
    bot = Bot(db, {"csgo": [MAJOR[0]], "lol": [LCK[0]]})
    opened = await resolve(bot)
    check("the live one wins across games", opened == LCK[0], str(opened))
    check("both games asked once", sorted(bot.tourneys.asked) == ["csgo", "lol"],
          str(bot.tourneys.asked))
    await db.close()

    print("\n[tournament_activity on its own]")
    db = await fresh()
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=5)
    await pick(db, 2, PORTO[0], PORTO[1], days_ago=1, status="open")
    act = await db.tournament_activity(GUILD, [PORTO[0], MAJOR[0]])
    check("only reports what it has", set(act) == {PORTO[0]}, str(set(act)))
    check("counts the picks", act[PORTO[0]]["picks"] == 2, str(act))
    check("counts the open ones", act[PORTO[0]]["open_picks"] == 1, str(act))
    check("last seen is the latest match, not the earliest",
          act[PORTO[0]]["last_seen"].startswith(
              (NOW - timedelta(days=1)).strftime("%Y-%m-%d")), str(act))
    check("no ids, no query", await db.tournament_activity(GUILD, []) == {})
    check("None ids are skipped",
          await db.tournament_activity(GUILD, [None]) == {})
    check("another server sees none of it",
          await db.tournament_activity(999, [PORTO[0]]) == {})
    dupes = await db.tournament_activity(GUILD, [PORTO[0], PORTO[0]])
    check("a repeated id is asked once", dupes[PORTO[0]]["picks"] == 2, str(dupes))
    await db.close()

    print("\n[the picker marks what's finished]")
    db = await fresh()
    await pick(db, 1, PORTO[0], PORTO[1], days_ago=40)
    await pick(db, 1, MAJOR[0], MAJOR[1], days_ago=1)
    await db.crown_champion(GUILD, PORTO[0], PORTO[1], "1", 10, 1, 0)
    cog = Leaderboard.__new__(Leaderboard)
    cog.bot = Bot(db, {})

    class Inter:
        guild_id = GUILD

    choices = await cog._board_ac(Inter(), "")
    by_value = {c.value: c.name for c in choices}
    check("the season table is still first", choices[0].value == "all",
          choices[0].value)
    check("the crowned event is flagged", "🏁 done" in by_value[str(PORTO[0])],
          by_value[str(PORTO[0])])
    check("the live one isn't", "done" not in by_value[str(MAJOR[0])],
          by_value[str(MAJOR[0])])
    check("every label is inside Discord's 100 chars",
          all(len(c.name) <= 100 for c in choices))
    check("filtering still works",
          {c.value for c in await cog._board_ac(Inter(), "katowice")}
          == {"all", str(MAJOR[0])},
          str([c.name for c in await cog._board_ac(Inter(), "katowice")]))
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
