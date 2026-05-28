"""stripe_payments.py
Stripe Checkout helpers for CiteIntegrity.

Payment model:
- Non-African billing countries are routed here.
- Checkout uses one-time payment mode, matching CiteIntegrity's package model.
- The Stripe webhook marks the same purchase table as paid and attaches the
  preview job as the first paid analysis run.

This build improves production diagnostics by returning and printing the exact
Stripe error type, code, parameter, request id, and HTTP status when Checkout
creation fails.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict

import stripe

from access_control import create_pending_purchase, mark_purchase_paid, record_purchase_run
from entitlements import get_price, validate_paid_package_for_document

STRIPE_PAYMENTS_VERSION = "1.0.2"
STRIPE_PAYMENTS_BUILD = "commercial-2026-05-28-stripe-success-fallback-activation"

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:8000").rstrip("/")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


class StripePaymentError(Exception):
    pass


def _require_stripe_key() -> str:
    if not STRIPE_SECRET_KEY:
        raise StripePaymentError("STRIPE_SECRET_KEY is not configured.")
    stripe.api_key = STRIPE_SECRET_KEY
    return STRIPE_SECRET_KEY


def amount_to_minor_units(amount: float) -> int:
    return int(round(float(amount) * 100))


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or default)
    except Exception:
        return default


def _stripe_error_payload(error: Exception) -> Dict[str, Any]:
    """
    Convert Stripe and non-Stripe exceptions into a safe JSON payload.

    This is intentionally verbose because Render logs and browser Network
    responses are the quickest way to diagnose live Checkout failures.
    """
    payload: Dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
    }

    for attr in ("user_message", "code", "param", "request_id", "http_status"):
        value = getattr(error, attr, None)
        if value:
            payload[attr] = value

    json_body = getattr(error, "json_body", None)
    if isinstance(json_body, dict):
        stripe_error = json_body.get("error") or {}
        if isinstance(stripe_error, dict):
            payload["stripe_error"] = {
                key: stripe_error.get(key)
                for key in ("type", "code", "decline_code", "param", "message")
                if stripe_error.get(key)
            }

    return payload


def _stringify_gateway_error(payload: Dict[str, Any]) -> str:
    pieces = []
    if payload.get("type"):
        pieces.append(str(payload["type"]))
    if payload.get("message"):
        pieces.append(str(payload["message"]))
    if payload.get("code"):
        pieces.append(f"code={payload['code']}")
    if payload.get("param"):
        pieces.append(f"param={payload['param']}")
    if payload.get("request_id"):
        pieces.append(f"request_id={payload['request_id']}")
    if payload.get("http_status"):
        pieces.append(f"http_status={payload['http_status']}")
    return " | ".join(pieces) or "Unknown Stripe error"


def _attach_preview_job_to_purchase(database_url: str, purchase: Dict[str, Any], source: str = "stripe") -> Dict[str, Any]:
    """
    Attach the paid preview job to purchase_runs and consume one analysis run.
    Safe to call repeatedly because record_purchase_run should enforce job-level uniqueness.
    """
    if not purchase or not purchase.get("preview_job_id"):
        return {
            "attached": False,
            "reason": "no_preview_job_id",
            "source": source,
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
        print(f"[STRIPE] Could not attach preview job to purchase from {source}: {type(e).__name__}: {e}")
        return {
            "attached": False,
            "reason": f"{type(e).__name__}: {str(e)}",
            "source": source,
        }


def initialize_citeintegrity_stripe_payment(
    *,
    database_url: str,
    user_email: str,
    tier_key: str,
    reference_count: int,
    citation_count: int,
    selected_currency: str = "USD",
    job_id: str = "",
    file_name: str = "",
    success_path: str = "/payment/stripe/success",
    cancel_path: str = "/pricing",
) -> Dict[str, Any]:
    try:
        _require_stripe_key()
    except StripePaymentError as e:
        print(f"[STRIPE_CONFIG_ERROR] {type(e).__name__}: {e}")
        return {
            "ok": False,
            "error": str(e),
            "gateway_error": f"{type(e).__name__}: {str(e)}",
        }

    selected_currency = str(selected_currency or "USD").strip().upper()

    # Keep the international Stripe route in USD for launch because
    # entitlements.py already defines fixed USD prices for every package.
    if selected_currency != "USD":
        selected_currency = "USD"

    tier_check = validate_paid_package_for_document(
        tier_key,
        reference_count,
        citation_count,
    )

    if not tier_check.get("allowed"):
        return {
            "ok": False,
            "error": tier_check.get("message"),
            "tier_check": tier_check,
        }

    try:
        price = get_price(tier_key, selected_currency)
        amount = float(price["amount"])
        amount_minor = amount_to_minor_units(amount)
    except Exception as e:
        print(f"[STRIPE_PRICE_ERROR] {type(e).__name__}: {e}")
        return {
            "ok": False,
            "error": "Could not determine Stripe price for the selected package.",
            "gateway_error": f"{type(e).__name__}: {str(e)}",
        }

    provider_reference = f"CI-STRIPE-{secrets.token_urlsafe(16).replace('_', '').replace('-', '')}"

    try:
        purchase = create_pending_purchase(
            database_url,
            user_email=user_email,
            tier_key=tier_key,
            currency=selected_currency,
            provider_reference=provider_reference,
            payment_provider="stripe",
            preview_job_id=job_id,
            preview_file_name=file_name,
            preview_reference_count=reference_count,
            preview_citation_count=citation_count,
        )
    except Exception as e:
        print(f"[STRIPE_PENDING_PURCHASE_ERROR] {type(e).__name__}: {e}")
        return {
            "ok": False,
            "error": "Could not create a pending Stripe purchase.",
            "gateway_error": f"{type(e).__name__}: {str(e)}",
        }

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            customer_email=user_email,
            client_reference_id=provider_reference,
            line_items=[
                {
                    "price_data": {
                        "currency": selected_currency.lower(),
                        "unit_amount": amount_minor,
                        "product_data": {
                            "name": f"CiteIntegrity {tier_key.replace('_', ' ').title()} Full Review",
                            "description": f"Full Integrity Review for {file_name or 'uploaded document'}",
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{APP_BASE_URL}{success_path}?session_id={{CHECKOUT_SESSION_ID}}&job_id={job_id}",
            cancel_url=f"{APP_BASE_URL}{cancel_path}?cancelled=1",
            metadata={
                "product": "CiteIntegrity",
                "purchase_id": str(purchase.get("id")),
                "provider_reference": provider_reference,
                "tier_key": tier_key,
                "job_id": job_id,
                "file_name": file_name,
                "reference_count": str(reference_count),
                "citation_count": str(citation_count),
                "payment_provider": "stripe",
            },
        )
    except Exception as e:
        error_payload = _stripe_error_payload(e)
        gateway_error = _stringify_gateway_error(error_payload)
        print(f"[STRIPE_CHECKOUT_ERROR] {gateway_error}")
        return {
            "ok": False,
            "error": "Stripe Checkout could not start.",
            "gateway_error": gateway_error,
            "gateway_error_details": error_payload,
            "stripe_mode_hint": "Use sk_test_ for test mode and sk_live_ only after live account capabilities are active.",
            "provider_reference": provider_reference,
            "purchase_id": purchase.get("id"),
            "amount": amount,
            "amount_minor": amount_minor,
            "currency": selected_currency,
            "display_amount": price.get("display"),
        }

    return {
        "ok": True,
        "provider": "stripe",
        "checkout_url": session.url,
        "session_id": session.id,
        "reference": provider_reference,
        "purchase_id": purchase.get("id"),
        "amount": amount,
        "amount_minor": amount_minor,
        "currency": selected_currency,
        "display_amount": price["display"],
        "access_token": purchase.get("access_token"),
    }



def verify_and_activate_stripe_session(
    *,
    database_url: str,
    session_id: str,
) -> Dict[str, Any]:
    """
    Fallback activation from the Stripe success page.

    The webhook remains the main trusted route, but this safely verifies the
    session directly with Stripe and unlocks the purchase when the webhook is
    delayed or failing during test setup.
    """
    if not session_id:
        return {
            "ok": False,
            "activated": False,
            "message": "Stripe session ID is missing.",
            "job_id": "",
        }

    try:
        _require_stripe_key()

        session = stripe.checkout.Session.retrieve(session_id)
        metadata = session.get("metadata") or {}

        provider_reference = (
            session.get("client_reference_id")
            or metadata.get("provider_reference")
            or ""
        )

        job_id = metadata.get("job_id", "") or ""
        payment_status = str(session.get("payment_status") or "").lower()

        if payment_status not in {"paid", "no_payment_required"}:
            return {
                "ok": False,
                "activated": False,
                "message": f"Stripe session payment status is {payment_status}.",
                "job_id": job_id,
                "provider_reference": provider_reference,
            }

        if not provider_reference:
            return {
                "ok": False,
                "activated": False,
                "message": "Stripe provider reference was not found in the session.",
                "job_id": job_id,
            }

        purchase = mark_purchase_paid(
            database_url,
            provider_reference=provider_reference,
        )

        attached: Dict[str, Any] = {}
        if purchase:
            attached = _attach_preview_job_to_purchase(
                database_url,
                purchase,
                source="stripe_success_page",
            )

        activated_purchase = attached.get("purchase") or purchase or {}

        return {
            "ok": True,
            "activated": bool(purchase),
            "message": "Stripe payment verified and purchase activated.",
            "job_id": job_id or activated_purchase.get("preview_job_id", ""),
            "provider_reference": provider_reference,
            "purchase": activated_purchase,
            "preview_job_attached": attached,
        }

    except Exception as e:
        print(f"[STRIPE_SUCCESS_VERIFY_ERROR] {type(e).__name__}: {e}")
        return {
            "ok": False,
            "activated": False,
            "message": f"{type(e).__name__}: {str(e)}",
            "job_id": "",
        }


def handle_stripe_webhook(*, database_url: str, raw_body: bytes, signature: str) -> Dict[str, Any]:
    if not STRIPE_WEBHOOK_SECRET:
        print("[STRIPE_WEBHOOK_ERROR] STRIPE_WEBHOOK_SECRET is not configured.")
        return {
            "ok": False,
            "status_code": 500,
            "message": "STRIPE_WEBHOOK_SECRET is not configured.",
        }

    try:
        event = stripe.Webhook.construct_event(
            raw_body,
            signature,
            STRIPE_WEBHOOK_SECRET,
        )
    except ValueError as e:
        print(f"[STRIPE_WEBHOOK_ERROR] Invalid payload: {e}")
        return {
            "ok": False,
            "status_code": 400,
            "message": "Invalid Stripe webhook payload.",
        }
    except stripe.error.SignatureVerificationError as e:
        print(f"[STRIPE_WEBHOOK_ERROR] Invalid signature: {e}")
        return {
            "ok": False,
            "status_code": 400,
            "message": "Invalid Stripe webhook signature.",
        }

    event_type = event.get("type")
    data = event.get("data", {}).get("object", {})

    if event_type == "checkout.session.completed":
        provider_reference = (
            data.get("client_reference_id")
            or data.get("metadata", {}).get("provider_reference")
            or ""
        )

        payment_status = str(data.get("payment_status") or "").lower()
        amount_minor = _safe_int(data.get("amount_total"), 0)
        amount = (amount_minor / 100) if amount_minor else 0
        currency = str(data.get("currency") or "USD").upper()

        if payment_status not in {"paid", "no_payment_required"}:
            print(f"[STRIPE_WEBHOOK_INFO] Checkout completed but payment_status={payment_status}, reference={provider_reference}")
            return {
                "ok": True,
                "status_code": 200,
                "event": event_type,
                "message": f"Checkout completed but payment_status is {payment_status}.",
                "reference": provider_reference,
                "amount": amount,
                "amount_minor": amount_minor,
                "currency": currency,
            }

        purchase = mark_purchase_paid(
            database_url,
            provider_reference=provider_reference,
        )

        attached = {}
        if purchase:
            attached = _attach_preview_job_to_purchase(
                database_url,
                purchase,
                source="stripe_webhook",
            )

        activated_purchase = attached.get("purchase") or purchase

        print(f"[STRIPE_WEBHOOK_SUCCESS] reference={provider_reference}, purchase_activated={bool(purchase)}")
        return {
            "ok": True,
            "status_code": 200,
            "event": event_type,
            "reference": provider_reference,
            "amount": amount,
            "amount_minor": amount_minor,
            "currency": currency,
            "purchase_activated": bool(purchase),
            "preview_job_attached": attached,
            "purchase": activated_purchase,
        }

    return {
        "ok": True,
        "status_code": 200,
        "event": event_type,
        "message": "Stripe webhook received. No purchase activation required.",
    }
