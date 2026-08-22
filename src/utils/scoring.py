"""How prediction points are earned.

    payout  = 10 × underdog multiplier × stage weight × (2 if doubled down)
    a miss  = 0, or −10 if it was doubled

**One stake, for everyone.** Every correct call is worth the same 10 before the
room is taken into account. Nobody can bet their season on one match, and an
ordinary wrong pick costs nothing at all. What separates people is being
*right*, not being brave.

**The multiplier is the whole game.** It's the reciprocal of the share of the
server that backed the same team: call the match everyone else called and it
barely moves; call the one nobody saw coming and it pays triple. That costs no
API call, uses the split already printed on the live alert, and self-balances —
as a room gets sharper, consensus tightens and the easy calls pay less.

**The cap is soft.** A 1-in-10 call and a 1-in-50 call are not meaningfully
different reads, so above 2× the curve bends and approaches 3× without ever
reaching it. No cliff, no way to farm a lone contrarian vote for a fixed 3×.

**Conviction tokens** are the only lever, and they're the only way to lose
points. Three per tournament, declared with the pick and locked when the match
starts: a doubled call pays twice, and a doubled miss costs the base back.
That's the whole point of them — without a downside, spending one is free
upside and the only decision is timing. With one, doubling a match you're not
sure about is a genuinely bad idea.

The loss is capped at what you've actually banked in that tournament, so a
board can be dented but never driven below zero. Ordinary wrong picks still
cost nothing: this is a bet you opt into, not a penalty for turning up.

Everything here is a pure function of numbers already in the database, so the
whole model is testable without Discord, without the API, and without a clock.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

# ── the stake ────────────────────────────────────────────────────────────────
BASE_POINTS = 10             # what a correct call is worth before the room

# ── the multiplier ───────────────────────────────────────────────────────────
# Below this many voters the "crowd" is too small to mean anything — two people
# disagreeing would otherwise hand one of them a 2× read of the room.
MIN_VOTERS_FOR_ODDS = 3
SOFT_KNEE = 2.0              # exact reciprocal up to here, compressed above it
MAX_MULTIPLIER = 3.0         # approached, never reached

# ── conviction tokens ────────────────────────────────────────────────────────
TOKENS_PER_TOURNAMENT = 3
DOUBLE_DOWN = 2              # what a token multiplies the payout by
# What a doubled miss costs. Flat rather than scaled by the stage weight: a
# final already pays double for being a final, and taxing the downside there
# too would make the biggest matches the ones nobody dares touch.
DOUBLE_DOWN_PENALTY = BASE_POINTS

# ── what's at stake ──────────────────────────────────────────────────────────
# Late matches count for more, so a tournament stays winnable for someone who
# joined halfway through — without making the group stage pointless to play.
FINAL_WEIGHT = 2.0
PLAYOFF_WEIGHT = 1.5

# Matched against the match name and its stage. PandaScore is consistent about
# these: "Grand final", "Semifinal 1: T1 vs GEN", stages called "Playoffs".
_FINAL = re.compile(r"\b(grand final|grand-final)\b|\bfinals?\b")
_NOT_THE_FINAL = re.compile(r"\b(semi|quarter|lower|upper|winners|losers)[ -]?finals?\b")
_KNOCKOUT = re.compile(
    r"\b(semi|quarter)[ -]?finals?\b|\b(playoffs?|knockout|bracket|elimination"
    r"|decider|top ?8|top ?4)\b"
)

# ── perfect day ──────────────────────────────────────────────────────────────
# Two clean calls' worth. Big enough to chase, small enough that it can't
# outweigh actually reading the room well over a tournament.
PERFECT_DAY_BONUS = BASE_POINTS * 2
MIN_PERFECT_DAY_MATCHES = 2       # sweeping a one-match day isn't a sweep


def payout_multiplier(backers: int, voters: int) -> float:
    """How contrarian a correct call was, as a multiplier.

    *backers* is how many people picked the winning team, *voters* how many
    predicted the match at all — both within one server.

    Up to 2× this is exactly ``voters / backers``: half the room called it, you
    get double. Past that it bends, because the difference between being one of
    five and one of fifty is mostly how many people bothered to vote, not how
    much sharper the read was.
    """
    if voters < MIN_VOTERS_FOR_ODDS or backers <= 0:
        return 1.0
    raw = voters / backers
    if raw <= SOFT_KNEE:
        return max(1.0, raw)
    # Continuous at the knee (the exponent is 0 there) and asymptotic to
    # MAX_MULTIPLIER, so 4× reads pay 2.86 and 20× reads pay 2.999.
    headroom = MAX_MULTIPLIER - SOFT_KNEE
    return MAX_MULTIPLIER - headroom * math.exp(-(raw - SOFT_KNEE) / headroom)


def stage_weight(match: dict) -> float:
    """How much this match counts for, by how deep in the event it is.

    A grand final is worth double, the rest of a bracket one and a half, and a
    group-stage game its face value. Read from the match rather than stored on
    a schedule, because "when do the playoffs start" is not a question the API
    answers directly and every event words it differently.
    """
    name = (match.get("name") or "").lower()
    stage = ((match.get("tournament") or {}).get("name") or "").lower()
    both = f"{name} {stage}"

    if _FINAL.search(both) and not _NOT_THE_FINAL.search(both):
        return FINAL_WEIGHT
    if _KNOCKOUT.search(both) or _NOT_THE_FINAL.search(both):
        return PLAYOFF_WEIGHT
    return 1.0


@dataclass(frozen=True)
class Payout:
    points: int
    multiplier: float
    doubled: bool
    backers: int
    voters: int
    weight: float = 1.0

    @property
    def share(self) -> float:
        """The fraction of the room that was on this side."""
        return self.backers / self.voters if self.voters else 1.0

    def explain(self) -> str:
        """One line a human can check the maths against."""
        bits = [f"{BASE_POINTS} base"]
        if self.multiplier > 1.0:
            bits.append(f"×{self.multiplier:.2f} ({self.backers}/{self.voters} called it)")
        if self.weight > 1.0:
            bits.append(f"×{self.weight:g} stage")
        if self.doubled:
            bits.append(f"×{DOUBLE_DOWN} doubled down")
        return f"**+{self.points}** pts — {' · '.join(bits)}"


def payout_for(backers: int, voters: int, *, doubled: bool = False,
               weight: float = 1.0) -> Payout:
    """What a correct pick pays. For what a wrong one costs, see penalty_for."""
    multiplier = payout_multiplier(backers, voters)
    # Half *up*, not Python's default banker's rounding: round() pays 12 for
    # 12.5 and 14 for 13.5, which looks arbitrary on a scoreboard people are
    # checking by hand. Always rounding a half in the player's favour doesn't.
    #
    # Rounded *before* doubling, so a token visibly turns 26 into 52 rather
    # than into 53 — "×2" has to survive being checked by hand too.
    points = max(1, math.floor(BASE_POINTS * multiplier * max(0.0, weight) + 0.5))
    return Payout(
        points=points * (DOUBLE_DOWN if doubled else 1),
        multiplier=multiplier,
        doubled=doubled,
        backers=backers,
        voters=voters,
        weight=weight,
    )


def penalty_for(doubled: bool, banked: int) -> int:
    """What a losing pick costs, as a negative number.

    Only a doubled pick costs anything, and only as much as the member has
    actually banked in that tournament — the promise is that a board can dip,
    never that it can go negative.
    """
    if not doubled:
        return 0
    return -min(DOUBLE_DOWN_PENALTY, max(0, banked))


def tokens_left(spent: int) -> int:
    """Conviction tokens still available this tournament."""
    return max(0, TOKENS_PER_TOURNAMENT - max(0, spent))


def potential_odds(backers: int, voters: int) -> float:
    """What backers of a team *would* be paid, for showing live on an alert."""
    return payout_multiplier(backers, voters)
