"""
Task 2 - Deterministic Concentration & Risk Metrics Engine

Computes issuer / sector / geography / asset-class concentration, HHI, and
(where price history is available) correlation clusters and QoQ realized
volatility signals — all in code, no Claude call. Claude only ever sees
the numbers this module produces.

Design principles carried over from ingestion/normalize.py:
- Exposure is computed on absolute market value (shorts count as exposure,
  not an offset), with direction preserved separately per position. This
  is deliberate for issuer/sector/geography/asset_class: a short Reliance
  position still carries real single-name Reliance risk and must not net
  against a long Reliance position elsewhere in the book.
- Positions missing a required grouping tag (e.g. sector) are excluded from
  that specific calc and reported as excluded, never silently folded into
  a bucket or zeroed out.

CURRENCY IS THE DELIBERATE EXCEPTION to the absolute-value rule above (see
design_document.md §6 for the full rationale). Currency concentration
measures how much of the fund's NAV is denominated in each currency - i.e.
how NAV moves when FX moves - and that quantity has a conservation law:
currency exposures should sum to ~100% of NAV, because every unit of the
fund is denominated in something. A short USD-denominated position REDUCES
the fund's USD sensitivity; counting it at absolute magnitude (as every
other category correctly does) would assert the opposite and can push the
reported total past 100% of NAV, which is not a risk finding but a
violated identity - a fund cannot have more currency exposure than it has
money. `_compute_currency_concentration()` therefore sums SIGNED
market_value per currency, not abs(), and checks the ~100% invariant
explicitly.
"""

import os
import sys

import pandas as pd
from dataclasses import dataclass, field

# Allow `python engine/concentration.py` (not just `python -m engine.concentration`)
# to find the engine package - direct script invocation puts this file's
# directory on sys.path, not the repo root (same pattern as ingestion/normalize.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.geopolitical_risk import (
    DEFAULT_GEOPOLITICAL_RISK_CONFIG, MATERIAL_GEOGRAPHY_EXPOSURE_PCT, get_geopolitical_tier,
)

DEFAULT_LIMITS = {
    "single_issuer_limit_pct": 8.0,
    "sector_limit_pct": 25.0,
    "geography_limit_pct": 70.0,
    "asset_class_limit_pct": 60.0,
    # Currency exposure is a distinct risk from geography (e.g. a US-listed
    # ADR can be EUR-denominated) - grouped on the position's native
    # `currency` tag, not on geography. 60% is a starting point, not a
    # regulatory figure - configurable per fund mandate like the other limits.
    "currency_limit_pct": 60.0,
    "correlation_threshold": 0.85,
    "warning_buffer_pct": 3.0,
    # Geopolitical overlay (engine/geopolitical_risk.py) - a distinct check
    # from the limit-based classification above: a geography can be well
    # under geography_limit_pct and still carry material exposure to an
    # ELEVATED/HIGH-tier region. Named constant, not a magic number.
    "material_geography_exposure_pct": MATERIAL_GEOGRAPHY_EXPOSURE_PCT,
}

UNASSIGNED_ISSUER = "Unassigned (no issuer tag)"

# Currency exposures are NET (signed), not absolute (see module docstring) -
# they should sum to ~100% of NAV, since every unit of the fund is
# denominated in something. A sum materially off 100% is a real data
# problem (e.g. a missing/garbled currency tag, or an FX conversion bug),
# not a risk finding - this tolerance is a named constant, not a magic
# number, so the invariant check is auditable.
CURRENCY_SUM_TOLERANCE_PCT_POINTS = 1.0


@dataclass
class ConcentrationEntry:
    name: str
    market_value: float
    pct: float
    limit: float
    status: str  # OK | WARNING | BREACH


