"""
Independent Claude Verification Layer (audit, not scoring)

Design intent: engine/concentration.py remains the sole source of truth for
concentration math - this module never feeds severity scoring or the main
analysis call. It's an on-demand, user-triggered audit: Claude independently
re-derives 3 headline figures (top sector %, top issuer %, HHI) from RAW
position data and the result is compared against the engine's own output.

Sending RAW positions here (not the engine's pre-computed summary table) is
deliberate - the whole point is independence from the engine's own rollup.
Feeding Claude the summary table would make this check circular.

Uses Sonnet (not Haiku) because this call requires reliable arithmetic over
many positions, not cheap templating. Forced JSON output via tool use, same
pattern as claude/client.py.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import anthropic

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude.client import AUDIT_LOG_PATH, MODEL, _compute_cost, append_audit_entry

VERIFY_TOOL_NAME = "portfolio_verification"
VERIFY_MAX_TOKENS = 1500

# |claude_value - engine_value| at or below this counts as MATCH - avoids
# cosmetic float/rounding false flags. Named constant, not inlined, so the
# threshold is auditable and adjustable in one place.
MATCH_TOLERANCE_PCT_POINTS = 0.05

VERIFY_SYSTEM_PROMPT = """You are an independent auditor for a portfolio risk \
platform. You will receive RAW position-level data (not any pre-computed \
summary), the fund's NAV, the configured limits, and 3 headline figures the \
deterministic engine already computed. Your job is to independently \
re-derive each of those 3 figures from the raw positions and report whether \
your recomputation matches the engine's.

