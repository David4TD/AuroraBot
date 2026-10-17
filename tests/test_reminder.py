"""The 30-minute reminder card."""
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
from src.utils.remindercard import build_reminder_card, worth_line  # noqa: E402
from src.utils.scoring import (                         # noqa: E402
    BASE_POINTS, DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, FINAL_WEIGHT,
    MAX_MULTIPLIER, PLAYOFF_WEIGHT,
)

UTC = timezone.utc
NOW = datetime.now(UTC)
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def fixture(mid=555, name="Semifinal 1: T1 vs GEN", stage="Playoffs",
            best_of=5, minutes=30, streams=True):
    m = {
        "id": mid, "name": name, "status": "not_started",
        "begin_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "number_of_games": best_of, "match_type": "best_of",
        "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": 293, "name": "LCK"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": 88, "name": stage, "tier": "a"},
        "opponents": [{"opponent": {"id": 101, "name": "T1"}},
                      {"opponent": {"id": 102, "name": "Gen.G"}}],
    }
    if streams:
        m["streams_list"] = [{"main": True, "raw_url": "https://twitch.tv/lck"}]
    return m


class Icons:
    def icon(self, t): return ""
    def partial(self, t): return None


class Bot:
    def __init__(self, db=None):
        self.db, self.icons = db, Icons()
        self.settings = type("S", (), {
            "alert_lead_minutes": 30, "cs_slug": "csgo", "top_tier_only": True,
            "tier_allowlist": frozenset({"s", "a"}),
        })()
        self._channels = {}

    def get_channel(self, cid): return self._channels.get(cid)


class Channel:
    def __init__(self, cid, guild_id=7):
        self.id = cid
        self.guild = type("G", (), {"id": guild_id})()
        self.sent = []

    async def send(self, content=None, embed=None, view=None, **kw):
        self.sent.append((content, embed, view))
        return type("M", (), {"id": 1, "channel": self, "guild": self.guild})()


def field(embed, needle):
    return next((f for f in embed.fields if needle in f.name), None)


