"""paystack_payments.py, Paystack helpers for CiteIntegrity.

Payment model used in this build:
- GHS selected: charge the normal GHS plan price through Paystack.
- USD selected: use the fixed USD plan price from entitlements.py, convert it
  to a GHS charge amount using PAYSTACK_USD_TO_GHS_RATE, then send GHS
  to Paystack.
- Paystack will display the GHS amount it receives. The customer's bank/card
  provider may then convert that GHS amount to the customer's card currency.
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, Optional
from entitlements import DEFAULT_CURRENCY, get_price, normalise_currency, validate_paid_package_for_document
from access_control import create_pending_purchase, mark_purchase_paid, record_purchase_run

PAYSTACK_PAYMENTS_VERSION = "1.5.32"
PAYSTACK_PAYMENTS_BUILD = "commercial-2026-05-20-paystack-usd-price-converted-to-ghs-charge-FINAL"

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

PAYSTACK_USER_AGENT = os.environ.get(
    "PAYSTACK_USER_AGENT",
    "CiteIntegrity/1.0 (+https://citeintegrity.org; payments@citeintegrity.org)"
).strip()

class PaystackError(Exception):
    pass

def _require_secret_key() -> str:
    if not PAYSTACK_SECRET_KEY:
        raise PaystackError("PAYSTACK_SECRET_KEY is not configured.")
    return PAYSTACK_SECRET_KEY

def amount_to_subunit(amount: float) -> int:
    return int(round(float(amount) * 100))

def _float_env(name: str, default: str) -> float:
    """Read a positive float from an environment variable."""
    raw = os.environ.get(name, default)
    try:
        value = float(str(raw).strip())
        if value <= 0:
            raise ValueError("rate must be positive")
        return value
    except Exception:
        return float(default)

def get_paystack_charge_amount(tier_key: str, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    """
    Return the exact amount to send to Paystack.

    GHS selected:
        Use the normal GHS plan price from entitlements.py.

    USD selected:
        Use the fixed USD plan price from entitlements.py, then convert that
        USD price to a GHS amount using PAYSTACK_USD_TO_GHS_RATE. Paystack
        receives and displays GHS, not USD.

    Why this is needed:
        Sending currency=USD requires USD to be enabled on the Paystack account.
        This build avoids that failure while ensuring the amount charged is the
        GHS equivalent of the USD plan price, not the lower GHS plan price.
    """
    selected_currency = normalise_currency(currency)

    # GHS path: use the standard GHS price directly.
    if selected_currency != "USD":
        ghs_price = get_price(tier_key, "GHS")
        amount = float(ghs_price["amount"])
        return {
            "amount": amount,
            "currency": "GHS",
            "amount_subunit": amount_to_subunit(amount),
            "display": ghs_price["display"],
            "selected_currency": "GHS",
            "selected_amount": amount,
            "selected_display": ghs_price["display"],
            "charged_currency": "GHS",
            "charged_amount": amount,
            "charged_display": ghs_price["display"],
            "exchange_rate": None,
            "conversion_note": None,
            "payment_model": "paystack_ghs_direct_charge",
        }

    # USD path: DO NOT send USD to Paystack. Convert the fixed USD plan price
    # to GHS and charge Paystack in GHS.
    usd_price = get_price(tier_key, "USD")
    usd_amount = float(usd_price["amount"])

    # Set this in Render. Example: PAYSTACK_USD_TO_GHS_RATE=16.00
    # If Paystack/card conditions change, update the environment variable and
    # redeploy/restart without editing code.
    exchange_rate = _float_env("PAYSTACK_USD_TO_GHS_RATE", os.environ.get("USD_TO_GHS_RATE", "16.00"))
    charged_amount = round(usd_amount * exchange_rate, 2)
    charged_display = f"GHS {charged_amount:,.2f}"

    return {
        "amount": charged_amount,
        "currency": "GHS",
        "amount_subunit": amount_to_subunit(charged_amount),
        "display": f"USD {usd_amount:,.2f} charged as {charged_display}",
        "selected_currency": "USD",
        "selected_amount": usd_amount,
        "selected_display": f"USD {usd_amount:,.2f}",
        "charged_currency": "GHS",
        "charged_amount": charged_amount,
        "charged_display": charged_display,
        "exchange_rate": exchange_rate,
        "conversion_note": (
            f"USD price converted to GHS at {exchange_rate:,.4f}. "
            "Paystack receives the converted GHS amount."
        ),
        "payment_model": "usd_plan_price_converted_to_ghs_paystack_charge",
    }

def _paystack_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    secret = _require_secret_key()
    url = f"{PAYSTACK_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": PAYSTACK_USER_AGENT,
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise PaystackError(f"Paystack HTTP error {e.code}: {raw}") from e
    except Exception as e:
        raise PaystackError(f"Paystack request failed: {e}") from e


def _sync_purchase_charge_amount(
    database_url: str,
    *,
    purchase_id: Any,
    charge: Dict[str, Any],
) -> None:
    """
    Keep purchases.amount/currency aligned with what Paystack is actually asked
    to charge. This matters when the user selects USD but Paystack receives the
    converted GHS amount.

    The current purchases table has amount and currency fields, but no separate
    selected_currency or exchange_rate columns. The richer selected-vs-charged
    details are therefore kept in Paystack metadata and returned to the frontend.
    """
    if not database_url or not purchase_id:
        return
    try:
        import psycopg2
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE purchases
            SET amount = %s,
                currency = %s
            WHERE id = %s
            """,
            (float(charge.get("amount") or 0), str(charge.get("currency") or "GHS"), purchase_id),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[PAYSTACK] Could not sync purchase charge amount: {e}")

