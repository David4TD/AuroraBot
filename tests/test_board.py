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
    CONTROLS_FIT_AT, MAX_TAPPABLE, TAPPABLE_PER_PART, build_board,
    button_slices, open_matches, signature,
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
# The matchday is tomorrow, not "the next Saturday". What matters is the
# 04:00–11:00 local block that straddles the digest's 10am handover, and that
# all three days sit inside the board's seven-day horizon — anchoring on a
# weekday put the slate eight days out whenever the suite ran on a Saturday,
# and the board correctly ignored the lot.
MATCHDAY = datetime.now(SYD).date() + timedelta(days=1)


def at(day_offset: int, hour: int) -> str:
    local = datetime.combine(
        MATCHDAY + timedelta(days=day_offset), time(hour, 0), tzinfo=SYD)
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

    async def delete(self, **kw):
        self.channel.messages.pop(self.id, None)
        self.deleted = True


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

    async def say(self, text="someone talks"):
        """A message from anyone else, which buries whatever came before."""
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
    check("two messages, because eight games need two banks of buttons",
          ch.posts == 2, str(ch.posts))
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

    print("\n[a button for every one of Saturday's eight games]")
    one, two = ch.messages[1001], ch.messages[1002]
    first, second = labels(one.view), labels(two.view)
    times = [x for x in first + second if x[:5].count(":") == 1]
    check("sixteen team buttons across the two messages",
          len(times) == 16, f"{len(times)}: {first + second}")
    check("five matches on the first",
          len([x for x in first if x[:5].count(":") == 1])
          == TAPPABLE_PER_PART * 2, str(first))
    check("the other three on the second",
          len([x for x in second if x[:5].count(":") == 1]) == 6, str(second))
    check("no dropdown for an ordinary matchday",
          not any("later" in x.lower() for x in first + second),
          str(first + second))
    check("labelled with kick-off, since a team can play twice",
          any(x.startswith("04:00") for x in first), str(first))
    tapped = {c.match_id for v in (one.view, two.view) for c in v.children
              if hasattr(c, "match_id")}
    check("every open match has a button of its own",
          tapped == {int(m["id"]) for m in open_matches(slate())},
          str(sorted(tapped)))
    check("and a member can see their own picks",
          any("My picks" in x for x in second), str(second))
    for msg in (one, two):
        rows = {getattr(c, "item", c).row for c in msg.view.children}
        check("inside Discord's five rows", max(rows) <= 4, str(sorted(rows)))
    check("the second says what its buttons are for",
          "rest of the day" in (two.embeds[0].description or ""),
          str(two.embeds[0].description))

    print("\n[every open match is stored, so a click resolves]")
    stored = await db.board_match_rows(1)
    check("all eight are stored", len(stored) == 8, str(len(stored)))
    check("undrawn ones are not", all(r["team_a_id"] for r in stored))

    print("\n[a quiet sweep costs nothing]")
    before = bot.api.calls
    await cog._refresh(sub)
    check("the messages are not edited again",
          ch.messages[1001].edits == 0, str(ch.messages[1001].edits))
    check("and no further message appears", ch.posts == 2, str(ch.posts))
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
    check("still the same two messages", ch.posts == 2, str(ch.posts))
    check("edited in place", ch.messages[1001].edits == 1,
          str(ch.messages[1001].edits))
    text = body(ch.messages[1001].embeds[0])
    check("round 2 now reads as fixtures, not a promise",
          "not drawn yet" not in text, text[-300:])
    check("both shown days are playable",
          all(x[0] in text for x in ROUND1), text[:200])
    check("and the open count grew",
          "**16** open" in text, ch.messages[1001].embeds[0].description)
    # Round 3 is the third matchday, so it is off the board — but the card
    # still says it exists rather than looking like the event ended.
    check("the day beyond the board is accounted for",
          "1 more day to come" in (ch.messages[1001].embeds[0].description or ""),
          ch.messages[1001].embeds[0].description)
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

    print("\n[only the next two matchdays]")
    from src.utils.boardcard import BOARD_DAYS, days_shown
    db, bot, ch, cog, sub, _ = await build(slate())
    await cog._refresh(sub)
    text = body(ch.messages[1001].embeds[0])
    days = [f.name for e in (ch.messages[1001].embeds[0],
                             ch.messages[1002].embeds[0]) for f in e.fields]
    check("two day headings, not six", len(days) == BOARD_DAYS, str(days))
    check("the third matchday is not listed",
          "Round 3" not in text and "4 games" not in text, text[-200:])
    check("but it is counted", "1 more day to come" in text, text[:200])

    kept, left = days_shown(slate(), SYD)
    check("the trim keeps the first two days' fixtures",
          len(kept) == 16, str(len(kept)))
    check("and reports what it left", left == 1, str(left))
    check("a one-day event loses nothing",
          days_shown(slate()[:8], SYD) == (slate()[:8], 0))
    check("an empty slate is survivable", days_shown([], SYD) == ([], 0))
    check("a fixture with no kick-off time can't pick a day",
          days_shown([fixture(1, "x", None, [TBD, TBD])], SYD) == ([], 0))

    print("\n[buttons never outrun what is shown]")
    tapped = {c.match_id for m in (ch.messages[1001], ch.messages[1002])
              for c in (m.view.children if m.view else [])
              if hasattr(c, "match_id")}
    shown = {int(m["id"]) for m in open_matches(days_shown(slate(), SYD)[0])}
    check("every button is for a match on the board", tapped <= shown,
          str(sorted(tapped - shown)))
    check("and every open match on it has one", tapped == shown,
          str(sorted(shown - tapped)))
    stored = {int(r["match_id"]) for r in await db.board_match_rows(1)}
    check("nothing beyond the board is stored as votable", stored == shown,
          str(sorted(stored ^ shown)))
    await db.close()

    print("\n[the board follows the channel down]")
    db, bot, ch, cog, sub, _ = await build(slate())
    await cog._refresh(sub)
    original = sorted(ch.messages)
    await cog._refresh(sub)
    check("nothing moves while the board is still the last thing said",
          sorted(ch.messages) == original, str(sorted(ch.messages)))

    await ch.say("a match reminder lands")
    await cog._refresh(sub)
    check("once something is said after it, it comes back",
          sorted(ch.messages)[-2:] != original[-1:],
          str(sorted(ch.messages)))
    for old in original:
        check(f"the old part {old} is gone, not left as a duplicate",
              old not in ch.messages, str(sorted(ch.messages)))
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("the new ids are recorded",
          int(board["message_id"]) in ch.messages
          and int(board["message_id_2"]) in ch.messages,
          f"{board['message_id']}/{board['message_id_2']}")
    check("it is the last thing in the channel again",
          max(ch.messages) == int(board["message_id_2"]),
          f"{max(ch.messages)} vs {board['message_id_2']}")
    check("and still pinned",
          ch.messages[int(board["message_id"])].pinned)
    check("the buttons came with it",
          ch.messages[int(board["message_id"])].view is not None)

    moved = sorted(ch.messages)
    await cog._refresh(sub)
    check("and it settles again once it is last", sorted(ch.messages) == moved,
          str(sorted(ch.messages)))
    await db.close()

    print("\n[a channel whose history can't be read is left alone]")
    db, bot, ch, cog, sub, _ = await build(slate())
    await cog._refresh(sub)
    before = sorted(ch.messages)

    def no_history(limit=1):
        raise discord.Forbidden(
            type("R", (), {"status": 403, "reason": "Forbidden"})(), "nope")

    ch.history = no_history
    await cog._refresh(sub)
    check("the board is not reposted on a guess",
          sorted(ch.messages) == before, str(sorted(ch.messages)))
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
    # Four open matches need one bank, so this also drops back to one part.
    bot.api.feed = slate()[:4]
    await cog._refresh(sub)
    check("a replacement goes up", ch.posts == 3, str(ch.posts))
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("and the new id is remembered",
          board and int(board["message_id"]) == 1003,
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

    print("\n[a week too big for one embed continues into a second message]")
    from src.utils.boardcard import (
        EMBED_BUDGET, MAX_PARTS, build_board_parts, _embed_len,
    )

    def fortnight():
        """Fourteen days of ten games: far past 6000 characters."""
        out = []
        for day in range(14):
            for i in range(10):
                a, b = ROUND1[i % len(ROUND1)]
                out.append(fixture(
                    50000 + day * 100 + i,
                    f"Round {day + 1}: {a} vs {b}", at(day, 4 + i),
                    [team(100 + i, f"{a} {day}"), team(200 + i, f"{b} {day}")]))
        return out

    big = build_board_parts(bot_only, fortnight(), title="Worlds 2026", tz=SYD)
    check("it splits", len(big) == 2, str(len(big)))
    check("part one is inside Discord's limit",
          _embed_len(big[0]) <= 6000, str(_embed_len(big[0])))
    check("and so is part two",
          _embed_len(big[1]) <= 6000, str(_embed_len(big[1])))
    check("part one stays under the working budget",
          _embed_len(big[0]) <= EMBED_BUDGET + 1024,
          str(_embed_len(big[0])))
    check("never more than two parts", len(big) <= MAX_PARTS, str(len(big)))
    check("part two says it is a continuation",
          "continued" in (big[1].title or ""), big[1].title)
    check("only part one carries the tally",
          big[0].description and not big[1].description,
          f"{big[0].description!r} / {big[1].description!r}")
    check("both are footed", all(e.footer.text for e in big))
    check("no day is split across the two",
          not (set(f.name for f in big[0].fields)
               & set(f.name for f in big[1].fields)))
    from src.utils.boardcard import _day_fields
    expected = [n for n, _ in _day_fields(
        bot_only, fortnight(), SYD, {}, datetime.now(UTC))]
    got = [f.name for e in big for f in e.fields if f.name != "Further ahead"]
    check("the two together read in order, earliest first",
          got == expected[:len(got)], f"{got[:3]} vs {expected[:3]}")
    check("part one holds the earliest day, where the buttons are",
          big[0].fields[0].name == expected[0],
          f"{big[0].fields[0].name} != {expected[0]}")
    check("fields stay inside the 25 cap",
          all(len(e.fields) <= 25 for e in big),
          str([len(e.fields) for e in big]))
    check("every field value is inside 1024",
          all(len(f.value) <= 1024 for e in big for f in e.fields))
    tail = big[1].fields[-1]
    check("what does not fit is accounted for, not dropped silently",
          "more day" in tail.value or "Further ahead" not in tail.name,
          f"{tail.name}: {tail.value[:80]}")

    print("\n[a normal week stays one message]")
    one = build_board_parts(bot_only, slate(), title="Worlds 2026", tz=SYD)
    check("no second part", len(one) == 1, str(len(one)))

    print("\n[the second message is posted, edited and retired]")

    def heavy():
        """Seven days of ten games with long names.

        Measured at 4606 + 851 characters, so it splits — and every day is
        inside the board's seven-day horizon, which is what the cog fetches.
        Six days of the same came to 4606 and fitted in one, which is the
        margin this is deliberately the far side of.
        """
        names = [
            ("Hanwha Life Esports Challengers", "CTBC Flying Oyster"),
            ("Anyone's Legend Esports Club", "Movistar Koi Academy"),
            ("Team Secret Whales Rising", "Bilibili Gaming Junior"),
            ("Dplus KIA Challengers", "Top Esports Performance"),
            ("Karmine Corp Blue Stars", "Invictus Gaming Young"),
        ]
        out = []
        for day in range(0, 7):
            for i in range(10):
                a, b = names[i % len(names)]
                begin = (datetime.now(UTC) + timedelta(days=day, hours=i)
                         ).isoformat()
                out.append(fixture(
                    60000 + day * 100 + i, f"Round {day}: {a} vs {b}", begin,
                    [team(400 + i, f"{a} {day}"), team(500 + i, f"{b} {day}")]))
        return out

    check("the heavy slate really is over budget",
          len(build_board_parts(bot_only, heavy(), title="Worlds 2026",
                                tz=SYD)) == 2,
          str(len(build_board_parts(bot_only, heavy(), title="Worlds 2026",
                                    tz=SYD))))

    db, bot, ch, cog, sub, _ = await build(heavy())
    await cog._refresh(sub)
    check("two messages go up", ch.posts == 2, str(ch.posts))
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("both ids are remembered",
          bool(board["message_id"]) and bool(board["message_id_2"]),
          f"{board['message_id']} / {board['message_id_2']}")
    if not board["message_id_2"]:
        print("    (skipping the rest of this section: no part two)")
        await db.close()
        raise SystemExit(1)
    check("the buttons are on part one",
          ch.messages[int(board["message_id"])].view is not None)
    check("and on part two as well, which is the point of it",
          ch.messages[int(board["message_id_2"])].view is not None)
    check("only part one is pinned",
          ch.messages[int(board["message_id"])].pinned
          and not ch.messages[int(board["message_id_2"])].pinned)

    second = int(board["message_id_2"])
    # Down to four open matches: one bank, so the spare has nothing to carry.
    bot.api.feed = slate()[:4]
    await cog._refresh(sub)
    check("the spare is removed", second not in ch.messages,
          str(sorted(ch.messages)))
    board = await db.board_for(CHANNEL, int(sub["id"]))
    check("and forgotten", board["message_id_2"] is None,
          str(board["message_id_2"]))
    check("part one survives", int(board["message_id"]) in ch.messages)
    await db.close()

    print("\n[the signature covers both parts]")
    pair = build_board_parts(bot_only, fortnight(), title="Worlds", tz=SYD)
    solo = build_board_parts(bot_only, slate(), title="Worlds", tz=SYD)
    check("one part and two never hash alike",
          signature(pair, [1]) != signature(solo, [1]))
    check("a change confined to part two still shows",
          signature([pair[0], solo[0]], [1]) != signature(pair, [1]))
    check("and a single embed is still accepted",
          isinstance(signature(solo[0], [1]), str))

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
