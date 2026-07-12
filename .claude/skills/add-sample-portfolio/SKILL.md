---
name: add-sample-portfolio
description: Add a new sample portfolio JSON to data/sample_portfolios/ and validate it end-to-end through ingestion, concentration, and scoring (and optionally a real Claude call) before it's used in the dashboard or demo. Use when asked to "add a sample portfolio", "create a test portfolio", "add a demo scenario", or "add an edge-case portfolio" for this project.
---

# Add a Sample Portfolio

Adds a new portfolio JSON file to `data/sample_portfolios/` and proves it
works through the real pipeline - not just "valid JSON," but actually
produces sensible concentration/severity output with no unintended data
quality flags, before it's ever loaded into `app.py`.

## Schema (from `ingestion/normalize.py`)

Top level: `portfolio_id`, `fund_name`, `fund_type`, `base_currency`,
`nav`, `as_of` (ISO 8601), `positions` (list).

Each position requires: `position_id`, `account_id`, `instrument`,
`asset_class` (one of `Equity`, `Bond`, `Derivative`, `Cash`),
`quantity`, `market_value`, `currency`. Optional but meaningful:
`sector`, `geography`, `issuer`, `counterparty`, `ticker`,
`price_history` (a list of daily prices, same length across positions
that should be correlated - see "Adding price history" below).

Conventions to follow (don't deviate without a reason):
- Short positions: negative `quantity` AND negative `market_value` (see
  any existing `_Short` position for the pattern). The engine takes
  `abs(market_value)` for exposure automatically.
- Cash positions: `asset_class: "Cash"`, `sector: "Cash"`, `issuer: null`.
  Cash is excluded from issuer concentration entirely (not single-name
  risk) but still counts toward sector/asset-class concentration - don't
  let cash silently dominate a sector bucket unless that's the point of
  the scenario.
- `nav` should equal `sum(market_value for all positions)` (net, not
  abs) within 5%, or `ingestion/normalize.py` will raise a
  `nav_mismatch` data-quality flag. If you want that flag intentionally,
  fine - otherwise compute NAV as the exact sum.

## Steps

1. **Decide the scenario's purpose.** A demo needs variety, not just
   more files: e.g. "breach-heavy / HIGH severity," "clean / LOW
   severity, no breaches," or a structural edge case (all-cash, missing
   sector tags, empty portfolio). Know which one you're building before
   picking numbers.

2. **Generate positions programmatically, don't hand-tally percentages.**
   Manually computing "this position is 3.2% of NAV, that sector totals
   24%..." across a dozen positions is error-prone and slow. Instead,
   write a short Python script (see the pattern in this project's own
   history - `port_2026_0501.json` was built this way) that:
   - Defines positions with target percentages of NAV (or fixed dollar
     amounts against a fixed NAV).
   - Calls `engine.concentration.compute_concentration()` directly on
     the draft dict to check every category's status.
   - Iterates the numbers until the result matches the intended scenario
     (e.g. zero BREACH for a "clean" fund, or specific breaches for a
     "stressed" fund) - don't guess and hope, verify computationally.

3. **Validate through the full deterministic pipeline:**
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, '.')
   from ingestion.normalize import load_portfolio, normalize_portfolio
   from engine.concentration import compute_concentration, DEFAULT_LIMITS
   from engine.scoring import compute_severity
   raw = load_portfolio('data/sample_portfolios/YOUR_FILE.json')
   p = normalize_portfolio(raw)
   print('flags:', [(f.position_id, f.issue) for f in p.data_quality_flags])
   report = compute_concentration(p, DEFAULT_LIMITS)
   sev = compute_severity(report)
   print('severity:', sev.severity, sev.score, sev.structural_notes)
   "
   ```
   Confirm the data-quality flags list only contains what you intended
   (nothing accidental like a stray `nav_mismatch`), and the severity/
   structural notes match the scenario's purpose.

4. **Adding price history (for correlation clusters / volatility
   signals):** give 2+ thematically-related positions a shared
   synthetic price series (a common random-walk factor + small
   idiosyncratic noise per name) so `compute_correlation_clusters`
   finds a real >0.85 cluster, and scale one name's *returns* in the
   most recent 30 observations by ~1.3-1.6x to produce a realistic
   `compute_volatility_signals` QoQ increase. All `price_history` arrays
   feeding correlation must be the same length. Check the actual
   resulting correlation matrix and vol numbers with pandas before
   committing the file - don't assume the parameters will land right;
   tune and re-check like any other numeric generation.

5. **Run one real Claude call against it** (costs a few cents) to
   confirm the full pipeline works end to end and the reasoning is
   sensible for the scenario - especially for edge cases, check Claude
   doesn't invent risk that isn't there (e.g. an all-cash fund should
   stay LOW, not get escalated for no cited reason):
   ```bash
   python3 -c "
   import sys; sys.path.insert(0, '.')
   from ingestion.normalize import load_portfolio, normalize_portfolio
   from engine.concentration import compute_concentration, DEFAULT_LIMITS
   from engine.scoring import compute_severity
   from claude.client import analyze_portfolio
   raw = load_portfolio('data/sample_portfolios/YOUR_FILE.json')
   p = normalize_portfolio(raw)
   report = compute_concentration(p, DEFAULT_LIMITS)
   sev = compute_severity(report)
   analysis = analyze_portfolio(p, report, sev, DEFAULT_LIMITS)
   print(analysis['severity'], analysis['confidence_pct'])
   print(analysis['rationale_summary'])
   "
   ```
   Requires `ANTHROPIC_API_KEY` in the environment.

6. **Wire it into the demo.** `app.py`'s sample-portfolio dropdown globs
   `data/sample_portfolios/*.json` automatically - no code change
   needed. If the new file is meant to be a headline demo scenario,
   mention it by name in `README.md`'s rubric-evaluation section
   alongside the other sample portfolios.

## Don't

- Don't hand-edit percentages without re-running the validation script -
  a single position size change shifts every category's percentages.
- Don't add a `price_history` array of a different length than the
  others you're correlating it with - `compute_correlation_clusters`
  builds a `pd.DataFrame` from all of them and needs equal lengths.
- Don't skip the real Claude call for a new demo-facing scenario - a
  file that only "looks right" in the deterministic layer can still
  produce a confusing or wrong-feeling narrative once Claude reasons
  over it, and that's what judges will actually see live.
