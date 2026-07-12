"""
Task 4 - Escalation Engine

Severity -> action mapping (design_document.md §10). The trigger input is
Claude's FINAL severity verdict - i.e. the value returned by
claude.client.analyze_portfolio()['severity'], already put through the
§8.2 one-tier adjustment - never the raw engine.scoring base score. The
whole point of that adjustment is that Claude may have overridden the
rules-engine severity for a cited reason, and the escalation engine must
act on that adjusted call, not re-derive its own.

Slack and Jira are simulated as structured objects (printed with a
realistic payload shape), not live webhooks. The CRITICAL-only escalation
memo is drafted by Claude Haiku, per the project's model-routing rule
(Sonnet for analysis/rationale, Haiku for cheap templating) - it is a
second, independent Claude call and is logged to the audit trail exactly
like the Sonnet analysis call.
"""

import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import anthropic

# Allow `python escalation/actions.py` (not just `python -m escalation.actions`)
# to find the claude package - direct script invocation puts this file's
# directory on sys.path, not the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from claude.client import append_audit_entry

# Severity -> ordered list of actions, per design_document.md §10 table.
SEVERITY_ACTIONS = {
    "LOW": ["audit_log"],
    "MEDIUM": ["slack_alert", "dashboard_flag"],
    "HIGH": ["slack_alert", "jira_ticket", "dashboard_flag"],
    "CRITICAL": ["slack_alert", "jira_ticket", "dashboard_flag", "escalation_memo"],
}

SLACK_CHANNEL = "#risk-desk-alerts"
JIRA_PROJECT = "RISK"

MEMO_MODEL = "claude-haiku-4-5"
MEMO_MAX_TOKENS = 600
# $/1M tokens - claude-haiku-4-5 pricing.
MEMO_PRICE_PER_MTOK_INPUT = 1.00
MEMO_PRICE_PER_MTOK_OUTPUT = 5.00

MEMO_SYSTEM_PROMPT = """You draft short internal escalation memos for a risk \
committee from an already-completed portfolio risk analysis. Do not \
re-analyze the data, recompute figures, or introduce new numbers - only \
synthesize the severity verdict and rationale you are given into a concise, \
formal memo (3-5 short paragraphs) addressed to the Risk Committee. Return \
only the memo body text, no subject line, no preamble."""


class EscalationError(Exception):
    """Raised when a required downstream action (e.g. the Haiku memo call)
    fails. Callers should still consider the other actions taken - this is
    raised only after the audit entry for the failure has been logged.
    """


@dataclass
class SlackAlert:
    channel: str
    urgent: bool
    text: str


@dataclass
class JiraTicket:
    ticket_id: str
    project: str
    priority: str
    summary: str
    description: str


@dataclass
class DashboardFlag:
    portfolio_id: str
    severity: str
    confidence_pct: int
    headline: str


@dataclass
class EscalationMemo:
    recipient: str
    subject: str
    body: str


def _build_slack_alert(portfolio_id, severity, confidence_pct, headline, urgent) -> SlackAlert:
    tag = f"{severity} - URGENT" if urgent else severity
    headline_text = headline or "See dashboard for details."
    text = (
        f"[{tag}] Portfolio {portfolio_id} concentration risk alert "
        f"(confidence {confidence_pct}%). {headline_text}"
    )
    return SlackAlert(channel=SLACK_CHANNEL, urgent=urgent, text=text)


def _build_jira_ticket(portfolio_id, severity, confidence_pct, breaches, headline, key_drivers) -> JiraTicket:
    breaches = breaches or []
    key_drivers = key_drivers or []
    breach_names = [f"{b.get('category')}:{b.get('name')}" for b in breaches if b.get("status") == "BREACH"]
    ticket_id = f"{JIRA_PROJECT}-{portfolio_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    priority = "Highest" if severity == "CRITICAL" else "High"
    summary = f"[{severity}] Concentration risk review - {portfolio_id}"
    drivers_block = "\n".join(f"- {d}" for d in key_drivers) or "- (none provided)"
    description = (
        f"{headline or 'See audit trail for full analysis.'}\n\n"
        f"Key drivers:\n{drivers_block}\n\n"
        f"{len(breach_names)} BREACH-status metric(s): {', '.join(breach_names) or 'none'}.\n"
        f"Confidence: {confidence_pct}%."
    )
    return JiraTicket(ticket_id=ticket_id, project=JIRA_PROJECT, priority=priority,
                       summary=summary, description=description)


