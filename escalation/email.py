"""
Task 4 (cont.) - Real Email Escalation Channel

Unlike Slack/Jira (still simulated), this actually sends via Gmail SMTP
(smtp.gmail.com:587, STARTTLS) using a Gmail app password. Credentials are
loaded from a .env file (never hardcoded, never committed - see
.env.example and .gitignore).

Composition (building the subject/body) and transmission (actually sending)
are deliberately separate functions - see compose_escalation_email() vs
send_email() - so the email can be previewed in the dashboard without being
sent, and sending only ever happens on an explicit user action.
"""

import os
import re
import smtplib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText

from dotenv import load_dotenv

from claude.client import append_audit_entry

load_dotenv()

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")
DEFAULT_ESCALATION_EMAIL_TO = os.environ.get("ESCALATION_EMAIL_TO", "")

# A hung connection during a live demo is worse than a fast, clear failure.
SMTP_TIMEOUT_S = 10

# Matches design_document.md §10's severity->action table: email joins
# Slack/Jira as an eligible action only at these tiers.
EMAIL_ELIGIBLE_SEVERITIES = {"HIGH", "CRITICAL"}

CONCENTRATION_CATEGORIES = [
    ("issuer", "issuer_concentration"),
    ("sector", "sector_concentration"),
    ("geography", "geography_concentration"),
    ("asset_class", "asset_class_concentration"),
    ("currency", "currency_concentration"),
]

# Minimal validation only (not full RFC 5321) - just enough to catch typos
# and reject a malformed address before it reaches Gmail, rather than
# letting Gmail refuse the whole batch with a cryptic 553.
_ADDRESS_PATTERN = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


class EmailEscalationError(Exception):
    """Raised on SMTP failure (auth, connection, timeout) or on a recipient
    address that fails minimal validation before anything is sent. Callers
    must degrade gracefully - this must never block or crash the main
    analysis, which has already completed by the time an email is composed
    or sent.
    """


@dataclass
class ComposedEmail:
    recipient: str
    subject: str
    body: str


@dataclass
class SendResult:
    """Returned on any send attempt that reached the SMTP server (i.e. not
    a pre-flight validation rejection, which raises instead). `failed` is
    empty on a fully successful send - a non-empty `failed` with a
    non-empty `succeeded` means a PARTIAL failure, not a total one.
    """
    succeeded: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)  # recipient -> error message


def parse_recipients(raw: str) -> list:
    """Split a raw recipient string on commas, semicolons, and/or
    whitespace, stripping empties. A single address yields a length-1 list.
    """
    if not raw:
        return []
    return [part for part in re.split(r"[,;\s]+", raw.strip()) if part]


def _invalid_addresses(recipients: list) -> list:
    return [addr for addr in recipients if not _ADDRESS_PATTERN.match(addr)]


def _breach_table_lines(report) -> list:
    """WARNING/BREACH lines across all 5 concentration categories, with
    value vs limit - pulled directly from the deterministic engine report,
    not from Claude's free-text `breaches` list, so the numbers are exact.
    """
    lines = []
    for category_label, attr_name in CONCENTRATION_CATEGORIES:
        for entry in getattr(report, attr_name, []) or []:
            if entry.status in ("WARNING", "BREACH"):
                lines.append(
                    f"  [{entry.status}] {category_label}: {entry.name} - "
                    f"{entry.pct}% (limit {entry.limit}%)"
                )
    return lines


def compose_escalation_email(portfolio_id: str, fund_name: str, nav: float, base_currency: str,
                              severity: str, confidence_pct: int, report, headline: str,
                              jira_ticket_id: str, slack_channel: str, recipient: str) -> ComposedEmail:
    """Build the subject + plain-text body for an escalation email. Pure
    composition, no network activity - safe to call for a preview that is
    never sent.
    """
    subject = f"[{severity}] Concentration breach - {portfolio_id} ({fund_name})"
    timestamp = datetime.now(timezone.utc).isoformat()
    breach_lines = _breach_table_lines(report)
    breach_block = "\n".join(breach_lines) if breach_lines else "  (none at WARNING/BREACH status)"

    body = (
        f"Portfolio: {portfolio_id} - {fund_name}\n"
        f"NAV: {nav:,.0f} {base_currency}\n"
        f"Timestamp: {timestamp}\n"
        f"Final severity: {severity} (confidence {confidence_pct}%)\n"
        f"\n"
        f"Rationale: {headline or 'See dashboard for full analysis.'}\n"
        f"\n"
        f"Breach / warning metrics (value vs limit):\n"
        f"{breach_block}\n"
        f"\n"
        f"Simulated Slack alert channel: {slack_channel}\n"
        f"Simulated Jira ticket: {jira_ticket_id or 'n/a'}\n"
        f"\n"
        f"This is an automated escalation from the Portfolio Risk & Exposure Platform.\n"
    )
    return ComposedEmail(recipient=recipient, subject=subject, body=body)


