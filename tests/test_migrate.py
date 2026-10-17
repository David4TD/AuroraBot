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

    print("\n[a fresh database]")
    db = Database(path)
    await db.connect()
    check("has digest_pending", "digest_pending" in await tables(db))
    await db.close()

    print("\n[an existing one that predates the table]")
    db = Database(path)
    await db.connect()
    await db.conn.execute("DROP TABLE digest_pending")
    await db.conn.commit()
    check("dropped for the test", "digest_pending" not in await tables(db))
    await db.close()

    db = Database(path)
    await db.connect()
    check("the next startup recreates it", "digest_pending" in await tables(db))
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
    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
