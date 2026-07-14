"""
Task 2 (cont.) - Geopolitical Risk Overlay: Risk-Desk Config

This is a RISK-DESK CONFIG INPUT, not something Claude infers from its own
world knowledge. Every entry here is desk-owned, sourced, and dated -
Claude only ever reasons over what's configured here (see
claude/prompts.py's explicit instruction against introducing geopolitical
claims not present in this config). The whole point of `source` and
`as_of` is auditability: a reviewer can trace any tier back to who said so
and when, rather than trusting an opaque number or a model's own recall.

An absent or stale `as_of` date is itself a risk-management concern (see
design_document.md) - a tier nobody has revisited in months is a signal
that the desk's process, not just the geography, needs attention.

IMPORTANT: the tier/note/source/as_of values below are ILLUSTRATIVE
PLACEHOLDER data for this demo project, not a real current geopolitical
assessment. Before any real use, the risk desk must replace every entry
with its own actual, sourced, dated assessment and keep it current.
"""

VALID_TIERS = ("LOW", "ELEVATED", "HIGH")

# A geography can be well under its concentration limit and still carry
# material exposure to an ELEVATED/HIGH-tier region - this is a distinct
# check from the limit-based OK/WARNING/BREACH classification in
# engine/concentration.py, which stays purely limit-based. Named constant,
# not inlined, so the materiality threshold is auditable and adjustable.
MATERIAL_GEOGRAPHY_EXPOSURE_PCT = 15.0

# Used for any geography not explicitly listed below - e.g. a newly
# uploaded portfolio with a geography the desk hasn't assessed yet. Never
# inferred: an absent config entry is itself flagged, not silently
# defaulted to "fine."
DEFAULT_TIER_FALLBACK = {
    "tier": "LOW",
    "note": "No tier configured for this geography.",
    "source": None,
    "as_of": None,
}

# Populated for every geography appearing across data/sample_portfolios/*.json
# as of this feature's build date (2026-07-14). Add an entry whenever a new
# geography is introduced to a sample portfolio, rather than relying on the
# fallback above for demo data.
DEFAULT_GEOPOLITICAL_RISK_CONFIG = {
    "India": {
        "tier": "HIGH",
        "note": (
            "Illustrative for this demo: border-tension and trade-policy "
            "uncertainty flagged by desk."
        ),
        "source": "Risk Desk weekly geopolitical briefing",
        "as_of": "2026-07-10",
    },
    "United States": {
        "tier": "HIGH",
        "note": (
            "Illustrative for this demo: set to HIGH specifically to exercise "
            "the immaterial-exposure case - a HIGH tier on an exposure below "
            "MATERIAL_GEOGRAPHY_EXPOSURE_PCT must NOT raise geopolitical_flag."
        ),
        "source": "Risk Desk weekly geopolitical briefing",
        "as_of": "2026-07-10",
    },
    "Hong Kong": {
        "tier": "ELEVATED",
        "note": "Illustrative for this demo: monitoring regulatory and capital-flow developments.",
        "source": "Risk Desk weekly geopolitical briefing",
        "as_of": "2026-06-15",
    },
    "Australia": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "Brazil": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "Canada": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "France": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "Germany": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "Japan": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "South Korea": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "Switzerland": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
    "United Kingdom": {
        "tier": "LOW", "note": "No elevated risk factors identified.",
        "source": "Risk Desk baseline assessment", "as_of": "2026-06-01",
    },
}


def get_geopolitical_tier(geography: str, config: dict = None) -> dict:
    """Look up the configured tier record for `geography`. Falls back to
    DEFAULT_TIER_FALLBACK (LOW, "no tier configured") for anything not
    present in the config - never inferred, never guessed.
    """
    config = DEFAULT_GEOPOLITICAL_RISK_CONFIG if config is None else config
    return config.get(geography, DEFAULT_TIER_FALLBACK)
