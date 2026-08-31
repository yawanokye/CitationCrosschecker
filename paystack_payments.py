"""Paystack helpers for CiteIntegrity's Ghana and Nigeria markets."""
from __future__ import annotations
import hashlib, hmac, json, os, re, secrets, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, Optional
from entitlements import DEFAULT_CURRENCY, get_price, validate_paid_package_for_document
from access_control import (
    attach_paid_purchase_to_preview_job,
    create_pending_purchase,
    get_purchase_by_provider_reference,
    mark_purchase_paid,
)

PAYSTACK_PAYMENTS_VERSION = "2.0.0"
PAYSTACK_PAYMENTS_BUILD = "commercial-local-ghs-ngn-direct-charge"

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_GH_SECRET_KEY = os.environ.get("PAYSTACK_GH_SECRET_KEY", PAYSTACK_SECRET_KEY).strip()
PAYSTACK_NG_SECRET_KEY = os.environ.get(
    "PAYSTACK_NG_SECRET_KEY", PAYSTACK_SECRET_KEY or PAYSTACK_GH_SECRET_KEY
).strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")
PAYSTACK_CALLBACK_URL = os.environ.get("PAYSTACK_CALLBACK_URL", f"{APP_BASE_URL}/payment/callback").strip()
PAYSTACK_REFERENCE_PREFIX = re.sub(
    r"[^A-Z0-9]", "", os.environ.get("PAYSTACK_REFERENCE_PREFIX", "CIT").strip().upper()
)[:12] or "CIT"
PAYSTACK_SOURCE_APP = os.environ.get("PAYSTACK_SOURCE_APP", "citeintegrity").strip().lower() or "citeintegrity"
PAYSTACK_CONFIRMATION_SECRET = os.environ.get("PAYSTACK_CONFIRMATION_SECRET", "").strip()

PAYSTACK_USER_AGENT = os.environ.get(
    "PAYSTACK_USER_AGENT",
    "CiteIntegrity/1.0 (+https://citeintegrity.org; payments@citeintegrity.org)"
).strip()

class PaystackError(Exception):
    pass


def is_citeintegrity_reference(reference: str) -> bool:
    return str(reference or "").strip().upper().startswith(f"{PAYSTACK_REFERENCE_PREFIX}-")