def _build_dashboard_flag(portfolio_id, severity, confidence_pct, headline) -> DashboardFlag:
    return DashboardFlag(portfolio_id=portfolio_id, severity=severity,
                          confidence_pct=confidence_pct, headline=headline or f"{severity} severity")


def _draft_escalation_memo(portfolio_id, severity, confidence_pct, breaches,
                            conflicting_signals, headline, key_drivers, compounding_signal, client=None) -> EscalationMemo:
    """Cheap Haiku templating call - drafts memo prose from the analysis
    Claude Sonnet already produced. Logs its own audit trail entry, same as
    every other Claude call in this project.

    Unlike the Slack/Jira/dashboard cards (which need the RIGHT-SIZED field
    for a quick scan), a memo is expected to be longer-form - so it draws on
    `compounding_signal` and `key_drivers` as source material rather than
    just the one-sentence `headline`.
    """
    client = client or anthropic.Anthropic()
    breaches = breaches or []
    conflicting_signals = conflicting_signals or []
    key_drivers = key_drivers or []

    user_content = (
        f"Portfolio: {portfolio_id}\n"
        f"Severity: {severity}\n"
        f"Confidence: {confidence_pct}%\n"
        f"Headline: {headline or 'Not provided.'}\n"
        f"Breach count: {len(breaches)}\n"
        f"Key drivers:\n- " + "\n- ".join(key_drivers) + "\n\n"
        f"Conflicting/compounding signals:\n- " + "\n- ".join(conflicting_signals) + "\n\n"
        f"Root cause vs. independent risks analysis:\n{compounding_signal or 'Not provided.'}"
    )

    try:
        response = client.messages.create(
            model=MEMO_MODEL,
            max_tokens=MEMO_MAX_TOKENS,
            system=MEMO_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
    except anthropic.APIError as e:
        append_audit_entry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio_id": portfolio_id,
            "model": MEMO_MODEL,
            "error_type": "memo_draft_failed",
            "error": str(e),
        })
        raise EscalationError(f"Escalation memo drafting failed: {e}") from e

    body = next((b.text for b in response.content if b.type == "text"), "").strip()
    usage = response.usage
    cost_usd = round(
        (usage.input_tokens * MEMO_PRICE_PER_MTOK_INPUT
         + usage.output_tokens * MEMO_PRICE_PER_MTOK_OUTPUT) / 1_000_000,
        6,
    )

    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": portfolio_id,
        "model": MEMO_MODEL,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": cost_usd,
        "purpose": "escalation_memo_draft",
    })

    return EscalationMemo(recipient="Risk Committee", subject=f"[{severity}] {portfolio_id} concentration risk", body=body)


