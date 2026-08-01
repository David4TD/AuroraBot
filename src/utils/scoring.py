"""How prediction points are earned.

    payout = stake × underdog × streak

**Underdog.** The multiplier comes from your own server's votes rather than any
odds feed: if nine people back the favourite and you're the one who called the
upset, you're paid for it. That costs no API call and makes the split already
shown on the live alert mean something.

**Streak.** A small, capped bonus for consecutive correct calls, so showing up
daily is worth something and a run is worth protecting.

**Stakes.** A budget per tournament, per server. Backing everything heavily is
arithmetically impossible, so choosing *which* matches to commit to is the skill.
Running out never locks anyone out — picks still count at :data:`FREE_STAKE`.

Everything here is a pure function of numbers already in the database, so the
whole model is testable without Discord, without the API, and without a clock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ── stakes ───────────────────────────────────────────────────────────────────
TOURNAMENT_BUDGET = 500      # per server, per tournament
STAKE_CHOICES = (10, 25, 50)
DEFAULT_STAKE = 10
# What a pick is worth once the budget is spent. Deliberately non-zero: being
# locked out of a tournament you're following is the fastest way to lose someone.
FREE_STAKE = 5

# ── underdog ─────────────────────────────────────────────────────────────────
# Below this many voters the "crowd" is too small to mean anything — two people
# disagreeing would otherwise hand one of them a 2× multiplier.
MIN_VOTERS_FOR_ODDS = 3
MIN_ODDS = 1.0
MAX_ODDS = 3.0

# ── streak ───────────────────────────────────────────────────────────────────
STREAK_STEP = 0.1
STREAK_CAP = 5               # beyond this the bonus stops growing
MAX_STREAK_MULTIPLIER = 1.0 + STREAK_STEP * STREAK_CAP   # 1.5×

# ── perfect day ──────────────────────────────────────────────────────────────
PERFECT_DAY_BONUS = 50
MIN_PERFECT_DAY_MATCHES = 2  # sweeping a one-match day isn't a sweep


def underdog_multiplier(backers: int, voters: int) -> float:
    """How contrarian a correct call was, as a multiplier.

    *backers* is how many people picked the winning team, *voters* how many
    predicted the match at all — both within one server.
    """
    if voters < MIN_VOTERS_FOR_ODDS or backers <= 0:
        return MIN_ODDS
    share = backers / voters
    return max(MIN_ODDS, min(MAX_ODDS, 1.0 / share))


def streak_multiplier(streak: int) -> float:
    """Bonus for *streak* consecutive correct calls **before** this one."""
    if streak <= 0:
        return 1.0
    return min(MAX_STREAK_MULTIPLIER, 1.0 + STREAK_STEP * min(streak, STREAK_CAP))


def allowed_stakes(spent: int) -> list[int]:
    """Stakes a member can still afford this tournament.

    Always non-empty: once the budget is gone the only option is
    :data:`FREE_STAKE`, so the pick still counts.
    """
    remaining = max(0, TOURNAMENT_BUDGET - spent)
    affordable = [s for s in STAKE_CHOICES if s <= remaining]
    return affordable or [FREE_STAKE]


def clamp_stake(stake: int, spent: int) -> int:
    """The largest allowed stake not above *stake*."""
    options = allowed_stakes(spent)
    usable = [s for s in options if s <= stake]
    return max(usable) if usable else min(options)


@dataclass(frozen=True)
class Payout:
    points: int
    stake: int
    odds: float
    streak_bonus: float
    streak: int

    def explain(self) -> str:
        """One line a human can check the maths against."""
        bits = [f"{self.stake} staked"]
        if self.odds > 1.0:
            bits.append(f"×{self.odds:.1f} underdog")
        if self.streak_bonus > 1.0:
            bits.append(f"×{self.streak_bonus:.1f} streak ({self.streak})")
        return f"**+{self.points}** pts — {' · '.join(bits)}"


def payout_for(stake: int, backers: int, voters: int, streak: int) -> Payout:
    """What a correct pick pays. A wrong pick always pays zero."""
    odds = underdog_multiplier(backers, voters)
    bonus = streak_multiplier(streak)
    # Half *up*, not Python's default banker's rounding: round() pays 12 for
    # 12.5 and 14 for 13.5, which looks arbitrary on a scoreboard people are
    # checking by hand. Always rounding a half in the player's favour doesn't.
    raw = stake * odds * bonus
    return Payout(
        points=max(1, math.floor(raw + 0.5)),
        stake=stake,
        odds=odds,
        streak_bonus=bonus,
        streak=streak,
    )


def potential_odds(backers: int, voters: int) -> float:
    """What backers of a team *would* be paid, for showing live on an alert."""
    return underdog_multiplier(backers, voters)
