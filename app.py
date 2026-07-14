"""
Task 5 - Streamlit Dashboard

Wires the full pipeline together end to end, per design_document.md §11's
demo script: load portfolio -> ingestion output -> deterministic
concentration metrics (no Claude call) -> the ONE Sonnet call on button
press -> escalation actions firing on Claude's final verdict -> token/cost
for that run -> the full audit trail.

Demo-first, not production UI - no auth, no persistence beyond the
existing audit_log.json, no styling beyond Streamlit's built-ins.
"""

import glob
import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from ingestion.normalize import load_portfolio, normalize_portfolio
from engine.concentration import compute_concentration, DEFAULT_LIMITS
from engine.scoring import compute_severity
from claude.client import AUDIT_LOG_PATH, ClaudeAnalysisError, analyze_portfolio
from claude.verify import ClaudeVerificationError, verify_portfolio
from escalation.actions import escalate
from escalation.email import DEFAULT_ESCALATION_EMAIL_TO, EmailEscalationError, send_email

SAMPLE_DIR = "data/sample_portfolios"
SEVERITY_ORDER = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

st.set_page_config(page_title="Portfolio Risk & Exposure Platform", layout="wide")
st.title("Portfolio Risk & Exposure Platform")


def _entries_df(entries) -> pd.DataFrame:
    if not entries:
        return pd.DataFrame(columns=["name", "market_value", "pct", "limit", "status"])
    return pd.DataFrame([asdict(e) for e in entries])


def _flags_df(flags) -> pd.DataFrame:
    if not flags:
        return pd.DataFrame(columns=["position_id", "issue", "detail"])
    return pd.DataFrame([asdict(f) for f in flags])


def _severity_banner(label: str, severity: str, extra: str = "") -> None:
    text = f"{label}: {severity}" + (f"  ({extra})" if extra else "")
    if severity in ("HIGH", "CRITICAL"):
        st.error(text)
    elif severity == "MEDIUM":
        st.warning(text)
    elif severity == "LOW":
        st.success(text)
    else:
        st.info(text)


def _compounding_signal_box(severity: str, text: str) -> None:
    if severity in ("HIGH", "CRITICAL"):
        st.warning(text)
    else:
        st.info(text)


# --- 1. Load / select a portfolio ---
st.header("1. Load Portfolio")

sample_files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.json")))
col_a, col_b = st.columns(2)
with col_a:
    selected_sample = st.selectbox("Sample portfolio", sample_files, format_func=os.path.basename)
with col_b:
    uploaded = st.file_uploader("...or upload a different portfolio JSON", type="json")

if uploaded is not None:
    # Streamlit reruns this whole script on every interaction, and the
    # UploadedFile's read cursor does NOT reset between reruns - without
    # seek(0), every rerun after the first would read an already-exhausted
    # stream (empty bytes) and fail to parse. Wrapped in try/except so a
    # non-JSON or malformed upload shows a clean error instead of crashing
    # the script.
    uploaded.seek(0)
    try:
        raw = json.load(uploaded)
    except json.JSONDecodeError as e:
        st.error(f"Could not parse \"{uploaded.name}\" as JSON: {e}")
        st.stop()
    source_key = f"upload:{uploaded.name}:{uploaded.size}"
else:
    raw = load_portfolio(selected_sample)
    source_key = f"sample:{selected_sample}"

if st.session_state.get("source_key") != source_key:
    # Portfolio changed - drop downstream state that belonged to the old one,
    # so a stale Claude/escalation result never gets shown for a new file.
    for key in ("analysis", "analyzed_source_key", "claude_audit_entry", "escalation_result",
                "verification_result", "email_send_result"):
        st.session_state.pop(key, None)
    st.session_state["source_key"] = source_key

try:
    portfolio = normalize_portfolio(raw)
except (KeyError, AttributeError, TypeError) as e:
    st.error(
        f"This file doesn't match the expected portfolio schema (missing/wrong-shaped "
        f"field): {e}. Expected top-level portfolio_id/fund_name/nav/positions, with each "
        f"position having position_id/account_id/instrument/asset_class/quantity/"
        f"market_value/currency."
    )
    st.stop()