async def main():
    bot = Bot()

    print("\n[the card]")
    e = build_reminder_card(bot, fixture(), "lol")
    check("marked as starting soon", e.title.startswith("⏰ Starting soon"),
          e.title)
    check("event named", "LCK" in e.title and "Playoffs" in e.title, e.title)
    check("both teams in the description",
          "T1" in e.description and "Gen.G" in e.description, e.description)
    check("a countdown, not a bare date",
          ":R>" in e.description, e.description)
    check("the countdown alone, no wall clock",
          ":t>" not in e.description, e.description)
    check("best-of shown once", e.description.count("Bo5") == 1, e.description)
    check("the stream is linked", "twitch.tv/lck" in e.description, e.description)
    check("predictions-close note in the footer",
          "close at kick-off" in e.footer.text, e.footer.text)

    print("\n[what the old generic embed wasted]")
    names = [f.name for f in e.fields]
    check("no Format field", not any("Format" in n for n in names), str(names))
    check("no duplicate Best of field",
          not any(n == "Best of" for n in names), str(names))
    check("no constant Tier field", not any("Tier" in n for n in names), str(names))
    check("no internal match id", "555" not in (e.footer.text or ""),
          e.footer.text)

    print("\n[it makes the case for predicting, in one line]")
    check("no fields at all now", e.fields == [], str(names))
    worth = e.description.split("\n")[-1]
    check("the range is on its own line",
          worth.startswith("🎲") and "pts" in worth, worth)
    check("low end is base × stage",
          f"**{int(BASE_POINTS * PLAYOFF_WEIGHT)}–" in worth, worth)
    check("high end is the capped read",
          f"{int(BASE_POINTS * MAX_MULTIPLIER * PLAYOFF_WEIGHT)}**" in worth,
          worth)
    check("the token names both halves",
          f"×{DOUBLE_DOWN}" in worth and f"−{DOUBLE_DOWN_PENALTY}" in worth,
          worth)
    check("/scoring moved to the footer", "/scoring" in e.footer.text,
          e.footer.text)
    check("the whole card is three lines",
          len(e.description.split("\n")) == 3, e.description)

    print("\n[the stage changes what it's worth]")
    group = build_reminder_card(bot, fixture(name="Group A", stage="Group Stage"), "lol")
    check("a group game has no stage tag",
          "stage ×" not in group.description, group.description)
    check("and is worth the plain base",
          f"**{BASE_POINTS}–" in group.description, group.description)
    final = build_reminder_card(bot, fixture(name="Grand final"), "lol")
    check("the final is tagged, by name", "🏆 Grand final ×2" in final.description,
          final.description)
    check("with the doubled figures",
          f"**{int(BASE_POINTS * FINAL_WEIGHT)}–"
          f"{int(BASE_POINTS * MAX_MULTIPLIER * FINAL_WEIGHT)}**"
          in final.description, final.description)
    semi = build_reminder_card(bot, fixture(name="Semifinal 1"), "lol")
    check("a semifinal is bracket, not final",
          "Semifinal ×1.5" in semi.description, semi.description)
    check("and isn't called the final anywhere",
          "Grand final" not in semi.description, semi.description)

    print("\n[the worth line on its own]")
    plain = worth_line(fixture(stage="Group Stage", name="x"))
    check("group stage", f"**{BASE_POINTS}–" in plain, plain)
    check("it doesn't re-explain the stage, which is on the line above",
          "counts" not in plain and "bracket" not in plain, plain)

    print("\n[when predictions are off for the subscription]")
    quiet = build_reminder_card(bot, fixture(), "lol", predictable=False)
    check("no call-to-action", "pts" not in quiet.description, quiet.description)
    check("but the fixture still renders", "T1" in quiet.description)

    print("\n[degrading]")
    e = build_reminder_card(bot, fixture(streams=False), "lol")
    check("no stream, no link", "Watch" not in e.description, e.description)
    noclock = fixture()
    noclock["begin_at"] = None
    e = build_reminder_card(bot, noclock, "lol")
    check("no start time, no countdown", "<t:" not in e.description, e.description)
    check("but it still renders", "T1" in e.description)
    bo1 = build_reminder_card(bot, fixture(best_of=1), "lol")
    check("a Bo1 doesn't say 'Bo1'", "Bo1" not in bo1.description, bo1.description)
    walkover = fixture()
    walkover["opponents"] = walkover["opponents"][:1]
    check("a one-sided fixture degrades, not crashes",
          build_reminder_card(bot, walkover, "lol") is not None)

    print("\n[and never leaks the vote split before kick-off]")
    blob = (e.description or "") + "".join(f.name + f.value for f in e.fields)
    for word in ("voted", "backed", "Server picks", "called it"):
        check(f"no {word!r} on a reminder", word not in blob, blob[:150])

    print("\n[end to end: the alert posts it]")
    db = Database(str(Path(tempfile.mkdtemp()) / "r.db"))
    await db.connect()
    await db.set_games(guild_id=7, enabled=set(ALL_KEYS), known=ALL_KEYS,
                       updated_by=1)
    sub_id = await db.add_subscription(
        guild_id=7, channel_id=500, game="lol", created_by=1,
        scope="tournament", tournament_id=88, tournament_name="LCK Playoffs")
    ch = Channel(500)
    bot2 = Bot(db)
    bot2._channels[500] = ch
    cog = Alerts.__new__(Alerts)
    cog.bot = bot2
    sub = (await db.list_subscriptions(7))[0]

    await cog._announce(sub, fixture(), state="reminder")
    check("the reminder went out", len(ch.sent) == 1, str(len(ch.sent)))
    content, embed, view = ch.sent[0]
    check("the ping says when", "Starting in" in content, content)
    check("and points at the buttons", "Tap a team" in content, content)
    check("it's the new card", embed.title.startswith("⏰ Starting soon"),
          embed.title)
    check("with two vote buttons", view is not None and len(view.children) == 2,
          str(len(view.children) if view else 0))

    await db.set_subscription_predictions(sub_id, 7, False)
    quiet_sub = (await db.list_subscriptions(7))[0]
    ch.sent.clear()
    await cog._announce(quiet_sub, fixture(mid=556), state="reminder")
    content, embed, view = ch.sent[0]
    check("predictions off: no buttons", view is None)
    check("no call to action either", "pts" not in embed.description)
    check("and the ping doesn't promise one", "Tap a team" not in content,
          content)

    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
