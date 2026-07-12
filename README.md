# Portfolio Risk & Exposure Platform

An AI-powered portfolio concentration/risk analysis tool. It ingests
portfolio holdings, computes concentration and diversification metrics
deterministically, asks Claude to reason over those pre-computed numbers
exactly once per analysis, scores severity, and fires simulated
escalation actions (Slack, Jira, dashboard) - with every step written to
a JSON audit trail.

Full design rationale, data model, prompt design, and scoring spec live
in [`design_document.md`](design_document.md). This README is the
practical entry point: what it does, how the four tasks map to the
architecture, and how to run it.

## What it does

1. **Ingests** a portfolio (JSON) - validates required fields, flags data
   quality issues (missing sector tags, short positions, stale holdings)
   without silently dropping anything.
2. **Computes concentration** - issuer, sector, geography, and
   asset-class exposure as a % of NAV, an HHI diversification score, and
   correlation clusters (if price history is available) - all in plain
   code, no LLM call.
3. **Analyzes with Claude** (Sonnet) - exactly one structured API call per
   analysis. Claude receives the computed metrics, the limits, and a
   rules-based base severity score, and returns breach reasoning,
   conflicting/compounding signals, a severity verdict (which may adjust
   the base score by exactly one tier, with a cited reason), and a
   confidence value.
4. **Escalates** - maps Claude's final severity to actions (log only /
   Slack / Slack + Jira / Slack + Jira + a Claude-Haiku-drafted memo),
   simulated as structured objects rather than live webhooks.
5. **Audits** - every Claude call (Sonnet analysis, Haiku memo drafts,
   API errors) and every escalation logs an entry to `audit_log.json`:
   timestamp, portfolio ID, severity, actions taken, confidence, and for
   Claude calls, token counts and computed $ cost.

## The 4 tasks, mapped to the code

| Task | What it is | Where it lives |
|---|---|---|
| 1. Ingestion | Parse, validate, normalize raw position data; flag data-quality issues rather than dropping rows | [`ingestion/normalize.py`](ingestion/normalize.py) |
| 2. Quant Engine | Deterministic concentration math (issuer/sector/geography/asset-class %, HHI, correlation clusters) - **no Claude call** | [`engine/concentration.py`](engine/concentration.py) |
| 3. Claude Reasoning + Scoring | The one Sonnet call (prompt + forced-JSON schema + client wrapper) and the rules-based base severity score it starts from | [`claude/prompts.py`](claude/prompts.py), [`claude/client.py`](claude/client.py), [`engine/scoring.py`](engine/scoring.py) |
| 4. Escalation | Severity → action mapping, simulated Slack/Jira, Haiku-drafted escalation memo for CRITICAL | [`escalation/actions.py`](escalation/actions.py) |
| Dashboard | Streamlit UI wiring all four tasks together end to end | [`app.py`](app.py) |

This mirrors the pipeline in `design_document.md` §3 (System
Architecture): ingestion → quant engine → Claude reasoning → risk
scoring → escalation → dashboard/audit trail, each stage feeding the
next in one direction, with only one Claude call in the whole chain.

## The core design principle: deterministic math, Claude only reasons

**Claude never computes a number.** Every percentage, limit comparison,
HHI value, and severity score is plain Python/pandas arithmetic,
computed before Claude is ever called. Claude's one call per portfolio
receives that finished table of numbers and is asked only to explain,
weigh conflicting signals, and render a judgment call on severity - never
to re-derive or second-guess the math.

This is both an accuracy safeguard (LLMs are unreliable at arithmetic,
code isn't) and the basis of the API-efficiency story: the input to
Claude is a small, structured JSON payload of pre-computed metrics, not
raw position-level data, so one call is enough regardless of portfolio
size. See [`Custom_MD_Files/how_concentration_calculations_work.md`](Custom_MD_Files/how_concentration_calculations_work.md)
for the full worked-through arithmetic behind the concentration engine,
using the sample portfolio's real numbers.

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Anthropic API key (never commit this - export it in your shell)
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. Run the dashboard
streamlit run app.py
```

Opens at `http://localhost:8501`. Load the bundled sample portfolio (or
upload your own JSON in the same shape) and step through the dashboard's
7 sections in order - each one is a stage of the pipeline above.

To exercise a single stage directly instead of the full dashboard:

```bash
python ingestion/normalize.py      # ingestion only
python engine/concentration.py     # + concentration metrics
python engine/scoring.py           # + rules-based base severity
python claude/client.py            # + the one Sonnet call (uses ANTHROPIC_API_KEY)
python escalation/actions.py       # + escalation actions and audit trail
```

Each of these prints its output and, from `claude/client.py` onward,
appends to `audit_log.json`.

## How to evaluate this against the rubric

Official weights (Wissen Technology Hackathon 2026 problem statement),
mapped to where each is addressed - `design_document.md` §2 has the full
table:

- **AI Exposure Analysis & Rationale (25%)** - run the dashboard, click
  "Run Claude Analysis," and read the rationale/conflicting-signals
  output against the raw breach table above it (dashboard §4). Includes
  the volatility-context signal (`engine/concentration.py`'s QoQ realized
  volatility proxy) feeding into Claude's reasoning, not just concentration
  ratios.
- **Working Demo (25%)** - `design_document.md` §11 is the exact demo
  script the dashboard was built to satisfy, section by section. Three
  sample portfolios are selectable from the dashboard's dropdown (a
  breach-heavy fund, a clean/diversified fund, and a structural edge
  case) to show the system isn't a one-scenario show.
- **Automation & Escalation (20%)** - `escalation/actions.py`, exercised
  live via dashboard §5; `audit_log.json` (dashboard §7) is the complete,
  append-only audit trail across every stage.
- **Risk Model Quality (15%)** - `engine/scoring.py`'s base score and
  band boundaries are named constants, not magic numbers; compare the
  base severity banner against Claude's adjusted verdict on the
  dashboard to see the one-tier, cited-reason adjustment in action.
  Edge cases (empty portfolio, all-cash, missing sector/geography tags)
  are handled explicitly, not silently dropped - see the edge-case
  sample portfolio and `engine/scoring.py`'s structural-suppression logic.
- **API Efficiency (10%)** - the token/cost panel (dashboard §6) shows
  the actual input/output/cache tokens and $ cost for that one call; the
  system prompt and limits config are cached (`cache_creation_input_tokens`
  vs `cache_read_input_tokens` in `audit_log.json` shows the cache
  actually being hit across calls). Worth doing well, but it's the
  lowest-weighted criterion - don't over-invest here at the expense of
  the demo or escalation quality.
- **Docs/README (5%)** - this file plus `design_document.md`.
