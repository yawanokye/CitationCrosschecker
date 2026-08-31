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
            # Safe upgrades for installations created by an earlier schema.
            for statement in (
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS preview_job_id TEXT",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS preview_file_name TEXT",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS preview_reference_count INTEGER DEFAULT 0",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS preview_citation_count INTEGER DEFAULT 0",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS market TEXT",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS billing_country TEXT",
                "ALTER TABLE purchases ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ",
            ):
                cursor.execute(statement)
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
    market: str = "",
    billing_country: str = "",
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
                    market, billing_country,
                    analyses_total, analyses_used, expires_at
                )
                VALUES (%s, 'one_off', %s, %s, 'full', %s, %s, 'pending',
                        %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, 0, NOW() + (%s * INTERVAL '1 day'))
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
                    str(market or "")[:40],
                    str(billing_country or "")[:8].upper(),
                    package["analysis_runs"],
                    package["validity_days"],
                ),
            )
            purchase = dict(cursor.fetchone())
        conn.commit()

    purchase["access_token"] = token
    return purchase

def mark_purchase_paid(database_url: str, *, provider_reference: str) -> Optional[Dict[str, Any]]:
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE purchases SET status = 'paid', paid_at = COALESCE(paid_at, NOW()) WHERE provider_reference = %s RETURNING *", (provider_reference,))
            row = cursor.fetchone()
        conn.commit()
    return dict(row) if row else None


def get_purchase_by_provider_reference(database_url: str, *, provider_reference: str) -> Optional[Dict[str, Any]]:
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM purchases WHERE provider_reference = %s LIMIT 1", (provider_reference,))
            row = cursor.fetchone()
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
                  AND p.status IN ('paid', 'active')
                  AND (p.expires_at IS NULL OR p.expires_at > NOW())
                LIMIT 1
                """,
                (job_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row else None

def validate_purchase_for_new_run(database_url: str, *, token: str, reference_count: int, citation_count: int, word_count: int = 0) -> Dict[str, Any]:
    purchase = get_purchase_by_token(database_url, token=token)
    if not purchase:
        return {"allowed": False, "reason": "invalid_or_expired_access", "message": "The review access link is invalid, unpaid, or expired."}
    check = can_use_purchase_for_document(purchase, reference_count, citation_count, word_count)
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
            cursor.execute("SELECT * FROM purchases WHERE id = %s FOR UPDATE", (purchase_id,))
            locked_purchase = cursor.fetchone()
            if not locked_purchase:
                return {"run": None, "purchase": None, "reason": "purchase_not_found"}
            if int(locked_purchase.get("analyses_used") or 0) >= int(locked_purchase.get("analyses_total") or ANALYSES_PER_PURCHASE):
                return {"run": None, "purchase": dict(locked_purchase), "reason": "no_analysis_credit_remaining"}
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


def attach_paid_purchase_to_preview_job(
    database_url: str,
    *,
    purchase_id: int,
    job_id: str,
    file_name: str = "",
    reference_count: int = 0,
    citation_count: int = 0,
) -> Dict[str, Any]:
    """Idempotently attach a verified paid purchase to its preview job.

    A failed checkout may already have pre-linked the job to a pending purchase.
    In that case the newly paid purchase replaces only that unpaid link. An
    existing paid link is preserved, while the new purchase still records the
    current review as used so duplicate checkout attempts cannot create extra
    rechecks.
    """
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM purchases WHERE id = %s FOR UPDATE", (purchase_id,))
            purchase = cursor.fetchone()
            if not purchase or str(purchase.get("status") or "").lower() not in {"paid", "active"}:
                return {"attached": False, "reason": "purchase_not_paid", "purchase": dict(purchase) if purchase else None}

            cursor.execute(
                """
                SELECT pr.*, p.status AS linked_purchase_status,
                       (p.expires_at IS NULL OR p.expires_at > NOW()) AS linked_purchase_current
                FROM purchase_runs pr
                JOIN purchases p ON p.id = pr.purchase_id
                WHERE pr.job_id = %s
                FOR UPDATE
                """,
                (job_id,),
            )
            existing = cursor.fetchone()
            attached = False
            if existing:
                existing_paid = (
                    str(existing.get("linked_purchase_status") or "").lower() in {"paid", "active"}
                    and bool(existing.get("linked_purchase_current"))
                )
                same_purchase = str(existing.get("purchase_id")) == str(purchase_id)
                if same_purchase or not existing_paid:
                    cursor.execute(
                        """
                        UPDATE purchase_runs
                        SET purchase_id=%s, file_name=%s, reference_count=%s, citation_count=%s
                        WHERE job_id=%s RETURNING *
                        """,
                        (purchase_id, file_name, reference_count, citation_count, job_id),
                    )
                    run = cursor.fetchone()
                    attached = True
                else:
                    run = existing
            else:
                cursor.execute(
                    """
                    INSERT INTO purchase_runs(purchase_id, job_id, file_name, reference_count, citation_count)
                    VALUES (%s, %s, %s, %s, %s) RETURNING *
                    """,
                    (purchase_id, job_id, file_name, reference_count, citation_count),
                )
                run = cursor.fetchone()
                attached = True

            cursor.execute(
                """
                UPDATE purchases
                SET analyses_used = LEAST(GREATEST(COALESCE(analyses_used, 0), 1), COALESCE(analyses_total, 2))
                WHERE id = %s RETURNING *
                """,
                (purchase_id,),
            )
            updated_purchase = cursor.fetchone()
        conn.commit()
    return {
        "attached": attached,
        "run": dict(run) if run else None,
        "purchase": dict(updated_purchase) if updated_purchase else dict(purchase),
        "reason": "attached" if attached else "job_already_covered_by_paid_purchase",
    }

def purchase_is_paid_for_job(database_url: str, *, job_id: str) -> Dict[str, Any]:
    purchase = get_purchase_for_job(database_url, job_id=job_id)
    if not purchase:
        return {"paid": False, "tier_key": "", "currency": "GHS", "purchase": None}
    return {"paid": str(purchase.get("status") or "").lower() in {"paid", "active"}, "tier_key": purchase.get("document_tier") or "", "currency": purchase.get("currency") or "GHS", "purchase": purchase}
