"""Clearing a match's reminder and live cards once the result is posted.

A day of Worlds is eight matches; at three cards each that is twenty-four
messages between the board and the top of the channel. The reminder exists to
get a pick in before kick-off and the live card to say a game is worth
watching — neither is true once the result is out.
"""
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

import discord                                          # noqa: E402
from src.cogs.alerts import Alerts                      # noqa: E402
from src.cogs.games import ALL_KEYS                     # noqa: E402
from src.database.db import Database                    # noqa: E402

UTC = timezone.utc
GUILD, CHANNEL, TOURNEY = 7, 500, 88
MATCH = 1710931
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def finished_match():
    began = datetime.now(UTC) - timedelta(hours=2)
    return {
        "id": MATCH, "name": "Round 1: T1 vs LOS", "status": "finished",
        "begin_at": began.isoformat(),
        "end_at": (began + timedelta(hours=1)).isoformat(),
        "number_of_games": 3, "winner_id": 101,
        "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": 297, "name": "Worlds"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": TOURNEY, "name": "Group Stage", "tier": "s"},
        "opponents": [{"opponent": {"id": 101, "name": "T1"}},
                      {"opponent": {"id": 102, "name": "LOS"}}],
        "results": [{"team_id": 101, "score": 2}, {"team_id": 102, "score": 1}],
        "games": [],
    }


class Icons:
    def icon(self, t):
        return ""

    def partial(self, t):
        return None


class Message:
    def __init__(self, mid, channel, content):
        self.id, self.channel, self.content = mid, channel, content
        self.deleted = False

    async def delete(self, **kw):
        self.deleted = True
        self.channel.messages.pop(self.id, None)
        self.channel.deleted.append(self.id)


class Channel:
    def __init__(self, cid, *, can_delete=True, can_fetch=True):
        self.id = cid
        self.guild = type("G", (), {"id": GUILD})()
        self.messages: dict[int, Message] = {}
        self.deleted: list[int] = []
        self.sent: list[str] = []
        self.can_delete, self.can_fetch = can_delete, can_fetch
        self._next = 9000

    async def send(self, content=None, embed=None, embeds=None, view=None, **kw):
        self._next += 1
        msg = Message(self._next, self, content or "")
        self.messages[msg.id] = msg
        self.sent.append(content or "")
        return msg

    async def fetch_message(self, mid):
        if not self.can_fetch:
            raise discord.Forbidden(
                type("R", (), {"status": 403, "reason": "Forbidden"})(), "no")
        if mid not in self.messages:
            raise discord.NotFound(
                type("R", (), {"status": 404, "reason": "Not Found"})(), "gone")
        if not self.can_delete:
            raise discord.Forbidden(
                type("R", (), {"status": 403, "reason": "Forbidden"})(), "no")
        return self.messages[mid]


class Api:
    def __init__(self, match):
        self.match = match

    async def get_match(self, match_id):
        return self.match


class Bot:
    def __init__(self, db, channel, match):
        self.db, self.icons, self.api = db, Icons(), Api(match)
        self.settings = type("S", (), {
            "cs_slug": "csgo", "top_tier_only": True,
            "tier_allowlist": frozenset({"s", "a"}), "alert_lead_minutes": 30,
            "digest_tz_name": "UTC", "digest_hour": 0,
        })()
        self._channels = {channel.id: channel}

    def get_channel(self, cid):
        return self._channels.get(cid)