st.write(
    f"**{portfolio.fund_name}** ({portfolio.portfolio_id}) - {portfolio.fund_type}, "
    f"NAV {portfolio.nav:,.0f} {portfolio.base_currency}, as of {portfolio.as_of}"
)

# --- 2. Ingestion output ---
st.header("2. Ingestion Output")
st.write(f"Positions loaded: {len(portfolio.positions)}")

fx_source_labels = {
    "live": "fetched live",
    "cache_fresh": "reused from cache (fetched recently, still fresh)",
    "cache_fallback": "live fetch failed - fell back to cache",
    "unavailable": "no live or cached rates available - market values left unconverted",
    "n/a": "not applicable",
}
fx_label = fx_source_labels.get(portfolio.fx_source, portfolio.fx_source)
st.caption(f"FX rates (base {portfolio.base_currency}): {fx_label}" + (
    f", as of {portfolio.fx_fetched_at}" if portfolio.fx_fetched_at else ""
))

st.subheader("Data quality flags")
if portfolio.data_quality_flags:
    st.dataframe(_flags_df(portfolio.data_quality_flags), width="stretch")
else:
    st.success("No data quality issues found.")

# --- 3. Deterministic concentration metrics (no Claude call) ---
st.header("3. Concentration Metrics (deterministic - no Claude call)")
report = compute_concentration(portfolio, DEFAULT_LIMITS)
severity_result = compute_severity(report)

st.metric("HHI (diversification index)", report.hhi)

tab1, tab2, tab3, tab4, tab5 = st.tabs(["Issuer", "Sector", "Geography", "Asset Class", "Currency"])
with tab1:
    st.dataframe(_entries_df(report.issuer_concentration), width="stretch")
    if report.excluded_from_issuer:
        st.caption(f"Excluded from issuer calc (cash, not single-name risk): {report.excluded_from_issuer}")
with tab2:
    st.dataframe(_entries_df(report.sector_concentration), width="stretch")
    if report.excluded_from_sector:
        st.caption(f"Excluded from sector calc (missing tag): {report.excluded_from_sector}")
with tab3:
    st.dataframe(_entries_df(report.geography_concentration), width="stretch")
    if report.excluded_from_geography:
        st.caption(f"Excluded from geography calc (missing tag): {report.excluded_from_geography}")
    st.caption(
        "Geopolitical tier / note / source / as_of columns are a RISK-DESK CONFIG INPUT "
        "(engine/geopolitical_risk.py) - never inferred by Claude or by this engine."
    )
    flagged_geos = [e for e in report.geography_concentration if e.geopolitical_flag]
    if flagged_geos:
        for e in flagged_geos:
            st.warning(
                f"**Geopolitical flag:** {e.name} at {e.pct}% exposure carries a "
                f"{e.geopolitical_tier}-tier geopolitical risk - {e.geopolitical_note} "
                f"(source: {e.geopolitical_source}, as_of: {e.geopolitical_as_of})"
            )
    else:
        st.caption("No geography currently combines an ELEVATED/HIGH tier with material exposure.")
with tab4:
    st.dataframe(_entries_df(report.asset_class_concentration), width="stretch")
with tab5:
    st.dataframe(_entries_df(report.currency_concentration), width="stretch")
    st.caption(
        "NET (signed) exposure per currency, not absolute - a short position reduces net "
        "currency sensitivity rather than adding to it, unlike issuer/sector/geography/asset "
        "class above (all absolute by design). Entries should sum to ~100% of NAV; "
        "`NET_SHORT` means net short that currency, not a diversification breach."
    )
    st.metric("Sum of currency exposures", f"{report.currency_sum_pct}%")
    if report.currency_data_quality_flag:
        st.error(f"**Data quality issue, not a risk finding:** {report.currency_data_quality_flag}")
    net_short_currencies = [e for e in report.currency_concentration if e.status == "NET_SHORT"]
    if net_short_currencies:
        for e in net_short_currencies:
            st.info(f"**NET_SHORT:** {e.name} at {e.pct}% - net short this currency, not a concentration breach.")

