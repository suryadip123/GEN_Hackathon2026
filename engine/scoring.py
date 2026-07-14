"""
Task 3 - Rules-Based Base Severity Score

Computes the deterministic base severity score from design_document.md §8:
a weighted combination of max single-issuer breach magnitude, max sector
breach magnitude, HHI level, and count of correlation flags, mapped onto
auditable LOW/MEDIUM/HIGH/CRITICAL bands with numeric boundaries defined
here rather than inline magic numbers.

This is the *base* score only (§8.1/§8.3). The Claude-informed one-tier
adjustment (§8.2) is a separate downstream step in the claude/ layer, which
starts from this score and its component breakdown rather than recomputing
anything - no Claude call happens in this module.
"""

from dataclasses import dataclass, field

# --- Component weights (must sum to 1.0) ---
WEIGHTS = {
    "issuer_breach": 0.35,
    "sector_breach": 0.25,
    "hhi": 0.25,
    "correlation": 0.15,
}
assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9, "WEIGHTS must sum to 1.0"

# Percentage points over limit at which a breach component's score saturates
# to 100. Issuer limit (8%) is tighter than sector limit (25%), so a smaller
# absolute overshoot is treated as equally severe for issuer breaches.
ISSUER_BREACH_SATURATION_PCT = 12.0
SECTOR_BREACH_SATURATION_PCT = 20.0

# HHI bands adapted from the standard antitrust HHI convention (unconcentrated /
# moderately concentrated / highly concentrated), reused here as a proxy for
# portfolio diversification rather than market concentration.
HHI_UNCONCENTRATED_MAX = 1500.0
HHI_MODERATE_MAX = 2500.0
HHI_UNCONCENTRATED_SCORE_MAX = 30.0
HHI_MODERATE_SCORE_MAX = 70.0
HHI_THEORETICAL_MAX = 10000.0

# Number of flagged correlation clusters at which that component saturates to 100.
CORRELATION_SATURATION_COUNT = 5

# Severity bands over the final 0-100 composite score. [low, high).
SEVERITY_BANDS = [
    (0.0, 25.0, "LOW"),
    (25.0, 50.0, "MEDIUM"),
    (50.0, 75.0, "HIGH"),
    (75.0, 100.0 + 1e-9, "CRITICAL"),
]


@dataclass
class ScoreComponents:
    issuer_breach_score: float
    sector_breach_score: float
    hhi_score: float
    correlation_score: float
    max_issuer_breach_pct: float
    max_sector_breach_pct: float
    hhi: float
    correlation_cluster_count: int


@dataclass
class SeverityResult:
    portfolio_id: str
    severity: str  # LOW | MEDIUM | HIGH | CRITICAL | NONE
    score: float
    components: ScoreComponents = None
    structural_notes: list = field(default_factory=list)
    note: str = ""


def _max_breach_magnitude(entries, structural_notes, category_label):
    """Max (pct - limit) among BREACH-status entries in a concentration
    category. A category with a single entry is trivially 100%-in-one-bucket
    (nothing to diversify against) - per design_document.md §8's single-asset/
    all-cash edge case, this is a structural characteristic of the portfolio,
    not a limit event, so it's excluded from the score and logged as a note
    instead of contributing false BREACH severity.
    """
    if len(entries) <= 1:
        if entries and entries[0].status == "BREACH":
            structural_notes.append(
                f"{category_label} concentration is a single bucket "
                f"({entries[0].name} at {entries[0].pct}%) - structural, not a limit event."
            )
        return 0.0
    breaches = [e.pct - e.limit for e in entries if e.status == "BREACH"]
    return max(breaches) if breaches else 0.0


def _scale(value, saturation):
    if saturation <= 0:
        return 0.0
    return max(0.0, min(100.0, value / saturation * 100.0))


def _hhi_score(hhi):
    if hhi <= HHI_UNCONCENTRATED_MAX:
        return (hhi / HHI_UNCONCENTRATED_MAX) * HHI_UNCONCENTRATED_SCORE_MAX
    if hhi <= HHI_MODERATE_MAX:
        span = HHI_MODERATE_MAX - HHI_UNCONCENTRATED_MAX
        frac = (hhi - HHI_UNCONCENTRATED_MAX) / span
        return HHI_UNCONCENTRATED_SCORE_MAX + frac * (HHI_MODERATE_SCORE_MAX - HHI_UNCONCENTRATED_SCORE_MAX)
    span = HHI_THEORETICAL_MAX - HHI_MODERATE_MAX
    frac = min(1.0, (hhi - HHI_MODERATE_MAX) / span)
    return HHI_MODERATE_SCORE_MAX + frac * (100.0 - HHI_MODERATE_SCORE_MAX)


def _band(score):
    for low, high, label in SEVERITY_BANDS:
        if low <= score < high:
            return label
    return SEVERITY_BANDS[-1][2]


def _is_empty_portfolio(report) -> bool:
    return not (
        report.issuer_concentration
        or report.sector_concentration
        or report.geography_concentration
        or report.asset_class_concentration
        or report.excluded_from_issuer
        or report.excluded_from_sector
        or report.excluded_from_geography
        or report.correlation_clusters
        or report.hhi
    )