@dataclass
class GeographyConcentrationEntry(ConcentrationEntry):
    """Adds the geopolitical risk-desk overlay to a geography's entry.
    OK/WARNING/BREACH above stays purely limit-based, unaffected by any of
    these fields - the geopolitical tier is a separate, additional signal,
    never a reclassification of the limit check.
    """
    geopolitical_tier: str = "LOW"
    geopolitical_note: str = ""
    geopolitical_source: str = None
    geopolitical_as_of: str = None
    # True only when tier is ELEVATED/HIGH AND exposure is material
    # (>= material_geography_exposure_pct) - NOT the same thing as a limit
    # breach; a geography can be well under its limit and still carry
    # material exposure to a HIGH-tier region.
    geopolitical_flag: bool = False


@dataclass
class CorrelationCluster:
    holdings: list
    min_corr: float
    status: str = "FLAGGED"


@dataclass
class VolatilitySignal:
    position_id: str
    issuer: str
    realized_vol_pct: float       # annualized realized vol, trailing window
    vol_change_qoq_pct: float     # % change vs. the same-length window ~1 quarter earlier


@dataclass
class ConcentrationReport:
    portfolio_id: str
    nav: float
    issuer_concentration: list = field(default_factory=list)
    sector_concentration: list = field(default_factory=list)
    geography_concentration: list = field(default_factory=list)
    asset_class_concentration: list = field(default_factory=list)
    currency_concentration: list = field(default_factory=list)
    hhi: float = 0.0
    correlation_clusters: list = field(default_factory=list)
    volatility_signals: list = field(default_factory=list)
    excluded_from_sector: list = field(default_factory=list)
    excluded_from_geography: list = field(default_factory=list)
    excluded_from_issuer: list = field(default_factory=list)
    # Currency is NET, so unlike the other categories its entries should
    # sum to ~100% of NAV - currency_sum_pct is that actual sum, and
    # currency_data_quality_flag is populated (else None) when it's off by
    # more than CURRENCY_SUM_TOLERANCE_PCT_POINTS.
    currency_sum_pct: float = 0.0
    currency_data_quality_flag: str = None


def _status(pct: float, limit: float, warning_buffer_pct: float) -> str:
    if pct > limit:
        return "BREACH"
    if pct > limit - warning_buffer_pct:
        return "WARNING"
    return "OK"


def _attach_geopolitical_overlay(geography_entries: list, material_pct: float, geo_config: dict) -> list:
    """Convert plain ConcentrationEntry geography entries into
    GeographyConcentrationEntry, attaching the risk-desk-configured tier -
    never inferred by code or Claude, only looked up from
    engine/geopolitical_risk.py's config. Does not touch pct/limit/status.
    """
    overlaid = []
    for entry in geography_entries:
        tier_record = get_geopolitical_tier(entry.name, geo_config)
        tier = tier_record["tier"]
        is_material = entry.pct >= material_pct
        flag = tier in ("ELEVATED", "HIGH") and is_material
        overlaid.append(GeographyConcentrationEntry(
            name=entry.name, market_value=entry.market_value, pct=entry.pct,
            limit=entry.limit, status=entry.status,
            geopolitical_tier=tier, geopolitical_note=tier_record["note"],
            geopolitical_source=tier_record["source"], geopolitical_as_of=tier_record["as_of"],
            geopolitical_flag=flag,
        ))
    return overlaid


def _group_and_compute(df: pd.DataFrame, group_col: str, nav: float,
                        limit: float, warning_buffer_pct: float):
    """Group by group_col on abs(market_value), compute pct of NAV, classify
    against limit. Rows where group_col is null are excluded and their
    position_ids returned separately rather than folded into a bucket.
    """
    present = df[df[group_col].notna()]
    excluded_ids = df.loc[df[group_col].isna(), "position_id"].tolist()

    if present.empty:
        return [], excluded_ids

    totals = present.groupby(group_col)["abs_market_value"].sum().sort_values(ascending=False)

    entries = []
    for name, mv in totals.items():
        pct = (mv / nav * 100) if nav else 0.0
        entries.append(ConcentrationEntry(
            name=name,
            market_value=float(mv),
            pct=round(float(pct), 2),
            limit=limit,
            status=_status(pct, limit, warning_buffer_pct),
        ))
    return entries, excluded_ids