if report.correlation_clusters:
    st.write("Correlation clusters (>0.85):")
    st.dataframe(pd.DataFrame([asdict(c) for c in report.correlation_clusters]), width="stretch")
else:
    st.caption("No correlation clusters flagged (no price history provided, or none exceeded threshold).")

if report.volatility_signals:
    st.write("Volatility signals (30-day realized vol, QoQ change):")
    st.dataframe(pd.DataFrame([asdict(v) for v in report.volatility_signals]), width="stretch")
else:
    st.caption("No volatility signals available (no price history provided).")

st.subheader("Base severity score (rules-based only, before Claude)")
_severity_banner("Base severity", severity_result.severity, f"score {severity_result.score}")
if severity_result.structural_notes:
    for note in severity_result.structural_notes:
        st.caption(f"Structural note: {note}")

# --- 4. Claude Verification - independent, user-triggered cross-check ---
# This does NOT run automatically - engine/concentration.py stays the system
# of record; this is an on-demand audit, never a dependency of severity
# scoring or the main analysis below.
st.header("4. Claude Verification (independent cross-check)")
st.caption(
    "On-demand audit only - re-derives 3 headline figures from RAW position data "
    "and compares them to the engine's output above. The engine remains the system "
    "of record; this never feeds severity scoring."
)

if st.button("Run Independent Verification"):
    with st.spinner("Calling Claude Sonnet for independent verification..."):
        try:
            verification = verify_portfolio(portfolio, report, DEFAULT_LIMITS)
            st.session_state["verification_result"] = verification
            st.session_state["verified_source_key"] = source_key
        except ClaudeVerificationError as e:
            st.error(f"Verification call failed (analysis above is unaffected): {e}")

verification = st.session_state.get("verification_result")
if verification and st.session_state.get("verified_source_key") == source_key:
    if verification["overall_verdict"] == "ALL_MATCHED":
        st.success("ALL_MATCHED - Claude's independent recomputation agrees with the engine.")
    else:
        st.warning(
            "DISCREPANCY_FOUND - at least one figure didn't match within tolerance. "
            "The engine (engine/concentration.py) remains the system of record; "
            "investigate before trusting Claude's recomputation over it."
        )

    figure_labels = {"top_sector_pct": "Top sector %", "top_issuer_pct": "Top issuer %", "hhi": "HHI"}
    verify_rows = [
        {
            "Figure": figure_labels.get(f["figure"], f["figure"]),
            "Engine value": f["engine_value"],
            "Claude's recomputed value": f["claude_value"],
            "Status": f["status"],
        }
        for f in verification["figures"]
    ]
    st.dataframe(pd.DataFrame(verify_rows), width="stretch")

    for f in verification["figures"]:
        if f["status"] == "MISMATCH":
            st.warning(
                f"**{figure_labels.get(f['figure'], f['figure'])}** - engine: {f['engine_value']}  "
                f"vs. Claude: {f['claude_value']}  (rule applied: {f['rule_applied']})"
            )

    with st.expander("Rules Claude stated it applied, and per-figure notes"):
        for f in verification["figures"]:
            st.markdown(f"- **{figure_labels.get(f['figure'], f['figure'])}**: {f['rule_applied']} — {f['note']}")

    audit = verification["_audit"]
    vc1, vc2, vc3 = st.columns(3)
    vc1.metric("Input tokens", audit["input_tokens"])
    vc2.metric("Output tokens", audit["output_tokens"])
    vc3.metric("Cost (USD)", f"${audit['cost_usd']:.6f}")
else:
    st.info("Click \"Run Independent Verification\" to trigger the one-off Sonnet audit call.")

# --- 5. Claude analysis - the ONE Sonnet call ---
st.header("5. Claude Risk Analysis (Sonnet - one call)")