def compute_severity(report) -> SeverityResult:
    """Compute the rules-based base severity score (design_document.md §8)
    from an engine.concentration.ConcentrationReport.
    """
    portfolio_id = getattr(report, "portfolio_id", "UNKNOWN")

    if _is_empty_portfolio(report):
        return SeverityResult(
            portfolio_id=portfolio_id, severity="NONE", score=0.0,
            note="Empty portfolio - no analysis performed, logged only.",
        )

    structural_notes = []
    max_issuer_breach = _max_breach_magnitude(report.issuer_concentration, structural_notes, "Issuer")
    max_sector_breach = _max_breach_magnitude(report.sector_concentration, structural_notes, "Sector")

    issuer_score = _scale(max_issuer_breach, ISSUER_BREACH_SATURATION_PCT)
    sector_score = _scale(max_sector_breach, SECTOR_BREACH_SATURATION_PCT)
    correlation_count = len(report.correlation_clusters)
    correlation_score = _scale(correlation_count, CORRELATION_SATURATION_COUNT)

    # A portfolio that is entirely one asset class (e.g. all-cash, or a single
    # holding) is trivially at maximum HHI by construction - that's a
    # structural fact about the portfolio, not diversification risk, so per
    # §8's single-asset/all-cash edge case it's excluded from the score too.
    is_single_asset_class = len(report.asset_class_concentration) <= 1
    if is_single_asset_class and report.asset_class_concentration:
        entry = report.asset_class_concentration[0]
        structural_notes.append(
            f"Portfolio is entirely one asset class ({entry.name} at {entry.pct}%) - "
            "HHI is trivially maximal, not diversification risk; excluded from severity score."
        )
        hhi_score = 0.0
    else:
        hhi_score = _hhi_score(report.hhi)

    # Geopolitical overlay (engine/geopolitical_risk.py) - same precedent as
    # geography/asset-class/currency: computed and surfaced, never fed into
    # the weighted composite below. No new weight is introduced for it.
    for entry in report.geography_concentration:
        if getattr(entry, "geopolitical_flag", False):
            structural_notes.append(
                f"Geopolitical overlay: {entry.name} at {entry.pct}% exposure carries a "
                f"{entry.geopolitical_tier}-tier geopolitical risk (source: {entry.geopolitical_source}, "
                f"as_of: {entry.geopolitical_as_of}) - surfaced for Claude's reasoning, does not affect this score."
            )

    # Currency's net-exposure sum invariant (engine/concentration.py) is a
    # data-quality signal, not a risk finding - surfaced here for visibility,
    # never fed into the composite below.
    if getattr(report, "currency_data_quality_flag", None):
        structural_notes.append(f"Currency data quality: {report.currency_data_quality_flag}")

    composite = round(
        WEIGHTS["issuer_breach"] * issuer_score
        + WEIGHTS["sector_breach"] * sector_score
        + WEIGHTS["hhi"] * hhi_score
        + WEIGHTS["correlation"] * correlation_score,
        2,
    )

    components = ScoreComponents(
        issuer_breach_score=round(issuer_score, 2),
        sector_breach_score=round(sector_score, 2),
        hhi_score=round(hhi_score, 2),
        correlation_score=round(correlation_score, 2),
        max_issuer_breach_pct=round(max_issuer_breach, 2),
        max_sector_breach_pct=round(max_sector_breach, 2),
        hhi=report.hhi,
        correlation_cluster_count=correlation_count,
    )

    return SeverityResult(
        portfolio_id=portfolio_id,
        severity=_band(composite),
        score=composite,
        components=components,
        structural_notes=structural_notes,
    )


if __name__ == "__main__":
    import sys
    import os

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ingestion.normalize import load_portfolio, normalize_portfolio
    from engine.concentration import compute_concentration

    raw = load_portfolio("data/sample_portfolios/port_2026_0442.json")
    portfolio = normalize_portfolio(raw)
    report = compute_concentration(portfolio)
    result = compute_severity(report)

    print(f"Portfolio: {portfolio.fund_name} ({result.portfolio_id})")
    print(f"Severity: {result.severity}  (score={result.score})")
    c = result.components
    print(f"  issuer_breach_score={c.issuer_breach_score}  (max breach {c.max_issuer_breach_pct} pts over limit)")
    print(f"  sector_breach_score={c.sector_breach_score}  (max breach {c.max_sector_breach_pct} pts over limit)")
    print(f"  hhi_score={c.hhi_score}  (hhi={c.hhi})")
    print(f"  correlation_score={c.correlation_score}  ({c.correlation_cluster_count} clusters)")
    if result.structural_notes:
        print("Structural notes:")
        for n in result.structural_notes:
            print(f"  - {n}")

    print("\n--- Edge case: empty portfolio ---")
    empty_report = compute_concentration({"portfolio_id": "EMPTY-TEST", "nav": 1_000_000, "positions": []})
    empty_result = compute_severity(empty_report)
    print(f"Severity: {empty_result.severity}  note: {empty_result.note}")

    print("\n--- Edge case: single-position all-cash portfolio ---")
    cash_only = {
        "portfolio_id": "CASH-ONLY-TEST",
        "nav": 1_000_000,
        "positions": [{
            "position_id": "POS-CASH-1",
            "market_value": 1_000_000,
            "asset_class": "Cash",
            "issuer": None,
            "sector": "Cash",
            "geography": "United States",
        }],
    }
    cash_report = compute_concentration(cash_only)
    cash_result = compute_severity(cash_report)
    print(f"Severity: {cash_result.severity}  (score={cash_result.score})")
    if cash_result.structural_notes:
        for n in cash_result.structural_notes:
            print(f"  - {n}")
    print(f"  excluded_from_issuer: {cash_report.excluded_from_issuer}")