async def build(**channel_kw):
    db = Database(str(Path(tempfile.mkdtemp()) / "t.db"))
    await db.connect()
    await db.set_games(guild_id=GUILD, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    ch = Channel(CHANNEL, **channel_kw)
    match = finished_match()
    bot = Bot(db, ch, match)
    cog = Alerts.__new__(Alerts)
    cog.bot = bot
    return db, bot, ch, cog, match


async def post_alerts(ch, db, *, reminder=True, live=True):
    """Stand in for the two cards a match gets before it finishes."""
    out = {}
    if reminder:
        msg = await ch.send(content="⏰ **Starting in ~30 minutes!**")
        await db.mark_alerted(MATCH, "reminder", CHANNEL, msg.id)
        out["reminder"] = msg.id
    if live:
        msg = await ch.send(content="🔴 **A match just went live!**")
        await db.mark_alerted(MATCH, "live", CHANNEL, msg.id)
        out["live"] = msg.id
    return out


async def main():
    print("\n[both cards go when the result lands]")
    db, bot, ch, cog, match = await build()
    ids = await post_alerts(ch, db)
    check("two cards in the channel", len(ch.messages) == 2, str(len(ch.messages)))
    await cog._post_result(match, "lol", CHANNEL)
    check("the result was posted",
          any("Full time" in c for c in ch.sent), str(ch.sent))
    check("the reminder is gone", ids["reminder"] not in ch.messages)
    check("the live card is gone", ids["live"] not in ch.messages)
    check("both were deleted, not just dropped",
          sorted(ch.deleted) == sorted(ids.values()), str(ch.deleted))
    check("the result itself survives", len(ch.messages) == 1,
          str(len(ch.messages)))
    check("and it is the result",
          "Full time" in list(ch.messages.values())[0].content,
          list(ch.messages.values())[0].content)
    await db.close()

    print("\n[the ids are what make it safe]")
    db, bot, ch, cog, match = await build()
    ids = await post_alerts(ch, db)
    stranger = await ch.send(content="someone else talking")
    await cog._post_result(match, "lol", CHANNEL)
    check("a message the bot didn't post about this match is untouched",
          stranger.id in ch.messages, str(sorted(ch.messages)))
    check("only the two alerts were removed",
          sorted(ch.deleted) == sorted(ids.values()), str(ch.deleted))
    recorded = await db.alert_messages_for(MATCH, CHANNEL)
    check("and the lookup is scoped to this match and channel",
          sorted(recorded) == sorted(ids.values()), str(recorded))
    check("another channel's alerts are not returned",
          await db.alert_messages_for(MATCH, 999) == [])
    check("nor another match's",
          await db.alert_messages_for(123456, CHANNEL) == [])
    await db.close()

    print("\n[a result that never posts leaves the cards alone]")
    db, bot, ch, cog, match = await build(can_delete=True)

    async def refuse(*a, **kw):
        raise discord.Forbidden(
            type("R", (), {"status": 403, "reason": "Forbidden"})(), "no")

    ids = await post_alerts(ch, db)
    ch.send = refuse
    await cog._post_result(match, "lol", CHANNEL)
    check("nothing was deleted", ch.deleted == [], str(ch.deleted))
    check("the cards are still there",
          all(i in ch.messages for i in ids.values()), str(sorted(ch.messages)))
    check("but the match is marked done, so it isn't retried forever",
          await db.was_alerted(MATCH, "finished", CHANNEL))
    await db.close()

    print("\n[tidying is best-effort, never at the result's expense]")
    db, bot, ch, cog, match = await build(can_delete=False)
    ids = await post_alerts(ch, db)
    await cog._post_result(match, "lol", CHANNEL)
    check("the result still went out",
          any("Full time" in c for c in ch.sent), str(ch.sent))
    check("the cards stay when the bot can't remove them",
          all(i in ch.messages for i in ids.values()), str(sorted(ch.messages)))
    await db.close()

    db, bot, ch, cog, match = await build(can_fetch=False)
    await post_alerts(ch, db)
    await cog._post_result(match, "lol", CHANNEL)
    check("and a channel it can't read is survived too",
          any("Full time" in c for c in ch.sent), str(ch.sent))
    await db.close()

    print("\n[a card someone already deleted]")
    db, bot, ch, cog, match = await build()
    ids = await post_alerts(ch, db)
    ch.messages.pop(ids["reminder"])
    await cog._post_result(match, "lol", CHANNEL)
    check("the other one still goes", ids["live"] in ch.deleted, str(ch.deleted))
    check("and nothing raised", any("Full time" in c for c in ch.sent))
    await db.close()

    print("\n[a match with only one of the two cards]")
    db, bot, ch, cog, match = await build()
    ids = await post_alerts(ch, db, reminder=False)
    await cog._post_result(match, "lol", CHANNEL)
    check("the live card goes", ids["live"] in ch.deleted, str(ch.deleted))
    check("and the result stands", len(ch.messages) == 1, str(len(ch.messages)))
    await db.close()

    print("\n[alerts posted before this existed have no id]")
    db, bot, ch, cog, match = await build()
    msg = await ch.send(content="⏰ **Starting in ~30 minutes!**")
    # The pre-upgrade shape: a row with no message recorded against it.
    await db.mark_alerted(MATCH, "reminder", CHANNEL)
    check("nothing is recorded for it",
          await db.alert_messages_for(MATCH, CHANNEL) == [])
    await cog._post_result(match, "lol", CHANNEL)
    check("so it is left where it is", msg.id in ch.messages)
    check("rather than guessing at which message it was", ch.deleted == [])
    await db.close()

    print("\n[the result card is never a candidate]")
    db, bot, ch, cog, match = await build()
    await post_alerts(ch, db)
    await cog._post_result(match, "lol", CHANNEL)
    after = list(ch.messages.values())[0]
    await db.mark_alerted(MATCH, "finished", CHANNEL, after.id)
    listed = await db.alert_messages_for(MATCH, CHANNEL)
    check("'finished' is outside the states tidied", after.id not in listed,
          str(listed))
    # The reminder and live ids stay on their rows after being deleted. That
    # costs nothing — a second pass finds them gone and moves on — and the
    # rows are pruned after three days anyway.
    check("the result is still in the channel", after.id in ch.messages)
    ch.deleted.clear()
    await cog._clear_stale_cards(ch, MATCH)
    check("a second pass deletes nothing", ch.deleted == [], str(ch.deleted))
    check("and leaves the result alone", after.id in ch.messages)
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
