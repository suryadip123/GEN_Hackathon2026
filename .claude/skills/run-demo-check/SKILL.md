---
name: run-demo-check
description: Pre-demo smoke test - runs every sample portfolio in data/sample_portfolios/ through ingestion, concentration, and scoring (free, deterministic), and reports pass/fail per file plus severity/flag summary. Optionally exercises the real Claude call and escalation actions for one portfolio to confirm the live API path works before a demo. Use before a live demo/presentation, after changing engine/ or ingestion/ code, or when asked to "check the demo is working", "smoke test the portfolios", or "verify everything still works".
---

# Run Demo Check

A pre-demo confidence check for this project. Confirms every sample
portfolio in `data/sample_portfolios/` still loads and produces sensible
output after code changes, without needing to manually re-run each stage
by hand or burn API calls on every file.

## When to run this

- Right before a live demo or presentation - catch a broken sample file
  or a regression before a judge sees it, not during.
- After changing anything in `ingestion/`, `engine/concentration.py`, or
  `engine/scoring.py` - confirm existing sample portfolios still produce
  the same category of result (a "clean" fund should still come out
  LOW, a "breach-heavy" fund should still come out HIGH/CRITICAL, etc.).
- When asked to verify the demo works, smoke-test the portfolios, or
  check nothing broke.

## Step 1 - Deterministic pass (free, always run this)

Runs ingestion -> concentration -> scoring for every file in
`data/sample_portfolios/*.json`. No API key needed, no cost, safe to run
as often as you like.

```bash
python3 -c "
import glob, sys
sys.path.insert(0, '.')
from ingestion.normalize import load_portfolio, normalize_portfolio
from engine.concentration import compute_concentration, DEFAULT_LIMITS
from engine.scoring import compute_severity

results = []
for path in sorted(glob.glob('data/sample_portfolios/*.json')):
    try:
        raw = load_portfolio(path)
        p = normalize_portfolio(raw)
        report = compute_concentration(p, DEFAULT_LIMITS)
        sev = compute_severity(report)
        breach_count = sum(
            1 for cat in (report.issuer_concentration, report.sector_concentration,
                          report.geography_concentration, report.asset_class_concentration)
            for e in cat if e.status == 'BREACH'
        )
        results.append((path, 'OK', p.portfolio_id, len(p.positions),
                        len(p.data_quality_flags), sev.severity, sev.score, breach_count))
    except Exception as e:
        results.append((path, 'FAIL', str(e), '', '', '', '', ''))

print(f'{\"file\":40s} {\"status\":6s} {\"portfolio_id\":18s} {\"pos\":4s} {\"flags\":6s} {\"severity\":9s} {\"score\":7s} {\"breaches\"}')
for path, status, pid, npos, nflags, sev_label, score, nbreach in results:
    print(f'{path:40s} {status:6s} {str(pid):18s} {str(npos):4s} {str(nflags):6s} {str(sev_label):9s} {str(score):7s} {nbreach}')

n_fail = sum(1 for r in results if r[1] == 'FAIL')
print(f'\n{len(results)} portfolios checked, {n_fail} failed.')
"
```

Read the output for:
- Any `FAIL` row - the exception message is right there, fix before demoing.
- Data-quality flag counts that don't match what you'd expect for that
  file (e.g. a "clean" portfolio suddenly showing an unexpected flag
  means something upstream changed).
- Severity labels that don't match each file's intended scenario - if
  the breach-heavy fund stops coming out HIGH, or the clean fund stops
  coming out LOW, that's a regression worth investigating before
  assuming it's fine.

## Step 2 - Live Claude + escalation pass (costs money, ask before running)

This makes real Sonnet API calls (and a Haiku call if any portfolio
scores CRITICAL) - **confirm with the user before running this**, since
it spends from the team's API budget. Only worth it right before an
actual demo, or after changing `claude/prompts.py`, `claude/client.py`,
or `escalation/actions.py`.

Run it against **one representative portfolio per scenario type**, not
every file every time - e.g. the breach-heavy one and the edge case, not
all of them if there are many. Requires `ANTHROPIC_API_KEY` set:

```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from ingestion.normalize import load_portfolio, normalize_portfolio
from engine.concentration import compute_concentration, DEFAULT_LIMITS
from engine.scoring import compute_severity
from claude.client import analyze_portfolio, ClaudeAnalysisError
from escalation.actions import escalate

path = 'data/sample_portfolios/REPLACE_ME.json'
raw = load_portfolio(path)
p = normalize_portfolio(raw)
report = compute_concentration(p, DEFAULT_LIMITS)
sev = compute_severity(report)
try:
    analysis = analyze_portfolio(p, report, sev, DEFAULT_LIMITS)
except ClaudeAnalysisError as e:
    print('CLAUDE CALL FAILED:', e)
else:
    print('Claude severity:', analysis['severity'], analysis['confidence_pct'])
    result = escalate(
        portfolio_id=report.portfolio_id, severity=analysis['severity'],
        confidence_pct=analysis['confidence_pct'], breaches=analysis['breaches'],
        conflicting_signals=analysis['conflicting_signals'],
        rationale_summary=analysis['rationale_summary'],
    )
    print('Escalation actions taken:', result['actions_taken'])
"
```

Check `audit_log.json`'s last entries afterward to confirm token counts
and cost were logged correctly for the call(s) just made.

## Don't

- Don't run Step 2 against every sample portfolio reflexively - it costs
  real money per file; pick representative scenarios.
- Don't treat a Step 1 pass as proof the live demo works - it only
  proves the deterministic layer (ingestion/concentration/scoring); the
  Claude and escalation stages need Step 2 at least once before a demo.
- Don't skip re-running this after editing `engine/scoring.py`'s
  constants (weights, saturation points, band boundaries) - those
  changes can silently shift every sample portfolio's severity band.