def escalate(portfolio_id, severity, confidence_pct, breaches=None, conflicting_signals=None,
             headline=None, key_drivers=None, compounding_signal=None, haiku_client=None) -> dict:
    """Execute the severity -> action mapping from design_document.md §10.

    `severity` must be Claude's final verdict (post one-tier adjustment),
    not the raw engine.scoring base score. Simulates Slack/Jira as printed
    structured objects, drafts a Haiku escalation memo for CRITICAL only,
    and writes one audit trail entry recording every action taken.

    Each downstream surface gets the RIGHT-SIZED field, not one long blob:
    `headline` (one sentence) drives Slack/Jira/dashboard, `key_drivers`
    adds bullets to the Jira description, and `compounding_signal` (plus
    `key_drivers`) is source material for the longer-form Haiku memo.
    """
    if severity not in SEVERITY_ACTIONS:
        raise ValueError(f"Unknown severity: {severity!r} - expected one of {list(SEVERITY_ACTIONS)}")

    result = {"portfolio_id": portfolio_id, "severity": severity, "actions_taken": []}

    if severity == "LOW":
        result["actions_taken"].append("audit_log")
        append_audit_entry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "portfolio_id": portfolio_id,
            "severity": severity,
            "confidence_pct": confidence_pct,
            "actions_taken": result["actions_taken"],
        })
        return result

    slack = _build_slack_alert(portfolio_id, severity, confidence_pct, headline, urgent=(severity == "CRITICAL"))
    result["slack_alert"] = asdict(slack)
    result["actions_taken"].append("slack_alert")
    print(f"[SIMULATED SLACK -> {slack.channel}] {slack.text}")

    if severity in ("HIGH", "CRITICAL"):
        ticket = _build_jira_ticket(portfolio_id, severity, confidence_pct, breaches, headline, key_drivers)
        result["jira_ticket"] = asdict(ticket)
        result["actions_taken"].append("jira_ticket")
        print(f"[SIMULATED JIRA] {ticket.ticket_id} ({ticket.priority}): {ticket.summary}")

    flag = _build_dashboard_flag(portfolio_id, severity, confidence_pct, headline)
    result["dashboard_flag"] = asdict(flag)
    result["actions_taken"].append("dashboard_flag")
    print(f"[DASHBOARD FLAG] {flag.severity} - {flag.headline}")

    if severity == "CRITICAL":
        memo = _draft_escalation_memo(portfolio_id, severity, confidence_pct, breaches, conflicting_signals,
                                       headline, key_drivers, compounding_signal, client=haiku_client)
        result["escalation_memo"] = asdict(memo)
        result["actions_taken"].append("escalation_memo")
        print(f"[ESCALATION MEMO -> {memo.recipient}] {memo.subject}\n{memo.body}")

    append_audit_entry({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": portfolio_id,
        "severity": severity,
        "confidence_pct": confidence_pct,
        "actions_taken": result["actions_taken"],
    })

    return result


if __name__ == "__main__":
    import json

    from ingestion.normalize import load_portfolio, normalize_portfolio
    from engine.concentration import compute_concentration, DEFAULT_LIMITS
    from engine.scoring import compute_severity
    from claude.client import analyze_portfolio, ClaudeAnalysisError

    from claude.client import AUDIT_LOG_PATH

    audit_log_start_len = 0
    if os.path.exists(AUDIT_LOG_PATH):
        with open(AUDIT_LOG_PATH, "r") as f:
            audit_log_start_len = len(json.load(f))

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio)
    severity_result = compute_severity(report)

    print("=== Running real Claude analysis (step 3) to get the actual verdict ===")
    try:
        analysis = analyze_portfolio(portfolio, report, severity_result, DEFAULT_LIMITS)
    except ClaudeAnalysisError as e:
        print(f"Claude call failed: {e}")
        sys.exit(1)

    print(f"Final severity: {analysis['severity']}  confidence: {analysis['confidence_pct']}%\n")

    print("=== Escalating on Claude's final verdict ===")
    result = escalate(
        portfolio_id=report.portfolio_id,
        severity=analysis["severity"],
        confidence_pct=analysis["confidence_pct"],
        breaches=analysis["breaches"],
        conflicting_signals=analysis["conflicting_signals"],
        headline=analysis["headline"],
        key_drivers=analysis["key_drivers"],
        compounding_signal=analysis["compounding_signal"],
    )

    print("\n=== Escalation result ===")
    print(json.dumps(result, indent=2))

    print("\n=== Edge case: synthetic CRITICAL (exercises the Haiku memo path) ===")
    critical_result = escalate(
        portfolio_id="CRITICAL-TEST",
        severity="CRITICAL",
        confidence_pct=95,
        breaches=analysis["breaches"],
        conflicting_signals=analysis["conflicting_signals"],
        headline=analysis["headline"],
        key_drivers=analysis["key_drivers"],
        compounding_signal=analysis["compounding_signal"],
    )
    print(json.dumps(critical_result, indent=2))

    print("\n=== Edge case: LOW (log-only) ===")
    low_result = escalate(portfolio_id="LOW-TEST", severity="LOW", confidence_pct=90)
    print(json.dumps(low_result, indent=2))

    with open(AUDIT_LOG_PATH, "r") as f:
        log = json.load(f)
    new_entries = log[audit_log_start_len:]
    print(f"\n=== All {len(new_entries)} audit trail entries written by this run ===")
    print(json.dumps(new_entries, indent=2))
