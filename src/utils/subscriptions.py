"""Reading an alert subscription's options.

Lives here rather than on the alerts cog because the digest needs it too, and a
cog importing another cog is the coupling that made `/games` break silently once
already.
"""
from __future__ import annotations


def wants_votes(sub) -> bool:
    """Whether this subscription's alerts carry vote buttons.

    Defaults to True for a row written before the column existed — the poll can
    be mid-flight while the migration runs, and going quiet would be the wrong
    way to fail.
    """
    try:
        return bool(sub["predictions"])
    except (KeyError, IndexError, TypeError):
        return True
