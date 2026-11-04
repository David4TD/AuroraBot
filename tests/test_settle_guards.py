"""Settlement that can't pay twice, and a perfect day that is the server's day.

Each section here was a way points could land on a board that nobody earned:
two settles of the same match racing, a day paid again when a later pick on
it won, and a day judged by its UTC date rather than the server's own. Plus
the two places a pick's own record could be wrong: a pick in one server
changing another server's, and a kick-off time that moved after people picked.
"""
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

from src.database.db import Database                    # noqa: E402
from src.utils.predictions import Outcome, submit_prediction  # noqa: E402
from src.utils.scoring import PERFECT_DAY_BONUS, payout_for  # noqa: E402
from src.utils.settle import settle_match               # noqa: E402

UTC = timezone.utc
SYD = ZoneInfo("Australia/Sydney")
GUILD, OTHER = 4242, 9999
T1, GEN = 101, 102
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


class Bot:
    def __init__(self, db, tz="UTC"):
        self.db = db
        self.settings = type("S", (), {"digest_tz_name": tz})()


def match(mid, begin):
    return {"id": mid, "name": "T1 vs Gen.G", "status": "finished",
            "begin_at": begin.isoformat().replace("+00:00", "Z"),
            "winner_id": T1,
            "tournament": {"id": 77, "name": "Group Stage"}}


async def pick(db, user, name, m, team, guild=GUILD):
    return await submit_prediction(
        db, user_id=user, display_name=name, match_id=int(m["id"]), game="lol",
        team={"id": team, "name": "T1" if team == T1 else "Gen.G"},
        opponent=None, begin_at=m["begin_at"], guild_id=guild,
        tournament_id=77, tournament_name="Group Stage",
    )


async def backdate(db, m):
    """Picks are made before kick-off; settling needs the match in the past."""
    await db.conn.execute(
        "UPDATE predictions SET match_starts_at = ? WHERE match_id = ?",
        (m["begin_at"], int(m["id"])))
    await db.conn.commit()


async def fresh(tz="UTC"):
    db = Database(str(Path(tempfile.mkdtemp()) / "s.db"))
    await db.connect()
    return db, Bot(db, tz)


async def total(db, user):
    cur = await db.conn.execute(
        "SELECT COALESCE(SUM(points_awarded), 0) AS t FROM predictions "
        "WHERE discord_id = ?", (str(user),))
    return int((await cur.fetchone())["t"])


async def lifetime(db, user):
    cur = await db.conn.execute(
        "SELECT points, predictions_won FROM users WHERE discord_id = ?",
        (str(user),))
    row = await cur.fetchone()
    return int(row["points"]), int(row["predictions_won"])