if st.button("Run Claude Analysis"):
    already_analyzed = (
        st.session_state.get("analysis") is not None
        and st.session_state.get("analyzed_source_key") == source_key
    )
    if already_analyzed:
        st.info("Already analyzed this exact portfolio - reusing the cached result below. No new Sonnet call was made.")
    else:
        with st.spinner("Calling Claude Sonnet..."):
            try:
                analysis = analyze_portfolio(portfolio, report, severity_result, DEFAULT_LIMITS)
                st.session_state["analysis"] = analysis
                st.session_state["analyzed_source_key"] = source_key
                with open(AUDIT_LOG_PATH, "r") as f:
                    st.session_state["claude_audit_entry"] = json.load(f)[-1]
            except ClaudeAnalysisError as e:
                st.error(f"Claude call failed: {e}")

analysis = st.session_state.get("analysis")
if analysis:
    # Three distinct lines, deliberately never merged into one sentence -
    # conflating "Claude agrees with a deterministic score" with "Claude's
    # confidence in its own assessment" wrongly implies Claude is unsure
    # about arithmetic that was never in question.
    _severity_banner("Base severity (rules engine)", severity_result.severity, f"score {severity_result.score}")

    agreement = "agrees, no tier adjustment"
    if severity_result.severity in SEVERITY_ORDER and analysis["severity"] in SEVERITY_ORDER:
        base_rank = SEVERITY_ORDER.index(severity_result.severity)
        claude_rank = SEVERITY_ORDER.index(analysis["severity"])
        if claude_rank != base_rank:
            direction = "escalated" if claude_rank > base_rank else "de-escalated"
            agreement = (
                f"adjusted from {severity_result.severity} ({direction} one tier), "
                f"reason: see Key Drivers / Compounding Signal below"
            )
    _severity_banner("Claude's verdict", analysis["severity"], agreement)

    st.info(
        f"**Claude's confidence in its assessment:** {analysis['confidence_pct']}% - "
        f"{analysis['confidence_rationale']}"
    )

    st.subheader(analysis["headline"])

    st.write("**Key drivers:**")
    for driver in analysis["key_drivers"]:
        st.markdown(f"- {driver}")

    _compounding_signal_box(analysis["severity"], f"**Root cause vs. independent risks:** {analysis['compounding_signal']}")

    st.caption(f"*Data gaps: {analysis['data_gaps']}*")

    with st.expander("Full breaches table, conflicting signals & historical comparison"):
        st.write(f"**Historical comparison:** {analysis['historical_comparison']}")
        st.write("**Conflicting / reinforcing signals:**")
        for signal in analysis["conflicting_signals"]:
            st.markdown(f"- {signal}")
        st.write("**Breaches / warnings (full list Claude reasoned over):**")
        st.dataframe(pd.DataFrame(analysis["breaches"]), width="stretch")
else:
    st.info("Click \"Run Claude Analysis\" above to trigger the single Sonnet call.")

# --- 6. Escalation actions ---
st.header("6. Escalation Actions (design_document.md §10)")