def initialize_citeintegrity_payment(
    *,
    database_url: str,
    user_email: str,
    tier_key: str,
    reference_count: int,
    citation_count: int,
    selected_currency: str = DEFAULT_CURRENCY,
    job_id: str = "",
    file_name: str = "",
    callback_path: str = "/payment/paystack/callback",
) -> Dict[str, Any]:
    selected_currency = normalise_currency(selected_currency)

    tier_check = validate_paid_package_for_document(
        tier_key,
        reference_count,
        citation_count
    )

    if not tier_check.get("allowed"):
        return {
            "ok": False,
            "error": tier_check.get("message"),
            "tier_check": tier_check
        }

    charge = get_paystack_charge_amount(tier_key, selected_currency)

    provider_reference = (
        f"CI-{secrets.token_urlsafe(16).replace('_', '').replace('-', '')}"
    )

    purchase = create_pending_purchase(
        database_url,
        user_email=user_email,
        tier_key=tier_key,
        # Store the actual Paystack charge currency. For USD selection,
        # Paystack still receives the converted GHS amount.
        currency=charge["currency"],
        provider_reference=provider_reference,
        payment_provider="paystack",
        preview_job_id=job_id,
        preview_file_name=file_name,
        preview_reference_count=reference_count,
        preview_citation_count=citation_count,
    )

    _sync_purchase_charge_amount(
        database_url,
        purchase_id=purchase.get("id"),
        charge=charge,
    )

    metadata = {
        "product": "CiteIntegrity",
        "purchase_id": purchase["id"],
        "tier_key": tier_key,
        "package_key": purchase["package_key"],
        "job_id": job_id,
        "file_name": file_name,
        "reference_count": reference_count,
        "citation_count": citation_count,
        "analysis_runs": purchase["analyses_total"],
        "selected_currency": selected_currency,
        "selected_amount": charge.get("selected_amount"),
        "selected_display": charge.get("selected_display"),
        "charged_currency": charge["currency"],
        "charged_amount": charge["amount"],
        "charged_display": charge.get("charged_display") or charge.get("display"),
        "exchange_rate": charge.get("exchange_rate"),
        "conversion_note": charge.get("conversion_note"),
        "payment_model": charge.get("payment_model"),
    }

    payload = {
        "email": user_email,
        "amount": str(charge["amount_subunit"]),
        "currency": charge["currency"],
        "reference": provider_reference,
        "callback_url": f"{APP_BASE_URL}{callback_path}",
        "metadata": metadata,
    }

    try:
        response = _paystack_request("POST", "/transaction/initialize", payload)
    except PaystackError as e:
        return {
            "ok": False,
            "error": (
                "Payment could not start. CiteIntegrity sends a GHS amount to Paystack. "
                "If USD was selected, the USD plan price is converted to GHS before checkout. "
                "Please try again, or confirm that your Paystack test secret key is active."
            ),
            "gateway_error": str(e),
            "selected_currency": selected_currency,
            "currency": charge["currency"],
            "amount": charge["amount"],
            "display_amount": charge["display"],
        }

    if not response.get("status"):
        return {
            "ok": False,
            "error": response.get("message", "Paystack initialization failed."),
            "paystack_response": response
        }

    data = response.get("data") or {}

    return {
        "ok": True,
        "authorization_url": data.get("authorization_url"),
        "access_code": data.get("access_code"),
        "reference": data.get("reference") or provider_reference,
        "purchase_id": purchase["id"],
        "amount": charge["amount"],
        "currency": charge["currency"],
        "amount_subunit": charge["amount_subunit"],
        "display_amount": charge["display"],
        "selected_currency": charge.get("selected_currency"),
        "selected_amount": charge.get("selected_amount"),
        "selected_display": charge.get("selected_display"),
        "charged_currency": charge.get("charged_currency") or charge["currency"],
        "charged_amount": charge.get("charged_amount") or charge["amount"],
        "charged_display": charge.get("charged_display") or charge["display"],
        "exchange_rate": charge.get("exchange_rate"),
        "conversion_note": charge.get("conversion_note"),
        "payment_model": charge.get("payment_model"),
        "access_token": purchase.get("access_token"),
    }

