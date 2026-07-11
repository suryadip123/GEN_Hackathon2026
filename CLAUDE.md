# Portfolio Risk & Exposure Platform

Hackathon project: AI-powered portfolio concentration/risk analysis using the
Claude API. Full architecture, data model, prompt design, and risk scoring
spec live in `design_document.md` at repo root — read it before writing code
in a new area, don't re-derive the design from scratch.

## Tech Stack
- Backend: Python + FastAPI
- Quant engine: pandas / numpy
- LLM: Claude Sonnet (analysis/rationale calls), Claude Haiku (lightweight text gen)
- Frontend: Streamlit
- No database — in-memory + local JSON audit log

## Commands
- Run ingestion test: `python ingestion/normalize.py`
- Run app: `streamlit run app.py`
- Install deps: `pip install -r requirements.txt`

## Structure
- `ingestion/` — load, validate, normalize position data (built, tested)
- `engine/` — deterministic concentration math + severity scoring (no Claude calls)
- `claude/` — system prompt, JSON schema, API client wrapper
- `escalation/` — severity → action mapping (Slack/Jira simulation)
- `app.py` — Streamlit dashboard wiring it together
- `data/sample_portfolios/` — synthetic test data

## Hard Constraints (API budget: $40 total, judged on efficiency)
- Exactly ONE Claude call per portfolio analysis — never per-position.
- Use forced JSON output (tool use) matching schema in design_document.md §7.
- Enable prompt caching on the system prompt (limits config is static).
- Log token counts + computed $ cost for every Claude call to the audit trail.
- Quant/concentration math is always deterministic code — never ask Claude to compute numbers, only to reason over pre-computed ones.