def _log_send_result(portfolio_id, severity, recipients, subject, outcome, error=None, per_recipient=None) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "portfolio_id": portfolio_id,
        "call_type": "escalation_email",
        "recipients": list(recipients),  # JSON array, not a single string
        "subject": subject,
        "severity": severity,
        "outcome": outcome,
    }
    if error:
        entry["error"] = error
    if per_recipient:
        entry["per_recipient"] = per_recipient
    append_audit_entry(entry)


def send_email(recipient: str, subject: str, body: str, portfolio_id: str = None, severity: str = None) -> SendResult:
    """Actually transmit the email via Gmail SMTP to one or more recipients.

    `recipient` may contain multiple addresses separated by commas,
    semicolons, and/or whitespace - parsed into a real list before use, so
    Gmail never sees them collapsed into one malformed address.

    Raises EmailEscalationError for a TOTAL failure: missing credentials, a
    recipient that fails minimal validation (rejected before anything is
    sent, listing the bad address), or an SMTP-level failure (auth,
    connection, timeout) that means no recipient was reached.

    Returns a SendResult on any attempt that reached the SMTP server - if
    some recipients were refused by Gmail but others succeeded (smtplib's
    sendmail() returns a non-empty dict without raising), that is a PARTIAL
    failure, not a total one, and is reported as such rather than raising.
    """
    recipients = parse_recipients(recipient)

    if not SMTP_USER or not SMTP_APP_PASSWORD:
        _log_send_result(portfolio_id, severity, recipients, subject, "failed",
                          "SMTP_USER/SMTP_APP_PASSWORD not configured (check .env)")
        raise EmailEscalationError(
            "SMTP credentials not configured - set SMTP_USER and SMTP_APP_PASSWORD in .env "
            "(see .env.example)."
        )

    if not recipients:
        _log_send_result(portfolio_id, severity, recipients, subject, "failed", "No recipient address provided")
        raise EmailEscalationError("No recipient address provided.")

    invalid = _invalid_addresses(recipients)
    if invalid:
        error = f"Invalid recipient address(es), nothing sent: {', '.join(invalid)}"
        _log_send_result(portfolio_id, severity, recipients, subject, "failed", error)
        raise EmailEscalationError(error)

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(recipients)  # header: comma-separated string

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            # to_addrs: the actual LIST, never the joined header string -
            # these are two different things and must not share one value.
            refused = server.sendmail(SMTP_USER, recipients, msg.as_string())
    except (smtplib.SMTPException, OSError, TimeoutError) as e:
        _log_send_result(portfolio_id, severity, recipients, subject, "failed", str(e))
        raise EmailEscalationError(f"Failed to send escalation email: {e}") from e

    failed = {addr: str(err) for addr, err in refused.items()}
    succeeded = [addr for addr in recipients if addr not in failed]

    if failed:
        per_recipient = {addr: "sent" for addr in succeeded}
        per_recipient.update({addr: f"failed: {err}" for addr, err in failed.items()})
        _log_send_result(portfolio_id, severity, recipients, subject, "partial", per_recipient=per_recipient)
    else:
        _log_send_result(portfolio_id, severity, recipients, subject, "sent")

    return SendResult(succeeded=succeeded, failed=failed)


if __name__ == "__main__":
    from engine.concentration import compute_concentration, DEFAULT_LIMITS
    from engine.scoring import compute_severity
    from ingestion.normalize import load_portfolio, normalize_portfolio

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio, DEFAULT_LIMITS)
    severity_result = compute_severity(report)

    composed = compose_escalation_email(
        portfolio_id=report.portfolio_id, fund_name=portfolio.fund_name, nav=report.nav,
        base_currency=portfolio.base_currency, severity=severity_result.severity,
        confidence_pct=90, report=report, headline="Test headline for CLI demo.",
        jira_ticket_id="RISK-TEST-123", slack_channel="#risk-desk-alerts",
        recipient=DEFAULT_ESCALATION_EMAIL_TO or "test@example.com",
    )
    print("=== Composed email (preview only, not sent) ===")
    print("Subject:", composed.subject)
    print()
    print(composed.body)
