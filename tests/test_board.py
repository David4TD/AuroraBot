"""The event board: one pinned message per subscription, edited in place.

Built on the real Worlds 2026 Swiss shape, which is what the board exists for:
Round 1 is eight games from 04:00 to 11:00 Sydney, straddling the digest's 10am
handover, so a daily card split that one round across two nights. Rounds 2 and
3 are scheduled but undrawn, so they read "TBD vs TBD" until the round before
them finishes.
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
from src.cogs.board import Board, _boardable            # noqa: E402
from src.cogs.digest import Digest                      # noqa: E402
from src.cogs.games import ALL_KEYS                     # noqa: E402
from src.database.db import Database                    # noqa: E402
from src.utils.boardcard import (                       # noqa: E402
    TAPPABLE, build_board, open_matches, signature,
)

UTC = timezone.utc
SYD = ZoneInfo("Australia/Sydney")
GUILD, CHANNEL, LEAGUE, TOURNEY = 7, 500, 297, 22047
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


# ── the real Round 1 slate, 04:00–11:00 Sydney ───────────────────────────────
ROUND1 = [
    ("Gen.G", "Invictus Gaming"), ("Team Liquid", "CTBC Flying Oyster"),
    ("Anyone's Legend", "Karmine Corp"), ("G2 Esports", "Dplus KIA"),
    ("Hanwha Life Esports", "Top Esports"), ("LOS", "T1"),
    ("Team Secret Whales", "LYON"), ("Bilibili Gaming", "Movistar KOI"),
]

# Far enough out that nothing has started, and on a Saturday in Sydney so the
# 04:00–11:00 block is the same one the digest used to cut in half.
SAT = datetime.now(SYD).date() + timedelta(days=(5 - datetime.now(SYD).weekday()) % 7 or 7)


def at(day_offset: int, hour: int) -> str:
    local = datetime.combine(
        SAT + timedelta(days=day_offset), time(hour, 0), tzinfo=SYD)
    return local.astimezone(UTC).isoformat()


def fixture(mid, name, begin, teams, stage="Group Stage", bo=1):
    return {
        "id": mid, "name": name, "status": "not_started", "begin_at": begin,
        "number_of_games": bo, "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": LEAGUE, "name": "Worlds"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": TOURNEY, "name": stage, "tier": "s"},
        "opponents": [{"opponent": t} for t in teams],
        "streams_list": [],
    }


def team(tid, name):
    return {"id": tid, "name": name}


TBD = {"id": None, "name": "TBD"}


def slate():
    """Round 1 drawn, Rounds 2 and 3 scheduled but not."""
    out = []
    for i, (a, b) in enumerate(ROUND1):
        out.append(fixture(1000 + i, f"Round 1: {a} vs {b}", at(0, 4 + i),
                           [team(100 + i, a), team(200 + i, b)]))
    for i in range(8):
        out.append(fixture(2000 + i, "Round 2: TBD vs TBD", at(1, 4 + i),
                           [TBD, TBD]))
    for i in range(4):
        out.append(fixture(3000 + i, "Round 3: TBD vs TBD", at(2, 4 + i),
                           [TBD, TBD], bo=3))
    return out


class Icons:
    def icon(self, t):
        return ""

    def partial(self, t):
        return None


class Api:
    def __init__(self, feed):
        self.feed = feed
        self.calls = 0

    async def upcoming_until(self, slug, horizon, **kw):
        self.calls += 1
        return list(self.feed)

    async def running_all(self, slug, **kw):
        return []


class Message:
    def __init__(self, mid, channel):
        self.id, self.channel = mid, channel
        self.embeds, self.view = [], None
        self.edits = 0
        self.pinned = False
        self.author = type("A", (), {"id": 999})()

    async def edit(self, embed=None, view=None, **kw):
        self.embeds = [embed] if embed else []
        self.view = view
        self.edits += 1
        return self

    async def pin(self, **kw):
        self.pinned = True

    async def unpin(self, **kw):
        self.pinned = False


class Channel:
    def __init__(self, cid):
        self.id = cid
        self.guild = type("G", (), {"id": GUILD})()
        self.messages: dict[int, Message] = {}
        self.posts = 0

    async def send(self, content=None, embed=None, embeds=None, view=None, **kw):
        self.posts += 1
        msg = Message(1000 + self.posts, self)
        msg.embeds = [embed] if embed else list(embeds or [])
        msg.view = view
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            # discord.NotFound reads .status and .reason off the response.
            resp = type("R", (), {"status": 404, "reason": "Not Found"})()
            raise discord.NotFound(resp, "gone")
        return self.messages[mid]

    async def pins(self):
        return [m for m in self.messages.values() if m.pinned]


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


async def build(feed, scope="league"):
    db = Database(str(Path(tempfile.mkdtemp()) / "b.db"))
    await db.connect()
    await db.set_games(guild_id=GUILD, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    kw = ({"scope": "league", "league_id": LEAGUE, "league_name": "Worlds 2026"}
          if scope == "league" else
          {"scope": "tournament", "tournament_id": TOURNEY,
           "tournament_name": "Worlds Group Stage"})
    sub_id = await db.add_subscription(
        guild_id=GUILD, channel_id=CHANNEL, game="lol", created_by=1, **kw)
    ch = Channel(CHANNEL)
    bot = Bot(db, Api(feed), ch)
    cog = Board.__new__(Board)
    cog.bot = bot
    sub = (await db.list_subscriptions(GUILD))[0]
    return db, bot, ch, cog, sub, sub_id


def labels(view):
    return [getattr(c, "item", c).label for c in view.children] if view else []


def body(embed):
    return (embed.description or "") + "\n".join(
        f.name + "\n" + f.value for f in embed.fields)


async def main():
    print("\n[the slate the daily card used to cut in half]")
    db, bot, ch, cog, sub, sub_id = await build(slate())
    check("one round, eight games, straddling 10am",
          len(ROUND1) == 8 and True)
    posted = await cog._refresh(sub)
    check("a board goes up", posted is True)
    check("one message, not one per match", ch.posts == 1, str(ch.posts))
    embed = ch.messages[1001].embeds[0]
    text = body(embed)
    check("all eight of Round 1 are on it",
          all(a in text for a, _ in ROUND1), text[:200])
    check("under a single day heading",
          sum(1 for f in embed.fields if "game" in f.name) >= 1,
          str([f.name for f in embed.fields]))
    check("and it is pinned", ch.messages[1001].pinned is True)

    print("\n[undrawn rounds are a promise, not forty rows of TBD]")
    check("no TBD vs TBD anywhere", "TBD" not in text, text[:200])
    check("round 2 is still accounted for", "8 games" in text, text[:300])
    check("and says why it is blank", "not drawn yet" in text, text[:400])

    print("\n[what a shared message can and cannot say]")
    check("it counts what is open", "8** open to predict" in text
          or "**8** open" in text, embed.description)
    check("it never claims to know who is reading",
          "you " not in text.lower().replace("you've", ""), text[:300])

    print("\n[one tap for the matches nearest kick-off]")
    view = ch.messages[1001].view
    taps = [x for x in labels(view) if "more" in x.lower() or "picks" in x.lower()]
    check("four matches get buttons",
          len(labels(view)) == TAPPABLE * 2 + len(taps),
          str(labels(view)))
    check("labelled with the kick-off time, since a team can play twice",
          any(x.startswith("04:00") for x in labels(view)), str(labels(view)))
    check("the rest are behind one more button",
          any("more" in x.lower() for x in labels(view)), str(labels(view)))
    check("and a member can see their own picks",
          any("My picks" in x for x in labels(view)), str(labels(view)))
    rows = {getattr(c, "item", c).row for c in view.children}
    check("inside Discord's five rows", max(rows) <= 4, str(sorted(rows)))

    print("\n[every open match is votable, not just the four]")
    stored = await db.board_match_rows(1)
    check("all eight are stored for the picker", len(stored) == 8,
          str(len(stored)))
    check("undrawn ones are not", all(r["team_a_id"] for r in stored))

    print("\n[a quiet sweep costs nothing]")
    before = bot.api.calls
    await cog._refresh(sub)
    check("the message is not edited again",
          ch.messages[1001].edits == 0, str(ch.messages[1001].edits))
    check("and no second message appears", ch.posts == 1, str(ch.posts))
    check("though the feed is still read", bot.api.calls == before + 1)

    print("\n[a round being drawn edits the same message]")
    feed = slate()
    for i in range(8):
        feed[8 + i] = fixture(
            2000 + i, f"Round 2: {ROUND1[i][0]} vs {ROUND1[7 - i][1]}",
            at(1, 4 + i),
            [team(100 + i, ROUND1[i][0]), team(200 + 7 - i, ROUND1[7 - i][1])])
    bot.api.feed = feed
    await cog._refresh(sub)
    check("still one message", ch.posts == 1, str(ch.posts))
    check("edited in place", ch.messages[1001].edits == 1,
          str(ch.messages[1001].edits))
    text = body(ch.messages[1001].embeds[0])
    check("round 2 now reads as fixtures", "not drawn yet" in text
          and text.count("not drawn yet") == 1, text[-300:])
    check("and the open count grew",
          "**16** open" in text, ch.messages[1001].embeds[0].description)
    await db.close()

    print("\n[voting from the board]")
    db, bot, ch, cog, sub, sub_id = await build(slate())
    await cog._refresh(sub)

    class Inter:
        def __init__(self):
            self.client = bot
            self.user = type("U", (), {"id": 42, "display_name": "dave"})()
            self.guild_id = GUILD
            self.sent = []
            self.response = self

        async def send_message(self, content=None, view=None, ephemeral=False,
                               **kw):
            self.sent.append((content, view, ephemeral))

    from src.cogs.board import _vote
    i = Inter()
    await _vote(i, 1, 1000, 0)
    check("the pick is acknowledged", len(i.sent) == 1)
    check("privately", i.sent[0][2] is True)
    row = await db.get_prediction(42, 1000)
    check("and recorded", row is not None)
    check("for the right team",
          row and row["predicted_team_name"] == "Gen.G",
          row["predicted_team_name"] if row else "")
    check("filed under the match's own tournament",
          row and int(row["tournament_id"]) == TOURNEY,
          str(row["tournament_id"] if row else None))

    print("\n[a match that started between edits]")
    await db.conn.execute(
        "UPDATE board_matches SET begin_at = ? WHERE match_id = 1001",
        ((datetime.now(UTC) - timedelta(minutes=5)).isoformat(),))
    await db.conn.commit()
    i = Inter()
    await _vote(i, 1, 1001, 0)
    check("is refused, not silently taken",
          "already started" in (i.sent[0][0] or ""), str(i.sent[0][0]))
    check("and nothing was written",
          await db.get_prediction(42, 1001) is None)

    print("\n[a match no longer on the board]")
    i = Inter()
    await _vote(i, 1, 999999, 0)
    check("says so rather than erroring",
          "isn't on the board" in (i.sent[0][0] or ""), str(i.sent[0][0]))
    await db.close()

    print("\n[the counts on the board are aggregate, never personal]")
    db, bot, ch, cog, sub, _ = await build(slate())
    await cog._refresh(sub)
    for uid in (1, 2, 3):
        await db.ensure_user(uid, f"u{uid}")
        await db.create_prediction(
            discord_id=uid, match_id=1000, game="lol", predicted_team_id=100,
            predicted_team_name="Gen.G", opponent_team_name="Invictus Gaming",
            match_starts_at=at(0, 4), stake=10, guild_id=GUILD,
            tournament_id=TOURNEY, tournament_name="Group Stage")
    counts = await db.picks_for_matches(GUILD, [1000, 1001])
    check("three picks counted on the first game", counts.get(1000) == 3,
          str(counts))
    check("none on the second", 1001 not in counts, str(counts))
    check("another server sees none of it",
          await db.picks_for_matches(123, [1000]) == {}, "leak")
    await cog._refresh(sub)
    text = body(ch.messages[1001].embeds[0])
    check("the board shows the count", "🎲 3" in text, text[:400])
    check("but not who, and not the split",
          "Gen.G** vs" in text and "u1" not in text, text[:200])
    await db.close()

    print("\n[the daily digest stands down where a board exists]")
    db, bot, ch, cog, sub, sub_id = await build(slate())
    digest = Digest.__new__(Digest)
    digest.bot = bot
    check("no board yet, so the digest would post",
          await db.board_for(CHANNEL, sub_id) is None)
    await cog._refresh(sub)
    check("once a board exists the digest sees it",
          await db.board_for(CHANNEL, sub_id) is not None)
    posts_before = ch.posts
    await digest._sweep_once()
    check("and posts nothing beside it", ch.posts == posts_before,
          str(ch.posts))
    await db.close()

    print("\n[the message being deleted]")
    db, bot, ch, cog, sub, _ = await build(slate())
    await cog._refresh(sub)
    del ch.messages[1001]
    bot.api.feed = slate()[:4]          # force a change so it re-renders
    await cog._refresh(sub)
    check("a replacement goes up", ch.posts == 2, str(ch.posts))
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("and the new id is remembered",
          board and int(board["message_id"]) == 1002,
          str(board["message_id"] if board else None))
    await db.close()

    print("\n[predictions off: schedule, no buttons]")
    db, bot, ch, cog, sub, sub_id = await build(slate())
    await db.set_subscription_predictions(sub_id, GUILD, False)
    quiet = (await db.list_subscriptions(GUILD))[0]
    await cog._refresh(quiet)
    check("the board still goes up", ch.posts == 1)
    check("with no buttons at all", ch.messages[1001].view is None)
    await db.close()

    print("\n[an empty event]")
    db, bot, ch, cog, sub, _ = await build([])
    check("nothing posted when nothing is on",
          await cog._refresh(sub) is False)
    check("and no message", ch.posts == 0, str(ch.posts))
    await db.close()

    print("\n[scope]")
    db, bot, ch, cog, sub, _ = await build(slate(), scope="tournament")
    check("a tournament subscription boards too",
          await cog._refresh(sub) is True)
    await db.close()
    check("a whole-game subscription does not", not _boardable(
        {"scope": "game", "league_id": None, "tournament_id": None}))
    check("nor a team one", not _boardable(
        {"scope": "team", "league_id": None, "tournament_id": None}))

    print("\n[renderer edges]")
    bot_only = type("B", (), {"icons": Icons()})()
    empty = build_board(bot_only, [], title="Worlds", tz=SYD)
    check("an empty board explains itself",
          "fills itself in" in (empty.description or ""), empty.description)
    check("and carries no fields", empty.fields == [])
    only_tbd = build_board(
        bot_only, [fixture(1, "Round 1: TBD vs TBD", at(0, 4), [TBD, TBD])],
        title="Worlds", tz=SYD)
    check("a wholly undrawn day still shows the day",
          "1 game" in body(only_tbd), body(only_tbd)[:200])
    check("with nothing open to predict",
          "**0** open" in (only_tbd.description or ""), only_tbd.description)
    check("open_matches ignores undrawn fixtures",
          open_matches([fixture(1, "x", at(0, 4), [TBD, TBD])]) == [])
    check("and ones already under way",
          open_matches([fixture(1, "x", (datetime.now(UTC) - timedelta(
              hours=1)).isoformat(), [team(1, "A"), team(2, "B")])]) == [])

    print("\n[the signature is what makes continuous editing cheap]")
    one = build_board(bot_only, slate(), title="Worlds", tz=SYD)
    two = build_board(bot_only, slate(), title="Worlds", tz=SYD)
    check("the same slate hashes the same",
          signature(one, [1, 2]) == signature(two, [1, 2]))
    check("a changed slate does not",
          signature(one, [1, 2]) != signature(one, [1, 3]))
    check("and so does a changed body",
          signature(one, [1]) != signature(empty, [1]))


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
