# AI-Powered Portfolio Exposure & Risk Escalation Platform
### Design Document — Hackathon Submission

---

## 1. Problem Restatement

Portfolio managers and risk desks manually review thousands of positions across accounts/funds for concentration breaches — single-issuer, sector, geography, asset class — plus correlation/volatility risk. This is periodic and reactive. We are building a system that:

1. **Ingests** portfolio holdings (equities, bonds, derivatives, cash) across multiple accounts/funds, real-time or batch.
2. **Analyzes** exposure against configurable limits using Claude, explaining any breach.
3. **Scores** severity (LOW / MEDIUM / HIGH / CRITICAL) with a confidence value and rationale.
4. **Escalates** automatically — at least two downstream actions per breach — with full audit trail.

## 2. Rubric-to-Architecture Mapping

| Criterion | Weight | Where it's addressed |
|---|---|---|
| AI Exposure Analysis & Rationale | 25% | §6 Claude Reasoning Layer, §7 Prompt Design |
| API Efficiency | 25% | §9 API Efficiency Strategy |
| Risk Model Quality | 20% | §8 Risk Severity Scoring |
| Automation & Escalation | 15% | §10 Escalation Engine |
| Working Demo | — (end-to-end) | §12 Demo Script |
| Docs/README | 10% | This document + README |

*(Note: rubric percentages as given sum to slightly over/under 100% in the original listing — treating as provided, not recalculated.)*

---

## 3. System Architecture

```
                ┌─────────────────────────┐
 Sample Data →  │  1. Ingestion Layer     │  (Task 1)
 (CSV/JSON,     │  parse, validate,       │
  batch/stream) │  normalize positions    │
                └───────────┬─────────────┘
                            ↓
                ┌─────────────────────────┐
                │  2. Quant Engine        │  deterministic math
                │  (pandas) — concentration│  NO Claude call here
                │  ratios, HHI, vol proxy  │
                └───────────┬─────────────┘
                            ↓
                ┌─────────────────────────┐
                │  3. Claude Reasoning    │  (Task 2)
                │  Layer — ONE structured │  Sonnet 4.6/5
                │  call: breach rationale,│
                │  conflicting signals    │
                └───────────┬─────────────┘
                            ↓
                ┌─────────────────────────┐
                │  4. Risk Scoring        │  (Task 3)
                │  rules + Claude verdict │
                │  LOW/MED/HIGH/CRITICAL  │
                │  + confidence score     │
                └───────────┬─────────────┘
                            ↓
                ┌─────────────────────────┐
                │  5. Escalation Engine   │  (Task 4)
                │  severity → actions     │
                │  (Slack, ticket, log)   │
                └───────────┬─────────────┘
                            ↓
                ┌─────────────────────────┐
                │  6. Dashboard + Audit   │
                │  Trail (Streamlit)      │
                └─────────────────────────┘
```

**Design principle:** Claude never does arithmetic. Code computes every ratio deterministically (auditable, instant, free); Claude receives the *computed* numbers and is asked only to reason, explain, and judge — this is both an accuracy safeguard and a token-efficiency decision (small structured input, no need to re-derive numbers).

---

## 4. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python + FastAPI | fast to build, clean route separation |
| Quant engine | pandas / numpy | deterministic concentration math |
| LLM | Claude Sonnet 4.6/5 (analysis), Claude Haiku 4.5 (lightweight text tasks) | balance of reasoning quality and cost |
| Structured output | Anthropic tool-use / forced JSON schema | single call, no re-parsing |
| Prompt caching | system prompt (limits, scoring rubric) cached | ~90% input cost cut on repeated context |
| Frontend | Streamlit | fastest path to a working live dashboard |
| Alerts | Simulated Slack webhook + mock Jira ticket object + dashboard log entry | proves escalation without needing live infra |
| Data store | In-memory / local JSON audit log | sufficient for a demo; no DB dependency |

---

## 5. Data Model (Task 1 — Ingestion)

Assumed schema (to be reconciled against actual sample data on arrival):

**Position-level record:**
```json
{
  "position_id": "POS-00123",
  "account_id": "ACC-Alpha-01",
  "fund_id": "PORT-2026-0442",
  "instrument": "Reliance Industries Ltd",
  "ticker": "RELIANCE.NS",
  "asset_class": "Equity",        // Equity | Bond | Derivative | Cash
  "quantity": 15000,
  "market_value": 4900000,
  "currency": "INR",
  "sector": "Energy",
  "geography": "India",
  "issuer": "Reliance Industries Ltd",
  "counterparty": null,            // relevant for derivatives
  "price_history": [ ... ],        // optional: for volatility/correlation calc
  "as_of": "2026-07-11T09:15:32+05:30"
}
```