async def main():
    future = datetime.now(UTC) + timedelta(days=1)

    print("\n[two settles of the same match at once pay once]")
    db, bot = await fresh()
    day = datetime(2026, 11, 3, 12, tzinfo=UTC)
    first, second = match(1, future), match(2, future)
    for m in (first, second):
        await pick(db, 1, "Alice", m, T1)
        await pick(db, 2, "Bob", m, GEN)
        await pick(db, 3, "Carol", m, GEN)
    first["begin_at"] = day.isoformat().replace("+00:00", "Z")
    second["begin_at"] = (day + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    await backdate(db, first)
    await backdate(db, second)
    await settle_match(bot, 1, T1, first)
    counts = await asyncio.gather(settle_match(bot, 2, T1, second),
                                  settle_match(bot, 2, T1, second))
    check("between them they settle each pick once", sum(counts) == 3,
          str(counts))
    expected = 2 * payout_for(1, 3).points + PERFECT_DAY_BONUS
    check("the board total is one payout per match plus one perfect day",
          await total(db, 1) == expected, f"{await total(db, 1)} vs {expected}")
    points, won = await lifetime(db, 1)
    check("the lifetime total agrees", points == expected, str(points))
    check("and two wins are two wins, not three", won == 2, str(won))
    await db.close()

    print("\n[a day pays once, even if a later pick on it wins too]")
    db, bot = await fresh()
    base = datetime(2026, 11, 3, 1, tzinfo=UTC)
    ms = [match(10 + n, future) for n in range(3)]
    for m in ms[:2]:
        await pick(db, 1, "Alice", m, T1)
        await pick(db, 2, "Bob", m, GEN)
        await pick(db, 3, "Carol", m, GEN)
    for n, m in enumerate(ms[:2]):
        m["begin_at"] = (base + timedelta(hours=n)).isoformat()
        await backdate(db, m)
        await settle_match(bot, int(m["id"]), T1, m)
    after_two = await total(db, 1)
    check("the clean pair pays the bonus",
          after_two == 2 * payout_for(1, 3).points + PERFECT_DAY_BONUS,
          str(after_two))
    late = ms[2]
    await pick(db, 1, "Alice", late, T1)
    await pick(db, 2, "Bob", late, GEN)
    await pick(db, 3, "Carol", late, GEN)
    late["begin_at"] = (base + timedelta(hours=5)).isoformat()
    await backdate(db, late)
    await settle_match(bot, int(late["id"]), T1, late)
    check("a third win on the same day adds only its own payout",
          await total(db, 1) == after_two + payout_for(1, 3).points,
          f"{await total(db, 1)} vs {after_two + payout_for(1, 3).points}")
    await db.close()

    print("\n[the day is the server's, not UTC's]")
    db, bot = await fresh("Australia/Sydney")
    # 10:00 and 12:00 Sydney on the same local day: 23:00 and 01:00 UTC,
    # which are two different UTC dates.
    local = datetime(2026, 11, 4, 10, tzinfo=SYD)
    a, b = match(20, future), match(21, future)
    for m in (a, b):
        await pick(db, 1, "Alice", m, T1)
        await pick(db, 2, "Bob", m, GEN)
        await pick(db, 3, "Carol", m, GEN)
    a["begin_at"] = local.astimezone(UTC).isoformat()
    b["begin_at"] = (local + timedelta(hours=2)).astimezone(UTC).isoformat()
    check("the fixture really does straddle a UTC date",
          a["begin_at"][:10] != b["begin_at"][:10],
          f"{a['begin_at']} / {b['begin_at']}")
    for m in (a, b):
        await backdate(db, m)
        await settle_match(bot, int(m["id"]), T1, m)
    check("calling the whole local day pays the bonus",
          await total(db, 1) == 2 * payout_for(1, 3).points + PERFECT_DAY_BONUS,
          str(await total(db, 1)))
    cur = await db.conn.execute("SELECT day FROM perfect_days")
    days = [r["day"] for r in await cur.fetchall()]
    check("recorded against the local date", days == ["2026-11-04"], str(days))

    print("\n[and a local day boundary still separates two days]")
    db2, bot2 = await fresh("Australia/Sydney")
    late_night = datetime(2026, 11, 4, 23, tzinfo=SYD)
    c, d = match(30, future), match(31, future)
    for m in (c, d):
        await pick(db2, 1, "Alice", m, T1)
        await pick(db2, 2, "Bob", m, GEN)
        await pick(db2, 3, "Carol", m, GEN)
    c["begin_at"] = late_night.astimezone(UTC).isoformat()
    d["begin_at"] = (late_night + timedelta(hours=2)).astimezone(UTC).isoformat()
    check("same UTC date", c["begin_at"][:10] == d["begin_at"][:10])
    for m in (c, d):
        await backdate(db2, m)
        await settle_match(bot2, int(m["id"]), T1, m)
    check("but two local days, so no bonus for either",
          await total(db2, 1) == 2 * payout_for(1, 3).points,
          str(await total(db2, 1)))
    await db.close()
    await db2.close()

    print("\n[a pick in one server does not move another server's]")
    db, bot = await fresh()
    m = match(40, future)
    out, _ = await pick(db, 1, "Alice", m, T1, guild=GUILD)
    check("server A takes the pick", out is Outcome.CREATED)
    out, text = await pick(db, 1, "Alice", m, GEN, guild=OTHER)
    check("server B refuses to change it", out is Outcome.CLOSED, text)
    row = await db.get_prediction(1, 40)
    check("server A's pick is untouched",
          int(row["predicted_team_id"]) == T1
          and str(row["guild_id"]) == str(GUILD), dict(row).__repr__())
    out, _ = await pick(db, 1, "Alice", m, GEN, guild=GUILD)
    check("server A can still change its own", out is Outcome.CHANGED)
    await db.close()

    print("\n[a kick-off that moves is followed]")
    db, bot = await fresh()
    m = match(50, future)
    await pick(db, 1, "Alice", m, T1)
    earlier = (future - timedelta(hours=3)).isoformat()
    changed = await db.refresh_match_starts([(50, earlier), (51, earlier)])
    check("the open pick takes the new time", changed == 1, str(changed))
    row = await db.get_prediction(1, 50)
    check("stored", row["match_starts_at"] == earlier, row["match_starts_at"])
    check("an unchanged time writes nothing",
          await db.refresh_match_starts([(50, earlier)]) == 0)
    check("a missing time is ignored, not blanked",
          await db.refresh_match_starts([(50, None)]) == 0)
    await backdate(db, m)
    await settle_match(bot, 50, T1, m)
    check("a settled pick keeps the time it settled with",
          await db.refresh_match_starts([(50, earlier)]) == 0)
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
