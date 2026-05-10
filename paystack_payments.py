"""paystack_payments.py, Paystack helpers with applicant currency choice, GHS or USD."""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, urllib.error, urllib.parse, urllib.request
from typing import Any, Dict, Optional
from entitlements import DEFAULT_CURRENCY, get_price, normalise_currency, validate_paid_package_for_document
from access_control import create_pending_purchase, mark_purchase_paid

PAYSTACK_BASE_URL = "https://api.paystack.co"
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

class PaystackError(Exception):
    pass

def _require_secret_key() -> str:
    if not PAYSTACK_SECRET_KEY:
        raise PaystackError("PAYSTACK_SECRET_KEY is not configured.")
    return PAYSTACK_SECRET_KEY

def amount_to_subunit(amount: float) -> int:
    return int(round(float(amount) * 100))

def get_paystack_charge_amount(tier_key: str, currency: str = DEFAULT_CURRENCY) -> Dict[str, Any]:
    currency = normalise_currency(currency)
    price = get_price(tier_key, currency)
    return {"amount": price["amount"], "currency": currency, "amount_subunit": amount_to_subunit(price["amount"]), "display": price["display"]}

def _paystack_request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    secret = _require_secret_key()
    url = f"{PAYSTACK_BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {secret}", "Content-Type": "application/json", "Accept": "application/json"}
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise PaystackError(f"Paystack HTTP error {e.code}: {raw}") from e
    except Exception as e:
        raise PaystackError(f"Paystack request failed: {e}") from e

def initialize_citeintegrity_payment(*, database_url: str, user_email: str, tier_key: str, reference_count: int, citation_count: int, selected_currency: str = DEFAULT_CURRENCY, callback_path: str = "/payment/paystack/callback") -> Dict[str, Any]:
    selected_currency = normalise_currency(selected_currency)
    tier_check = validate_paid_package_for_document(tier_key, reference_count, citation_count)
    if not tier_check.get("allowed"):
        return {"ok": False, "error": tier_check.get("message"), "tier_check": tier_check}
    charge = get_paystack_charge_amount(tier_key, selected_currency)
    provider_reference = f"CI-{secrets.token_urlsafe(16).replace('_', '').replace('-', '')}"
    purchase = create_pending_purchase(database_url, user_email=user_email, tier_key=tier_key, currency=selected_currency, provider_reference=provider_reference, payment_provider="paystack")
    metadata = {
        "product": "CiteIntegrity",
        "purchase_id": purchase["id"],
        "tier_key": tier_key,
        "package_key": purchase["package_key"],
        "reference_count": reference_count,
        "citation_count": citation_count,
        "analysis_runs": purchase["analyses_total"],
        "selected_currency": selected_currency,
    }
    payload = {
        "email": user_email,
        "amount": str(charge["amount_subunit"]),
        "currency": charge["currency"],
        "reference": provider_reference,
        "callback_url": f"{APP_BASE_URL}{callback_path}",
        "metadata": metadata,
    }
    response = _paystack_request("POST", "/transaction/initialize", payload)
    if not response.get("status"):
        return {"ok": False, "error": response.get("message", "Paystack initialization failed."), "paystack_response": response}
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

def verify_and_activate_purchase(*, database_url: str, reference: str) -> Dict[str, Any]:
    verification = verify_paystack_transaction(reference)
    if not verification.get("verified"):
        return {"ok": False, "activated": False, "message": "Payment was not successful.", "verification": verification}
    purchase = mark_purchase_paid(database_url, provider_reference=reference)
    if not purchase:
        return {"ok": False, "activated": False, "message": "Payment verified, but no matching purchase was found.", "verification": verification}
    return {"ok": True, "activated": True, "purchase": purchase, "verification": verification}

def verify_paystack_webhook_signature(raw_body: bytes, signature: str) -> bool:
    secret = _require_secret_key().encode("utf-8")
    digest = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, str(signature or ""))

def handle_paystack_webhook(*, database_url: str, raw_body: bytes, signature: str) -> Dict[str, Any]:
    if not verify_paystack_webhook_signature(raw_body, signature):
        return {"ok": False, "status_code": 401, "message": "Invalid Paystack webhook signature."}
    event = json.loads(raw_body.decode("utf-8"))
    event_type = event.get("event")
    data = event.get("data") or {}
    reference = data.get("reference")
    if event_type == "charge.success" and reference:
        purchase = mark_purchase_paid(database_url, provider_reference=reference)
        return {"ok": True, "status_code": 200, "event": event_type, "reference": reference, "purchase_activated": bool(purchase)}
    return {"ok": True, "status_code": 200, "event": event_type, "message": "Webhook received. No purchase activation required."}
