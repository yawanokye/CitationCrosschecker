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
import psycopg2
from psycopg2.extras import RealDictCursor

from access_control import create_pending_purchase, mark_purchase_paid, record_purchase_run
from entitlements import get_price, validate_paid_package_for_document

STRIPE_PAYMENTS_VERSION = "1.0.1"
STRIPE_PAYMENTS_BUILD = "commercial-2026-05-27-africa-paystack-global-stripe-diagnostics"

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


def _attach_preview_job_to_purchase(
    database_url: str,
    purchase: Dict[str, Any],
    source: str = "stripe",
    fallback_job_id: str = "",
    fallback_file_name: str = "",
    fallback_reference_count: int = 0,
    fallback_citation_count: int = 0,
) -> Dict[str, Any]:
    """
    Force-link the paid purchase to the preview job.

    Why this is stronger than record_purchase_run() alone:
    - purchase_runs.job_id is UNIQUE.
    - If an earlier pending/failed payment already inserted the same job_id,
      ON CONFLICT DO NOTHING can leave the current paid Stripe purchase detached.
    - The results page unlocks only when purchase_is_paid_for_job(job_id) can
      join purchase_runs -> purchases and find a paid purchase.

    This helper therefore inserts the run when missing, or re-points an existing
    run for the same job_id to the newly paid Stripe purchase.
    """
    if not purchase:
        return {"attached": False, "reason": "no_purchase", "source": source}

    purchase_id = purchase.get("id")
    job_id = (
        str(purchase.get("preview_job_id") or "").strip()
        or str(fallback_job_id or "").strip()
    )

    if not purchase_id:
        return {"attached": False, "reason": "no_purchase_id", "source": source}

    if not job_id:
        return {"attached": False, "reason": "no_preview_job_id", "source": source}

    file_name = (
        str(purchase.get("preview_file_name") or "").strip()
        or str(fallback_file_name or "").strip()
    )

    reference_count = _safe_int(
        purchase.get("preview_reference_count"),
        _safe_int(fallback_reference_count, 0),
    )
    citation_count = _safe_int(
        purchase.get("preview_citation_count"),
        _safe_int(fallback_citation_count, 0),
    )

    try:
        with psycopg2.connect(database_url, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM purchase_runs WHERE job_id = %s FOR UPDATE",
                    (job_id,),
                )
                existing_run = cursor.fetchone()

                linked_or_relinked = False

                if existing_run:
                    existing_purchase_id = existing_run.get("purchase_id")

                    if str(existing_purchase_id) != str(purchase_id):
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
                                reference_count,
                                reference_count,
                                citation_count,
                                citation_count,
                                job_id,
                            ),
                        )
                        run_row = cursor.fetchone()
                        linked_or_relinked = True
                    else:
                        run_row = existing_run
                else:
                    cursor.execute(
                        """
                        INSERT INTO purchase_runs
                            (purchase_id, job_id, file_name, reference_count, citation_count)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (purchase_id, job_id, file_name, reference_count, citation_count),
                    )
                    run_row = cursor.fetchone()
                    linked_or_relinked = True

                if linked_or_relinked:
                    cursor.execute(
                        """
                        UPDATE purchases
                        SET analyses_used = LEAST(
                            COALESCE(analyses_used, 0) + 1,
                            COALESCE(analyses_total, 2)
                        )
                        WHERE id = %s
                        RETURNING *
                        """,
                        (purchase_id,),
                    )
                    purchase_row = cursor.fetchone()
                else:
                    cursor.execute("SELECT * FROM purchases WHERE id = %s", (purchase_id,))
                    purchase_row = cursor.fetchone()

            conn.commit()

        print(
            f"[STRIPE_FORCE_ATTACH] source={source}, job_id={job_id}, "
            f"purchase_id={purchase_id}, attached=True, relinked={linked_or_relinked}"
        )

        return {
            "attached": True,
            "source": source,
            "job_id": job_id,
            "run": dict(run_row) if run_row else None,
            "purchase": dict(purchase_row) if purchase_row else purchase,
            "relinked_or_inserted": linked_or_relinked,
        }

    except Exception as e:
        print(f"[STRIPE_FORCE_ATTACH_ERROR] source={source}, job_id={job_id}: {type(e).__name__}: {e}")

        # Last resort: use existing access_control helper. This preserves the
        # original behaviour if direct SQL fails for any environment-specific reason.
        try:
            attached = record_purchase_run(
                database_url,
                purchase_id=purchase_id,
                job_id=job_id,
                file_name=file_name,
                reference_count=reference_count,
                citation_count=citation_count,
            )
            return {
                "attached": bool(attached.get("run") or attached.get("purchase")),
                "source": f"{source}_fallback_record_purchase_run",
                "job_id": job_id,
                "run": attached.get("run"),
                "purchase": attached.get("purchase") or purchase,
                "fallback": True,
            }
        except Exception as inner:
            print(f"[STRIPE_FORCE_ATTACH_FALLBACK_ERROR] {type(inner).__name__}: {inner}")
            return {
                "attached": False,
                "reason": f"{type(e).__name__}: {str(e)}",
                "fallback_reason": f"{type(inner).__name__}: {str(inner)}",
                "source": source,
                "job_id": job_id,
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
    fallback_job_id: str = "",
) -> Dict[str, Any]:
    """
    Paystack-style fallback activation for Stripe Checkout.

    Stripe webhooks remain the primary production confirmation path. However,
    this helper safely verifies the Checkout Session directly with Stripe on the
    success page and then activates the same purchase record. This prevents the
    user from paying successfully but remaining locked when webhook delivery is
    delayed or misconfigured during testing.
    """
    if not session_id:
        return {
            "ok": False,
            "activated": False,
            "message": "Stripe session_id is missing.",
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

        job_id = (metadata.get("job_id", "") or str(fallback_job_id or "")).strip()
        payment_status = str(session.get("payment_status") or "").lower()

        if payment_status not in {"paid", "no_payment_required"}:
            print(
                f"[STRIPE_SUCCESS_VERIFY] Session not paid: "
                f"session_id={session_id}, status={payment_status}, reference={provider_reference}"
            )
            return {
                "ok": False,
                "activated": False,
                "message": f"Stripe session payment status is {payment_status}.",
                "job_id": job_id,
                "provider_reference": provider_reference,
                "payment_status": payment_status,
            }

        if not provider_reference:
            print(f"[STRIPE_SUCCESS_VERIFY] Missing provider_reference for session_id={session_id}")
            return {
                "ok": False,
                "activated": False,
                "message": "Stripe provider reference was not found in the session.",
                "job_id": job_id,
                "payment_status": payment_status,
            }

        purchase = mark_purchase_paid(
            database_url,
            provider_reference=provider_reference,
        )

        if not purchase:
            print(
                f"[STRIPE_SUCCESS_VERIFY] Payment verified but no pending purchase found: "
                f"session_id={session_id}, reference={provider_reference}, job_id={job_id}"
            )
            return {
                "ok": False,
                "activated": False,
                "message": "Payment verified, but no matching purchase was found.",
                "job_id": job_id,
                "provider_reference": provider_reference,
                "payment_status": payment_status,
            }

        # If the deployed access_control table does not carry preview_job_id,
        # use the verified Stripe session metadata or query-string job_id.
        try:
            if job_id and not purchase.get("preview_job_id"):
                purchase = dict(purchase)
                purchase["preview_job_id"] = job_id
        except Exception:
            pass

        attached = _attach_preview_job_to_purchase(
            database_url,
            purchase,
            source="stripe_success_page",
            fallback_job_id=job_id,
            fallback_file_name=metadata.get("file_name", "") or "",
            fallback_reference_count=_safe_int(metadata.get("reference_count"), 0),
            fallback_citation_count=_safe_int(metadata.get("citation_count"), 0),
        )

        activated_purchase = attached.get("purchase") or purchase
        final_job_id = job_id or activated_purchase.get("preview_job_id", "") or ""

        print(
            f"[STRIPE_SUCCESS_VERIFY] activated=True, reference={provider_reference}, "
            f"job_id={final_job_id}, attached={attached.get('attached')}"
        )

        return {
            "ok": True,
            "activated": True,
            "message": "Stripe payment verified and purchase activated.",
            "job_id": final_job_id,
            "provider_reference": provider_reference,
            "payment_status": payment_status,
            "purchase": activated_purchase,
            "preview_job_attached": attached,
        }

    except Exception as e:
        error_payload = _stripe_error_payload(e)
        gateway_error = _stringify_gateway_error(error_payload)
        print(f"[STRIPE_SUCCESS_VERIFY_ERROR] {gateway_error}")
        return {
            "ok": False,
            "activated": False,
            "message": gateway_error,
            "gateway_error": gateway_error,
            "gateway_error_details": error_payload,
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

        metadata = data.get("metadata", {}) or {}
        attached = {}
        if purchase:
            try:
                if metadata.get("job_id") and not purchase.get("preview_job_id"):
                    purchase = dict(purchase)
                    purchase["preview_job_id"] = metadata.get("job_id")
            except Exception:
                pass

            attached = _attach_preview_job_to_purchase(
                database_url,
                purchase,
                source="stripe_webhook",
                fallback_job_id=metadata.get("job_id", "") or "",
                fallback_file_name=metadata.get("file_name", "") or "",
                fallback_reference_count=_safe_int(metadata.get("reference_count"), 0),
                fallback_citation_count=_safe_int(metadata.get("citation_count"), 0),
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
