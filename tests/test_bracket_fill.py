"""A knockout fixture whose feeder match has not been played at digest time.

Reproduces the real case: VCT Champions 2026, lower bracket final at
2026-10-17 07:19Z, whose opponent 100T was only decided at 13:29Z on the 16th
— 13 hours after the midnight digest that owned the match had gone out.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

from src.cogs.digest import Digest                      # noqa: E402
from src.cogs.games import ALL_KEYS                     # noqa: E402
from src.database.db import Database                    # noqa: E402
from src.utils.schedule import digest_window, post_at   # noqa: E402

UTC = timezone.utc
GUILD, CHANNEL, TOURNEY = 7, 500, 21917
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def fixture(mid, name, begin, teams, status="not_started"):
    return {
        "id": mid, "name": name, "status": status, "begin_at": begin,
        "number_of_games": 5, "videogame": {"id": 26, "slug": "valorant"},
        "league": {"id": 1, "name": "VCT"},
        "serie": {"full_name": "Champions 2026"},
        "tournament": {"id": TOURNEY, "name": "Playoffs", "tier": "s"},
        "opponents": [{"opponent": t} for t in teams],
        "streams_list": [],
    }


VIT = {"id": 101, "name": "Team Vitality"}
HUNDRED = {"id": 102, "name": "100 Thieves"}
LOUD = {"id": 103, "name": "LOUD"}
TBD = {"id": None, "name": "TBD"}

LBF = "Lower Bracket Final: VIT vs TBD"
DRAWN = "Lower Bracket Final: VIT vs 100T"

# The real fixture, used for the window arithmetic below.
REAL_KICKOFF = "2026-10-17T07:19:45Z"

# A digest only lists matches that haven't started, so the behavioural cases
# need a kick-off that is still ahead of the clock the code reads. The shape is
# the one that broke: a fixture far enough out that the card covering it is
# posted before the feeder match is played.
KICK = datetime.now(UTC) + timedelta(hours=20)
KICKOFF = KICK.isoformat()


def owning_day(kick, tz, hour):
    """The digest day whose window contains *kick*."""
    for delta in range(-3, 4):
        day = (kick.astimezone(tz) + timedelta(days=delta)).date()
        start, end = digest_window(day, tz, hour)
        if start <= kick < end:
            return day
    raise AssertionError(f"no digest window owns {kick}")


DAY = owning_day(KICK, UTC, 0)


class Icons:
    def icon(self, t):
        return ""

    def partial(self, t):
        return None


class Api:
    """Serves the feed as it looked at one moment."""

    def __init__(self, upcoming, single=None):
        self.upcoming, self.single = upcoming, single or {}
        self.match_lookups = 0

    async def upcoming_matches(self, slug=None, per_page=50, page=1):
        return list(self.upcoming) if page == 1 else []

    async def running_matches(self, slug=None, per_page=50, page=1):
        return []

    async def upcoming_until(self, slug, horizon, **kw):
        return list(self.upcoming)

    async def running_all(self, slug, **kw):
        return []

    async def get_match(self, match_id):
        self.match_lookups += 1
        return self.single[match_id]


class Tourneys:
    async def roster(self, team_id):
        return []


class Channel:
    def __init__(self, cid):
        self.id = cid
        self.guild = type("G", (), {"id": GUILD})()
        self.sent = []

    async def send(self, content=None, embed=None, embeds=None, view=None, **kw):
        self.sent.append({"embeds": embeds or ([embed] if embed else []),
                          "view": view})
        return type("M", (), {"id": len(self.sent), "channel": self})()


class Bot:
    def __init__(self, db, api, channel):
        self.db, self.api, self.icons = db, api, Icons()
        self.tourneys = Tourneys()
        self.settings = type("S", (), {
            "digest_tz_name": "UTC", "digest_hour": 0, "cs_slug": "csgo",
            "top_tier_only": True, "tier_allowlist": frozenset({"s", "a"}),
            "alert_lead_minutes": 30,
        })()
        self._channels = {channel.id: channel}

    def get_channel(self, cid):
        return self._channels.get(cid)


async def build(upcoming, single=None):
    db = Database(str(Path(tempfile.mkdtemp()) / "d.db"))
    await db.connect()
    await db.set_games(guild_id=GUILD, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    sub_id = await db.add_subscription(
        guild_id=GUILD, channel_id=CHANNEL, game="valorant", created_by=1,
        scope="tournament", tournament_id=TOURNEY,
        tournament_name="VCT Playoffs")
    ch = Channel(CHANNEL)
    bot = Bot(db, Api(upcoming, single), ch)
    cog = Digest.__new__(Digest)
    cog.bot = bot
    sub = (await db.list_subscriptions(GUILD))[0]
    return db, bot, ch, cog, sub, sub_id


def buttons(view):
    """Button labels. A DynamicItem wraps the real Button as ``.item``."""
    if not view:
        return []
    return [getattr(c, "item", c).label for c in view.children]


async def worlds_play_in():
    """The second report of this, to the hour: Worlds 2026 Play-In.

    The Play-In grand final kicked off 2026-10-18 18:07Z. On a Sydney midnight
    schedule the card that owns it posted 2026-10-18 00:00 local — and the
    lower bracket final that decided MVK's place did not start until 05:06 that
    morning, finishing around 09:49. So at post time the fixture read "KC vs
    TBD", and the old code dropped it; the alerts still caught it at T-30,
    which is exactly what was reported.

    Rebuilt on the real offsets rather than the real dates, because a digest
    refuses to list a match that has already started and these have.
    """
    print("\n[Worlds 2026 Play-In, on a Sydney midnight schedule]")
    tz = ZoneInfo("Australia/Sydney")
    kick = datetime.now(UTC) + timedelta(hours=29)
    day = owning_day(kick, tz, 0)
    posted = post_at(day, tz, 0).astimezone(UTC)
    start, end = digest_window(day, tz, 0)
    check("the Sydney card owns it", start <= kick < end, f"{start} .. {end}")
    # The root cause, stated as the invariant rather than as a number: the card
    # goes out before the window it covers has even opened, so it is always
    # describing fixtures from far enough back that a bracket can still be open.
    check("the card posts before its own window opens", posted < start,
          f"posted {posted}, window opens {start}")
    check("half a day or more before kick-off",
          (kick - posted).total_seconds() / 3600 > 12,
          f"{(kick - posted).total_seconds() / 3600:.1f}h")

    kc = {"id": 201, "name": "Karmine Corp"}
    mvk = {"id": 202, "name": "MVK Esports"}
    half = fixture(1710931, "Grand Final: KC vs TBD", kick.isoformat(), [kc, TBD])
    db, bot, ch, cog, sub, _ = await build([half])
    bot.settings.digest_tz_name = "Australia/Sydney"
    went_out = await cog._post_digest(sub, day)
    check("nothing listable, so no sheet goes out", went_out is False)
    check("but the fixture is held, not dropped",
          len(await db.pending_digest_matches()) == 1)

    # Ten hours later the lower bracket final is done and MVK are through.
    bot.api.single = {1710931: fixture(
        1710931, "Grand Final: KC vs MVK", kick.isoformat(), [kc, mvk])}
    await cog._fill_brackets()
    check("the card goes out once MVK are known", len(ch.sent) == 1,
          str(len(ch.sent)))
    said = ch.sent[0]["embeds"][0].description if ch.sent else ""
    check("naming both teams", "Karmine Corp" in said and "MVK Esports" in said,
          said)
    check("with a button to pick between them",
          bool(ch.sent) and len(buttons(ch.sent[0]["view"])) == 2,
          str(buttons(ch.sent[0]["view"]) if ch.sent else []))
    check("and in time for the pick to be worth making",
          kick > datetime.now(UTC) + timedelta(hours=12))
    await db.close()


async def main():
    await worlds_play_in()

    print("\n[the window that owned it posted before the feeder was played]")
    start, end = digest_window(date(2026, 10, 16), UTC, 0)
    kick = datetime.fromisoformat(REAL_KICKOFF.replace("Z", "+00:00"))
    check("the 16th's digest owns a 07:19Z match on the 17th",
          start <= kick < end, f"{start} .. {end}")
    check("but that digest posted at 00:00, ten hours before its own window",
          start > datetime(2026, 10, 16, 0, 0, tzinfo=UTC), f"opens {start}")
    feeder_done = datetime(2026, 10, 16, 13, 29, 9, tzinfo=UTC)
    check("and 13 hours after it went out the bracket was still open",
          feeder_done > datetime(2026, 10, 16, 0, 0, tzinfo=UTC))

    print("\n[at digest time: one side still TBD]")
    half = fixture(1685248, LBF, KICKOFF, [VIT, TBD])
    ready = fixture(1685247, "Lower Bracket Semifinal: 100T vs G2", KICKOFF,
                    [HUNDRED, LOUD])
    db, bot, ch, cog, sub, sub_id = await build([half, ready])
    posted = await cog._post_digest(sub, DAY)
    check("the digest still posts", posted is True)
    check("the confirmed match is listed",
          await db.digest_match_count(1) == 1,
          str(await db.digest_match_count(1)))
    pending = await db.pending_digest_matches()
    check("the TBD fixture is held, not dropped", len(pending) == 1,
          str(len(pending)))
    check("and it is the right one",
          bool(pending) and int(pending[0]["match_id"]) == 1685248)
    body = "".join(f.name + f.value
                   for e in ch.sent[0]["embeds"] for f in e.fields)
    check("the card says one is still to be drawn", "Not drawn yet" in body,
          body[:150])
    check("in the singular", "1 more fixture" in body and " is waiting" in body,
          body[:220])
    check("no button for a team that does not exist yet",
          "TBD" not in str(buttons(ch.sent[0]["view"])),
          str(buttons(ch.sent[0]["view"])))

    print("\n[the feeder is played; the sweep fills the bracket]")
    bot.api.single = {1685248: fixture(1685248, DRAWN, KICKOFF, [VIT, HUNDRED])}
    ch.sent.clear()
    await cog._fill_brackets()
    check("a follow-up card goes out", len(ch.sent) == 1, str(len(ch.sent)))
    said = ch.sent[0]["embeds"][0].description
    check("it names both teams",
          "100 Thieves" in said and "Team Vitality" in said, said)
    labels = buttons(ch.sent[0]["view"])
    check("with a button per team", len(labels) == 2, str(labels))
    check("numbered on from the first message",
          all(x.startswith("2. ") for x in labels), str(labels))
    check("the match is votable now",
          await db.digest_match(1, 1685248) is not None)
    check("and no longer pending", await db.pending_digest_matches() == [])

    print("\n[it only posts once]")
    ch.sent.clear()
    await cog._fill_brackets()
    check("a second sweep posts nothing", ch.sent == [], str(len(ch.sent)))
    check("and costs no API call", bot.api.match_lookups == 1,
          str(bot.api.match_lookups))
    await db.close()

    print("\n[while the bracket is still open it just waits]")
    db, bot, ch, cog, sub, _ = await build([fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await cog._post_digest(sub, DAY)
    bot.api.single = {9: fixture(9, LBF, KICKOFF, [VIT, TBD])}
    ch.sent.clear()
    await cog._fill_brackets()
    check("nothing posted", ch.sent == [])
    check("still pending for the next sweep",
          len(await db.pending_digest_matches()) == 1)
    await db.close()

    print("\n[a slot that fills too late is given up, not posted dead]")
    db, bot, ch, cog, sub, _ = await build([fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await cog._post_digest(sub, DAY)
    bot.api.single = {9: fixture(
        9, DRAWN, (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
        [VIT, HUNDRED], status="running")}
    ch.sent.clear()
    await cog._fill_brackets()
    check("no card with dead buttons", ch.sent == [], str(len(ch.sent)))
    check("and it stops being re-checked",
          await db.pending_digest_matches() == [])
    await db.close()

    print("\n[a day whose only fixture is undrawn]")
    db, bot, ch, cog, sub, _ = await build([fixture(9, LBF, KICKOFF, [VIT, TBD])])
    posted = await cog._post_digest(sub, DAY)
    check("no sheet is posted with nothing on it", posted is False)
    check("but the fixture is still held",
          len(await db.pending_digest_matches()) == 1)
    bot.api.single = {9: fixture(9, DRAWN, KICKOFF, [VIT, HUNDRED])}
    await cog._fill_brackets()
    check("and it still reaches the channel once drawn", len(ch.sent) == 1,
          str(len(ch.sent)))
    check("numbered first, since nothing preceded it",
          all(x.startswith("1. ") for x in buttons(ch.sent[0]["view"])),
          str(buttons(ch.sent[0]["view"])))
    await db.close()

    print("\n[predictions off: schedule, no buttons]")
    db, bot, ch, cog, sub, sub_id = await build(
        [fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await db.set_subscription_predictions(sub_id, GUILD, False)
    quiet = (await db.list_subscriptions(GUILD))[0]
    await cog._post_digest(quiet, DAY)
    bot.api.single = {9: fixture(9, DRAWN, KICKOFF, [VIT, HUNDRED])}
    await cog._fill_brackets()
    check("the card still goes out", len(ch.sent) == 1, str(len(ch.sent)))
    check("with no vote buttons", ch.sent[0]["view"] is None)
    await db.close()

    print("\n[the channel unsubscribed in between]")
    db, bot, ch, cog, sub, sub_id = await build(
        [fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await cog._post_digest(sub, DAY)
    await db.remove_subscriptions([sub_id], GUILD)
    bot.api.single = {9: fixture(9, DRAWN, KICKOFF, [VIT, HUNDRED])}
    ch.sent.clear()
    await cog._fill_brackets()
    check("nothing posted into a channel that stopped asking", ch.sent == [])
    check("and the row is cleared", await db.pending_digest_matches() == [])
    await db.close()

    print("\n[a lookup that fails does not take the sweep down]")
    db, bot, ch, cog, sub, _ = await build([fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await cog._post_digest(sub, DAY)

    async def boom(match_id):
        raise RuntimeError("PandaScore is having a day")

    bot.api.get_match = boom
    await cog._fill_brackets()
    check("the sweep survives", True)
    check("and the fixture is kept for the next one",
          len(await db.pending_digest_matches()) == 1)
    await db.close()

    print("\n[a whole round drawn at once is one message, not eight]")
    # The real burst: Worlds Round 1 ended 11:42 and all eight Round 2
    # fixtures appeared between 12:01 and 12:03, so the next sweep had eight
    # pending at the same moment. It posted a card each.
    kick2 = datetime.now(UTC) + timedelta(hours=26)
    undrawn = [fixture(7000 + i, f"Round 2: TBD vs TBD",
                       (kick2 + timedelta(minutes=i * 30)).isoformat(), [TBD, TBD])
               for i in range(8)]
    db, bot, ch, cog, sub, _ = await build(undrawn)
    day2 = owning_day(kick2, UTC, 0)
    await cog._post_digest(sub, day2)
    check("all eight are held", len(await db.pending_digest_matches()) == 8,
          str(len(await db.pending_digest_matches())))

    pairs = [("DK", "T1"), ("CFO", "MKOI"), ("HLE", "GEN"), ("LY", "KC"),
             ("G2", "LOS"), ("IG", "TSW"), ("TES", "AL"), ("TL", "BLG")]
    bot.api.single = {
        7000 + i: fixture(7000 + i, f"Round 2: {a} vs {b}",
                          (kick2 + timedelta(minutes=i * 30)).isoformat(),
                          [{"id": 900 + i, "name": a}, {"id": 950 + i, "name": b}])
        for i, (a, b) in enumerate(pairs)
    }
    ch.sent.clear()
    await cog._fill_brackets()
    check("two messages, five then three — not eight",
          len(ch.sent) == 2, str(len(ch.sent)))
    buttons_out = sum(len(m["view"].children) for m in ch.sent if m["view"])
    check("every fixture still gets its own pair of buttons",
          buttons_out == 16, str(buttons_out))
    check("the first message carries five",
          len(ch.sent[0]["view"].children) == 10,
          str(len(ch.sent[0]["view"].children)))
    check("and the second the other three",
          len(ch.sent[1]["view"].children) == 6,
          str(len(ch.sent[1]["view"].children)))
    head = ch.sent[0]["embeds"][0]
    check("the banner names the round, not eight matchups",
          "Round 2" in (head.description or ""), str(head.description))
    check("and says how many were drawn",
          "8 fixtures" in (head.description or ""), str(head.description))
    check("parts are numbered", "(1/2)" in (head.title or ""), head.title)
    numbers = [getattr(c, "item", c).label.split(".")[0]
               for m in ch.sent for c in m["view"].children]
    check("numbering runs 1 to 8 across both",
          sorted({int(n) for n in numbers}) == list(range(1, 9)),
          str(sorted({int(n) for n in numbers})))
    check("nothing is left pending", await db.pending_digest_matches() == [])
    check("all eight are votable", await db.digest_match_count(1) == 8,
          str(await db.digest_match_count(1)))
    ch.sent.clear()
    await cog._fill_brackets()
    check("and a later sweep adds nothing", ch.sent == [], str(len(ch.sent)))
    await db.close()

    print("\n[a half-drawn round posts only what is drawn]")
    db, bot, ch, cog, sub, _ = await build(undrawn)
    await cog._post_digest(sub, day2)
    bot.api.single = {
        7000 + i: (
            fixture(7000 + i, f"Round 2: {a} vs {b}",
                    (kick2 + timedelta(minutes=i * 30)).isoformat(),
                    [{"id": 900 + i, "name": a}, {"id": 950 + i, "name": b}])
            if i < 3 else
            fixture(7000 + i, "Round 2: TBD vs TBD",
                    (kick2 + timedelta(minutes=i * 30)).isoformat(), [TBD, TBD])
        )
        for i, (a, b) in enumerate(pairs)
    }
    ch.sent.clear()
    await cog._fill_brackets()
    check("one message for the three that are drawn", len(ch.sent) == 1,
          str(len(ch.sent)))
    check("with three pairs of buttons",
          len(ch.sent[0]["view"].children) == 6,
          str(len(ch.sent[0]["view"].children)))
    check("the undrawn five stay pending",
          len(await db.pending_digest_matches()) == 5,
          str(len(await db.pending_digest_matches())))
    check("the banner is singular about parts",
          "(1/" not in (ch.sent[0]["embeds"][0].title or ""),
          ch.sent[0]["embeds"][0].title)
    await db.close()

    print("\n[housekeeping]")
    db, bot, ch, cog, sub, _ = await build([fixture(9, LBF, KICKOFF, [VIT, TBD])])
    await cog._post_digest(sub, DAY)
    await db.conn.execute(
        "UPDATE match_digests SET created_at = datetime('now', '-30 days')")
    await db.conn.commit()
    await db.prune_digests(days=14)
    check("pruning a digest takes its pending rows with it",
          await db.pending_digest_matches() == [])
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
