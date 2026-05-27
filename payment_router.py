"""payment_router.py
Route CiteIntegrity checkout by customer billing country.

Africa goes to Paystack. Every other billing country goes to Stripe.
Use billing country selected by the customer, not IP address.
"""

from __future__ import annotations

AFRICAN_COUNTRY_CODES = {
    "DZ", "AO", "BJ", "BW", "BF", "BI", "CV", "CM", "CF", "TD",
    "KM", "CG", "CD", "CI", "DJ", "EG", "GQ", "ER", "SZ", "ET",
    "GA", "GM", "GH", "GN", "GW", "KE", "LS", "LR", "LY", "MG",
    "MW", "ML", "MR", "MU", "MA", "MZ", "NA", "NE", "NG", "RW",
    "ST", "SN", "SC", "SL", "SO", "ZA", "SS", "SD", "TZ", "TG",
    "TN", "UG", "ZM", "ZW",
}


def normalise_country_code(country_code: str) -> str:
    return str(country_code or "").strip().upper()


def is_african_country(country_code: str) -> bool:
    return normalise_country_code(country_code) in AFRICAN_COUNTRY_CODES


def choose_payment_provider(country_code: str) -> str:
    """Return 'paystack' for Africa and 'stripe' for all other countries."""
    return "paystack" if is_african_country(country_code) else "stripe"
