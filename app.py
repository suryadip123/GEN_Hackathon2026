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

import pandas as pd
import streamlit as st

from ingestion.normalize import load_portfolio, normalize_portfolio
from engine.concentration import compute_concentration, DEFAULT_LIMITS
from engine.scoring import compute_severity
from claude.client import AUDIT_LOG_PATH, ClaudeAnalysisError, analyze_portfolio
from escalation.actions import escalate

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
    for key in ("analysis", "analyzed_source_key", "claude_audit_entry", "escalation_result"):
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

tab1, tab2, tab3, tab4 = st.tabs(["Issuer", "Sector", "Geography", "Asset Class"])
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
with tab4:
    st.dataframe(_entries_df(report.asset_class_concentration), width="stretch")

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

# --- 4. Claude analysis - the ONE Sonnet call ---
st.header("4. Claude Risk Analysis (Sonnet - one call)")

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
    col1, col2 = st.columns(2)
    with col1:
        _severity_banner("Base severity (rules only)", severity_result.severity, f"score {severity_result.score}")
    with col2:
        adjustment = "n/a"
        if severity_result.severity in SEVERITY_ORDER and analysis["severity"] in SEVERITY_ORDER:
            base_rank = SEVERITY_ORDER.index(severity_result.severity)
            claude_rank = SEVERITY_ORDER.index(analysis["severity"])
            if claude_rank > base_rank:
                adjustment = "ESCALATED by Claude"
            elif claude_rank < base_rank:
                adjustment = "DE-ESCALATED by Claude"
            else:
                adjustment = "unchanged"
        _severity_banner("Claude final verdict", analysis["severity"], f"{adjustment}, confidence {analysis['confidence_pct']}%")

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

# --- 5. Escalation actions ---
st.header("5. Escalation Actions (design_document.md §10)")

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
        )

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
else:
    st.info("Run the Claude analysis first.")

# --- 6. Token / cost for this run ---
st.header("6. API Efficiency - Token & Cost for This Run")

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

# --- 7. Full audit trail ---
st.header("7. Full Audit Trail")

if os.path.exists(AUDIT_LOG_PATH):
    with open(AUDIT_LOG_PATH, "r") as f:
        log = json.load(f)
    st.dataframe(pd.DataFrame(log), width="stretch")
else:
    st.info("No audit trail yet - run an analysis to create one.")
