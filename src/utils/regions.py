"""Region flags for leagues, events and teams.

**PandaScore does not expose a region or country field on leagues or
tournaments** (checked against API v2.53: the League model has only
id/name/slug/url/image_url/series/videogame, and Tournament adds tier, dates
and rosters — no geography). Teams are the exception: they carry ``location``
as an ISO-3166 alpha-2 code.

So event flags are derived here, in three passes, stopping at the first hit:

1. :data:`LEAGUE_REGIONS` — an exact match on the league name. Because
   AuroraBot only surfaces Tier 1 (S/A) events, this set is small and changes
   about once a season, so hard-coding it is accurate and free.
2. The same table matched *inside* a longer label, since tournament names look
   like "LCK Summer 2026 · Playoffs". Single-word keys must match a whole token
   so short acronyms can't fire on unrelated words.
3. :data:`_KEYWORD_REGIONS` — a substring scan for region words ("EMEA",
   "Pacific", "Korea"). Catches leagues missing from the table above, and new
   ones that follow the usual naming.
4. Nothing. No flag is shown rather than a wrong one.

Adding a league is a one-line edit to :data:`LEAGUE_REGIONS`.
"""
from __future__ import annotations

# ─── Regions ────────────────────────────────────────────────────────────────
# token → (flag, human label). Multi-country regions use a symbol rather than
# pretending to be one nation.
REGIONS: dict[str, tuple[str, str]] = {
    "international": ("🌍", "International"),
    "emea": ("🇪🇺", "EMEA"),
    "europe": ("🇪🇺", "Europe"),
    "americas": ("🌎", "Americas"),
    "asia": ("🌏", "Asia"),
    "pacific": ("🌏", "Asia-Pacific"),
    "sea": ("🌏", "Southeast Asia"),
    "mena": ("🕌", "MENA"),
    "oceania": ("🌊", "Oceania"),
    # Single-country regions carry their own flag.
    "kr": ("🇰🇷", "Korea"),
    "cn": ("🇨🇳", "China"),
    "us": ("🇺🇸", "North America"),
    "br": ("🇧🇷", "Brazil"),
    "jp": ("🇯🇵", "Japan"),
    "vn": ("🇻🇳", "Vietnam"),
    "tw": ("🇹🇼", "Taiwan"),
    "id": ("🇮🇩", "Indonesia"),
    "ph": ("🇵🇭", "Philippines"),
    "my": ("🇲🇾", "Malaysia"),
    "th": ("🇹🇭", "Thailand"),
    "tr": ("🇹🇷", "Türkiye"),
    "fr": ("🇫🇷", "France"),
    "de": ("🇩🇪", "Germany"),
    "es": ("🇪🇸", "Spain"),
    "it": ("🇮🇹", "Italy"),
    "pl": ("🇵🇱", "Poland"),
    "gb": ("🇬🇧", "United Kingdom"),
    "au": ("🇦🇺", "Australia"),
    "in": ("🇮🇳", "India"),
    "mx": ("🇲🇽", "Mexico"),
    "latam": ("🌎", "Latin America"),
}

# ─── Curated league → region ────────────────────────────────────────────────
# Keys are lowercased league names as PandaScore reports them. Kept flat and
# obvious so a season's rebrand is a one-line change.
LEAGUE_REGIONS: dict[str, str] = {
    # ── League of Legends ──
    "lck": "kr",
    "lck challengers league": "kr",
    "lpl": "cn",
    "lec": "emea",
    "lcs": "us",
    "lta": "americas",
    "lta north": "us",
    "lta south": "br",
    "lcp": "pacific",
    "cblol": "br",
    "lla": "latam",
    "pcs": "pacific",
    "vcs": "vn",
    "ljl": "jp",
    "lfl": "fr",
    "prime league": "de",
    "superliga": "es",
    "ultraliga": "pl",
    "tcl": "tr",
    "emea masters": "emea",
    "worlds": "international",
    "world championship": "international",
    "msi": "international",
    "mid-season invitational": "international",
    "first stand": "international",
    # ── Valorant ──
    "vct emea": "emea",
    "vct americas": "americas",
    "vct pacific": "pacific",
    "vct china": "cn",
    "vct masters": "international",
    "champions tour": "international",
    "valorant champions": "international",
    "game changers": "international",
    # ── Counter-Strike ──
    "iem": "international",
    "iem katowice": "international",
    "iem cologne": "international",
    "blast premier": "international",
    "blast bounty": "international",
    "esl pro league": "international",
    "esl challenger": "international",
    "pgl major": "international",
    "blast major": "international",
    "cct": "europe",
    "esea": "international",
    # ── Dota 2 ──
    "the international": "international",
    "dreamleague": "international",
    "esl one": "international",
    "pgl wallachia": "international",
    "blast slam": "international",
    # ── Rocket League ──
    "rlcs": "international",
    # ── Rainbow Six ──
    "six invitational": "international",
    "blast r6": "international",
    "six major": "international",
    # ── Overwatch ──
    "owcs": "international",
    "overwatch champions series": "international",
    # ── Call of Duty ──
    "cdl": "us",
    "call of duty league": "us",
    # ── Mobile Legends ──
    "mpl id": "id",
    "mpl ph": "ph",
    "mpl my": "my",
    "mpl sg": "sea",
    "msc": "international",
    "m-series": "international",
    "mlbb world championship": "international",
    # ── PUBG ──
    "pgc": "international",
    "pubg global championship": "international",
    "pgs": "international",
}

