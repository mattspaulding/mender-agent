"""Function tools FinPay uses.

Just one for v1: a mock exchange-rate lookup. Plausible numbers, deterministic,
no network. The whole point of FinPay is currency conversion, so this is the
critical path tool.
"""

from __future__ import annotations

# Mid-market rates as of an arbitrary frozen date — close enough to real to
# be unsurprising in the demo, far enough off that nobody mistakes them for
# a live feed.
_USD_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "JPY": 156.40,
    "CAD": 1.37,
    "AUD": 1.51,
    "CHF": 0.91,
    "CNY": 7.24,
    "INR": 83.50,
    "MXN": 17.10,
    "BRL": 5.13,
    "SEK": 10.65,
    "NOK": 10.78,
    "DKK": 6.86,
    "SGD": 1.35,
    "HKD": 7.81,
    "NZD": 1.65,
    "ZAR": 18.40,
    "KRW": 1370.0,
    "TRY": 32.30,
}


def get_exchange_rate(base: str, quote: str) -> dict:
    """Return the conversion rate from `base` to `quote`.

    Args:
        base: ISO-4217 source currency code (e.g. "USD").
        quote: ISO-4217 target currency code (e.g. "EUR").

    Returns:
        A dict with keys `base`, `quote`, `rate`, and `as_of`.
        `rate` is "1 unit of base = rate units of quote".
    """
    base = base.upper().strip()
    quote = quote.upper().strip()
    if base not in _USD_RATES:
        return {"error": f"unknown currency: {base}", "supported": sorted(_USD_RATES)}
    if quote not in _USD_RATES:
        return {"error": f"unknown currency: {quote}", "supported": sorted(_USD_RATES)}
    rate = _USD_RATES[quote] / _USD_RATES[base]
    return {
        "base": base,
        "quote": quote,
        "rate": round(rate, 6),
        "as_of": "2026-05-01T00:00:00Z",
    }
