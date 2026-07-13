"""
Task 3 (cont.) - Claude Reasoning Layer: API Client

Wraps the single Claude call per portfolio analysis (design_document.md
§7/§9): forced JSON output via tool use, prompt caching on the system
prompt + limits config, and full token/cost logging to the audit trail.

Quant math never happens here - this module only sends numbers
engine/concentration.py and engine/scoring.py already computed, and asks
Claude to reason over them.
"""

import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone

import anthropic

# Allow `python claude/client.py` (not just `python -m claude.client`) to find
# the claude package - direct script invocation puts this file's directory on
# sys.path, not the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude.prompts import SYSTEM_PROMPT, TOOL_NAME, TOOL_SCHEMA

# Claude Sonnet per CLAUDE.md ("Claude Sonnet (analysis/rationale calls)").
MODEL = "claude-sonnet-5"
MAX_TOKENS = 4096

# $/1M tokens - claude-sonnet-5 introductory pricing, in effect through
# 2026-08-31 (reverts to $3.00 / $15.00 standard rate after). Update if
# Anthropic's pricing page changes.
PRICE_PER_MTOK_INPUT = 2.00
PRICE_PER_MTOK_OUTPUT = 10.00
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute ephemeral TTL (default)
CACHE_READ_MULTIPLIER = 0.10

AUDIT_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "audit_log.json"
)


class ClaudeAnalysisError(Exception):
    """Raised when the Claude call fails, times out, or returns no usable
    tool response. Callers should catch this and fall back to the
    engine.scoring base severity result rather than blocking the analysis.
    """


def _entry_to_dict(entry) -> dict:
    return {
        "name": entry.name,
        "market_value": entry.market_value,
        "pct": entry.pct,
        "limit": entry.limit,
        "status": entry.status,
    }


def _cluster_to_dict(cluster) -> dict:
    return {"holdings": cluster.holdings, "min_corr": cluster.min_corr, "status": cluster.status}


def _volatility_to_dict(signal) -> dict:
    return {
        "position_id": signal.position_id,
        "issuer": signal.issuer,
        "realized_vol_pct": signal.realized_vol_pct,
        "vol_change_qoq_pct": signal.vol_change_qoq_pct,
    }


def build_user_message(portfolio, report, severity_result, historical_incidents=None) -> dict:
    """Assemble the per-call user message (design_document.md §7).

    Sends the FULL breach lists for every category (not just the worst
    entry) plus the step-2 severity/component breakdown, so Claude can do
    the compounding-signal analysis the base score structurally can't
    (see engine/scoring.py's max-breach-magnitude design and the
    corresponding prompt instruction in claude/prompts.py).
    """
    fund_name = getattr(portfolio, "fund_name", None) or portfolio.get("fund_name", "UNKNOWN")
    raw_flags = getattr(portfolio, "data_quality_flags", None)
    if raw_flags is None:
        raw_flags = portfolio.get("data_quality_flags", [])

    flag_dicts = [
        {"position_id": f.position_id, "issue": f.issue, "detail": f.detail}
        if hasattr(f, "position_id") else f
        for f in raw_flags
    ]
    for excluded_id in report.excluded_from_issuer:
        flag_dicts.append({
            "position_id": excluded_id, "issue": "excluded_from_issuer",
            "detail": "Cash position - excluded from issuer concentration (not single-name risk)",
        })
    for excluded_id in report.excluded_from_sector:
        flag_dicts.append({
            "position_id": excluded_id, "issue": "excluded_from_sector",
            "detail": "Missing sector tag - excluded from sector concentration",
        })
    for excluded_id in report.excluded_from_geography:
        flag_dicts.append({
            "position_id": excluded_id, "issue": "excluded_from_geography",
            "detail": "Missing geography tag - excluded from geography concentration",
        })

    components = asdict(severity_result.components) if severity_result.components else None

    return {
        "portfolio_id": report.portfolio_id,
        "fund_name": fund_name,
        "nav": report.nav,
        "metrics": {
            "issuer_concentration": [_entry_to_dict(e) for e in report.issuer_concentration],
            "sector_concentration": [_entry_to_dict(e) for e in report.sector_concentration],
            "geography_concentration": [_entry_to_dict(e) for e in report.geography_concentration],
            "asset_class_concentration": [_entry_to_dict(e) for e in report.asset_class_concentration],
            "currency_concentration": [_entry_to_dict(e) for e in report.currency_concentration],
            "hhi": report.hhi,
            "correlation_clusters": [_cluster_to_dict(c) for c in report.correlation_clusters],
            "volatility_signals": [_volatility_to_dict(v) for v in report.volatility_signals],
        },
        "base_severity": {
            "severity": severity_result.severity,
            "score": severity_result.score,
            "components": components,
            "structural_notes": severity_result.structural_notes,
        },
        "data_quality_flags": flag_dicts,
        "historical_incidents": historical_incidents or [],
    }