def _compute_currency_concentration(df: pd.DataFrame, nav: float, limit: float, warning_buffer_pct: float):
    """Currency concentration is the deliberate exception to this module's
    absolute-value rule (see module docstring): it sums SIGNED market_value
    per currency, divided by NAV - not abs() - because a short USD position
    genuinely reduces the fund's USD sensitivity rather than adding to it.

    Because currency exposures should sum to ~100% of NAV (every unit of
    the fund is denominated in something), that sum is checked against
    CURRENCY_SUM_TOLERANCE_PCT_POINTS; a violation is a data problem, not a
    risk finding, and is returned as `sum_flag` rather than silently
    ignored.

    A NEGATIVE net exposure (net short a currency) is classified NET_SHORT,
    never forced into the OK/WARNING/BREACH bands - those bands encode a
    long-side diversification limit ("too much in one currency"), which
    doesn't apply to being net short one. NET_SHORT is therefore not a
    breach and will not appear in Claude's WARNING/BREACH `breaches` list.
    """
    totals = df.groupby("currency")["market_value"].sum().sort_values(ascending=False)

    entries = []
    for name, mv in totals.items():
        pct = (mv / nav * 100) if nav else 0.0
        status = "NET_SHORT" if pct < 0 else _status(pct, limit, warning_buffer_pct)
        entries.append(ConcentrationEntry(
            name=name,
            market_value=float(mv),  # signed (net), not abs - see docstring
            pct=round(float(pct), 2),
            limit=limit,
            status=status,
        ))

    sum_pct = round(sum(e.pct for e in entries), 2)
    sum_flag = None
    if abs(sum_pct - 100.0) > CURRENCY_SUM_TOLERANCE_PCT_POINTS:
        sum_flag = (
            f"Currency exposures sum to {sum_pct}% of NAV, expected ~100% "
            f"(tolerance +/-{CURRENCY_SUM_TOLERANCE_PCT_POINTS}pp) - this indicates a data "
            f"problem (e.g. a missing/garbled currency tag or an FX conversion error), "
            f"not a risk finding."
        )
    return entries, sum_pct, sum_flag


def compute_hhi(df: pd.DataFrame, nav: float) -> float:
    """Herfindahl-Hirschman Index across individual positions, on the
    standard 0-10000 scale (sum of squared percentage market shares).
    Uses absolute market value as exposure.
    """
    if not nav:
        return 0.0
    shares_pct = df["abs_market_value"] / nav * 100
    return round(float((shares_pct ** 2).sum()), 1)


