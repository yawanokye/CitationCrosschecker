"""stripe_payments.py
Stripe Checkout helpers for CiteIntegrity.

Payment model:
- Every billing country except Ghana and Nigeria is routed here.
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
import psycopg2
from psycopg2.extras import RealDictCursor

from access_control import (create_pending_purchase, get_purchase_by_provider_reference,
                            mark_purchase_paid, attach_paid_purchase_to_preview_job,
                            purchase_is_paid_for_job)
from entitlements import get_price, validate_paid_package_for_document

STRIPE_PAYMENTS_VERSION = "2.0.17"
STRIPE_PAYMENTS_BUILD = "payment-report-recovery"

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


def _stripe_payment_matches_purchase(session: Any, purchase: Dict[str, Any]) -> bool:
    if not purchase:
        return False
    actual_minor = _safe_int(session.get("amount_total"), 0)
    expected_minor = amount_to_minor_units(float(purchase.get("amount") or 0))
    actual_currency = str(session.get("currency") or "").upper()
    expected_currency = str(purchase.get("currency") or "").upper()
    # customer_details.email can be edited during Checkout. Bind to the original
    # server-supplied customer_email, reference and purchase ID instead.
    actual_email = str(session.get("customer_email") or "").strip().lower()
    expected_email = str(purchase.get("user_email") or "").strip().lower()
    metadata = session.get("metadata") or {}
    references = [session.get("client_reference_id"), metadata.get("provider_reference")]
    expected_reference = str(purchase.get("provider_reference") or "")
    return (
        str(purchase.get("payment_provider") or "").lower() == "stripe"
        and actual_minor == expected_minor and expected_minor > 0
        and actual_currency == expected_currency
        and (not actual_email or actual_email == expected_email)
        and bool(expected_reference) and any(references)
        and all(not ref or str(ref) == expected_reference for ref in references)
        and (not metadata.get("purchase_id") or str(metadata["purchase_id"]) == str(purchase["id"]))
        and (not purchase.get("stripe_session_id") or session.get("id") == purchase["stripe_session_id"])
    )


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



def _row_to_dict(row: Any) -> Dict[str, Any]:
    """Return a plain dict for psycopg2 RealDictRow or mapping rows."""
    try:
        return dict(row) if row else {}
    except Exception:
        return row or {}


def _prelink_pending_purchase_to_job(
    database_url: str,
    *,
    purchase: Dict[str, Any],
    job_id: str,
    file_name: str = "",
    reference_count: int = 0,
    citation_count: int = 0,
) -> Dict[str, Any]:
    """
    Link the preview job to the pending Stripe purchase before redirecting to Stripe.

    This is the key Stripe unlock fix. Paystack returns through a callback with the
    original provider reference, but Stripe success/webhook delivery may be delayed
    or may not be able to recover preview_job_id from the database schema. By
    pre-linking purchase_runs while the purchase is still pending, the result
    unlocks immediately once the same purchase row is marked paid.

    Safety: a pending purchase link does not unlock anything because access checks
    still require purchases.status IN ('paid', 'active').
    """
    purchase_id = (purchase or {}).get("id")
    job_id = str(job_id or "").strip()

    if not (database_url and purchase_id and job_id):
        return {
            "ok": False,
            "linked": False,
            "reason": "missing_database_purchase_or_job_id",
            "purchase_id": purchase_id,
            "job_id": job_id,
        }

    try:
        with psycopg2.connect(database_url, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pr.*, p.status AS linked_purchase_status
                    FROM purchase_runs pr
                    LEFT JOIN purchases p ON p.id = pr.purchase_id
                    WHERE pr.job_id = %s
                    FOR UPDATE
                    """,
                    (job_id,),
                )
                existing = cursor.fetchone()

                if existing:
                    existing_status = str(existing.get("linked_purchase_status") or "").lower()
                    existing_purchase_id = existing.get("purchase_id")

                    # Never overwrite an already paid/active link. That may belong to a
                    # previously successful purchase for the same result.
                    if existing_status in {"paid", "active"}:
                        conn.commit()
                        return {
                            "ok": True,
                            "linked": True,
                            "action": "kept_existing_paid_link",
                            "purchase_id": existing_purchase_id,
                            "job_id": job_id,
                        }

                    cursor.execute(
                        """
                        UPDATE purchase_runs
                        SET purchase_id = %s,
                            file_name = COALESCE(NULLIF(%s, ''), file_name),
                            reference_count = CASE WHEN %s > 0 THEN %s ELSE reference_count END,
                            citation_count = CASE WHEN %s > 0 THEN %s ELSE citation_count END
                        WHERE job_id = %s
                        RETURNING *
                        """,
                        (
                            purchase_id,
                            file_name,
                            _safe_int(reference_count, 0),
                            _safe_int(reference_count, 0),
                            _safe_int(citation_count, 0),
                            _safe_int(citation_count, 0),
                            job_id,
                        ),
                    )
                    run = cursor.fetchone()
                    action = "updated_existing_pending_link"
                else:
                    cursor.execute(
                        """
                        INSERT INTO purchase_runs
                            (purchase_id, job_id, file_name, reference_count, citation_count)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            purchase_id,
                            job_id,
                            file_name,
                            _safe_int(reference_count, 0),
                            _safe_int(citation_count, 0),
                        ),
                    )
                    run = cursor.fetchone()
                    action = "inserted_pending_link"

            conn.commit()

        print(f"[STRIPE_PRELINK] action={action}, job_id={job_id}, purchase_id={purchase_id}")
        return {
            "ok": True,
            "linked": True,
            "action": action,
            "purchase_id": purchase_id,
            "job_id": job_id,
            "run": _row_to_dict(run),
        }

    except Exception as e:
        print(f"[STRIPE_PRELINK_ERROR] job_id={job_id}, purchase_id={purchase_id}: {type(e).__name__}: {e}")
        return {
            "ok": False,
            "linked": False,
            "reason": f"{type(e).__name__}: {str(e)}",
            "purchase_id": purchase_id,
            "job_id": job_id,
        }


def _mark_stripe_purchase_paid(database_url: str, *, provider_reference: str, purchase_id: Any = None) -> Dict[str, Any]:
    # Call only after the verified session is matched against the ledger.
    return mark_purchase_paid(database_url, provider_reference=provider_reference) or {}


def _attach_preview_job_to_purchase(database_url: str, purchase: Dict[str, Any], source: str = "stripe", **fallbacks) -> Dict[str, Any]:
    job_id = str(purchase.get("preview_job_id") or fallbacks.get("fallback_job_id") or "")
    if not job_id:
        return {"attached": False, "reason": "no_preview_job_id"}
    linked = attach_paid_purchase_to_preview_job(
        database_url, purchase_id=purchase["id"], job_id=job_id,
        file_name=str(purchase.get("preview_file_name") or fallbacks.get("fallback_file_name") or ""),
        reference_count=_safe_int(purchase.get("preview_reference_count"), 0),
        citation_count=_safe_int(purchase.get("preview_citation_count"), 0),
    )
    # A database write is not proof of usable access. Check the actual entitlement.
    linked["attached"] = bool(purchase_is_paid_for_job(database_url, job_id=job_id).get("paid"))
    linked["job_id"] = job_id
    return linked


def _fulfill_verified_session(database_url: str, session: Any, expected_job_id: str = "") -> Dict[str, Any]:
    metadata = session.get("metadata") or {}
    reference = str(session.get("client_reference_id") or metadata.get("provider_reference") or "")
    payment_status = str(session.get("payment_status") or "").lower()
    pending = get_purchase_by_provider_reference(database_url, provider_reference=reference) if reference else {}
    pending = pending or {}
    ledger_job = str(pending.get("preview_job_id") or "")
    metadata_job = str(metadata.get("job_id") or "")
    # A browser query may constrain a verified job, but can never supply its identity.
    job_id = ledger_job or metadata_job
    if not pending or not _stripe_payment_matches_purchase(session, pending):
        return {"ok": False, "activated": False, "status_code": 409, "job_id": "",
                "message": "Payment could not be matched to this purchase. Contact support with your receipt."}
    if not job_id or (ledger_job and metadata_job and ledger_job != metadata_job) or (expected_job_id and job_id != expected_job_id):
        return {"ok": False, "activated": False, "status_code": 409, "job_id": "",
                "message": "This checkout does not match the requested analysis."}
    if payment_status != "paid":
        return {"ok": True, "activated": False, "pending": True, "status_code": 200,
                "job_id": job_id, "payment_status": payment_status,
                "message": "Payment has not been confirmed. If you were charged, do not pay again. Use Restore access."}
    purchase = _mark_stripe_purchase_paid(database_url, provider_reference=reference, purchase_id=pending["id"])
    if not purchase:
        return {"ok": False, "activated": False, "status_code": 503, "job_id": job_id,
                "message": "Your payment is confirmed. Report access is being restored. Please do not pay again."}
    # Persist metadata-only legacy job IDs so the notification worker can find them.
    if not purchase.get("preview_job_id"):
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE purchases SET preview_job_id=%s WHERE id=%s AND COALESCE(preview_job_id, '')=''", (job_id, purchase["id"]))
            conn.commit()
        purchase = dict(purchase, preview_job_id=job_id)
    attached = _attach_preview_job_to_purchase(database_url, purchase)
    activated = bool(attached.get("attached"))
    return {"ok": activated, "activated": activated, "status_code": 200 if activated else 503,
            "job_id": job_id, "provider_reference": reference, "payment_status": payment_status,
            "purchase": attached.get("purchase") or purchase, "preview_job_attached": attached,
            "message": "Payment confirmed. Full Review access restored." if activated else
                       "Your payment is confirmed. Report access is being restored. Please do not pay again."}


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
    billing_country: str = "",
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
            market="international",
            billing_country=billing_country,
        )
    except Exception as e:
        print(f"[STRIPE_PENDING_PURCHASE_ERROR] {type(e).__name__}: {e}")
        return {
            "ok": False,
            "error": "Could not create a pending Stripe purchase.",
            "gateway_error": f"{type(e).__name__}: {str(e)}",
        }

    prelink = _prelink_pending_purchase_to_job(
        database_url,
        purchase=purchase,
        job_id=job_id,
        file_name=file_name,
        reference_count=reference_count,
        citation_count=citation_count,
    )

    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            customer_email=user_email,
            payment_intent_data={"receipt_email": user_email},
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
            cancel_url=f"{APP_BASE_URL}/results/{job_id}?payment=cancelled" if job_id else f"{APP_BASE_URL}{cancel_path}?cancelled=1",
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
                "billing_country": str(billing_country or "").upper(),
            },
        )
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE purchases SET stripe_session_id=%s WHERE id=%s", (session.id, purchase["id"]))
            conn.commit()
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
        "prelinked": prelink,
    }



def verify_and_activate_stripe_session(*, database_url: str, session_id: str, fallback_job_id: str = "") -> Dict[str, Any]:
    """Reconcile a server-retrieved session from return, recovery or support."""
    if not session_id:
        return {"ok": False, "activated": False, "status_code": 400, "job_id": "", "message": "Checkout session is missing."}
    try:
        _require_stripe_key()
        session = stripe.checkout.Session.retrieve(session_id)
        return _fulfill_verified_session(database_url, session, expected_job_id=fallback_job_id)
    except Exception as exc:
        print(f"[STRIPE_RECOVERY_DEFERRED] {type(exc).__name__}")
        return {"ok": False, "activated": False, "status_code": 503, "job_id": "",
                "message": "Payment verification is temporarily unavailable. If you were charged, do not pay again. Try Restore access shortly."}


def handle_stripe_webhook(*, database_url: str, raw_body: bytes, signature: str) -> Dict[str, Any]:
    if not STRIPE_WEBHOOK_SECRET:
        return {"ok": False, "status_code": 503, "message": "Payment verification is unavailable."}
    try:
        event = stripe.Webhook.construct_event(raw_body, signature, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.SignatureVerificationError):
        return {"ok": False, "status_code": 400, "message": "Invalid Stripe webhook signature or payload."}
    event_type = event.get("type")
    if event_type not in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
        return {"ok": True, "status_code": 200, "event": event_type}
    try:
        outcome = _fulfill_verified_session(database_url, event.get("data", {}).get("object", {}))
        # A signed event that failed fulfillment must be retried by Stripe.
        # An unpaid completed session is acknowledged, then async success fulfills it.
        if not outcome.get("ok"):
            outcome["status_code"] = 503
        outcome["event"] = event_type
        outcome["purchase_activated"] = bool(outcome.get("activated"))
        return outcome
    except Exception as exc:
        print(f"[STRIPE_WEBHOOK_RETRY] {type(exc).__name__}")
        return {"ok": False, "activated": False, "status_code": 503,
                "message": "Payment fulfillment will be retried."}