# ─── Keyword fallback ───────────────────────────────────────────────────────
# Ordered: the first match wins, so put the specific before the generic
# ("north america" before "america", "asia-pacific" before "asia").
_KEYWORD_REGIONS: list[tuple[str, str]] = [
    ("international", "international"),
    ("world championship", "international"),
    ("worlds", "international"),
    ("global", "international"),
    ("major", "international"),
    ("emea", "emea"),
    ("europe", "europe"),
    ("north america", "us"),
    ("latin america", "latam"),
    ("south america", "americas"),
    ("americas", "americas"),
    ("brazil", "br"),
    ("brasil", "br"),
    ("mexico", "mx"),
    ("korea", "kr"),
    ("korean", "kr"),
    ("china", "cn"),
    ("chinese", "cn"),
    ("japan", "jp"),
    ("vietnam", "vn"),
    ("taiwan", "tw"),
    ("indonesia", "id"),
    ("philippines", "ph"),
    ("malaysia", "my"),
    ("thailand", "th"),
    ("turkey", "tr"),
    ("türkiye", "tr"),
    ("india", "in"),
    ("oceania", "oceania"),
    ("australia", "au"),
    ("mena", "mena"),
    ("middle east", "mena"),
    ("asia-pacific", "pacific"),
    ("asia pacific", "pacific"),
    ("pacific", "pacific"),
    ("southeast asia", "sea"),
    ("sea ", "sea"),
    ("asia", "asia"),
    ("france", "fr"),
    ("german", "de"),
    ("spain", "es"),
    ("italy", "it"),
    ("poland", "pl"),
]


def flag_for_country(code: str | None) -> str | None:
    """ISO-3166 alpha-2 → regional-indicator flag emoji, e.g. ``KR`` → 🇰🇷.

    Built arithmetically (letter → U+1F1E6 + offset) rather than from a table,
    so any valid two-letter code works. Codes already covered by
    :data:`REGIONS` use that entry, keeping multi-country tokens consistent.
    """
    if not code:
        return None
    code = str(code).strip().lower()
    known = REGIONS.get(code)
    if known:
        return known[0]
    if len(code) != 2 or not code.isalpha():
        return None
    return "".join(chr(0x1F1E6 + ord(c) - ord("a")) for c in code)


def region_token(*names: str | None) -> str | None:
    """Resolve the first recognisable region across the given names.

    Pass the most specific name first (league, then serie), since the exact
    table is consulted for every name before falling back to keywords.
    """
    cleaned = [str(n).strip().lower() for n in names if n]

    for name in cleaned:                      # pass 1: exact league match
        if name in LEAGUE_REGIONS:
            return LEAGUE_REGIONS[name]

    # Pass 2: a league acronym inside a longer label. Tournament names arrive as
    # "LCK Summer 2026 · Playoffs", so the exact match above misses them.
    # Multi-word keys are checked as substrings; single words must match a whole
    # token, so "lta" can't fire on "atlanta".
    for name in cleaned:
        tokens = {t.strip("·,()[]") for t in name.split()}
        for key, token in LEAGUE_REGIONS.items():
            if (" " in key and key in name) or (" " not in key and key in tokens):
                return token

    for name in cleaned:                      # pass 3: keyword scan
        for keyword, token in _KEYWORD_REGIONS:
            if keyword in name:
                return token
    return None


def region_flag(*names: str | None) -> str | None:
    token = region_token(*names)
    return REGIONS[token][0] if token else None


def region_label(*names: str | None) -> str | None:
    token = region_token(*names)
    return REGIONS[token][1] if token else None


def _event_names(payload: dict) -> list[str | None]:
    """League and serie names from a match or tournament payload."""
    league = (payload.get("league") or {}).get("name")
    serie = (payload.get("serie") or {}).get("full_name") or (
        payload.get("serie") or {}
    ).get("name")
    return [league, serie, payload.get("name")]


def event_flag(payload: dict) -> str | None:
    """Flag for a match or tournament, from its league/serie names."""
    return region_flag(*_event_names(payload))


def event_region(payload: dict) -> str | None:
    """Human region label for a match or tournament, e.g. ``Korea``."""
    return region_label(*_event_names(payload))


def prefix_flag(text: str, payload: dict) -> str:
    """``LCK · Summer`` → ``🇰🇷 LCK · Summer``, unchanged if region unknown."""
    flag = event_flag(payload)
    return f"{flag} {text}" if flag else text


def team_flag(team: dict) -> str | None:
    """Flag from a team's ``location`` — the one geography PandaScore gives us."""
    return flag_for_country(team.get("location"))
