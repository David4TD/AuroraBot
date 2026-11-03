"""When the board is allowed to move to the bottom of the channel.

Editing the board where it sits is silent. *Moving* it is not: there is no way
to reorder a Discord message, so the card has to be deleted and posted again,
and everyone following the channel gets another notification for a card that
mostly says what it said five minutes ago. On the sweep interval that was up
to twelve times an hour in a channel the bot itself keeps talking in.

So a move waits for one of two things: the window, or a draw. A bracket slot
filling is the whole reason to look at the board, so it jumps the queue —
everything else can wait for the next window.

The clock is moved by backdating the stamp rather than by sleeping: what is
under test is the decision, not the passage of time.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

import discord                                          # noqa: E402
from src.cogs.board import MOVE_AFTER, Board, _move_due  # noqa: E402
from src.cogs.games import ALL_KEYS                     # noqa: E402
from src.database.db import Database                    # noqa: E402

UTC = timezone.utc
SYD = ZoneInfo("Australia/Sydney")
GUILD, CHANNEL, LEAGUE, TOURNEY = 7, 500, 297, 22047
MATCHDAY = datetime.now(SYD).date() + timedelta(days=1)
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


# ── a small slate: one drawn game, one still TBD ─────────────────────────────
def at(day_offset: int, hour: int) -> str:
    local = datetime.combine(
        MATCHDAY + timedelta(days=day_offset), time(hour, 0), tzinfo=SYD)
    return local.astimezone(UTC).isoformat()


def fixture(mid, name, begin, teams):
    return {
        "id": mid, "name": name, "status": "not_started", "begin_at": begin,
        "number_of_games": 3, "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": LEAGUE, "name": "Worlds"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": TOURNEY, "name": "Group Stage", "tier": "s"},
        "opponents": [{"opponent": t} for t in teams],
        "streams_list": [],
    }


TBD = {"id": None, "name": "TBD"}


def undrawn():
    """Round 1 drawn, the semifinal that follows it not."""
    return [
        fixture(1, "Round 1: T1 vs Gen.G", at(0, 5),
                [{"id": 101, "name": "T1"}, {"id": 102, "name": "Gen.G"}]),
        fixture(2, "Semifinal 1: TBD vs TBD", at(1, 5), [TBD, TBD]),
    ]


def drawn():
    """The same slate, with the semifinal now filled in."""
    feed = undrawn()
    feed[1] = fixture(2, "Semifinal 1: T1 vs KC", at(1, 5),
                      [{"id": 101, "name": "T1"},
                       {"id": 103, "name": "Karmine Corp"}])
    return feed


class Icons:
    def icon(self, t):
        return ""

    def partial(self, t):
        return None


class Api:
    def __init__(self, feed):
        self.feed = feed

    async def upcoming_until(self, slug, horizon, **kw):
        return list(self.feed)

    async def running_all(self, slug, **kw):
        return []


class Message:
    def __init__(self, mid, channel):
        self.id, self.channel = mid, channel
        self.embeds, self.view = [], None
        self.edits = 0
        self.pinned = False

    async def edit(self, embed=None, view=None, **kw):
        self.embeds = [embed] if embed else []
        self.view = view
        self.edits += 1
        return self

    async def pin(self, **kw):
        self.pinned = True

    async def unpin(self, **kw):
        self.pinned = False

    async def delete(self, **kw):
        self.channel.messages.pop(self.id, None)


class Channel:
    def __init__(self, cid):
        self.id = cid
        self.guild = type("G", (), {"id": GUILD})()
        self.messages: dict[int, Message] = {}
        self.sent = 0
        # Chatter goes through send() too, so counting every message would
        # count the thing that buries the board as the board moving.
        self.cards = 0

    async def send(self, content=None, embed=None, embeds=None, view=None, **kw):
        self.sent += 1
        msg = Message(1000 + self.sent, self)
        msg.embeds = [embed] if embed else list(embeds or [])
        msg.view = view
        if msg.embeds:
            self.cards += 1
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            resp = type("R", (), {"status": 404, "reason": "Not Found"})()
            raise discord.NotFound(resp, "gone")
        return self.messages[mid]

    async def pins(self):
        return [m for m in self.messages.values() if m.pinned]

    def history(self, limit=1):
        newest = sorted(self.messages, reverse=True)[:limit]
        channel = self

        class It:
            def __aiter__(self):
                self.rest = list(newest)
                return self

            async def __anext__(self):
                if not self.rest:
                    raise StopAsyncIteration
                return channel.messages[self.rest.pop(0)]

        return It()

    async def say(self, text="a match reminder lands"):
        return await self.send(content=text)


class Bot:
    def __init__(self, db, api, channel):
        self.db, self.api, self.icons = db, api, Icons()
        self.user = type("U", (), {"id": 999})()
        self.settings = type("S", (), {
            "digest_tz_name": "Australia/Sydney", "digest_hour": 0,
            "cs_slug": "csgo", "top_tier_only": True,
            "tier_allowlist": frozenset({"s", "a"}), "alert_lead_minutes": 30,
        })()
        self._channels = {channel.id: channel}

    def get_channel(self, cid):
        return self._channels.get(cid)


async def build(feed):
    db = Database(str(Path(tempfile.mkdtemp()) / "m.db"))
    await db.connect()
    await db.set_games(guild_id=GUILD, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    await db.add_subscription(
        guild_id=GUILD, channel_id=CHANNEL, game="lol", created_by=1,
        scope="league", league_id=LEAGUE, league_name="Worlds 2026")
    ch = Channel(CHANNEL)
    bot = Bot(db, Api(feed), ch)
    cog = Board.__new__(Board)
    cog.bot = bot
    sub = (await db.list_subscriptions(GUILD))[0]
    return db, bot, ch, cog, sub


async def age(db, sub, hours):
    await db.conn.execute(
        "UPDATE event_boards SET last_moved_at = datetime('now', ?) "
        "WHERE subscription_id = ?",
        (f"-{hours} hours", int(sub["id"])),
    )
    await db.conn.commit()


async def stamp(db, sub):
    row = await db.board_for(CHANNEL, int(sub["id"]))
    return row["last_moved_at"]


def last(ch):
    return max(ch.messages) if ch.messages else None


async def main():
    # ── the window ──────────────────────────────────────────────────────────
    print("\n[the first post starts the clock]")
    db, bot, ch, cog, sub = await build(undrawn())
    await cog._refresh(sub)
    check("the board goes up", ch.cards == 1, str(ch.cards))
    check("and is stamped as having just moved",
          await stamp(db, sub) is not None)

    print("\n[buried, but not for long enough]")
    for n in range(3):
        await ch.say(f"chatter {n}")
        await cog._refresh(sub)
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("three sweeps later it has still not moved",
          ch.cards == 1, str(ch.cards))
    check("the card people have is the one it posted",
          int(board["message_id"]) in ch.messages, str(sorted(ch.messages)))
    check("and it is genuinely buried, not just quiet",
          last(ch) != int(board["message_id"]), str(sorted(ch.messages)))

    print("\n[once the window passes, it moves]")
    await age(db, sub, hours=6)
    before = await stamp(db, sub)
    await cog._refresh(sub)
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("a new card is posted", ch.cards == 2, str(ch.cards))
    check("it is the last thing in the channel",
          last(ch) == int(board["message_id"]), str(sorted(ch.messages)))
    check("the old one is gone rather than duplicated",
          len([m for m in ch.messages.values() if m.embeds]) == 1,
          str(sorted(ch.messages)))
    check("it is still pinned", ch.messages[int(board["message_id"])].pinned)
    check("and the clock restarted", await stamp(db, sub) != before)

    print("\n[and the clock restarts, so it does not move again]")
    await ch.say("more chatter")
    await cog._refresh(sub)
    check("no third card", ch.cards == 2, str(ch.cards))
    await db.close()

    # ── the exception ───────────────────────────────────────────────────────
    print("\n[a draw landing jumps the queue]")
    db, bot, ch, cog, sub = await build(undrawn())
    await cog._refresh(sub)
    await ch.say("a result lands")
    await cog._refresh(sub)
    check("still sitting still, well inside the window",
          ch.cards == 1, str(ch.cards))

    bot.api.feed = drawn()
    await cog._refresh(sub)
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("the semifinal filling in moves it immediately",
          ch.cards == 2, str(ch.cards))
    check("to the bottom", last(ch) == int(board["message_id"]),
          str(sorted(ch.messages)))
    body = "\n".join(
        f.name + f.value
        for f in ch.messages[int(board["message_id"])].embeds[0].fields)
    check("carrying the team that was TBD", "Karmine Corp" in body, body[:300])
    check("and the draw restarts the clock too",
          _move_due(await db.board_for(CHANNEL, int(sub["id"]))) is False)

    print("\n[but only the once — the same draw is not news twice]")
    await ch.say("more chatter")
    await cog._refresh(sub)
    check("no further move", ch.cards == 2, str(ch.cards))
    await db.close()

    print("\n[a draw that lands while the board is already last only edits]")
    db, bot, ch, cog, sub = await build(undrawn())
    await cog._refresh(sub)
    first = int((await db.board_for(CHANNEL, int(sub["id"])))["message_id"])
    bot.api.feed = drawn()
    await cog._refresh(sub)
    check("nothing is reposted, because nothing buried it",
          ch.cards == 1, str(ch.cards))
    check("the same message is edited in place",
          ch.messages[first].edits == 1, str(ch.messages[first].edits))
    body = "\n".join(f.name + f.value
                     for f in ch.messages[first].embeds[0].fields)
    check("and it shows the draw", "Karmine Corp" in body, body[:300])
    await db.close()

    # ── asking for it ───────────────────────────────────────────────────────
    print("\n[/board moves it whatever the clock says]")
    db, bot, ch, cog, sub = await build(undrawn())
    await cog._refresh(sub)
    await ch.say("chatter")
    await cog._refresh(sub)
    check("the sweep declines", ch.cards == 1, str(ch.cards))
    await cog._refresh(sub, force=True)
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("asking explicitly does not", ch.cards == 2, str(ch.cards))
    check("and it lands at the bottom",
          last(ch) == int(board["message_id"]), str(sorted(ch.messages)))
    check("which also restarts the clock",
          _move_due(await db.board_for(CHANNEL, int(sub["id"]))) is False)

    print("\n[and /board on a board that is already last changes nothing]")
    posts = ch.cards
    await cog._refresh(sub, force=True)
    check("no pointless repost", ch.cards == posts, str(ch.cards))
    await db.close()

    # ── recovery ────────────────────────────────────────────────────────────
    print("\n[a card deleted by hand comes straight back]")
    db, bot, ch, cog, sub = await build(undrawn())
    await cog._refresh(sub)
    board = await db.board_for(CHANNEL, int(sub["id"]))
    await ch.messages[int(board["message_id"])].delete()
    await ch.say("chatter")
    await cog._refresh(sub)
    check("the window does not strand the channel without a board",
          ch.cards == 2, str(ch.cards))
    check("and the replacement restarts the clock, not the deleted one's",
          _move_due(await db.board_for(CHANNEL, int(sub["id"]))) is False)
    await db.close()

    # ── the predicate on its own ────────────────────────────────────────────
    print("\n[the window itself]")
    now = datetime.now(UTC)
    check("a board with no stamp is due", _move_due({"last_moved_at": None}))
    check("a row missing the column entirely is due, not a crash",
          _move_due({}) is True)
    check("no board at all is due", _move_due(None))
    fresh = (now - timedelta(minutes=1)).isoformat()
    check("one that just moved is not", _move_due({"last_moved_at": fresh},
                                                  now) is False)
    edge = (now - MOVE_AFTER).isoformat()
    check("exactly on the window is due",
          _move_due({"last_moved_at": edge}, now) is True)
    just_under = (now - MOVE_AFTER + timedelta(seconds=30)).isoformat()
    check("a few seconds short is not",
          _move_due({"last_moved_at": just_under}, now) is False)
    check("five hours is the window", MOVE_AFTER == timedelta(hours=5),
          str(MOVE_AFTER))
    naive = (now - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    check("SQLite's own naive UTC format is read as UTC",
          _move_due({"last_moved_at": naive}, now) is True, naive)
    check("and a stamp SQLite could not write is treated as unknown",
          _move_due({"last_moved_at": "not a date"}, now) is True)


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
