"""The live card's series line, the upcoming list, and the double-down copy."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

import discord                                          # noqa: E402
from src.cogs.meta import Meta                          # noqa: E402
from src.database.db import Database                    # noqa: E402
from src.utils.conviction import DoubleDownButton       # noqa: E402
from src.utils.livecard import build_live_card, map_progress  # noqa: E402
from src.utils.scoring import (                         # noqa: E402
    BASE_POINTS, DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, payout_for,
)
from src.utils.upcominglist import (                    # noqa: E402
    MAX_MATCHES, MAX_PER_DAY, build_upcoming_list,
)

UTC = timezone.utc
SYD = ZoneInfo("Australia/Sydney")
NOW = datetime.now(UTC)
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def game(pos, winner, status="finished"):
    return {"position": pos, "status": status, "length": 2100,
            "winner": {"id": winner} if winner else None}


def live(mid=555, played=0, best_of=5, started_min=0):
    games = [game(i + 1, 101) for i in range(played)]
    games += [game(played + i + 1, None, "not_started")
              for i in range(best_of - played)]
    return {
        "id": mid, "name": "Semifinal 1", "status": "running",
        "begin_at": (NOW - timedelta(minutes=started_min)).isoformat(),
        "number_of_games": best_of,
        "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": 293, "name": "LCK"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": 88, "name": "Playoffs", "tier": "a"},
        "opponents": [{"opponent": {"id": 101, "name": "T1"}},
                      {"opponent": {"id": 102, "name": "Gen.G"}}],
        "results": [{"team_id": 101, "score": played},
                    {"team_id": 102, "score": 0}],
        "games": games,
    }


def soon(mid, hours, teams=(("T1", 101), ("Gen.G", 102)), best_of=3,
         stage="Playoffs", league="LCK", name="match"):
    return {
        "id": mid, "name": name, "status": "not_started",
        "begin_at": (NOW + timedelta(hours=hours)).isoformat(),
        "number_of_games": best_of,
        "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": 293, "name": league},
        "serie": {"full_name": "2026"},
        "tournament": {"id": 88, "name": stage, "tier": "a"},
        "opponents": [{"opponent": {"id": t[1], "name": t[0]}} for t in teams],
    }


class Icons:
    def icon(self, t): return ""
    def partial(self, t): return None


class Bot:
    def __init__(self, db=None):
        self.db, self.icons = db, Icons()
        self.settings = type("S", (), {
            "alert_lead_minutes": 30, "top_tier_only": True,
            "tier_allowlist": frozenset({"s", "a"}),
        })()


class Interaction:
    def __init__(self, bot):
        self.client = bot
        self.embed = None

    @property
    def response(self): return self

    async def send_message(self, content=None, *, embed=None, **kw):
        self.embed = embed


async def main():
    bot = Bot()

    print("\n[a series that has just gone live]")
    fresh = live(played=0, best_of=5, started_min=0)
    check("no maps done reads as the format, not as progress",
          map_progress(fresh) == "Bo5", map_progress(fresh))
    e = await build_live_card(bot, fresh, "lol")
    check("the card says Bo5", "Bo5" in e.description, e.description)
    check("and not 'Map 1 of 5'", "Map 1 of 5" not in e.description,
          e.description)
    check("nor 'live 0m'", "live 0m" not in e.description, e.description)
    check("the score still leads", "**0 — 0**" in e.description, e.description)

    print("\n[once maps are actually won]")
    check("one map in", map_progress(live(played=1, best_of=5)) == "Map 2 of 5",
          map_progress(live(played=1, best_of=5)))
    check("three in", map_progress(live(played=3, best_of=5)) == "Map 4 of 5",
          map_progress(live(played=3, best_of=5)))
    check("never past the end",
          map_progress(live(played=5, best_of=5)) == "Map 5 of 5",
          map_progress(live(played=5, best_of=5)))
    e = await build_live_card(bot, live(played=2, best_of=5, started_min=72),
                              "lol")
    check("progress and elapsed appear together",
          "Map 3 of 5" in e.description and "live 1h 12m" in e.description,
          e.description)

    print("\n[edge cases on the series line]")
    bare = live(played=0, best_of=5)
    bare["games"] = []
    check("no games array still gives the format",
          map_progress(bare) == "Bo5", map_progress(bare))
    bare["number_of_games"] = 1
    check("a Bo1 says nothing at all", map_progress(bare) == "",
          map_progress(bare))
    e = await build_live_card(bot, live(played=0, best_of=1, started_min=0), "lol")
    check("a fresh Bo1 has no series line at all",
          "Bo" not in e.description and "Map" not in e.description,
          e.description)

    print("\n[the upcoming list: one line per match]")
    matches = [soon(1, 2), soon(2, 5, teams=(("HLE", 103), ("DK", 104)))]
    e = build_upcoming_list(bot, matches, "League of Legends", SYD)
    body = "\n".join(f.value for f in e.fields)
    check("two matches, two lines", len(body.split("\n")) == 2, repr(body))
    check("no indented continuation line", "╰" not in body, body)
    check("both the clock and the countdown",
          body.count(":t>") == 2 and body.count(":R>") == 2, body)
    check("best-of on the line", body.count("Bo3") == 2, body)

    print("\n[a single event is named once, in the footer]")
    check("not repeated on every line", "LCK" not in body, body)
    check("but stated in the footer", "LCK" in e.footer.text, e.footer.text)
    check("and how to play", "/predict" in e.footer.text, e.footer.text)

    print("\n[several events put it back on the line]")
    mixed = [soon(1, 2), soon(9, 3, league="EWC", teams=(("FUT", 5), ("magic", 6)))]
    e = build_upcoming_list(bot, mixed, "League of Legends", SYD)
    body = "\n".join(f.value for f in e.fields)
    check("each line carries its own event",
          "LCK" in body and "EWC" in body, body)
    check("so the footer doesn't", "LCK" not in (e.footer.text or ""),
          e.footer.text)

    print("\n[weighted stages are flagged, because they pay more]")
    weighted = [
        soon(1, 2, name="Grand final: T1 vs GEN"),
        soon(2, 4, name="Semifinal 1: HLE vs DK"),
        soon(3, 6, name="BLG vs AL", stage="Regular Season"),
    ]
    e = build_upcoming_list(bot, weighted, "LoL", SYD)
    body = "\n".join(f.value for f in e.fields)
    check("the final is marked", "🏆 Grand final ×2" in body, body)
    check("the bracket match is marked", "Semifinal ×1.5" in body, body)
    # A bare "x2" is the number a double down pays too. The multiplier has to
    # be attached to the round, or the row reads as "spend a token here".
    check("and says which multiplier it is",
          body.count("×") == 2 and "· ×2" not in body and "· ×1.5" not in body,
          body)
    check("no unnamed 'stage ×' left where the round is known",
          "stage ×" not in body, body)
    check("the group game is not",
          body.split("\n")[2].count("×") == 0, body.split("\n")[2])
    check("the footer says the bonus is automatic",
          "automatic" in e.footer.text, e.footer.text)
    check("and that it stacks with a token",
          "double down" in e.footer.text, e.footer.text)

    print("\n[upcoming edge cases]")
    tbd = soon(4, 3)
    tbd["opponents"] = tbd["opponents"][:1]
    e = build_upcoming_list(bot, [tbd], "LoL", SYD)
    check("an unconfirmed slot says TBD",
          "TBD" in "".join(f.value for f in e.fields))
    undated = soon(5, 3)
    undated["begin_at"] = None
    e = build_upcoming_list(bot, [undated], "LoL", SYD)
    check("no date gets its own group",
          any("confirmed" in f.name.lower() for f in e.fields),
          str([f.name for f in e.fields]))
    many = [soon(100 + i, 1 + i * 0.1) for i in range(MAX_MATCHES + 6)]
    e = build_upcoming_list(bot, many, "LoL", SYD)
    check("capped", sum(f.value.count(":t>") for f in e.fields) <= MAX_MATCHES,
          str(sum(f.value.count(":t>") for f in e.fields)))
    check("and says how many were left off",
          any("more" in f.value for f in e.fields))
    check("every field inside Discord's limit",
          all(len(f.value) <= 1024 for f in e.fields))
    check("empty list still renders", build_upcoming_list(
        bot, [], "LoL", SYD).title is not None)

    print("\n[the double down says what it risks, before the click]")
    btn = DoubleDownButton(1, False, 3)
    check("the multiplier is on the button", f"×{DOUBLE_DOWN}" in btn.label,
          btn.label)
    check("so is the cost", f"−{DOUBLE_DOWN_PENALTY}" in btn.label, btn.label)
    check("and how many are left", "3 left" in btn.label, btn.label)
    check("within Discord's 80-char label cap", len(btn.label) <= 80,
          f"{len(btn.label)}: {btn.label}")
    back = DoubleDownButton(1, True, 2)
    check("taking it back stays plain", "Take the double down back" == back.label,
          back.label)

    print("\n[/scoring explains the model in Discord]")
    db = Database(str(Path(tempfile.mkdtemp()) / "m.db"))
    await db.connect()
    cog = Meta.__new__(Meta)
    cog.bot = Bot(db)
    i = Interaction(cog.bot)
    await cog.scoring.callback(cog, i)
    e = i.embed
    check("it renders", e is not None)
    blob = (e.description or "") + "".join(
        f.name + f.value for f in e.fields) + (e.footer.text or "")
    check("names the base", str(BASE_POINTS) in blob)
    check("names the penalty", f"−{DOUBLE_DOWN_PENALTY}" in blob, blob[:200])
    check("calls it the only way to lose points",
          "only way to lose" in blob.lower())
    check("says a board can't go below zero", "below zero" in blob.lower())
    check("mentions tokens are per tournament",
          "per tournament" in blob.lower())
    check("shows a worked underdog example",
          str(payout_for(1, 5).points) in blob and str(payout_for(4, 5).points) in blob)
    check("covers the stage weighting", "Grand final" in blob)
    check("separates the stage bonus from the token",
          "two ×2s are different" in blob, blob[:120])
    check("and shows them stacking",
          str(payout_for(1, 5, doubled=True, weight=2.0).points) in blob)
    check("while the penalty stays flat",
          "never more" in blob or "never the risk" in blob)
    check("and the perfect day", "Perfect day" in blob)
    check("within Discord's embed limits",
          all(len(f.value) <= 1024 for f in e.fields) and len(blob) <= 6000)

    i = Interaction(cog.bot)
    await cog.help.callback(cog, i)
    play = next(f for f in i.embed.fields if "Play" in f.name)
    check("/help points at it", "/scoring" in play.value, play.value)

    print("\n[the README matches the code]")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("the old stake budget is gone", "500-point budget" not in readme)
    check("the old streak rule is gone",
          "points = stake × underdog × streak" not in readme)
    check("the double down has its own section", "### 💥 Double down" in readme)
    check("it states the penalty", f"−{DOUBLE_DOWN_PENALTY}" in readme)
    check("the README distinguishes the two multipliers",
          "two ×2s are different" in readme, "")
    check("and that a board can't go negative",
          "never go negative" in readme.lower() or "dip but never" in readme.lower())
    check("it points at /scoring", "/scoring" in readme)
    check("the perfect day figure is current",
          "+20" in readme and "+50" not in readme.split("## ⚙️")[0])

    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
