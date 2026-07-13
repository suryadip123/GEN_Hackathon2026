"""
Task 1 (cont.) - FX Rate Fetching for Multi-Currency Support

Fetches live FX rates (base = a portfolio's base_currency) from
open.er-api.com, with a local JSON cache as a fallback. The live demo must
keep working even if this third-party API is down or unreachable - a
network failure here degrades to cached (or, if there's no cache at all,
skipped) conversion rather than crashing the analysis.

Rate convention (matches open.er-api.com): rates[X] = units of X per 1 unit
of base_currency. To convert a native-currency amount into base_currency:
    amount_in_base = amount_native / rates[native_currency]
"""

import json
import os
import urllib.error
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone

FX_API_URL_TEMPLATE = "https://open.er-api.com/v6/latest/{base}"
FX_FETCH_TIMEOUT_S = 5

# Don't hit the live API on every Streamlit rerun (a rerun fires on every
# widget interaction) - reuse a cached rate set for this long before trying
# live again. Rates from this API only update ~daily anyway.
FX_CACHE_TTL_SECONDS = 3600

FX_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fx_rates_cache.json"
)


class FxRatesUnavailable(Exception):
    """Raised only when there is no live data AND no cache at all for this
    base_currency. Callers should treat this as "skip conversion, flag it"
    rather than letting it crash the whole portfolio analysis.
    """


@dataclass
class FxRatesResult:
    base_currency: str
    rates: dict
    fetched_at: str  # ISO 8601
    source: str       # "live" | "cache_fresh" | "cache_fallback"
    warning: str = None


def _fetch_live_rates(base_currency: str) -> dict:
    url = FX_API_URL_TEMPLATE.format(base=base_currency)
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-risk-platform/1.0"})
    with urllib.request.urlopen(req, timeout=FX_FETCH_TIMEOUT_S) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, "non-200 response", resp.headers, None)
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("result") != "success" or "rates" not in payload:
        raise ValueError(f"Unexpected FX API payload: result={payload.get('result')!r}")
    return payload["rates"]


def _load_cache() -> dict:
    if not os.path.exists(FX_CACHE_PATH):
        return {}
    try:
        with open(FX_CACHE_PATH, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {}


def _save_cache(base_currency: str, rates: dict, fetched_at: str) -> None:
    cache = _load_cache()
    cache[base_currency] = {"rates": rates, "fetched_at": fetched_at}
    with open(FX_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def get_fx_rates(base_currency: str, ttl_seconds: int = FX_CACHE_TTL_SECONDS) -> FxRatesResult:
    """Return FX rates for `base_currency`, live-first with a cache fallback.

    - Fresh cache (younger than `ttl_seconds`): reused directly, no network
      call - keeps repeated Streamlit reruns fast and quiet.
    - Stale or missing cache: tries a live fetch. On success, updates the
      cache and returns it.
    - Live fetch fails: falls back to whatever cache exists (even if stale),
      with a logged warning - the app keeps working during a demo even if
      this third-party API is down.
    - Live fetch fails AND there is no cache at all: raises
      FxRatesUnavailable - callers should catch this and skip conversion
      rather than crash.
    """
    cache = _load_cache()
    entry = cache.get(base_currency)

    if entry is not None:
        age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(entry["fetched_at"])).total_seconds()
        if age_s < ttl_seconds:
            return FxRatesResult(
                base_currency=base_currency, rates=entry["rates"],
                fetched_at=entry["fetched_at"], source="cache_fresh",
            )

    try:
        rates = _fetch_live_rates(base_currency)
    except Exception as e:
        if entry is not None:
            warning = (
                f"Live FX rate fetch failed for base {base_currency!r} ({e}); "
                f"falling back to cached rates from {entry['fetched_at']}."
            )
            warnings.warn(warning)
            return FxRatesResult(
                base_currency=base_currency, rates=entry["rates"],
                fetched_at=entry["fetched_at"], source="cache_fallback", warning=warning,
            )
        raise FxRatesUnavailable(
            f"FX fetch failed for base {base_currency!r} and no cached rates exist: {e}"
        ) from e

    fetched_at = datetime.now(timezone.utc).isoformat()
    _save_cache(base_currency, rates, fetched_at)
    return FxRatesResult(base_currency=base_currency, rates=rates, fetched_at=fetched_at, source="live")


if __name__ == "__main__":
    result = get_fx_rates("INR")
    print(f"Source: {result.source}  fetched_at: {result.fetched_at}")
    for code in ("INR", "USD", "EUR", "JPY"):
        print(f"  {code}: {result.rates.get(code)}")
