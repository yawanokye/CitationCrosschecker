"""access_control.py, email-only paid access helpers for CiteIntegrity."""
from __future__ import annotations
import hashlib, secrets
from typing import Any, Dict, Optional
import psycopg2
from psycopg2.extras import RealDictCursor
from entitlements import (
    ANALYSES_PER_PURCHASE,
    can_use_purchase_for_document,
    get_package,
    normalise_currency,
)

def generate_access_token() -> str:
    return secrets.token_urlsafe(32)

def hash_access_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()

def get_conn(database_url: str):
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)

def init_commercial_tables(database_url: str) -> None:
    from entitlements import PURCHASES_TABLE_SQL
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(PURCHASES_TABLE_SQL)
        conn.commit()

def create_pending_purchase(
    database_url: str,
    *,
    user_email: str,
    tier_key: str,
    currency: str = "GHS",
    provider_reference: str,
    payment_provider: str = "",
    preview_job_id: str = "",
    preview_file_name: str = "",
    preview_reference_count: int = 0,
    preview_citation_count: int = 0,
) -> Dict[str, Any]:
    currency = normalise_currency(currency)
    package = get_package(tier_key, paid=True, currency=currency)
    token = generate_access_token()
    token_hash = hash_access_token(token)

    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO purchases (
                    user_email, payment_type, package_key, document_tier, review_type,
                    amount, currency, status, payment_provider, provider_reference,
                    access_token_hash,
                    preview_job_id, preview_file_name, preview_reference_count, preview_citation_count,
                    analyses_total, analyses_used, expires_at
                )
                VALUES (%s, 'one_off', %s, %s, 'full', %s, %s, 'pending',
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, 0, NOW() + INTERVAL '90 days')
                RETURNING *
                """,
                (
                    user_email,
                    package["package_key"],
                    tier_key,
                    package["amount"],
                    package["currency"],
                    payment_provider,
                    provider_reference,
                    token_hash,
                    preview_job_id,
                    preview_file_name,
                    preview_reference_count,
                    preview_citation_count,
                    package["analysis_runs"],
                ),
            )
            purchase = dict(cursor.fetchone())
        conn.commit()

    purchase["access_token"] = token
    return purchase

def mark_purchase_paid(database_url: str, *, provider_reference: str) -> Optional[Dict[str, Any]]:
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE purchases SET status = 'paid' WHERE provider_reference = %s RETURNING *", (provider_reference,))
            row = cursor.fetchone()
        conn.commit()
    return dict(row) if row else None

def get_purchase_by_token(database_url: str, *, token: str) -> Optional[Dict[str, Any]]:
    token_hash = hash_access_token(token)
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM purchases
                WHERE access_token_hash = %s
                  AND status IN ('paid', 'active')
                  AND (expires_at IS NULL OR expires_at > NOW())
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (token_hash,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None

def get_purchase_for_job(database_url: str, *, job_id: str) -> Optional[Dict[str, Any]]:
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT p.* FROM purchases p
                JOIN purchase_runs pr ON pr.purchase_id = p.id
                WHERE pr.job_id = %s
                LIMIT 1
                """,
                (job_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None

def validate_purchase_for_new_run(database_url: str, *, token: str, reference_count: int, citation_count: int) -> Dict[str, Any]:
    purchase = get_purchase_by_token(database_url, token=token)
    if not purchase:
        return {"allowed": False, "reason": "invalid_or_expired_access", "message": "The review access link is invalid, unpaid, or expired."}
    check = can_use_purchase_for_document(purchase, reference_count, citation_count)
    check["purchase"] = purchase
    return check

def record_purchase_run(
    database_url: str,
    *,
    purchase_id: int,
    job_id: str,
    file_name: str = "",
    reference_count: int = 0,
    citation_count: int = 0,
) -> Dict[str, Any]:
    """
    Attach a job to a paid purchase and consume one analysis run.

    The analysis count increases only when a new job_id is inserted.
    This prevents Paystack callback and webhook from double-counting the same job.
    """
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO purchase_runs (
                    purchase_id, job_id, file_name, reference_count, citation_count
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO NOTHING
                RETURNING *
                """,
                (purchase_id, job_id, file_name, reference_count, citation_count),
            )
            run_row = cursor.fetchone()

            if run_row:
                cursor.execute(
                    """
                    UPDATE purchases
                    SET analyses_used = analyses_used + 1
                    WHERE id = %s
                      AND analyses_used < analyses_total
                    RETURNING *
                    """,
                    (purchase_id,),
                )
                purchase_row = cursor.fetchone()
            else:
                cursor.execute(
                    """
                    SELECT *
                    FROM purchases
                    WHERE id = %s
                    """,
                    (purchase_id,),
                )
                purchase_row = cursor.fetchone()

        conn.commit()

    return {
        "run": dict(run_row) if run_row else None,
        "purchase": dict(purchase_row) if purchase_row else None,
    }

def purchase_is_paid_for_job(database_url: str, *, job_id: str) -> Dict[str, Any]:
    purchase = get_purchase_for_job(database_url, job_id=job_id)
    if not purchase:
        return {"paid": False, "tier_key": "", "currency": "GHS", "purchase": None}
    return {"paid": str(purchase.get("status") or "").lower() in {"paid", "active"}, "tier_key": purchase.get("document_tier") or "", "currency": purchase.get("currency") or "GHS", "purchase": purchase}
