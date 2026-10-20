"""The new table has to appear on a database that predates it."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

from src.database.db import Database                    # noqa: E402

failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


async def tables(db):
    cur = await db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r["name"] for r in await cur.fetchall()}


async def main():
    path = str(Path(tempfile.mkdtemp()) / "m.db")

    ADDED = {"digest_pending", "event_boards", "board_matches"}

    print("\n[a fresh database]")
    db = Database(path)
    await db.connect()
    for name in sorted(ADDED):
        check(f"has {name}", name in await tables(db))
    await db.close()

    print("\n[an existing one that predates them]")
    db = Database(path)
    await db.connect()
    for name in sorted(ADDED):
        await db.conn.execute(f"DROP TABLE {name}")
    await db.conn.commit()
    check("dropped for the test", not (ADDED & await tables(db)))
    await db.close()

    db = Database(path)
    await db.connect()
    check("the next startup recreates them", ADDED <= await tables(db),
          str(sorted(ADDED - await tables(db))))
    check("and nothing else was lost",
          {"match_digests", "digest_matches", "predictions",
           "alert_subscriptions"} <= await tables(db))

    print("\n[startup is idempotent]")
    # A real digest row first: digest_pending is a child of match_digests, so
    # inventing an id would only prove the foreign key works.
    digest_id = await db.create_digest(
        guild_id=7, channel_id=500, subscription_id=1, tournament_id=88,
        tournament_name="VCT Playoffs", game="valorant", local_date="2026-10-16")
    await db.add_digest_pending(digest_id, [{"match_id": 42, "begin_at": None}])
    await db.close()
    db = Database(path)
    await db.connect()
    cur = await db.conn.execute("SELECT COUNT(*) AS n FROM digest_pending")
    row = await cur.fetchone()
    check("a second startup keeps the rows", int(row["n"]) == 1, str(row["n"]))

    print("\n[a board survives a restart too]")
    board_id = await db.create_board(
        guild_id=7, channel_id=500, subscription_id=1, tournament_id=297,
        tournament_name="Worlds 2026", game="lol")
    check("a board can be claimed", board_id is not None)
    check("and only once",
          await db.create_board(guild_id=7, channel_id=500, subscription_id=1,
                               tournament_id=297, tournament_name="Worlds 2026",
                               game="lol") is None)
    await db.set_board_matches(board_id, [{
        "match_id": 1000, "begin_at": None, "team_a": (1, "Gen.G"),
        "team_b": (2, "IG"), "tournament_id": 297, "tournament_name": "Swiss"}])
    await db.close()

    db = Database(path)
    await db.connect()
    check("the board is still there",
          await db.board_for(500, 1) is not None)
    check("with its matches", len(await db.board_match_rows(board_id)) == 1)
    print("\n[and deleting it takes its matches]")
    await db.delete_board(board_id)
    check("board gone", await db.board_for(500, 1) is None)
    check("matches gone", await db.board_match_rows(board_id) == [])
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
