"""
Temporary diagnostic - NOT part of the app. Bisects further: tests whether
merely HAVING a DataFrame in scope crashes, vs. specifically rendering one
via a table widget. Delete this file once the upload crash is resolved.
"""
import json
from dataclasses import asdict

import pandas as pd
import streamlit as st

from ingestion.normalize import normalize_portfolio
from engine.concentration import compute_concentration, DEFAULT_LIMITS
from engine.scoring import compute_severity

st.title("Upload Pipeline Diagnostic v3")

uploaded = st.file_uploader("Upload a JSON file", type="json")
if uploaded is not None:
    uploaded.seek(0)
    raw = json.load(uploaded)
    st.write("Stage 1 OK")

    portfolio = normalize_portfolio(raw)
    st.write("Stage 2 OK - positions:", len(portfolio.positions))

    report = compute_concentration(portfolio, DEFAULT_LIMITS)
    st.write("Stage 3 OK - HHI:", report.hhi)

    severity_result = compute_severity(report)
    st.write("Stage 4 OK - severity:", severity_result.severity)

    entries_as_dicts = [asdict(e) for e in report.issuer_concentration]
    st.write("Stage 5a OK - plain list of dicts built, len:", len(entries_as_dicts))

    st.write("Stage 5b - about to write the raw list via st.write (no DataFrame at all)...")
    st.write(entries_as_dicts)
    st.write("Stage 5b OK - plain list rendered via st.write")

    issuer_df = pd.DataFrame(entries_as_dicts)
    st.write("Stage 6 OK - DataFrame constructed, shape:", issuer_df.shape)

    st.write("Stage 7 - indexing into the DataFrame (no rendering)...")
    first_row = dict(issuer_df.iloc[0])
    st.write("Stage 7 OK - first row:", first_row)

    st.write("Stage 8 - trying st.table()...")
    st.table(issuer_df)
    st.write("Stage 8 OK")

    st.write("Stage 9 - trying st.dataframe()...")
    st.dataframe(issuer_df, width="stretch")
    st.write("Stage 9 OK")

    st.success("ALL STAGES COMPLETED")