def verify_central_confirmation_signature(raw_body: bytes, signature: str = "", authorization: str = "") -> bool:
    """Authenticate ProjectReady's server-to-server payment confirmation."""
    if not PAYSTACK_CONFIRMATION_SECRET:
        return False
    expected = hmac.new(
        PAYSTACK_CONFIRMATION_SECRET.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    supplied = str(signature or "").strip().lower()
    if supplied.startswith("sha256="):
        supplied = supplied[7:]
    signature_ok = bool(supplied) and hmac.compare_digest(expected, supplied)
    bearer = str(authorization or "").strip()
    bearer_ok = bearer.lower().startswith("bearer ") and hmac.compare_digest(
        bearer[7:].strip(), PAYSTACK_CONFIRMATION_SECRET
    )
    return signature_ok or bearer_ok

def _normalise_market(market: str) -> str:
    return "nigeria" if str(market or "").strip().lower() in {"ng", "nga", "nigeria"} else "ghana"


def _require_secret_key(market: str = "ghana") -> str:
    market = _normalise_market(market)
    secret = PAYSTACK_NG_SECRET_KEY if market == "nigeria" else PAYSTACK_GH_SECRET_KEY
    if not secret:
        variable = "PAYSTACK_NG_SECRET_KEY" if market == "nigeria" else "PAYSTACK_GH_SECRET_KEY"
        raise PaystackError(f"{variable} is not configured.")
    return secret

def amount_to_subunit(amount: float) -> int:
    return int(round(float(amount) * 100))

def get_paystack_charge_amount(tier_key: str, currency: str = DEFAULT_CURRENCY, market: str = "ghana") -> Dict[str, Any]:
    """Return the fixed local-market amount sent to Paystack."""
    market = _normalise_market(market)
    charged_currency = "NGN" if market == "nigeria" else "GHS"
    price = get_price(tier_key, charged_currency)
    charged_amount = float(price["amount"])
    return {
        "amount": charged_amount,
        "currency": charged_currency,
        "amount_subunit": amount_to_subunit(charged_amount),
        "display": price["display"],
        "selected_currency": charged_currency,
        "selected_amount": charged_amount,
        "selected_display": price["display"],
        "charged_currency": charged_currency,
        "charged_amount": charged_amount,
        "charged_display": price["display"],
        "exchange_rate": None,
        "conversion_note": None,
        "payment_model": f"paystack_{charged_currency.lower()}_direct_charge",
        "market": market,
    }

def _paystack_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None, market: str = "ghana") -> Dict[str, Any]:
    secret = _require_secret_key(market)
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
    """Keep the purchase ledger aligned with the exact local Paystack charge."""
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
    market: str = "ghana",
    billing_country: str = "GH",
) -> Dict[str, Any]:
    market = _normalise_market(market)
    selected_currency = "NGN" if market == "nigeria" else "GHS"

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

    charge = get_paystack_charge_amount(tier_key, selected_currency, market)

    order_id = secrets.token_hex(12).upper()
    provider_reference = f"{PAYSTACK_REFERENCE_PREFIX}-{order_id}"

    purchase = create_pending_purchase(
        database_url,
        user_email=user_email,
        tier_key=tier_key,
        currency=charge["currency"],
        provider_reference=provider_reference,
        payment_provider="paystack",
        preview_job_id=job_id,
        preview_file_name=file_name,
        preview_reference_count=reference_count,
        preview_citation_count=citation_count,
        market=market,
        billing_country=billing_country,
    )

    _sync_purchase_charge_amount(
        database_url,
        purchase_id=purchase.get("id"),
        charge=charge,
    )

    metadata = {
        "source_app": PAYSTACK_SOURCE_APP,
        "order_id": order_id,
        "product_code": "full_analysis",
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
        "market": market,
        "billing_country": str(billing_country or "").upper(),
    }

    payload = {
        "email": user_email,
        "amount": str(charge["amount_subunit"]),
        "currency": charge["currency"],
        "reference": provider_reference,
        "callback_url": PAYSTACK_CALLBACK_URL or f"{APP_BASE_URL}{callback_path}",
        "metadata": metadata,
    }

    try:
        response = _paystack_request("POST", "/transaction/initialize", payload, market)
    except PaystackError as e:
        return {
            "ok": False,
            "error": f"Payment could not start in {charge['currency']}. Please try again or confirm that the correct Paystack market key is active.",
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

def verify_paystack_transaction(reference: str, market: str = "ghana") -> Dict[str, Any]:
    reference = urllib.parse.quote(str(reference or "").strip())
    response = _paystack_request("GET", f"/transaction/verify/{reference}", market=market)
    if not response.get("status"):
        return {"ok": False, "verified": False, "message": response.get("message", "Verification failed."), "paystack_response": response}
    data = response.get("data") or {}
    status = str(data.get("status") or "").lower()
    return {"ok": True, "verified": status == "success", "transaction_status": status, "reference": data.get("reference"), "amount": data.get("amount"), "currency": data.get("currency"), "customer_email": ((data.get("customer") or {}).get("email") or ""), "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {}, "paystack_data": data}

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
        attached = attach_paid_purchase_to_preview_job(
            database_url,
            purchase_id=purchase["id"],
            job_id=purchase.get("preview_job_id", ""),
            file_name=purchase.get("preview_file_name", ""),
            reference_count=purchase.get("preview_reference_count", 0),
            citation_count=purchase.get("preview_citation_count", 0),
        )

        return {
            "attached": bool(attached.get("attached")),
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


def _payment_matches_purchase(verification: Dict[str, Any], purchase: Dict[str, Any]) -> Dict[str, Any]:
    expected_minor = amount_to_subunit(float(purchase.get("amount") or 0))
    actual_minor = int(verification.get("amount") or 0)
    expected_currency = str(purchase.get("currency") or "").upper()
    actual_currency = str(verification.get("currency") or "").upper()
    expected_email = str(purchase.get("user_email") or "").strip().lower()
    actual_email = str(verification.get("customer_email") or "").strip().lower()
    expected_reference = str(purchase.get("provider_reference") or "").strip()
    actual_reference = str(verification.get("reference") or "").strip()
    metadata = verification.get("metadata") if isinstance(verification.get("metadata"), dict) else {}
    metadata_source = str(metadata.get("source_app") or "").strip().lower()
    metadata_product = str(metadata.get("product_code") or "").strip().lower()
    reference_matches = bool(expected_reference) and hmac.compare_digest(expected_reference, actual_reference)
    source_matches = not metadata_source or metadata_source == PAYSTACK_SOURCE_APP
    product_matches = not metadata_product or metadata_product == "full_analysis"
    email_matches = not actual_email or expected_email == actual_email
    ok = (
        expected_minor == actual_minor
        and expected_currency == actual_currency
        and email_matches
        and reference_matches
        and source_matches
        and product_matches
    )
    return {
        "ok": ok,
        "expected_amount": expected_minor,
        "actual_amount": actual_minor,
        "expected_currency": expected_currency,
        "actual_currency": actual_currency,
        "email_matches": email_matches,
        "reference_matches": reference_matches,
        "source_matches": source_matches,
        "product_matches": product_matches,
    }


def verify_and_activate_purchase(*, database_url: str, reference: str, market: str = "ghana", source: str = "callback") -> Dict[str, Any]:
    verification = verify_paystack_transaction(reference, market)

    if not verification.get("verified"):
        return {
            "ok": False,
            "activated": False,
            "message": "Payment was not successful.",
            "verification": verification,
        }

    purchase = get_purchase_by_provider_reference(database_url, provider_reference=reference)

    if not purchase:
        return {
            "ok": False,
            "activated": False,
            "message": "Payment verified, but no matching purchase was found.",
            "verification": verification,
        }

    match = _payment_matches_purchase(verification, purchase)
    if not match.get("ok"):
        return {"ok": False, "activated": False, "message": "Verified payment details do not match the pending CiteIntegrity purchase.", "verification": verification, "match": match}

    purchase = mark_purchase_paid(database_url, provider_reference=reference)

    attached = _attach_preview_job_to_purchase(
        database_url,
        purchase,
        source=source,
    )

    return {
        "ok": True,
        "activated": True,
        "purchase": attached.get("purchase") or purchase,
        "preview_job_attached": attached,
        "verification": verification,
    }


def handle_central_payment_confirmation(
    *,
    database_url: str,
    raw_body: bytes,
    signature: str = "",
    authorization: str = "",
) -> Dict[str, Any]:
    """Verify a ProjectReady-routed CiteIntegrity payment and activate it once."""
    if not verify_central_confirmation_signature(raw_body, signature, authorization):
        return {"ok": False, "status_code": 401, "message": "Invalid central confirmation signature."}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return {"ok": False, "status_code": 400, "message": "Invalid confirmation payload."}

    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if isinstance(data.get("metadata"), dict):
        metadata = {**data.get("metadata"), **metadata}
    reference = str(payload.get("reference") or data.get("reference") or "").strip()
    source_app = str(payload.get("source_app") or metadata.get("source_app") or "").strip().lower()
    event = str(payload.get("event") or "charge.success").strip().lower()
    if event != "charge.success":
        return {"ok": True, "status_code": 200, "message": "No activation required for this event.", "event": event}
    if source_app != PAYSTACK_SOURCE_APP or not is_citeintegrity_reference(reference):
        return {"ok": False, "status_code": 400, "message": "Confirmation is not for CiteIntegrity."}

    purchase = get_purchase_by_provider_reference(database_url, provider_reference=reference)
    if not purchase or str(purchase.get("payment_provider") or "").lower() != "paystack":
        return {"ok": False, "status_code": 404, "message": "No matching CiteIntegrity purchase was found."}
    market = _normalise_market(str(purchase.get("market") or "ghana"))
    outcome = verify_and_activate_purchase(
        database_url=database_url,
        reference=reference,
        market=market,
        source="projectready_webhook",
    )
    outcome["status_code"] = 200 if outcome.get("activated") else 400
    return outcome

def verify_paystack_webhook_signature(raw_body: bytes, signature: str, market: str = "ghana") -> bool:
    secret = _require_secret_key(market).encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, str(signature or ""))

def handle_paystack_webhook(*, database_url: str, raw_body: bytes, signature: str, market: str = "ghana") -> Dict[str, Any]:
    if not verify_paystack_webhook_signature(raw_body, signature, market):
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
        pending = get_purchase_by_provider_reference(database_url, provider_reference=reference)
        verification = {
            "reference": reference,
            "amount": data.get("amount"), "currency": data.get("currency"),
            "customer_email": ((data.get("customer") or {}).get("email") or ""),
            "metadata": data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
        }
        match = _payment_matches_purchase(verification, pending or {}) if pending else {"ok": False}
        if not pending or not match.get("ok"):
            return {"ok": False, "status_code": 400, "message": "Webhook payment details do not match a pending purchase.", "reference": reference}
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