def _compute_cost(usage) -> float:
    input_cost = usage.input_tokens * PRICE_PER_MTOK_INPUT
    output_cost = usage.output_tokens * PRICE_PER_MTOK_OUTPUT
    cache_write_cost = (usage.cache_creation_input_tokens or 0) * PRICE_PER_MTOK_INPUT * CACHE_WRITE_MULTIPLIER
    cache_read_cost = (usage.cache_read_input_tokens or 0) * PRICE_PER_MTOK_INPUT * CACHE_READ_MULTIPLIER
    return (input_cost + output_cost + cache_write_cost + cache_read_cost) / 1_000_000


def append_audit_entry(entry: dict) -> None:
    log = []
    if os.path.exists(AUDIT_LOG_PATH):
        with open(AUDIT_LOG_PATH, "r") as f:
            try:
                log = json.load(f)
            except json.JSONDecodeError:
                log = []
    log.append(entry)
    with open(AUDIT_LOG_PATH, "w") as f:
        json.dump(log, f, indent=2)


def _log_error_and_raise(portfolio_id: str, error_type: str, exc: Exception):
    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": portfolio_id,
        "model": MODEL,
        "error_type": error_type,
        "error": str(exc),
    })
    raise ClaudeAnalysisError(f"Claude API call failed ({error_type}): {exc}") from exc


def analyze_portfolio(portfolio, report, severity_result, limits: dict,
                       historical_incidents=None, client=None) -> dict:
    """Make the single Claude call for this portfolio analysis run
    (design_document.md §7/§9) and return the parsed tool-use result
    (a dict matching claude.prompts.TOOL_SCHEMA).

    Raises ClaudeAnalysisError on API failure, timeout, or a missing/unusable
    tool response - callers should catch this and fall back to the base
    severity score (engine.scoring.compute_severity) instead of blocking the
    whole analysis.
    """
    client = client or anthropic.Anthropic()
    user_message = build_user_message(portfolio, report, severity_result, historical_incidents)

    # System prompt + limits config are static across calls in a session -
    # cache_control on the last block caches both (design_document.md §9).
    system = [
        {"type": "text", "text": SYSTEM_PROMPT},
        {
            "type": "text",
            "text": f"Configured limits:\n{json.dumps(limits, sort_keys=True)}",
            "cache_control": {"type": "ephemeral"},
        },
    ]

    started = time.monotonic()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=[TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": json.dumps(user_message, sort_keys=True)}],
        )
    except anthropic.RateLimitError as e:
        _log_error_and_raise(report.portfolio_id, "rate_limit_error", e)
    except anthropic.APIConnectionError as e:
        _log_error_and_raise(report.portfolio_id, "connection_error", e)
    except anthropic.APIStatusError as e:
        _log_error_and_raise(report.portfolio_id, f"api_error_{e.status_code}", e)

    elapsed_s = round(time.monotonic() - started, 2)

    tool_use_block = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use_block is None or tool_use_block.name != TOOL_NAME:
        _log_error_and_raise(
            report.portfolio_id,
            "missing_tool_response",
            RuntimeError(f"No {TOOL_NAME} tool_use block (stop_reason={response.stop_reason})"),
        )

    analysis = tool_use_block.input
    usage = response.usage
    cost_usd = round(_compute_cost(usage), 6)

    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": report.portfolio_id,
        "model": MODEL,
        "call_type": "analysis",
        "elapsed_s": elapsed_s,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
        "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
        "cost_usd": cost_usd,
        "severity": analysis.get("severity"),
        "confidence_pct": analysis.get("confidence_pct"),
    })

    return analysis


if __name__ == "__main__":
    from ingestion.normalize import load_portfolio, normalize_portfolio
    from engine.concentration import compute_concentration, DEFAULT_LIMITS
    from engine.scoring import compute_severity

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio)
    severity_result = compute_severity(report)

    print("=== System prompt ===")
    print(SYSTEM_PROMPT)

    user_message = build_user_message(portfolio, report, severity_result)
    print("\n=== User message ===")
    print(json.dumps(user_message, indent=2, sort_keys=True))

    print("\n=== Calling Claude ===")
    try:
        analysis = analyze_portfolio(portfolio, report, severity_result, DEFAULT_LIMITS)
    except ClaudeAnalysisError as e:
        print(f"Claude call failed: {e}")
        sys.exit(1)

    print("\n=== Raw tool-use response ===")
    print(json.dumps(analysis, indent=2, sort_keys=True))

    with open(AUDIT_LOG_PATH, "r") as f:
        last_entry = json.load(f)[-1]
    print("\n=== Audit trail entry ===")
    print(json.dumps(last_entry, indent=2, sort_keys=True))
