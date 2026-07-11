# How Concentration Calculations Work

This explains the data flow and math behind `engine/concentration.py`,
using the actual sample portfolio (`port_2026_0442.json`) as a worked example.

## The Pipeline

```
Raw position data (JSON)
        |
        v
Group by dimension (issuer, sector, geography, asset class)
        |
        v
Sum abs(market_value) per group
        |
        v
Divide by portfolio NAV -> percentage
        |
        v
Compare against configured limit -> OK / WARNING / BREACH
```

This entire chain is plain arithmetic, done in code. Claude is not called
at any point in this pipeline - it only enters afterward, once this table
of numbers already exists.

## Step by step, with real numbers

### Step 1 - Raw data

Each position in the JSON has fields like `market_value`, `sector`,
`issuer`, `geography`, `asset_class`. For a sector concentration
calculation, only `sector` and `market_value` matter.

### Step 2 - Group by dimension

For sector concentration, every position is bucketed by its `sector` field:

```
Energy:     Reliance (4.9M), ONGC (3.4M), Adani Green (3.3M)
Technology: Infosys (3.9M), Apple (6.5M)
Government: UST10Y (5.0M), IN10Y (4.0M)
...
```

### Step 3 - Sum absolute value per group

```
Energy total = 4.9M + 3.4M + 3.3M = 11.6M
```

"Absolute" matters because of the short Nifty futures position
(POS-0009, market value -2.5M). Its sector is "Index," not Energy, but the
same rule applies everywhere: a short's magnitude counts toward exposure -
it is not allowed to net against a long position elsewhere and cancel out.
A short and a long are not offsetting risks; they are two separate
exposures, so both are counted at full (absolute) size.

### Step 4 - Divide by NAV

Portfolio NAV is 39.4M.

```
Energy % = 11.6M / 39.4M x 100 = 29.44%
```

This matches the number produced by `engine/concentration.py`.

### Step 5 - Compare against the limit

The limits config sets Energy's sector limit at 25%. Since 29.44% > 25%,
it is classified `BREACH`.

If a value lands between the limit and the limit minus the configured
`warning_buffer_pct`, it is classified `WARNING` instead of `OK`. That is
how Government ended up `WARNING` at 22.84% - just under its 25% limit,
but inside the buffer zone.

## Why absolute value, and why divide by NAV

- **Absolute value (gross exposure):** concentration risk is about how
  much of the portfolio is riding on one name/sector/region, regardless of
  direction. A short position carries the same single-name risk as a long
  one of equal size, so both count at full magnitude rather than netting
  against each other.
- **Dividing by NAV:** NAV is the portfolio's total value, so `group value / NAV`
  answers "what fraction of the whole portfolio depends on this?" - which
  is exactly what a concentration limit is meant to police.

## Where Claude comes in (and where it doesn't)

Claude never recomputes or touches these percentages. It receives the
finished table (issuer/sector/geography/asset-class percentages, HHI,
correlation flags, all already classified OK/WARNING/BREACH) and is asked
only to:

- Explain *why* a breach matters in plain language
- Spot patterns across rows a rules engine alone would not narrate - e.g.
  "Energy is breached AND Technology is breached AND there is a
  correlation flag" is a compound signal worth flagging together, not
  three unrelated bullet points

Keeping the math entirely in code (deterministic, instant, free) and
reserving Claude for judgment on a small, already-summarized table is a
deliberate design choice - it keeps the numbers auditable and keeps the
per-analysis API cost small and predictable.
