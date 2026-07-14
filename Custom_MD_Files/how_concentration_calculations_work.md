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

## The one exception: currency is NET, not absolute

Every rule above - sum absolute value, ignore direction - is deliberately
**reversed** for currency concentration. This section exists because that
reversal was learned the hard way: the first implementation used the same
`abs(market_value)` rule for currency as for issuer/sector, and it produced
a number that cannot exist for a real portfolio.

### The bug this fixed, in real numbers

`port_2026_0501.json` ("Global Diversified Balanced Fund") holds a short
S&P 500 index futures position (`POS-C19`, market value -1.8M USD) as a
hedge, alongside its long USD-denominated holdings, on a 60M USD NAV.

**Before the fix** (absolute value, same rule as issuer/sector):
```
USD exposure = sum of abs(market_value) for every USD position
             = (all the long USD positions) + abs(-1.8M)   <- short ADDS instead of subtracting
             = 106.0% of NAV
```

A fund reporting **106% currency exposure** is not a risk finding - it's
an impossible number. A fund cannot have more currency exposure than it
has money; every dollar of NAV is denominated in exactly one currency (or
split across several), so total currency exposure is bounded at 100% of
NAV by definition. Seeing 106% was the signal that the calculation itself,
not the portfolio, was wrong.

**After the fix** (signed/net value):
```
USD exposure = sum of SIGNED market_value for every USD position, / NAV
             = (all the long USD positions) + (-1.8M)   <- short REDUCES the total
             = 100.0% of NAV
```

100% is exactly right for a fund where every position is USD-denominated:
the short futures hedge genuinely reduces the fund's net sensitivity to USD
moves, so it must subtract, not add.

### Why currency reverses the rule that's correct everywhere else

Issuer/sector/geography/asset-class concentration all answer the same
question: *"how much single-name/sector/region/asset-type risk is riding
on this bucket?"* A short Reliance position and a long Reliance position
are not offsetting - they're two separate exposures to the same
name/event, so both count at full (absolute) magnitude. That reasoning is
sound and stays unchanged.

Currency concentration answers a *different* question: *"how does this
fund's NAV move when this currency moves?"* That's a net sensitivity, and
net sensitivities net. A short USD position genuinely offsets a long USD
position's FX sensitivity - if you're long $100 of US equity and short $30
of USD via a hedge, your fund's real sensitivity to USD moving is $70, not
$130. Because it's a net sensitivity, currency exposures also have a
conservation law that issuer/sector/geography/asset-class do not: they
should sum to ~100% of NAV, since every unit of the fund is denominated in
*something*. `engine/concentration.py` now checks that sum explicitly
(`CURRENCY_SUM_TOLERANCE_PCT_POINTS`, ±1.0pp) and raises a data-quality
flag - not a risk finding - if it's materially off.

One more consequence of going net: the total can be **negative** for a
given currency (net short it), which is classified `NET_SHORT` rather than
forced into the OK/WARNING/BREACH bands built for "too much long exposure
in one currency" - being net short a currency is a different risk shape
entirely, not a diversification concern.

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
