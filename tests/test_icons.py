"""Team icons: minting once, and noticing when a team rebrands.

The replace-on-change path in `_mint` was always correct — it deletes the old
emoji to free the name, then creates the new one. Nothing ever reached it:
`icon()` returned the cached markdown on the first line and the URL was never
compared again, so a rebranded team kept its old badge until the icon happened
to be evicted.
"""
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
from src.services.emojis import (                       # noqa: E402
    MAX_ATTEMPTS, MAX_REMINTS, TeamIconStore,
)

failures: list[str] = []
OLD_LOGO = "https://cdn.pandascore.co/images/team/image/1/old.png"
NEW_LOGO = "https://cdn.pandascore.co/images/team/image/1/rebrand.png"


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


def team(tid=101, name="Gen.G", image_url=OLD_LOGO):
    return {"id": tid, "name": name, "image_url": image_url}


class Emoji:
    def __init__(self, eid, name, animated=False):
        self.id, self.name, self.animated = eid, name, animated


class Store(TeamIconStore):
    """The real store with the network and Discord ends replaced."""

    def __init__(self, db):
        super().__init__(bot=None, db=db)
        self.enabled = True
        self.created: list[tuple[str, bytes]] = []
        self.deleted: list[int] = []
        self._next_id = 5000

    async def _asset(self, team_id, image_url):
        return b"PNG" + image_url.encode()

    async def _delete_emoji(self, emoji_id):
        self.deleted.append(int(emoji_id))
        self._emoji_objects.pop(int(emoji_id), None)
        for name, emoji in list(self._by_name.items()):
            if emoji.id == int(emoji_id):
                self._by_name.pop(name)

    async def _create(self, name, image):
        self._next_id += 1
        return Emoji(self._next_id, name)

    async def _mint(self, team_id, name, image_url):
        # The real _mint reaches for bot.create_application_emoji; everything
        # else about it is under test, so only that call is stubbed.
        class Bot:
            create_application_emoji = staticmethod(
                lambda name, image: self._create(name, image))

        self.bot = Bot()
        self.created.append((name, image_url))
        return await super()._mint(team_id, name, image_url)


async def drain(store):
    """Run whatever icon() queued, as the worker would."""
    while not store._queue.empty():
        team_id, name, image_url = store._queue.get_nowait()
        store._queued.discard(team_id)
        await store._mint(team_id, name, image_url)


