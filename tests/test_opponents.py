"""What `opponents()` hands on, and why the icon store depends on it.

Every card draws its crests from this list. It returned only id and name, so a
team seen through a match — which is how the board, the reminder, the live and
result cards all see teams — arrived at the icon store with no logo to mint
from, and silently never got a badge. Teams that happened also to be seen
through a roster picker, which passes the whole team object, did. That is why
it looked like Discord was refusing specific teams.
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
from src.services.emojis import TeamIconStore           # noqa: E402
from src.utils.matches import opponents, team_ids       # noqa: E402
from src.utils.regions import team_flag                 # noqa: E402

failures: list[str] = []
LOGO = "https://cdn-api.pandascore.co/images/team/image/1566/qw_yi_qu_j.png"


def check(name, cond, extra=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        failures.append(name)
        print(f"  FAIL  {name} {extra}")


# Exactly the shape PandaScore returns, keys and all.
def match(both=True):
    a = {"id": 1566, "name": "Bilibili Gaming", "acronym": "BLG",
         "image_url": LOGO, "location": "CN", "slug": "bilibili-gaming",
         "dark_mode_image_url": None, "modified_at": "2026-01-01T00:00:00Z"}
    b = {"id": 136066, "name": "LYON", "acronym": "LYON",
         "image_url": LOGO.replace("1566", "136066"), "location": "MX",
         "slug": "lyon", "dark_mode_image_url": None,
         "modified_at": "2026-01-01T00:00:00Z"}
    ops = [{"opponent": a, "type": "Team"}]
    ops.append({"opponent": b, "type": "Team"} if both else {"opponent": None})
    return {"id": 99, "name": "Round 5: BLG vs LY", "opponents": ops}


class Emoji:
    def __init__(self, eid, name):
        self.id, self.name, self.animated = eid, name, False


class Store(TeamIconStore):
    def __init__(self, db):
        super().__init__(bot=None, db=db)
        self.enabled = True


async def main():
    print("\n[every side keeps its logo]")
    teams = opponents(match())
    check("both sides come back", len(teams) == 2, str(len(teams)))
    check("the first has a logo", bool(teams[0].get("image_url")),
          str(teams[0]))
    check("so does the second", bool(teams[1].get("image_url")),
          str(teams[1]))
    check("names survive", [t["name"] for t in teams]
          == ["Bilibili Gaming", "LYON"], str([t["name"] for t in teams]))
    check("ids are ints", all(isinstance(t["id"], int) for t in teams))
    check("and the location rides along for the flag fallback",
          team_flag(teams[0]) is not None, str(teams[0].get("location")))
    check("as does the acronym", teams[1]["acronym"] == "LYON",
          str(teams[1].get("acronym")))

    print("\n[which is what lets a crest be minted at all]")
    db = Database(str(Path(tempfile.mkdtemp()) / "o.db"))
    await db.connect()
    store = Store(db)
    for team in teams:
        store.icon(team)
    check("both teams are queued for a badge", store._queue.qsize() == 2,
          str(store._queue.qsize()))
    queued = {store._queue.get_nowait()[0] for _ in range(2)}
    check("and they are the right two", queued == {1566, 136066}, str(queued))

    print("\n[the shape that caused this]")
    stripped = [{"id": t["id"], "name": t["name"]} for t in teams]
    bare = Store(db)
    for team in stripped:
        bare.icon(team)
    check("id and name alone queue nothing at all",
          bare._queue.qsize() == 0, str(bare._queue.qsize()))
    check("so the team renders blank forever, through no fault of its logo",
          bare.icon(stripped[0]) == "")
    await db.close()

    print("\n[unconfirmed sides still degrade]")
    half = opponents(match(both=False))
    check("only the known side comes back", len(half) == 1, str(len(half)))
    check("and it is intact", half[0]["id"] == 1566 and half[0]["image_url"])
    check("no opponents at all", opponents({"opponents": []}) == [])
    check("missing key", opponents({}) == [])
    check("an opponent with no id is skipped",
          opponents({"opponents": [{"opponent": {"name": "TBD"}}]}) == [])

    print("\n[a team with no logo in the payload is reported honestly]")
    nologo = {"opponents": [{"opponent": {"id": 7, "name": "Obscure"}}]}
    only = opponents(nologo)
    check("it still comes back", len(only) == 1)
    check("with the key present and empty, not missing",
          "image_url" in only[0] and only[0]["image_url"] is None,
          str(only[0]))

    print("\n[callers that only wanted ids are unaffected]")
    check("team_ids still works", team_ids(match()) == {1566, 136066},
          str(team_ids(match())))


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILURE(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
