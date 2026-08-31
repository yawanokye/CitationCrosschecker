"""Resolve the commercial market, currency and provider from billing country.

Only Ghana and Nigeria use Paystack in the first commercial release. Every
other country uses Stripe. The server is authoritative, never the browser.
"""

from __future__ import annotations

from typing import Dict


PAYSTACK_MARKETS: Dict[str, Dict[str, str]] = {
    "GH": {"market": "ghana", "currency": "GHS", "provider": "paystack", "label": "Ghana"},
    "NG": {"market": "nigeria", "currency": "NGN", "provider": "paystack", "label": "Nigeria"},
}


def normalise_country_code(country_code: str) -> str:
    return str(country_code or "").strip().upper()[:2]


def choose_payment_provider(country_code: str) -> str:
    """Return Paystack only for Ghana/Nigeria and Stripe for all other markets."""
    return "paystack" if normalise_country_code(country_code) in PAYSTACK_MARKETS else "stripe"


def resolve_payment_market(country_code: str) -> Dict[str, str]:
    code = normalise_country_code(country_code)
    if code in PAYSTACK_MARKETS:
        return {"country_code": code, **PAYSTACK_MARKETS[code]}
    return {
        "country_code": code or "INTL",
        "market": "international",
        "currency": "USD",
        "provider": "stripe",
        "label": "International",
    }


def currency_for_country(country_code: str) -> str:
    return resolve_payment_market(country_code)["currency"]