**Portfolio-level wrapper:**
```json
{
  "portfolio_id": "PORT-2026-0442",
  "fund_name": "Alpha Growth Opportunities Fund",
  "fund_type": "Multi-Asset – Long Only",
  "base_currency": "INR",
  "nav": 50000000,
  "positions": [ ... ]
}
```

**Ingestion layer responsibilities:**
- Accept batch (CSV/JSON upload) and simulate "real-time feed" via a replay/streaming mode over the same file for demo purposes.
- Validate: required fields present, market values numeric, NAV consistency (sum of position values ≈ NAV within tolerance).
- Normalize: currency tagging, asset-class enum coercion, missing-sector flag.
- Explicitly tag rows with data-quality issues (missing sector, zero value, negative quantity for shorts) rather than silently dropping them — these become edge cases handled downstream, not swept away.

---

## 6. Quant Engine — Deterministic Metrics (pre-Claude)

Computed in code, per portfolio:

- **Issuer concentration**: `issuer_market_value / NAV` for every issuer
- **Sector concentration**: grouped `market_value / NAV` by sector
- **Geography concentration**: grouped `market_value / NAV` by geography
- **Asset class concentration**: grouped `market_value / NAV` by asset class
- **Herfindahl-Hirschman Index (HHI)**: overall diversification score across positions
- **Correlation clusters**: pairwise rolling correlation (if price history available) → flag clusters >0.85
- **Volatility proxy**: rolling realized volatility per holding, QoQ delta

Each metric is compared against a **limits config** (JSON, user-editable):
```json
{
  "single_issuer_limit_pct": 8.0,
  "sector_limit_pct": 25.0,
  "geography_limit_pct": 70.0,
  "asset_class_limit_pct": 60.0,
  "correlation_threshold": 0.85,
  "warning_buffer_pct": 3.0
}
```
`warning_buffer_pct` creates the WARNING band (e.g. sector at 22.4% vs 25% limit = "approaching threshold") purely in code — no Claude call needed for this classification tier.

---

## 7. Claude Reasoning Layer — Prompt Design (Task 2)

**One call per portfolio analysis.** Input = computed metrics + limits + flags, not raw positions (keeps input small, deterministic, cache-friendly).

**System prompt (cached across calls):**
```
You are a portfolio risk analyst assistant. You will receive pre-computed
concentration metrics for a portfolio, along with configured limits and any
flagged data-quality issues. Do not recompute or second-guess the numeric
values provided — treat them as ground truth.

Your job:
1. Identify which metrics breach or approach their limits.
2. Explain WHY this matters in plain language, especially where signals
   interact (e.g. a position within its individual limit but concentrated
   in a stressed, correlated sector).
3. Note any conflicting or reinforcing signals across categories.
4. If historical incident data is provided, compare current conditions
   against it; if none is provided, state that no historical comparison
   is available rather than inferring one.
5. Return ONLY the structured JSON matching the provided schema. No prose
   outside the JSON.
```

**User message (per-call, variable):**
```json
{
  "portfolio_id": "PORT-2026-0442",
  "fund_name": "Alpha Growth Opportunities Fund",
  "nav": 50000000,
  "metrics": {
    "issuer_concentration": [{"issuer": "Reliance Industries Ltd", "pct": 9.8, "limit": 8.0, "status": "BREACH"}],
    "sector_concentration": [{"sector": "Energy", "pct": 22.4, "limit": 25.0, "status": "WARNING"}],
    "geography_concentration": [{"geography": "India", "pct": 61.0, "limit": 70.0, "status": "OK"}],
    "correlation_clusters": [{"holdings": 3, "min_corr": 0.85, "status": "FLAGGED"}],
    "volatility_signals": [{"issuer": "Reliance Industries Ltd", "vol_change_qoq_pct": 40}]
  },
  "data_quality_flags": [],
  "historical_incidents": []
}
```

**Forced JSON output schema (via tool use):**
```json
{
  "breaches": [
    {"category": "issuer", "name": "Reliance Industries Ltd", "status": "BREACH", "detail": "..."}
  ],
  "conflicting_signals": ["..."],
  "historical_comparison": "none available | <comparison text>",
  "severity": "HIGH",
  "confidence_pct": 91,
  "rationale_summary": "...",
  "estimated_review_minutes": 15
}
```

This structured schema is what feeds §8 and §10 directly — no second Claude call to reformat.

