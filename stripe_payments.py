"""stripe_payments.py
Stripe Checkout helpers for CiteIntegrity.

Payment model:
- Non-African billing countries are routed here.
- Checkout uses one-time payment mode, matching CiteIntegrity's package model.
- The Stripe webhook marks the same purchase table as paid and attaches the
  preview job as the first paid analysis run.
"""

from __future__ import annotations

import os
import secrets
from typing import Any, Dict

import stripe

from access_control import create_pending_purchase, mark_purchase_paid, record_purchase_run
from entitlements import get_price, validate_paid_package_for_document

STRIPE_PAYMENTS_VERSION = "1.0.0"
STRIPE_PAYMENTS_BUILD = "commercial-2026-05-27-africa-paystack-global-stripe"

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
        print(f"[STRIPE] Could not attach preview job to purchase from {source}: {e}")
        return {
            "attached": False,
            "reason": str(e),
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
        return {"ok": False, "error": str(e)}

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

    price = get_price(tier_key, selected_currency)
    amount = float(price["amount"])
    amount_minor = amount_to_minor_units(amount)

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
        return {
            "ok": False,
            "error": "Could not create a pending Stripe purchase.",
            "gateway_error": str(e),
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
            success_url=f"{APP_BASE_URL}{success_path}?session_id={{CHECKOUT_SESSION_ID}}",
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
        return {
            "ok": False,
            "error": "Stripe Checkout could not start.",
            "gateway_error": str(e),
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


def handle_stripe_webhook(*, database_url: str, raw_body: bytes, signature: str) -> Dict[str, Any]:
    if not STRIPE_WEBHOOK_SECRET:
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
    except ValueError:
        return {
            "ok": False,
            "status_code": 400,
            "message": "Invalid Stripe webhook payload.",
        }
    except stripe.error.SignatureVerificationError:
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
