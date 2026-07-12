---
name: add-concentration-rule
description: Add a new concentration limit/rule (e.g. a new grouping dimension like counterparty or currency exposure) to the deterministic risk engine, wired end-to-end through engine/concentration.py, engine/scoring.py, the Claude prompt/schema, and design_document.md. Use when asked to "add a new limit", "add a concentration rule", "track counterparty exposure", "add a new risk category", or similar extensions to the concentration engine for this project.
---

# Add a Concentration Rule

Adds a new concentration dimension to the deterministic engine and wires it
through every downstream layer that needs to know about it - so a new rule
never silently stops at `engine/concentration.py` while Claude, the severity
score, and the design doc stay unaware it exists.

## Why this needs a checklist

This project deliberately keeps quant math (`engine/`) and LLM reasoning
(`claude/`) in separate layers with a single, schema-constrained hop between
them (see `design_document.md` §6-9 and `CLAUDE.md`'s hard constraints). That
separation means a new limit touches several files that don't obviously
depend on each other in the code, but do depend on each other conceptually.
Missing one leaves the system in a state where, for example, the engine
computes a breach but Claude never sees it, or Claude's schema has an enum
value nothing produces.

## Steps

1. **Decide the grouping dimension and threshold.** What field on a position
   groups this rule (e.g. `counterparty`, `currency`)? Is a breach of this
   dimension actually a distinct risk, or does it overlap with an existing
   one (issuer, sector, geography, asset class)? Pick a limit percentage and
   write down the reasoning the same way `DEFAULT_LIMITS` in
   `engine/concentration.py` documents its existing thresholds.

2. **Add the limit constant and grouping logic** in
   `engine/concentration.py`:
   - Add the new key to `DEFAULT_LIMITS`.
   - Call `_group_and_compute()` with the new grouping column, same pattern
     as `sector_concentration` / `geography_concentration`. Decide whether
     missing tags should be excluded-and-reported (like sector/geography) or
     excluded entirely (like cash for issuer) - state which and why.
   - Add the new list field to the `ConcentrationReport` dataclass and
     return it from `compute_concentration()`.

3. **Decide if it feeds the severity score** in `engine/scoring.py`. If yes,
   add a new weight to `WEIGHTS` and rebalance the others so they still sum
   to 1.0 (there's an `assert` enforcing this - it will catch you if you
   forget), add a saturation constant analogous to
   `ISSUER_BREACH_SATURATION_PCT`, and fold it into the composite score in
   `compute_severity()`. If it's informational only (like volatility
   signals), skip this step and say so explicitly - don't silently leave it
   half-wired.

4. **Wire it into the Claude layer:**
   - `claude/client.py`'s `build_user_message()` - add the new metric list to
     the `metrics` dict sent to Claude, same shape as the existing
     `_entry_to_dict()` conversions.
   - `claude/prompts.py`'s `TOOL_SCHEMA` - add the new category name to the
     `breaches[].category` enum so Claude's forced-JSON output can actually
     reference it. If you skip this, Claude has no valid way to report a
     breach in the new dimension even though it can see the data.
   - Only make ONE Claude call still happen per analysis (`CLAUDE.md`'s hard
     constraint) - you're extending the existing single call's payload and
     schema, not adding a second call.

5. **Update `design_document.md`** (§6 metrics, §7 schema, §8 scoring as
   applicable) so the design doc stays the source of truth rather than
   drifting from the code.

6. **Add a tab in `app.py`.** Section 3 of the dashboard
   (`tab1, tab2, tab3, tab4 = st.tabs([...])`, around line 133) hardcodes one
   tab per concentration category - a new rule computed by the engine will
   NOT appear in the GUI until you add a matching tab and
   `st.dataframe(_entries_df(report.<new_field>), ...)` block, following the
   existing tab pattern exactly. Easy to forget because the engine and
   scoring layers work fine without it - the gap only shows up when someone
   actually looks at the dashboard.

7. **Validate with the existing sample portfolios** - run `run-demo-check`'s
   Step 1 (free, deterministic) against all files in
   `data/sample_portfolios/` and confirm: no `FAIL` rows, and severities for
   existing scenarios haven't shifted in a way you didn't intend (adding a
   new weighted component always changes every portfolio's composite score
   somewhat - the question is whether the shift keeps each scenario in its
   intended band). Then run `streamlit run app.py` and click through each
   sample portfolio in the dropdown to confirm the new tab renders correctly
   and no other section breaks.

8. **Consider a new sample portfolio** (via `add-sample-portfolio`) that
   specifically exercises the new rule with a real brea