def compute_correlation_clusters(positions: list, threshold: float) -> list:
    """Pairwise correlation on price_history, where provided. price_history
    is optional per design_document.md §13 - positions without it are
    simply excluded from this calc, not flagged as a data-quality issue.
    """
    priced = {
        p["position_id"]: p["price_history"]
        for p in positions
        if p.get("price_history")
    }
    if len(priced) < 2:
        return []

    price_df = pd.DataFrame(priced)
    corr = price_df.corr()

    clusters = []
    ids = list(priced.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            c = corr.loc[ids[i], ids[j]]
            if pd.notna(c) and c >= threshold:
                clusters.append(CorrelationCluster(
                    holdings=[ids[i], ids[j]],
                    min_corr=round(float(c), 3),
                ))
    return clusters


VOL_WINDOW_DAYS = 30          # trailing realized-vol window
VOL_LOOKBACK_GAP_DAYS = 63    # ~1 trading quarter back, for the QoQ comparison window
TRADING_DAYS_PER_YEAR = 252


def compute_volatility_signals(positions: list) -> list:
    """QoQ realized-volatility proxy per position, where price_history is
    provided (design_document.md §6/§7 - same optionality policy as
    compute_correlation_clusters: positions without enough history are
    excluded from this calc, not flagged as a data-quality issue).

    Compares the trailing VOL_WINDOW_DAYS realized volatility (annualized
    stdev of daily returns) against the same-length window starting
    VOL_LOOKBACK_GAP_DAYS earlier, expressed as a % change - this is the
    "30-day realized volatility up X% quarter-over-quarter" signal Claude
    reasons over alongside concentration breaches.
    """
    min_prices_needed = VOL_WINDOW_DAYS + VOL_LOOKBACK_GAP_DAYS + 1
    signals = []
    for p in positions:
        history = p.get("price_history")
        if not history or len(history) < min_prices_needed:
            continue

        returns = pd.Series(history, dtype=float).pct_change().dropna()
        current_window = returns.iloc[-VOL_WINDOW_DAYS:]
        prior_window = returns.iloc[-(VOL_WINDOW_DAYS + VOL_LOOKBACK_GAP_DAYS):-VOL_LOOKBACK_GAP_DAYS]
        if len(current_window) < VOL_WINDOW_DAYS or len(prior_window) < VOL_WINDOW_DAYS:
            continue

        current_vol = current_window.std() * (TRADING_DAYS_PER_YEAR ** 0.5) * 100
        prior_vol = prior_window.std() * (TRADING_DAYS_PER_YEAR ** 0.5) * 100
        if not prior_vol:
            continue

        change_pct = (current_vol - prior_vol) / prior_vol * 100
        signals.append(VolatilitySignal(
            position_id=p["position_id"],
            issuer=p.get("issuer") or p.get("instrument") or p["position_id"],
            realized_vol_pct=round(float(current_vol), 2),
            vol_change_qoq_pct=round(float(change_pct), 1),
        ))
    return signals


def compute_concentration(portfolio, limits: dict = None, geopolitical_config: dict = None) -> ConcentrationReport:
    """Compute all §6 concentration metrics for a normalized portfolio.

    Accepts either a NormalizedPortfolio (from ingestion.normalize) or a
    raw dict with the same shape (portfolio_id, nav, positions).

    `geopolitical_config` overrides engine.geopolitical_risk's
    DEFAULT_GEOPOLITICAL_RISK_CONFIG (risk-desk owned, sourced, dated) -
    never inferred from Claude or from the positions themselves.
    """
    limits = {**DEFAULT_LIMITS, **(limits or {})}
    geopolitical_config = DEFAULT_GEOPOLITICAL_RISK_CONFIG if geopolitical_config is None else geopolitical_config

    portfolio_id = getattr(portfolio, "portfolio_id", None) or portfolio.get("portfolio_id", "UNKNOWN")
    nav = getattr(portfolio, "nav", None)
    if nav is None:
        nav = portfolio.get("nav", 0)
    positions = getattr(portfolio, "positions", None)
    if positions is None:
        positions = portfolio.get("positions", [])

    if not positions:
        return ConcentrationReport(portfolio_id=portfolio_id, nav=nav)

    df = pd.DataFrame(positions)
    df["abs_market_value"] = df["market_value"].abs()

    # issuer: cash isn't single-name/issuer risk, so it's excluded from the
    # issuer concentration calc entirely (like a missing sector tag), rather
    # than grouped into a bucket - applying the single-issuer limit to it
    # would produce a misleading BREACH. Non-cash positions that still lack
    # an issuer tag are grouped under a labeled bucket instead, since
    # "unassigned issuer" is itself a meaningful (low-risk) concentration
    # fact for those.
    is_cash = df["asset_class"] == "Cash"
    excluded_issuer = df.loc[is_cash, "position_id"].tolist()
    issuer_df = df.loc[~is_cash].copy()
    issuer_df["issuer"] = issuer_df["issuer"].fillna(UNASSIGNED_ISSUER)
    issuer_entries, _ = _group_and_compute(
        issuer_df, "issuer", nav,
        limits["single_issuer_limit_pct"], limits["warning_buffer_pct"],
    )

    sector_entries, excluded_sector = _group_and_compute(
        df, "sector", nav, limits["sector_limit_pct"], limits["warning_buffer_pct"],
    )

    geography_entries, excluded_geo = _group_and_compute(
        df, "geography", nav, limits["geography_limit_pct"], limits["warning_buffer_pct"],
    )
    geography_entries = _attach_geopolitical_overlay(
        geography_entries, limits["material_geography_exposure_pct"], geopolitical_config,
    )

    asset_class_entries, _ = _group_and_compute(
        df, "asset_class", nav, limits["asset_class_limit_pct"], limits["warning_buffer_pct"],
    )

    # `currency` is a required position field (validated at ingestion), so
    # unlike sector/geography there's no missing-tag exclusion case here.
    # Unlike every other category above, currency concentration is NET
    # (signed), not absolute - see module docstring and
    # _compute_currency_concentration()'s docstring for why.
    currency_entries, currency_sum_pct, currency_data_quality_flag = _compute_currency_concentration(
        df, nav, limits["currency_limit_pct"], limits["warning_buffer_pct"],
    )

    hhi = compute_hhi(df, nav)
    clusters = compute_correlation_clusters(positions, limits["correlation_threshold"])
    volatility_signals = compute_volatility_signals(positions)

    return ConcentrationReport(
        portfolio_id=portfolio_id,
        nav=nav,
        issuer_concentration=issuer_entries,
        sector_concentration=sector_entries,
        geography_concentration=geography_entries,
        asset_class_concentration=asset_class_entries,
        currency_concentration=currency_entries,
        hhi=hhi,
        correlation_clusters=clusters,
        volatility_signals=volatility_signals,
        excluded_from_sector=excluded_sector,
        excluded_from_geography=excluded_geo,
        excluded_from_issuer=excluded_issuer,
        currency_sum_pct=currency_sum_pct,
        currency_data_quality_flag=currency_data_quality_flag,
    )


if __name__ == "__main__":
    import sys
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ingestion.normalize import load_portfolio, normalize_portfolio

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio)

    print(f"Portfolio: {portfolio.fund_name} ({report.portfolio_id})")
    print(f"NAV: {report.nav:,.0f} {portfolio.base_currency}")
    print(f"HHI: {report.hhi}")

    def _print_group(title, entries):
        print(f"\n{title}:")
        for e in entries:
            print(f"  {e.name:35s} {e.pct:7.2f}%  limit {e.limit:5.1f}%  [{e.status}]")

    _print_group("Issuer concentration", report.issuer_concentration)
    _print_group("Sector concentration", report.sector_concentration)
    _print_group("Geography concentration", report.geography_concentration)
    _print_group("Asset class concentration", report.asset_class_concentration)
    _print_group("Currency concentration", report.currency_concentration)
    print(f"  (sum of currency exposures: {report.currency_sum_pct}% of NAV)")
    if report.currency_data_quality_flag:
        print(f"  DATA QUALITY FLAG: {report.currency_data_quality_flag}")

    print("\nGeopolitical overlay (risk-desk config, not Claude-inferred):")
    for e in report.geography_concentration:
        flag = " [GEOPOLITICAL FLAG]" if e.geopolitical_flag else ""
        print(f"  {e.name:30s} tier={e.geopolitical_tier:8s} exposure={e.pct:6.2f}%{flag}")
        print(f"      note: {e.geopolitical_note}")
        print(f"      source: {e.geopolitical_source}  as_of: {e.geopolitical_as_of}")

    if report.excluded_from_sector:
        print(f"\nExcluded from sector calc (missing tag): {report.excluded_from_sector}")
    if report.excluded_from_geography:
        print(f"Excluded from geography calc (missing tag): {report.excluded_from_geography}")
    if report.excluded_from_issuer:
        print(f"Excluded from issuer calc (cash): {report.excluded_from_issuer}")

    print(f"\nCorrelation clusters (threshold {DEFAULT_LIMITS['correlation_threshold']}): "
          f"{len(report.correlation_clusters)} found")
    for c in report.correlation_clusters:
        print(f"  {c.holdings} min_corr={c.min_corr} [{c.status}]")

    print(f"\nVolatility signals (QoQ, {len(report.volatility_signals)} priced positions):")
    for v in report.volatility_signals:
        arrow = "UP" if v.vol_change_qoq_pct > 0 else "down"
        print(f"  {v.issuer:30s} realized_vol={v.realized_vol_pct:6.2f}%  "
              f"QoQ {arrow} {v.vol_change_qoq_pct:+.1f}%")