async def main():
    print("\n[a team with no icon yet]")
    db = Database(str(Path(tempfile.mkdtemp()) / "i.db"))
    await db.connect()
    store = Store(db)
    check("renders without one at first", store.icon(team()) == "")
    check("and queues a mint", store._queue.qsize() == 1,
          str(store._queue.qsize()))
    await drain(store)
    first = store.icon(team())
    check("then has one", first.startswith("<:"), first)
    check("the logo it came from is remembered",
          store._urls[101] == OLD_LOGO, str(store._urls.get(101)))

    print("\n[the same logo again is not re-minted]")
    before = len(store.created)
    for _ in range(5):
        store.icon(team())
    check("nothing is queued", store._queue.qsize() == 0,
          str(store._queue.qsize()))
    check("and nothing is created", len(store.created) == before,
          str(len(store.created)))

    print("\n[the team rebrands]")
    rebranded = team(image_url=NEW_LOGO)
    shown = store.icon(rebranded)
    check("the old badge still shows while the new one mints",
          shown == first, f"{shown} vs {first}")
    check("but a re-mint is queued", store._queue.qsize() == 1,
          str(store._queue.qsize()))
    await drain(store)
    second = store.icon(rebranded)
    check("the icon changes", second != first, f"{second} vs {first}")
    check("the old emoji is deleted, freeing the name",
          len(store.deleted) == 1, str(store.deleted))
    check("the new logo is recorded", store._urls[101] == NEW_LOGO,
          str(store._urls.get(101)))
    row = await db.get_team_emoji(101)
    check("and persisted, so a restart doesn't re-mint",
          row and row["image_url"] == NEW_LOGO,
          str(row["image_url"] if row else None))

    print("\n[and settles again]")
    before = len(store.created)
    for _ in range(5):
        store.icon(rebranded)
    check("no further work is queued", store._queue.qsize() == 0,
          str(store._queue.qsize()))
    check("and none is done", len(store.created) == before)

    print("\n[a logo URL that flaps is not chased forever]")
    flapper = Store(db)
    flapper.icon(team(202, "Flap", OLD_LOGO))
    await drain(flapper)
    mints = len(flapper.created)
    for i in range(10):
        # Alternating between two URLs for the same badge.
        flapper.icon(team(202, "Flap", NEW_LOGO if i % 2 == 0 else OLD_LOGO))
        await drain(flapper)
    extra = len(flapper.created) - mints
    check("it gives up after a few goes", extra <= MAX_REMINTS, str(extra))
    check("and the team still has an icon",
          flapper.icon(team(202, "Flap", OLD_LOGO)).startswith("<:"))

    print("\n[a team the feed gives no logo for]")
    check("renders empty", store.icon({"id": 303, "name": "No Logo"}) == "")
    check("and queues nothing", store._queue.qsize() == 0,
          str(store._queue.qsize()))
    check("a team with no id is survivable",
          store.icon({"name": "Nameless"}) == "")
    check("and None", store.icon(None) == "")

    print("\n[a logo Discord will not take]")
    class Broken(Store):
        async def _asset(self, team_id, image_url):
            return None

    broken = Broken(db)
    for _ in range(6):
        broken.icon(team(404, "Broken"))
        await drain(broken)
    check("it is tried a bounded number of times",
          broken._attempts.get(404, 0) <= MAX_ATTEMPTS,
          str(broken._attempts.get(404)))
    check("and then left alone", broken._queue.qsize() == 0)
    check("rendering without an icon rather than failing",
          broken.icon(team(404, "Broken")) == "")

    print("\n[a failure is retried later, not abandoned for the whole run]")
    import time as _time
    from src.services import emojis as _emojis

    flaky = Broken(db)
    for _ in range(4):
        flaky.icon(team(505, "Flaky"))
        await drain(flaky)
    check("it has given up for now",
          flaky._attempts.get(505, 0) >= MAX_ATTEMPTS,
          str(flaky._attempts.get(505)))
    check("and a retry time is set", 505 in flaky._retry_at)
    flaky.icon(team(505, "Flaky"))
    check("nothing is queued during the cooldown", flaky._queue.qsize() == 0,
          str(flaky._queue.qsize()))

    # Wind the clock past the cooldown rather than waiting an hour for it.
    flaky._retry_at[505] = _time.monotonic() - 1
    flaky.icon(team(505, "Flaky"))
    check("once it is up, the team is tried again",
          flaky._queue.qsize() == 1, str(flaky._queue.qsize()))
    check("and the count is reset so it gets a full go",
          flaky._attempts.get(505, 0) == 0, str(flaky._attempts.get(505)))

    # A logo that works on the retry — the transient-failure case this is for.
    class Recovers(Store):
        pass

    healed = Recovers(db)
    healed._attempts[606] = MAX_ATTEMPTS
    healed._retry_at[606] = _time.monotonic() - 1
    healed.icon(team(606, "Recovers"))
    await drain(healed)
    check("a team that failed to a rate limit gets its icon in the end",
          healed.icon(team(606, "Recovers")).startswith("<:"),
          healed.icon(team(606, "Recovers")))
    check("the cooldown is an hour by default",
          _emojis.RETRY_AFTER_SECONDS == 3600.0,
          str(_emojis.RETRY_AFTER_SECONDS))

    print("\n[eviction clears both maps together]")
    store._cache[999] = "<:x:1>"
    store._urls[999] = OLD_LOGO
    await db.put_team_emoji(team_id=999, emoji_id=1, emoji_name="x",
                            image_url=OLD_LOGO, animated=False)
    await store._evict()
    check("the icon is forgotten", 999 not in store._cache)
    check("and so is its logo", 999 not in store._urls,
          "a URL left behind reads as 'unchanged' and blocks a re-mint")

    print("\n[disabled store does nothing]")
    store.enabled = False
    check("no icon", store.icon(team()) == "")
    check("no work", store._queue.qsize() == 0)

    await db.close()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
