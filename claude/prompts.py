"""
Task 3 (cont.) - Claude Reasoning Layer: System Prompt + Forced-JSON Schema

Design per design_document.md §7. The system prompt is static across calls
(cacheable per §9); the per-call variable content (metrics, limits, flags)
is assembled separately in claude/client.py and never appears here.

Claude never computes numbers - engine/concentration.py and
engine/scoring.py already did that. This module only shapes how Claude
reasons over and reports on those pre-computed numbers.
"""

TOOL_NAME = "portfolio_risk_analysis"

SYSTEM_PROMPT = """You are a portfolio risk analyst assistant. You will receive \
pre-computed concentration metrics for a portfolio, the configured limits, a \
rules-based base severity score with its component breakdown, and any flagged \
data-quality issues. Do not recompute or second-guess the numeric values \
provided - treat them as ground truth.

Your job:
1. Identify which metrics breach or approach their limits, using the full \
lists in `metrics` (every issuer, sector, geography, asset-class, and \
currency entry) - not just the single worst entry in each category. \
Currency is a distinct risk from geography (e.g. a US-listed ADR can be \
EUR-denominated) - never conflate the two.
2. Count and weigh how many categories are breaching simultaneously, and how \
many distinct entries breach within each category. The base severity score \
you are given is built only from the single worst breach magnitude per \
category (max issuer breach, max sector breach) - it structurally cannot see \
whether that breach is isolated or one of many concurrent breaches. Many \
concurrent moderate breaches across categories is a materially different, \
and often more dangerous, risk profile than one severe isolated breach at \
the same score - treat that as its own distinct signal and say so explicitly \
whenever it applies.
3. Explain WHY this matters in plain language, especially where signals \
interact (e.g. a position within its individual limit but concentrated in a \
stressed, correlated sector).
4. Weigh `metrics.volatility_signals` (30-day realized volatility vs. the \
same window ~1 quarter earlier, per issuer) as context, not as a limit \
breach in its own right - a breaching or near-limit position whose realized \
volatility is sharply rising quarter-over-quarter is a materially more \
urgent case than the same breach with flat or falling volatility. Call this \
out explicitly in `conflicting_signals` or `key_drivers` when it applies; if \
no volatility signals are provided, say nothing about it rather than \
inferring one.
5. Note any conflicting or reinforcing signals across categories, including \
the concurrent-breach signal from point 2 and the volatility context from \
point 4.
6. If historical incident data is provided, compare current conditions \
against it; if none is provided, state plainly that no historical \
comparison is available rather than inferring one.
7. You may adjust the final severity up or down by exactly one tier from the \
provided base severity, but only when you cite a specific conflicting or \
compounding signal as the reason in `compounding_signal` or `key_drivers` - \
never adjust silently, and never move more than one tier.
8. Write for a reader who has seconds, not minutes, to grasp the verdict: \
`headline` is exactly one sentence stating the verdict and its urgency; \
`key_drivers` is 3-5 short bullet points (fragments, not full paragraphs) \
naming the specific factors that drove the severity call; `compounding_signal` \
is always populated with the point-2 judgment - one root cause manifesting \
across categories, or genuinely independent risks - even when the answer is \
that the risks look independent; `data_gaps` is one line naming what wasn't \
available (e.g. volatility, historical incidents) or "none" if nothing is \
missing.
9. Return ONLY the structured tool call matching the provided schema. No \
prose outside of it."""

TOOL_SCHEMA = {
    "name": TOOL_NAME,
    "description": (
        "Structured risk analysis of a portfolio's pre-computed concentration "
        "metrics, per design_document.md §7. Call this exactly once with "
        "your complete analysis."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "breaches": {
                "type": "array",
                "description": "Every metric at WARNING or BREACH status across all categories.",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "enum": ["issuer", "sector", "geography", "asset_class", "correlation", "currency"],
                        },
                        "name": {"type": "string"},
                        "status": {"type": "string", "enum": ["WARNING", "BREACH"]},
                        "detail": {"type": "string"},
                    },
                    "required": ["category", "name", "status", "detail"],
                    "additionalProperties": False,
                },
            },
            "conflicting_signals": {
                "type": "array",
                "description": (
                    "Signals that interact, reinforce, or conflict across categories - "
                    "including a concurrent-breach signal when many categories/entries "
                    "breach at once."
                ),
                "items": {"type": "string"},
            },
            "historical_comparison": {
                "type": "string",
                "description": "Comparison against historical_incidents, or 'none available'.",
            },
            "severity": {
                "type": "string",
                "enum": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
            },
            "confidence_pct": {
                "type": "integer",
                "description": "Your confidence in this analysis, 0-100.",
            },
            "headline": {
                "type": "string",
                "description": "One sentence: the verdict and its urgency.",
            },
            "key_drivers": {
                "type": "array",
                "description": (
                    "3-5 short bullet points (fragments, not full sentences) naming the "
                    "specific factors driving the severity verdict."
                ),
                "items": {"type": "string"},
            },
            "compounding_signal": {
                "type": "string",
                "description": (
                    "One paragraph judging whether this is one root cause manifesting "
                    "across multiple categories, or genuinely independent risks. The "
                    "highest-value insight in the analysis - always populate it, even "
                    "when the risks look independent."
                ),
            },
            "data_gaps": {
                "type": "string",
                "description": (
                    "One line naming what data wasn't available (e.g. volatility, "
                    "historical incidents), or 'none' if nothing is missing."
                ),
            },
            "estimated_review_minutes": {
                "type": "integer",
                "description": "Estimated minutes for a human reviewer to check this analysis, >= 0.",
            },
        },
        "required": [
            "breaches",
            "conflicting_signals",
            "historical_comparison",
            "severity",
            "confidence_pct",
            "headline",
            "key_drivers",
            "compounding_signal",
            "data_gaps",
            "estimated_review_minutes",
        ],
        "additionalProperties": False,
    },
}