if analysis:
    if st.button("Trigger Escalation"):
        st.session_state["escalation_result"] = escalate(
            portfolio_id=report.portfolio_id,
            severity=analysis["severity"],
            confidence_pct=analysis["confidence_pct"],
            breaches=analysis["breaches"],
            conflicting_signals=analysis["conflicting_signals"],
            headline=analysis["headline"],
            key_drivers=analysis["key_drivers"],
            compounding_signal=analysis["compounding_signal"],
            portfolio=portfolio, report=report,
        )
        st.session_state.pop("email_send_result", None)

    escalation_result = st.session_state.get("escalation_result")
    if escalation_result:
        st.write(f"**Actions taken:** {', '.join(escalation_result['actions_taken'])}")

        if "slack_alert" in escalation_result:
            slack = escalation_result["slack_alert"]
            st.write("**Simulated Slack alert**")
            st.markdown(
                f"- Channel: `{slack['channel']}`\n"
                f"- Urgent: {'Yes' if slack['urgent'] else 'No'}\n"
                f"- Message: {slack['text']}"
            )

        if "jira_ticket" in escalation_result:
            jira = escalation_result["jira_ticket"]
            st.write("**Simulated Jira ticket**")
            st.markdown(
                f"- Ticket: `{jira['ticket_id']}` ({jira['project']})\n"
                f"- Priority: {jira['priority']}\n"
                f"- Summary: {jira['summary']}\n"
                f"- Description: {jira['description']}"
            )

        if "dashboard_flag" in escalation_result:
            flag = escalation_result["dashboard_flag"]
            st.write("**Dashboard flag**")
            st.markdown(
                f"- Portfolio: `{flag['portfolio_id']}`\n"
                f"- Severity: {flag['severity']}\n"
                f"- Confidence: {flag['confidence_pct']}%\n"
                f"- Headline: {flag['headline']}"
            )

        if "escalation_memo" in escalation_result:
            memo = escalation_result["escalation_memo"]
            st.write("**Claude-drafted escalation memo (Haiku)**")
            st.markdown(
                f"- Recipient: {memo['recipient']}\n"
                f"- Subject: {memo['subject']}"
            )
            st.write(memo["body"])

        st.write("**Real email escalation**")
        if not escalation_result.get("email_eligible"):
            st.info(
                f"Email is not an eligible action at {escalation_result['severity']} severity "
                "(HIGH/CRITICAL only) - Slack/Jira/dashboard above still apply per the severity table."
            )
        elif "escalation_email_draft" not in escalation_result:
            st.warning("Email was eligible but couldn't be composed (missing portfolio/report context).")
        else:
            draft = escalation_result["escalation_email_draft"]
            recipient_input = st.text_input(
                "Recipient", value=draft["recipient"] or DEFAULT_ESCALATION_EMAIL_TO,
                key=f"email_recipient_{source_key}",
                help="Multiple recipients: separate with commas, semicolons, and/or whitespace.",
            )
            with st.expander("Preview composed email (not sent)", expanded=True):
                st.markdown(f"**Subject:** {draft['subject']}")
                st.text(draft["body"])

            if st.button("Send Escalation Email"):
                try:
                    send_result = send_email(
                        recipient=recipient_input, subject=draft["subject"], body=draft["body"],
                        portfolio_id=report.portfolio_id, severity=escalation_result["severity"],
                    )
                    status = "partial" if send_result.failed else "sent"
                    st.session_state["email_send_result"] = {
                        "status": status, "succeeded": send_result.succeeded,
                        "failed": send_result.failed,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                except EmailEscalationError as e:
                    st.session_state["email_send_result"] = {
                        "status": "failed", "succeeded": [], "failed": {}, "error": str(e),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }

            email_send_result = st.session_state.get("email_send_result")
            if email_send_result:
                ts = email_send_result["timestamp"]
                if email_send_result["status"] == "sent":
                    st.success(f"Sent to {', '.join(email_send_result['succeeded'])} at {ts}.")
                elif email_send_result["status"] == "partial":
                    st.warning(f"Partial failure at {ts} - some recipients were rejected:")
                    for addr in email_send_result["succeeded"]:
                        st.markdown(f"- **{addr}**: sent")
                    for addr, err in email_send_result["failed"].items():
                        st.markdown(f"- **{addr}**: failed - {err}")
                else:
                    st.error(f"Send failed at {ts}: {email_send_result.get('error', 'unknown error')}")
else:
    st.info("Run the Claude analysis first.")

# --- 7. Token / cost for this run ---
st.header("7. API Efficiency - Token & Cost for This Run")

claude_audit_entry = st.session_state.get("claude_audit_entry")
if claude_audit_entry:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Input tokens", claude_audit_entry.get("input_tokens"))
    c2.metric("Output tokens", claude_audit_entry.get("output_tokens"))
    c3.metric("Cache write", claude_audit_entry.get("cache_creation_input_tokens"))
    c4.metric("Cache read", claude_audit_entry.get("cache_read_input_tokens"))
    c5.metric("Cost (USD)", f"${claude_audit_entry.get('cost_usd', 0):.6f}")
else:
    st.info("Run the Claude analysis first.")

# --- 8. Full audit trail ---
st.header("8. Full Audit Trail")

if os.path.exists(AUDIT_LOG_PATH):
    with open(AUDIT_LOG_PATH, "r") as f:
        log = json.load(f)
    st.dataframe(pd.DataFrame(log), width="stretch")
else:
    st.info("No audit trail yet - run an analysis to create one.")
