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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

from src.cogs.digest import Digest                      # noqa: E402
from src.cogs.games import ALL_KEYS                     # noqa: E402
from src.database.db import Database                    # noqa: E402
from src.utils.schedule import digest_window            # noqa: E402

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


async def main():
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