Rules to apply (state which rule you used for each figure in `rule_applied`):
1. Exposure for every figure is abs(market_value) - market_value is already \
converted to the fund's base_currency; do not apply any further FX \
conversion, and do not use original_market_value for the math (it is native-\
currency context only).
2. `top_issuer_pct`: group by `issuer`, sum abs(market_value) per issuer, \
divide by NAV, x100. Positions with issuer null (cash) are excluded \
entirely from this figure - not zeroed, not grouped, excluded.
3. `top_sector_pct`: group by `sector`, sum abs(market_value) per sector, \
divide by NAV, x100. Positions with sector null are excluded entirely from \
this figure.
4. `hhi`: Herfindahl-Hirschman Index over EVERY position (no exclusions - \
cash and missing-tag positions all count here), on the standard 0-10000 \
scale: sum of (abs(market_value) / NAV x 100) squared, across all positions.
5. You are told which issuer/sector the engine considers "top" for figures 2 \
and 3 - recompute that SAME named entry's percentage, not whichever entry \
your own recomputation would rank highest (a ranking disagreement is a \
different failure mode than an arithmetic one, and this check is about the \
arithmetic).
6. For each figure, report your own recomputed value (`claude_value`), echo \
back the engine's value you were given (`engine_value`), and your own \
MATCH/MISMATCH judgment - the caller will independently re-verify this \
against a fixed numeric tolerance, so judge honestly rather than rounding in \
the engine's favor.
7. Return ONLY the structured tool call matching the schema. No prose \
outside of it."""

VERIFY_TOOL_SCHEMA = {
    "name": VERIFY_TOOL_NAME,
    "description": (
        "Independent recomputation of 3 headline concentration figures "
        "(top_sector_pct, top_issuer_pct, hhi) from raw position data, to "
        "cross-check engine/concentration.py's output. Call exactly once."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "figures": {
                "type": "array",
                "description": "Exactly one entry per figure: top_sector_pct, top_issuer_pct, hhi.",
                "items": {
                    "type": "object",
                    "properties": {
                        "figure": {
                            "type": "string",
                            "enum": ["top_sector_pct", "top_issuer_pct", "hhi"],
                        },
                        "rule_applied": {
                            "type": "string",
                            "description": "One line stating the exact rule/formula used.",
                        },
                        "claude_value": {
                            "type": "number",
                            "description": "Your own recomputed value from the raw positions.",
                        },
                        "engine_value": {
                            "type": "number",
                            "description": "The engine-computed value you were given for this figure, echoed back.",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["MATCH", "MISMATCH"],
                            "description": "Your own judgment - independently re-verified by the caller against a fixed tolerance.",
                        },
                        "note": {"type": "string", "description": "One-line note on this figure's check."},
                    },
                    "required": ["figure", "rule_applied", "claude_value", "engine_value", "status", "note"],
                    "additionalProperties": False,
                },
            },
            "overall_verdict": {
                "type": "string",
                "enum": ["ALL_MATCHED", "DISCREPANCY_FOUND"],
            },
        },
        "required": ["figures", "overall_verdict"],
        "additionalProperties": False,
    },
}


class ClaudeVerificationError(Exception):
    """Raised when the verification call fails, times out, or returns no
    usable tool response. Callers must degrade gracefully (report
    verification as unavailable) - this never blocks the main analysis,
    which has already completed by the time this is triggered.
    """


def _position_to_dict(pos) -> dict:
    return {
        "issuer": pos.get("issuer"),
        "sector": pos.get("sector"),
        "geography": pos.get("geography"),
        "asset_class": pos.get("asset_class"),
        "currency": pos.get("currency"),
        "market_value": pos.get("market_value"),
        "original_market_value": pos.get("original_market_value"),
    }


def build_verification_message(portfolio, report, limits: dict) -> dict:
    """Assemble the per-call payload: RAW positions (not the engine's summary
    table - see module docstring for why that distinction matters) + NAV +
    limits + the 3 engine-computed figures to check.
    """
    top_sector = report.sector_concentration[0] if report.sector_concentration else None
    top_issuer = report.issuer_concentration[0] if report.issuer_concentration else None

    return {
        "portfolio_id": report.portfolio_id,
        "nav": report.nav,
        "base_currency": getattr(portfolio, "base_currency", None) or portfolio.get("base_currency"),
        "positions": [_position_to_dict(p) for p in portfolio.positions] if hasattr(portfolio, "positions")
                     else [_position_to_dict(p) for p in portfolio.get("positions", [])],
        "limits": limits,
        "engine_figures": {
            "top_sector": {"name": top_sector.name if top_sector else None, "pct": top_sector.pct if top_sector else None},
            "top_issuer": {"name": top_issuer.name if top_issuer else None, "pct": top_issuer.pct if top_issuer else None},
            "hhi": report.hhi,
        },
    }


def _engine_actual_value(figure_name: str, top_sector, top_issuer, hhi) -> float:
    if figure_name == "top_sector_pct":
        return top_sector.pct if top_sector else 0.0
    if figure_name == "top_issuer_pct":
        return top_issuer.pct if top_issuer else 0.0
    if figure_name == "hhi":
        return hhi
    raise ValueError(f"Unknown figure: {figure_name!r}")


def verify_portfolio(portfolio, report, limits: dict, client=None) -> dict:
    """Make the one, user-triggered Sonnet verification call and return the
    parsed tool-use result, with status/overall_verdict re-derived
    deterministically in code (via MATCH_TOLERANCE_PCT_POINTS) rather than
    trusting Claude's own self-assessment - consistent with this project's
    rule that Claude never is the ground truth for arithmetic, only reasons
    over/checks numbers that code already computed or can independently verify.

    Raises ClaudeVerificationError on API failure - callers must catch this
    and report verification as unavailable rather than blocking anything.
    """
    client = client or anthropic.Anthropic()
    user_message = build_verification_message(portfolio, report, limits)

    top_sector = report.sector_concentration[0] if report.sector_concentration else None
    top_issuer = report.issuer_concentration[0] if report.issuer_concentration else None

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=VERIFY_MAX_TOKENS,
            system=VERIFY_SYSTEM_PROMPT,
            tools=[VERIFY_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": VERIFY_TOOL_NAME},
            messages=[{"role": "user", "content": json.dumps(user_message, sort_keys=True, default=str)}],
        )
    except anthropic.RateLimitError as e:
        _log_error_and_raise(report.portfolio_id, "rate_limit_error", e)
    except anthropic.APIConnectionError as e:
        _log_error_and_raise(report.portfolio_id, "connection_error", e)
    except anthropic.APIStatusError as e:
        _log_error_and_raise(report.portfolio_id, f"api_error_{e.status_code}", e)

    elapsed_s = round(time.monotonic() - started, 2)

    tool_use_block = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use_block is None or tool_use_block.name != VERIFY_TOOL_NAME:
        _log_error_and_raise(
            report.portfolio_id, "missing_tool_response",
            RuntimeError(f"No {VERIFY_TOOL_NAME} tool_use block (stop_reason={response.stop_reason})"),
        )

    result = tool_use_block.input

    # Re-derive status/overall_verdict deterministically - never trust
    # Claude's own MATCH/MISMATCH judgment or its echoed engine_value as
    # authoritative; the engine's real number and the fixed tolerance decide.
    any_mismatch = False
    for fig in result.get("figures", []):
        engine_actual = _engine_actual_value(fig["figure"], top_sector, top_issuer, report.hhi)
        fig["engine_value"] = engine_actual
        fig["status"] = (
            "MATCH" if abs(fig["claude_value"] - engine_actual) <= MATCH_TOLERANCE_PCT_POINTS else "MISMATCH"
        )
        if fig["status"] == "MISMATCH":
            any_mismatch = True
    result["overall_verdict"] = "DISCREPANCY_FOUND" if any_mismatch else "ALL_MATCHED"

    usage = response.usage
    cost_usd = round(_compute_cost(usage), 6)

    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": report.portfolio_id,
        "model": MODEL,
        "call_type": "verification",
        "elapsed_s": elapsed_s,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
        "cost_usd": cost_usd,
        "overall_verdict": result["overall_verdict"],
    })

    result["_audit"] = {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost_usd,
    }
    return result


def _log_error_and_raise(portfolio_id: str, error_type: str, exc: Exception):
    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": portfolio_id,
        "model": MODEL,
        "call_type": "verification",
        "error_type": error_type,
        "error": str(exc),
    })
    raise ClaudeVerificationError(f"Verification call failed ({error_type}): {exc}") from exc


if __name__ == "__main__":
    from ingestion.normalize import load_portfolio, normalize_portfolio
    from engine.concentration import compute_concentration, DEFAULT_LIMITS

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio, DEFAULT_LIMITS)

    print("=== Running verification call ===")
    try:
        result = verify_portfolio(portfolio, report, DEFAULT_LIMITS)
    except ClaudeVerificationError as e:
        print(f"Verification call failed: {e}")
        sys.exit(1)

    print(json.dumps(result, indent=2))