---

## 8. Risk Severity Scoring (Task 3)

**Hybrid model** — deterministic base score, Claude-informed adjustment:

1. **Base score (code):** weighted combination of:
   - max single-issuer breach magnitude
   - max sector breach magnitude
   - HHI level
   - count of correlation flags
2. **Claude adjustment:** Claude may raise/lower the tier by one level *only* when it cites a specific conflicting-signal reason (e.g. simultaneous sector + issuer stress + rising volatility) — the reason is logged alongside the adjustment, never silent.
3. **Final severity bands:** LOW / MEDIUM / HIGH / CRITICAL, each with a numeric score range, so the mapping is auditable rather than purely "Claude said so."

**Edge cases explicitly handled:**
- Empty portfolio → no analysis, log only
- Single-asset / all-cash portfolio → concentration metrics trivially at 100%, suppress false BREACH noise (flag as "structural, not a limit event")
- Missing sector/geography tags → position excluded from that specific concentration calc, flagged as data-quality issue, not silently zeroed
- Short positions (negative quantity) → concentration computed on absolute exposure, direction noted separately
- Conflicting signals (individually fine, collectively risky) → this is the deliberate "judgment" showcase for the rationale criterion

---

## 9. API Efficiency Strategy (25% weight — high priority)

- **One Claude call per portfolio per analysis run** — never per-position.
- **Prompt caching** on the system prompt + limits config (static across calls in a session) — cache write once, cache-hit reads at ~10% of input cost thereafter.
- **Forced structured JSON output** (tool use) — eliminates a second "please reformat as JSON" call.
- **Model routing:** Sonnet 4.6/5 for the analysis/rationale call (needs reasoning quality); Haiku 4.5 for any secondary lightweight text generation (e.g. drafting the Slack alert copy from already-structured data) — cheap task, cheap model.
- **No redundant re-analysis:** if underlying positions haven't changed since the last run, serve cached last result rather than re-calling.
- **Token budget logging:** every call logs input/output token counts and computed $ cost to the audit trail — this makes cost-consciousness *visible* to judges, not just true in theory.
- Batch API is not used for the live demo (needs real-time response) but is the documented approach for any offline bulk testing against multiple sample portfolios during development.

---

## 10. Escalation Engine (Task 4)

Severity → action mapping (minimum 2 actions per breach, per spec):

| Severity | Actions Triggered |
|---|---|
| LOW | Log to audit trail only |
| MEDIUM | Slack alert to desk channel + dashboard flag |
| HIGH | Slack alert + auto-created ticket (mock Jira object) + dashboard flag |
| CRITICAL | Slack alert (urgent) + ticket + dashboard flag + Claude-drafted escalation memo for risk committee |

All external actions (Slack, Jira) are **simulated objects** for the demo — printed/logged with realistic payload shape, not requiring live credentials, unless you have a real webhook ready. Every triggered action is written to an **audit trail** (timestamp, portfolio ID, severity, actions taken, Claude confidence) satisfying the "complete audit trail" requirement in the brief.

---

## 11. Demo Script (Working Demo criterion)

1. Load sample portfolio file → show ingestion + normalization output.
2. Show computed concentration metrics table (deterministic, instant).
3. Trigger single Claude call → show returned structured JSON live.
4. Dashboard renders: breach cards, severity verdict, confidence, rationale.
5. Show escalation actions firing (Slack payload, ticket object, audit log entry appearing).
6. Show token/cost counter for that call, to visibly demonstrate API efficiency.
7. (Stretch) Run a second, deliberately edge-case portfolio (all-cash or missing-sector) to show robustness.

---

## 12. Suggested Folder Structure

```
portfolio-risk-app/
├── data/
│   └── sample_portfolios/
├── ingestion/
│   └── normalize.py
├── engine/
│   ├── concentration.py      # deterministic metrics
│   └── scoring.py            # severity model
├── claude/
│   ├── prompts.py            # system + schema definitions
│   └── client.py             # API call wrapper w/ caching, token logging
├── escalation/
│   └── actions.py            # Slack/Jira/dashboard simulation
├── app.py                    # Streamlit dashboard
├── audit_log.json
├── requirements.txt
└── README.md
```

---

## 13. Open Items Pending Real Sample Data

- Confirm actual field names/format once sample data file is provided.
- Confirm whether limits/thresholds are supplied in the data or need to be defined by us.
- Confirm whether price history / correlation data is provided, or correlation must be simplified/omitted for the demo.
- Confirm whether historical incident data exists, or that field defaults to "none available" (recommended default per §7).
