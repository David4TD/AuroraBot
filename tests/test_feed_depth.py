"""Reading a tier-mixed feed deep enough to see the matches the bot shows.

Shaped on the measured Counter-Strike feed: 100 upcoming matches, only 4 of
them Tier 1, the first at index 24 — so a single 50-row page saw none of the
ones that mattered and half the Tier 1 slate got no reminder at all.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DISCORD_TOKEN"] = "fake"
os.environ["ESPORTS_API_KEY"] = "fake"

from src.services.pandascore import (                   # noqa: E402
    MAX_PAGES, PAGE_SIZE, PandaScoreClient,
)
from src.utils.tiers import filter_top_tier             # noqa: E402

UTC = timezone.utc
NOW = datetime.now(UTC)
TOP = frozenset({"s", "a"})
failures: list[str] = []


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def match(n, *, tier, minutes):
    return {
        "id": n,
        "begin_at": (NOW + timedelta(minutes=minutes)).isoformat(),
        "tournament": {"id": 1, "name": "Playoffs", "tier": tier},
        "opponents": [],
    }


class Fake(PandaScoreClient):
    """A client whose only real behaviour is paging over a canned feed."""

    def __init__(self, rows):
        super().__init__("fake")
        self.rows = rows
        self.requests: list[tuple[int, int]] = []

    async def upcoming_matches(self, slug=None, per_page=50, page=1):
        self.requests.append((page, per_page))
        start = (page - 1) * per_page
        return self.rows[start:start + per_page]

    async def running_matches(self, slug=None, per_page=50, page=1):
        self.requests.append((page, per_page))
        start = (page - 1) * per_page
        return self.rows[start:start + per_page]


# The measured CS shape: Tier 1 at 24, 51, 77, 99; everything else tier C.
CS = [
    match(i, tier="s" if i in (24, 51) else "a" if i in (77, 99) else "c",
          minutes=30 + i * 32)
    for i in range(100)
]


async def main():
    print("\n[the shape that was losing matches]")
    tops = [i for i, m in enumerate(CS) if m["tournament"]["tier"] in ("s", "a")]
    check("four Tier 1 matches in a hundred", len(tops) == 4, str(tops))
    check("the first sits at index 24", tops[0] == 24, str(tops[0]))
    old = filter_top_tier(CS[:50], allowed=TOP)
    check("a 50-row page saw one of the four", len(old) == 1, str(len(old)))

    print("\n[one page of 100 now covers a two-day horizon]")
    api = Fake(CS)
    got = await api.upcoming_until(None, NOW + timedelta(minutes=30))
    check("a single request", len(api.requests) == 1, str(api.requests))
    check("asks for the API maximum", api.requests[0][1] == PAGE_SIZE,
          str(api.requests))
    check("all four Tier 1 matches are visible",
          len(filter_top_tier(got, allowed=TOP)) == 4,
          str(len(filter_top_tier(got, allowed=TOP))))

    print("\n[a horizon past the end of the feed]")
    api = Fake(CS)
    got = await api.upcoming_until(None, NOW + timedelta(days=99))
    # A full page that is still inside the horizon can't be known to be the
    # last one without asking, so finding the end costs one empty request.
    check("one extra request discovers the end", len(api.requests) == 2,
          str(api.requests))
    check("and nothing is duplicated", len(got) == len(CS), str(len(got)))

    print("\n[and reads on while the window is still ahead of the feed]")
    # 300 matches, all inside the horizon: the first two pages are full and
    # still short of it, so it has to keep going.
    dense = [match(i, tier="c", minutes=i) for i in range(300)]
    dense += [match(999, tier="s", minutes=400)]
    api = Fake(dense)
    got = await api.upcoming_until(None, NOW + timedelta(minutes=500))
    check("it kept reading", len(api.requests) == 4, str(api.requests))
    check("and found the Tier 1 match on the last page",
          any(m["id"] == 999 for m in got))
    check("pages are consecutive from one",
          [p for p, _ in api.requests] == [1, 2, 3, 4], str(api.requests))

    print("\n[a pathological feed is still bounded]")
    endless = [match(i, tier="c", minutes=1) for i in range(PAGE_SIZE * 20)]
    api = Fake(endless)
    await api.upcoming_until(None, NOW + timedelta(days=365))
    check("never more than the page cap", len(api.requests) == MAX_PAGES,
          str(len(api.requests)))

    print("\n[a match with no kick-off time ends the read]")
    odd = [match(i, tier="c", minutes=1) for i in range(PAGE_SIZE)]
    odd[-1]["begin_at"] = None
    api = Fake(odd + [match(500, tier="s", minutes=2)])
    await api.upcoming_until(None, NOW + timedelta(days=1))
    check("rather than paging on an order it can't trust",
          len(api.requests) == 1, str(api.requests))

    print("\n[running matches read to a short page]")
    api = Fake([match(i, tier="s", minutes=-5) for i in range(150)])
    got = await api.running_all(None)
    check("two pages for 150 live matches", len(api.requests) == 2,
          str(api.requests))
    check("all of them returned", len(got) == 150, str(len(got)))

    api = Fake([match(1, tier="s", minutes=-5)])
    got = await api.running_all(None)
    check("one short page is one request", len(api.requests) == 1,
          str(api.requests))
    check("and nothing live is nothing fetched twice", len(got) == 1)

    api = Fake([])
    check("an empty feed is empty", await api.running_all(None) == [])
    check("and so is an empty upcoming feed",
          await api.upcoming_until(None, NOW) == [])

    print("\n[the reminder window is what the poll reads to]")
    # A match 25 minutes out is due on a 30-minute lead; one 10 hours out is
    # not, but it still has to be *seen* for the sort to be meaningful.
    api = Fake(CS)
    got = await api.upcoming_until(None, NOW + timedelta(minutes=30))
    due = [m for m in filter_top_tier(got, allowed=TOP)
           if (datetime.fromisoformat(m["begin_at"]) - NOW).total_seconds() / 60 <= 30]
    check("nothing inside the window is missed", due == [], str(due))
    close = CS[:1] + [match(1000, tier="s", minutes=25)]
    api = Fake(close)
    got = await api.upcoming_until(None, NOW + timedelta(minutes=30))
    check("a Tier 1 match 25 minutes out is found",
          any(m["id"] == 1000 for m in got))


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
