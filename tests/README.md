# Tests

```bash
python tests/run.py                 # every suite
python tests/run.py reminder        # just the ones matching "reminder"
python tests/test_bracket_fill.py   # one suite, full output
```

Needs `discord.py`, `aiosqlite`, `tzdata` and `Pillow` (see `requirements.txt`); nothing
else. No network: every suite stubs the PandaScore client, so they run offline
and can't be broken by a live fixture list changing under them.

The runner exits non-zero if any check fails **or** if a suite dies partway
through — a suite that crashes on its third check is not a suite that passed.

## What each one covers

| suite | what it pins down |
| --- | --- |
| `test_board.py` | The event board: one pinned message per event, edited in place. Built on the real Worlds Swiss slate, including the undrawn rounds and what a shared message may not claim about its reader. |
| `test_icons.py` | Team logo emojis: minting once, noticing a rebrand, and retrying a failure instead of abandoning the team for the run. |
| `test_tidy.py` | Clearing a match's reminder and live cards once its result is posted — and never touching anything else in the channel. |
| `test_feed_depth.py` | Reading a tier-mixed feed deep enough to see Tier 1. Shaped on the measured Counter-Strike feed, where the first Tier 1 match sat at index 24 of 100. |
| `test_bracket_fill.py` | A knockout fixture whose feeder match hasn't been played at digest time. Reproduces the VCT Champions lower bracket final that got no prediction sheet. |
| `test_migrate.py` | `schema.sql` applying to a database that predates a table, and staying idempotent across restarts. |
| `test_board_default.py` | Which board `/leaderboard` opens on when nobody names one — the live event, not the alphabetically first. |
| `test_reminder.py` | The 30-minute reminder card: three lines, the round named, and no vote split leaking before kick-off. |
| `test_cards2.py` | `/live`, `/upcoming` and `/scoring` — layout, the stage multiplier never appearing bare, and the embed field caps. |

## Writing one

Each suite is a standalone script that builds its own fixtures in order and
prints one line per check:

```python
check("a lower bracket final pays bracket rate",
      stage_weight(match) == PLAYOFF_WEIGHT, str(stage_weight(match)))
```

Deliberately not pytest. These assert on rendered Discord embeds and on a real
SQLite file, so nearly every check needs its own fixture built up in sequence;
as plain scripts they read as a description of the behaviour, which is what
makes a failure say what broke rather than which assertion tripped.

Two things worth keeping to:

- **Name the behaviour, not the code.** `"an upper bracket final is not the
  grand final"` survives a refactor; `"test_stage_weight_3"` doesn't.
- **Pass the actual value as `extra`.** It's printed on failure, and it is
  usually the whole diagnosis.

Dates: don't hardcode one. Several checks here were written against a fixed day
and went stale the moment a constant moved — derive the day from the thing
under test instead (`test_bracket_fill.py` computes which digest window owns a
kick-off rather than naming the date).
