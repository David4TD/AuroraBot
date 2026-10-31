"""What the result card says a pick was worth.

The card used to print only who was right. That left the standings printed
directly underneath it unreadable: someone leading on eighteen correct calls
while second place has twenty-one looks like an error until you can see that
the leader kept being on the unpopular side of a split. The multiplier is the
whole model, and this is the only place it is ever visible per match.

Everything here goes through the real settle path, so the numbers on the card
are the numbers that were actually banked — if the card and the payout ever
disagree, that is the bug this suite exists to catch.
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

from src.database.db import Database                    # noqa: E402
from src.utils.resultcard import build_result_card      # noqa: E402
from src.utils.scoring import (                         # noqa: E402
    BASE_POINTS, DOUBLE_DOWN, DOUBLE_DOWN_PENALTY, PERFECT_DAY_BONUS,
    payout_for,
)
from src.utils.settle import settle_match               # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
GUILD = 4242
T1, GEN = 101, 102
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


class Icons:
    """No badges minted in a test; the card must render without them."""

    def icon(self, team):
        return ""

    def partial(self, team):
        return None

    def prefix(self, team, text):
        return text


class Bot:
    def __init__(self, db):
        self.db = db
        self.icons = Icons()


def finished(mid=900, stage="Group Stage", name="T1 vs Gen.G", when=None):
    began = when or (NOW - timedelta(hours=2))
    return {
        "id": mid,
        "name": name,
        "status": "finished",
        "begin_at": began.isoformat(),
        "end_at": (began + timedelta(hours=1)).isoformat(),
        "number_of_games": 3,
        "winner_id": T1,
        "videogame": {"id": 1, "slug": "league-of-legends"},
        "league": {"id": 293, "name": "Worlds"},
        "serie": {"full_name": "2026"},
        "tournament": {"id": 77, "name": stage, "tier": "a"},
        "opponents": [{"opponent": {"id": T1, "name": "T1"}},
                      {"opponent": {"id": GEN, "name": "Gen.G"}}],
        "results": [{"team_id": T1, "score": 2}, {"team_id": GEN, "score": 0}],
        "games": [],
    }


def predictions_field(embed):
    for field in embed.fields:
        if field.name.startswith("🔮"):
            return field
    return None


async def seed(db, match, picks, when=None):
    """*picks* is ``[(name, team_id, doubled)]``, newest convention first."""
    starts = (when or (NOW - timedelta(hours=2))).isoformat()
    for n, (name, team, doubled) in enumerate(picks, start=1):
        await db.ensure_user(n, name)
        await db.create_prediction(
            discord_id=n, match_id=int(match["id"]), game="lol",
            predicted_team_id=team,
            predicted_team_name="T1" if team == T1 else "Gen.G",
            opponent_team_name="Gen.G" if team == T1 else "T1",
            match_starts_at=starts, stake=0, guild_id=GUILD,
            tournament_id=77, tournament_name="Group Stage",
        )
        if doubled:
            # By match, not "this member's most recent pick": created_at has
            # one-second resolution, so two picks seeded in the same tick are
            # indistinguishable by time and the token lands on the wrong one.
            cur = await db.conn.execute(
                "SELECT id FROM predictions WHERE discord_id = ? AND match_id = ?",
                (str(n), int(match["id"])),
            )
            await db.set_doubled(int((await cur.fetchone())["id"]), True)


async def fresh():
    db = Database(str(Path(tempfile.mkdtemp()) / "r.db"))
    await db.connect()
    return db, Bot(db)


async def main():
    # ── the split is priced, and the working is shown ───────────────────────
    print("\n[a contrarian call is priced on the card]")
    db, bot = await fresh()
    match = finished()
    # Two of five on the winner: the multiplier is 5/2 = 2.5, past the knee.
    await seed(db, match, [
        ("Alice", T1, False), ("Bob", T1, False),
        ("Carol", GEN, False), ("Dan", GEN, False), ("Erin", GEN, False),
    ])
    await settle_match(bot, int(match["id"]), T1, match)
    embed = await build_result_card(bot, match, "lol", GUILD)
    field = predictions_field(embed)
    expected = payout_for(2, 5)
    check("the field counts the callers", field.name == "🔮 Called it: 2/5",
          field.name)
    check("the working is printed", expected.formula() in field.value,
          field.value)
    check("the working names the split",
          "(2/5 called it)" in field.value, field.value)
    check("and the multiplier is the compressed one, not the raw 2.5",
          f"×{expected.multiplier:.2f}" in field.value
          and "×2.50" not in field.value, field.value)
    check("the winners carry the payout",
          f"✅ **+{expected.points}** — Alice, Bob" in field.value, field.value)
    check("the losers carry no number",
          "❌ Carol, Dan, Erin" in field.value, field.value)
    check("nobody is marked as doubling down",
          "💥" not in field.value, field.value)

    print("\n[the card and the bank agree]")
    board = await db.tournament_leaderboard(GUILD, 77, limit=5)
    banked = {r["display_name"]: int(r["points"]) for r in board}
    check("Alice banked exactly what the card says",
          banked.get("Alice") == expected.points, str(banked))
    check("and so did Bob", banked.get("Bob") == expected.points, str(banked))
    check("the losers banked nothing", banked.get("Carol", 0) == 0, str(banked))
    await db.close()

    # ── who doubled down ────────────────────────────────────────────────────
    print("\n[a token that lands is named and doubled]")
    db, bot = await fresh()
    match = finished(mid=901)
    await seed(db, match, [
        ("Alice", T1, False), ("Bob", T1, True),
        ("Carol", GEN, False), ("Dan", GEN, False),
    ])
    await settle_match(bot, int(match["id"]), T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    base = payout_for(2, 4)
    check("the plain winner is on the base line",
          f"✅ **+{base.points}** — Alice" in field.value, field.value)
    check("the doubled winner is on a line of their own",
          f"💥 **+{base.points * DOUBLE_DOWN}** doubled down — Bob"
          in field.value, field.value)
    check("Bob is not also listed as a plain winner",
          field.value.count("Bob") == 1, field.value)
    board = await db.tournament_leaderboard(GUILD, 77, limit=5)
    banked = {r["display_name"]: int(r["points"]) for r in board}
    check("and Bob really banked double",
          banked.get("Bob") == base.points * DOUBLE_DOWN, str(banked))
    await db.close()

    print("\n[a token that misses shows what it cost]")
    db, bot = await fresh()
    # Banked first, so there is something for the penalty to come out of.
    earlier = finished(mid=902)
    await seed(db, earlier, [("Alice", T1, False), ("Bob", T1, False),
                             ("Carol", GEN, False)])
    await settle_match(bot, 902, T1, earlier)
    match = finished(mid=903)
    await seed(db, match, [("Alice", T1, False), ("Bob", GEN, True),
                           ("Carol", GEN, False)])
    await settle_match(bot, 903, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("the doubled miss is called out",
          "💥 doubled down, missed —" in field.value, field.value)
    check("with the penalty next to the name",
          f"Bob **−{DOUBLE_DOWN_PENALTY}**" in field.value, field.value)
    check("a real minus sign, not a hyphen",
          "**-" not in field.value, field.value)
    check("the plain loser stays on the plain line",
          "❌ Carol" in field.value, field.value)
    await db.close()

    print("\n[a penalty capped by the bank reads as the capped figure]")
    db, bot = await fresh()
    match = finished(mid=904)
    # Bob has banked nothing at all, so the miss can cost nothing.
    await seed(db, match, [("Alice", T1, False), ("Bob", GEN, True),
                           ("Carol", GEN, False)])
    await settle_match(bot, 904, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("the card says 0, not the uncapped penalty",
          "Bob **0**" in field.value, field.value)
    check("and still shows the token was spent",
          "💥 doubled down, missed" in field.value, field.value)
    board = await db.tournament_leaderboard(GUILD, 77, limit=5)
    check("which is what the board did too",
          all(int(r["points"]) >= 0 for r in board),
          str([(r["display_name"], r["points"]) for r in board]))
    await db.close()

    # ── the stage, and the bonus that lands on one pick ─────────────────────
    print("\n[a final's weight is in the working]")
    db, bot = await fresh()
    match = finished(mid=905, stage="Playoffs", name="Grand final: T1 vs GEN")
    await seed(db, match, [("Alice", T1, False), ("Bob", GEN, False),
                           ("Carol", GEN, False)])
    await settle_match(bot, 905, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("the stage multiplier is shown", "×2 stage" in field.value,
          field.value)
    check("and the payout includes it",
          f"✅ **+{payout_for(1, 3, weight=2.0).points}** — Alice"
          in field.value, field.value)
    await db.close()

    print("\n[a perfect day is attributed, not left looking like a mistake]")
    db, bot = await fresh()
    day = NOW - timedelta(hours=3)
    first = finished(mid=906, when=day)
    second = finished(mid=907, when=day + timedelta(hours=1))
    # Alice sweeps both; Bob is wrong on the second, so only Alice sweeps.
    await seed(db, first, [("Alice", T1, False), ("Bob", T1, False),
                           ("Carol", GEN, False)], when=day)
    await settle_match(bot, 906, T1, first)
    await seed(db, second, [("Alice", T1, False), ("Bob", GEN, False),
                            ("Carol", GEN, False)], when=day)
    await settle_match(bot, 907, T1, second)
    field = predictions_field(await build_result_card(bot, second, "lol", GUILD))
    check("the bonus is shown against the person who earned it",
          f"Alice ☀️+{PERFECT_DAY_BONUS}" in field.value, field.value)
    check("the base payout line is still the going rate",
          f"✅ **+{payout_for(1, 3).points}**" in field.value, field.value)
    board = await db.tournament_leaderboard(GUILD, 77, limit=5)
    banked = {r["display_name"]: int(r["points"]) for r in board}
    check("and the total really does include the bonus",
          banked["Alice"] == payout_for(2, 3).points
          + payout_for(1, 3).points + PERFECT_DAY_BONUS, str(banked))
    await db.close()

    # ── the quiet cases ─────────────────────────────────────────────────────
    print("\n[nothing is invented when there is nothing to price]")
    db, bot = await fresh()
    match = finished(mid=908)
    embed = await build_result_card(bot, match, "lol", GUILD)
    check("no picks means no prediction field at all",
          predictions_field(embed) is None,
          str([f.name for f in embed.fields]))

    print("\n[an unsettled pick falls back to the roll call]")
    await seed(db, match, [("Alice", T1, False), ("Bob", GEN, False)])
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("the names still appear", "✅ Alice" in field.value, field.value)
    check("and the wrong side too", "❌ Bob" in field.value, field.value)
    check("but no price is quoted", "base" not in field.value, field.value)
    check("and nothing claims a payout", "**+" not in field.value, field.value)

    print("\n[and once it settles, the price appears]")
    await settle_match(bot, 908, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("the working is there now", "base" in field.value, field.value)
    check("with the payout", "✅ **+" in field.value, field.value)
    await db.close()

    print("\n[a small room is not priced as a read of it]")
    db, bot = await fresh()
    match = finished(mid=909)
    await seed(db, match, [("Alice", T1, False), ("Bob", GEN, False)])
    await settle_match(bot, 909, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("two voters pay face value",
          f"✅ **+{BASE_POINTS}** — Alice" in field.value, field.value)
    check("and no multiplier is claimed", "called it)" not in field.value,
          field.value)
    check("the base is still stated", f"{BASE_POINTS} base" in field.value,
          field.value)
    await db.close()

    print("\n[the field stays inside Discord's limit]")
    db, bot = await fresh()
    match = finished(mid=910)
    crowd = [(f"Person{n:02d}", T1 if n % 2 else GEN, n % 5 == 0)
             for n in range(1, 41)]
    await seed(db, match, crowd)
    await settle_match(bot, 910, T1, match)
    field = predictions_field(await build_result_card(bot, match, "lol", GUILD))
    check("40 predictors still fit in one field", len(field.value) <= 1024,
          str(len(field.value)))
    check("and the overflow is counted rather than dropped silently",
          "_+" in field.value, field.value)
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