def verify_paystack_transaction(reference: str) -> Dict[str, Any]:
    reference = urllib.parse.quote(str(reference or "").strip())
    response = _paystack_request("GET", f"/transaction/verify/{reference}")
    if not response.get("status"):
        return {"ok": False, "verified": False, "message": response.get("message", "Verification failed."), "paystack_response": response}
    data = response.get("data") or {}
    status = str(data.get("status") or "").lower()
    return {"ok": True, "verified": status == "success", "transaction_status": status, "reference": data.get("reference"), "amount": data.get("amount"), "currency": data.get("currency"), "customer_email": ((data.get("customer") or {}).get("email") or ""), "paystack_data": data}

def _attach_preview_job_to_purchase(database_url: str, purchase: Dict[str, Any], source: str = "paystack") -> Dict[str, Any]:
    """
    Attach the paid preview job to purchase_runs and consume one analysis run.

    This is safe to call from both callback and webhook because record_purchase_run()
    uses job_id uniqueness and should only increment analyses_used when a new row is inserted.
    """
    if not purchase or not purchase.get("preview_job_id"):
        return {
            "attached": False,
            "reason": "no_preview_job_id",
        }

    try:
        attached = record_purchase_run(
            database_url,
            purchase_id=purchase["id"],
            job_id=purchase.get("preview_job_id", ""),
            file_name=purchase.get("preview_file_name", ""),
            reference_count=purchase.get("preview_reference_count", 0),
            citation_count=purchase.get("preview_citation_count", 0),
        )

        return {
            "attached": bool(attached.get("run")),
            "source": source,
            "run": attached.get("run"),
            "purchase": attached.get("purchase"),
        }

    except Exception as e:
        print(f"[PAYSTACK] Could not attach preview job to purchase from {source}: {e}")
        return {
            "attached": False,
            "reason": str(e),
            "source": source,
        }


def verify_and_activate_purchase(*, database_url: str, reference: str) -> Dict[str, Any]:
    verification = verify_paystack_transaction(reference)

    if not verification.get("verified"):
        return {
            "ok": False,
            "activated": False,
            "message": "Payment was not successful.",
            "verification": verification,
        }

    purchase = mark_purchase_paid(database_url, provider_reference=reference)

    if not purchase:
        return {
            "ok": False,
            "activated": False,
            "message": "Payment verified, but no matching purchase was found.",
            "verification": verification,
        }

    attached = _attach_preview_job_to_purchase(
        database_url,
        purchase,
        source="callback",
    )

    return {
        "ok": True,
        "activated": True,
        "purchase": attached.get("purchase") or purchase,
        "preview_job_attached": attached,
        "verification": verification,
    }

def verify_paystack_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = _require_secret_key().encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, str(signature or ""))

def handle_paystack_webhook(*, database_url: str, raw_body: bytes, signature: str) -> Dict[str, Any]:
    if not verify_paystack_webhook_signature(raw_body, signature):
        return {
            "ok": False,
            "status_code": 401,
            "message": "Invalid Paystack webhook signature.",
        }

    event = json.loads(raw_body.decode("utf-8"))
    event_type = event.get("event")
    data = event.get("data") or {}
    reference = data.get("reference")

    if event_type == "charge.success" and reference:
        purchase = mark_purchase_paid(database_url, provider_reference=reference)
        attached = {}

        if purchase:
            attached = _attach_preview_job_to_purchase(
                database_url,
                purchase,
                source="webhook",
            )

        return {
            "ok": True,
            "status_code": 200,
            "event": event_type,
            "reference": reference,
            "purchase_activated": bool(purchase),
            "preview_job_attached": attached,
        }

    return {
        "ok": True,
        "status_code": 200,
        "event": event_type,
        "message": "Webhook received. No purchase activation required.",
    }
