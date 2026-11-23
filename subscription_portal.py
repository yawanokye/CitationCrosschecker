"""Customer portal routes. Accounting lives in subscriptions.py."""
from __future__ import annotations

import hashlib
import hmac
import json
import heapq
import logging
import os
import re
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

import subscriptions as ledger
from entitlements import recommend_document_tier_with_words
from payment_notifications import _send, smtp_ready, mail_error_kind
from paystack_payments import _paystack_request, _require_secret_key

log = logging.getLogger(__name__)
COOKIE = "ci_customer_session"


def send_login_code(email, code):
    if not smtp_ready():
        raise ledger.PortalError("Email sign-in is temporarily unavailable. Please contact support.", 503)
    message = EmailMessage()
    message["From"] = os.environ["SMTP_FROM_EMAIL"]
    message["To"] = email
    message["Subject"] = "Your CiteIntegrity sign-in code"
    message.set_content(f"Your CiteIntegrity sign-in code is {code}.\n\n"
                        "It expires in 10 minutes and can only be used once. "
                        "If you did not request this code, you can ignore this email.\n\nCiteIntegrity")
    try:
        _send(message)
    except Exception as exc:
        log.warning("Customer sign-in email delivery failed: %s", mail_error_kind(exc))
        raise ledger.PortalError("The sign-in email could not be delivered. Please try again shortly or contact support@citeintegrity.org. If you already paid, please do not pay again.", 503)


def require_same_origin(request):
    origin = request.headers.get("origin")
    target = urlsplit(str(request.base_url))
    allowed = {(target.scheme, target.netloc)}
    base = urlsplit(os.environ.get("APP_BASE_URL", "https://citeintegrity.org"))
    allowed.add((base.scheme, base.netloc))
    source = urlsplit(origin) if origin else None
    if (request.headers.get("x-ci-portal") != "1"
            or request.headers.get("sec-fetch-site") == "cross-site"
            or source and (source.scheme, source.netloc) not in allowed):
        raise HTTPException(403, "Please use the CiteIntegrity account page.")


def portal_webhook(database_url, raw, signature, market="ghana", *, central_authorized=False):
    """Return None for existing pay-as-you-go events. Authenticate before acting."""
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    reference = str(payload.get("reference") or data.get("reference") or data.get("provider_reference") or "")
    if not reference.startswith("CISUB-"):
        return None
    if market not in {"ghana", "gh"}:
        return {"ok": False, "status_code": 400}
    if not central_authorized:
        try:
            expected = hmac.new(_require_secret_key("ghana").encode(), raw, hashlib.sha512).hexdigest()
        except Exception:
            return {"ok": False, "status_code": 503}
        if not signature or not hmac.compare_digest(expected, signature):
            return {"ok": False, "status_code": 400}
    if payload.get("event") and payload.get("event") != "charge.success":
        return {"ok": True, "status_code": 200}
    try:
        verify_payment(database_url, reference)
    except ledger.PortalError as exc:
        return {"ok": False, "status_code": 503 if exc.status >= 500 else 409,
                "message": "Payment is awaiting verification. No credits have been added."}
    except Exception:
        log.warning("Subscription payment verification deferred")
        return {"ok": False, "status_code": 503}
    return {"ok": True, "status_code": 200, "credits_activated": True}


def verify_payment(database_url, reference):
    if not re.fullmatch(r"CISUB-[A-F0-9]{32}", reference):
        raise ledger.PortalError("Invalid payment reference.")
    response = _paystack_request("GET", "/transaction/verify/" + quote(reference, safe=""), market="ghana")
    if not response.get("status"):
        raise ledger.PortalError("Payment verification is pending. Please do not pay again.", 409)
    return ledger.activate_order(database_url, reference, response.get("data") or {})


def document_text(result):
    return str(result.get("main_text") or result.get("full_text") or result.get("document_text") or result.get("text") or "")


def document_fingerprint(result):
    words = re.findall(r"\w+", document_text(result).lower())
    return ledger.digest(" ".join(words)) if len(words) >= 20 else ""


def manuscript_signature(result, account_id):
    """Nonreversible salted hashes, never passages or reference strings."""
    words = re.findall(r"\w+", document_text(result).lower())
    hashes = {hmac.new(account_id.encode(), " ".join(words[i:i+5]).encode(), hashlib.sha256).hexdigest()[:24]
              for i in range(len(words)-4)}
    return {"words": len(words), "hashes": heapq.nsmallest(128, hashes)} if len(words) >= 20 else {}


def signatures_match(original, revised):
    if isinstance(original, str):
        try:
            original = json.loads(original)
        except ValueError:
            return False
    if not isinstance(original, dict):
        return False
    a,b = set(original.get("hashes") or []),set(revised.get("hashes") or [])
    left,right = original.get("words",0),revised.get("words",0)
    return bool(a and b and left and right) and min(left,right)/max(left,right)>=.6 and len(a & b)/max(len(a),len(b))>=.6


def reconcile_failed_reviews(database_url):
    if not database_url:
        return
    from access_control import get_conn
    with get_conn(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT g.job_id FROM ci_grants g JOIN jobs j ON j.job_id=g.job_id
                WHERE g.refunded_at IS NULL AND j.status IN ('failed','error') LIMIT 100""")
            failed = [row["job_id"] for row in cur.fetchall()]
    for job_id in failed:
        ledger.refund_failed_review(database_url, job_id)
    ledger.cleanup_portal_auth(database_url)


def report_metadata(database_url, job_ids):
    if not job_ids:
        return {}
    from access_control import get_conn
    with get_conn(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT job_id,status,file_name,
                COALESCE((result->'privacy'->>'content_deleted')::boolean,false)
                OR COALESCE((result->>'result_deleted')::boolean,false) AS deleted,
                result->'privacy'->>'scheduled_deletion_at' AS expires_at
                FROM jobs WHERE job_id=ANY(%s)""", (job_ids,))
            return {row["job_id"]: dict(row) for row in cur.fetchall()}


def one_off_reports(database_url, email):
    from access_control import get_conn
    with get_conn(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT pr.job_id,pr.created_at,
                (p.expires_at IS NULL OR p.expires_at>NOW()) AS current
                FROM purchases p JOIN purchase_runs pr ON pr.purchase_id=p.id
                WHERE LOWER(p.user_email)=%s AND p.status IN ('paid','active')
                ORDER BY pr.created_at DESC LIMIT 100""", (email,))
            return [{"job_id":row["job_id"], "created_at":row["created_at"].isoformat(),
                     "credits":0 if row["current"] else None,"refunded_at":None,
                     "recheck_of":"", "recheck_until":None} for row in cur.fetchall()]


def one_off_payments(database_url, email):
    from access_control import get_conn
    with get_conn(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT provider_reference,document_tier,amount,currency,status,created_at
                FROM purchases WHERE LOWER(user_email)=%s AND status IN ('paid','active')
                ORDER BY created_at DESC LIMIT 100""", (email,))
            return [{"reference":row["provider_reference"], "kind":"one_off", "plan_key":row["document_tier"],
                     "amount_subunit":int(round(float(row["amount"] or 0)*100)), "currency":row["currency"],
                     "status":"paid", "created_at":row["created_at"].isoformat()}
                    for row in cur.fetchall()]


def create_portal_router(database_url, load_counts, load_job, access_mode, paid_for_job):
    router = APIRouter()
    root = Path(__file__).resolve().parent

    def db():
        value = database_url()
        if not value:
            raise HTTPException(503, "Customer accounts are temporarily unavailable.")
        return value

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ledger.PortalError as exc:
            raise HTTPException(exc.status, str(exc))

    def account(request, mutate=False):
        if mutate:
            require_same_origin(request)
        value = call(ledger.session_account, db(), request.cookies.get(COOKIE, ""))
        if not value:
            raise HTTPException(401, "Please sign in to your CiteIntegrity account.")
        return value

    def review(request, job_id, recheck_of=""):
        owner = account(request)
        counts = load_counts(job_id)
        tier = recommend_document_tier_with_words(counts["reference_count"], counts["citation_count"], counts["word_count"])
        job = load_job(job_id)
        result = (job or {}).get("result") or {}
        links = call(ledger.report_links, db(), owner["id"])
        linked = next((r for r in links if r["job_id"] == job_id), None)
        parent = recheck_of or (linked or {}).get("recheck_of") or ""
        signature = manuscript_signature(result, owner["id"])
        if parent:
            # Validate ownership before loading the original temporary content.
            saved = call(ledger.grant, db(), parent)
            if not saved or saved["account_id"] != owner["id"]:
                raise HTTPException(403, "Choose a review from your own account for the included recheck.")
            if not signatures_match(saved.get("signature") or "{}", signature):
                raise HTTPException(409, "The included recheck must be a revision of the same manuscript.")
        return owner, tier, document_fingerprint(result), parent, signature

    def current_quote(owner, job_id, tier, fingerprint, parent, signature):
        for _ in range(4):
            result = call(ledger.quote_review, db(), owner["id"], job_id, tier, fingerprint, parent)
            duplicate = result.get("duplicate_of")
            if not duplicate:
                return result, parent
            try:
                load_counts(duplicate)
                return result, parent
            except HTTPException as exc:
                if exc.status_code not in {404, 410}:
                    raise
            original = call(ledger.grant, db(), duplicate)
            call(ledger.forget_expired_report_fingerprint, db(), duplicate)
            # After temporary deletion, an identical upload can use the one
            # included recheck. Never send the customer to a deleted report.
            root = (original or {}).get("recheck_of") or duplicate
            root_grant = call(ledger.grant, db(), root)
            if not parent and root_grant and signatures_match(root_grant.get("signature") or "{}", signature):
                try:
                    candidate = ledger.quote_review(db(), owner["id"], job_id, tier, fingerprint, root)
                    if not candidate.get("duplicate_of"):
                        return candidate, root
                except ledger.PortalError:
                    pass
        raise HTTPException(409, "Please refresh your account before unlocking this review.")

    @router.get("/account", include_in_schema=False)
    def portal_page():
        return FileResponse(root / "templates" / "account.html", headers={"Cache-Control": "no-store"})

    @router.get("/api/account/plans")
    def plans():
        return {"ok": True, **ledger.catalogue(), "payments_available": access_mode().get("mode") == "payment_required"}

    @router.post("/api/account/sign-in")
    def sign_in(request: Request, payload: dict = Body(...)):
        require_same_origin(request)
        call(ledger.rate_limit, db(), "login-ip:" + (request.client.host if request.client else "unknown"), 60, 900)
        return call(ledger.request_code, db(), payload.get("email"), send_login_code)

    @router.post("/api/account/verify-code")
    def sign_in_verify(request: Request, payload: dict = Body(...)):
        require_same_origin(request)
        call(ledger.rate_limit, db(), "verify-ip:" + (request.client.host if request.client else "unknown"), 120, 900)
        token, owner = call(ledger.verify_code, db(), payload.get("email"), payload.get("code"))
        response = JSONResponse({"ok": True, "email": owner["email"]}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax", path="/",
                            secure=os.environ.get("PAYMENT_COOKIE_SECURE", "true").lower() not in {"false", "0", "no"})
        return response

    @router.post("/api/account/logout")
    def sign_out(request: Request):
        require_same_origin(request)
        call(ledger.logout, db(), request.cookies.get(COOKIE, ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path="/")
        return response

    @router.get("/api/account")
    def my_account(request: Request):
        owner = call(ledger.session_account, db(), request.cookies.get(COOKIE, ""))
        if not owner:
            return JSONResponse({"ok": True, "signed_in": False}, headers={"Cache-Control": "no-store"})
        reconcile_failed_reviews(db())
        state = call(ledger.snapshot, db(), owner["id"])
        state["payments"] = sorted(state["payments"] + one_off_payments(db(), owner["email"]),
                                   key=lambda item: item["created_at"], reverse=True)[:100]
        return JSONResponse({"ok": True, "signed_in": True, **state},
                            headers={"Cache-Control": "no-store"})

    @router.get("/api/account/reports")
    def my_reports(request: Request):
        owner = account(request)
        links = call(ledger.report_links, db(), owner["id"])
        by_id = {item["job_id"]: item for item in links}
        for item in one_off_reports(db(), owner["email"]):
            existing = by_id.get(item["job_id"])
            if existing and item["credits"] is not None and existing.get("credits") is None:
                existing["credits"] = 0
            elif not existing:
                links.append(item)
        links = sorted(links, key=lambda item: item["created_at"], reverse=True)[:100]
        metadata = report_metadata(db(), [item["job_id"] for item in links])
        reports = []
        for item in links:
            job = metadata.get(item["job_id"]) or {"deleted": True, "status": "expired"}
            if job.get("status") in {"failed", "error"} and item.get("refunded_at") is None:
                call(ledger.refund_failed_review, db(), item["job_id"])
                item["refunded_at"] = ledger.stamp(ledger.now_utc())
            available = not job.get("deleted") and job.get("status") == "completed"
            reports.append({"job_id": item["job_id"], "file_name": job.get("file_name") or "Manuscript",
                            "status": "expired" if job.get("deleted") else job.get("status"),
                            "created_at": item["created_at"], "expires_at": job.get("expires_at"),
                            "unlocked": item.get("credits") is not None and not item.get("refunded_at"),
                            "recheck_until": item.get("recheck_until"), "recheck_of": item.get("recheck_of"),
                            "results_url": f"/results/{quote(item['job_id'], safe='')}" if available else None,
                            "download_url": f"/api/report-package/{quote(item['job_id'], safe='')}?delete_after=0"
                            if available and item.get("credits") is not None and not item.get("refunded_at") else None})
        return JSONResponse({"ok": True, "reports": reports}, headers={"Cache-Control": "no-store"})

    @router.post("/api/account/checkout")
    def checkout(request: Request, payload: dict = Body(...)):
        owner = account(request, True)
        if access_mode().get("mode") != "payment_required":
            raise HTTPException(409, "New subscription payments are currently unavailable.")
        if payload.get("accept_manual_renewal") is not True:
            raise HTTPException(400, "Confirm the prepaid term and manual renewal before checkout.")
        call(ledger.rate_limit, db(), "checkout:" + owner["id"], 5)
        try:
            _require_secret_key("ghana")
        except Exception:
            raise HTTPException(503, "Subscription checkout is temporarily unavailable.")
        saved = call(ledger.new_order, db(), owner["id"], plan_key=payload.get("plan_key", ""),
                     months=payload.get("months", 1), topup=payload.get("topup", 0))
        base = os.environ.get("APP_BASE_URL", "https://citeintegrity.org").rstrip("/")
        try:
            response = _paystack_request("POST", "/transaction/initialize", {
                "email": owner["email"], "amount": saved["amount_subunit"], "currency": "GHS",
                "reference": saved["reference"], "callback_url": base + "/payment/subscription/callback",
                "metadata": {"portal_order": saved["reference"], "source_app": "citeintegrity",
                             "product_code": "subscription" if saved["kind"] == "subscription" else "credit_topup"},
            }, "ghana")
        except Exception:
            raise HTTPException(502, "Checkout could not start. Please try again shortly.")
        checkout_url = (response.get("data") or {}).get("authorization_url") or ""
        if not response.get("status") or urlsplit(checkout_url).netloc != "checkout.paystack.com":
            raise HTTPException(502, "Checkout could not start. Please try again shortly.")
        return {"ok": True, "checkout_url": checkout_url, "reference": saved["reference"],
                "amount": saved["amount_subunit"] / 100, "currency": "GHS", "renewal": "manual"}

    @router.post("/api/account/payments/{reference}/verify")
    def reconcile_payment(request: Request, reference: str):
        owner = account(request, True)
        call(ledger.rate_limit, db(), "payment-verify:" + owner["id"], 10)
        saved = call(ledger.order, db(), reference)
        if not saved or saved["account_id"] != owner["id"]:
            raise HTTPException(404, "Payment not found.")
        try:
            call(verify_payment, db(), reference)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Payment verification is temporarily unavailable. Please do not pay again.")
        return {"ok": True, "message": "Payment verified. Your credits are available."}

    @router.get("/payment/subscription/callback")
    def payment_callback(reference: str = "", trxref: str = ""):
        try:
            verify_payment(db(), reference or trxref)
            status = "success"
        except Exception:
            status = "pending"
        return RedirectResponse("/account?payment=" + status, status_code=303)

    @router.get("/api/account/reports/{job_id}/quote")
    def credit_quote(request: Request, job_id: str, recheck_of: str = ""):
        owner, tier, fingerprint, parent, signature = review(request, job_id, recheck_of)
        result, parent = current_quote(owner, job_id, tier, fingerprint, parent, signature)
        if paid_for_job(job_id) and not result.get("already_unlocked"):
            return {"ok": True, "cost": 0, "already_unlocked": True, "tier_key": tier}
        return JSONResponse({"ok": True, **result}, headers={"Cache-Control": "no-store"})

    @router.post("/api/account/reports/{job_id}/unlock")
    def unlock(request: Request, job_id: str, payload: dict = Body(...)):
        account(request, True)
        if access_mode().get("mode") == "open_access":
            return {"ok": True, "cost": 0, "job_id": job_id, "already_unlocked": True}
        owner, tier, fingerprint, parent, signature = review(request, job_id, payload.get("recheck_of", ""))
        if paid_for_job(job_id):
            return {"ok": True, "cost": 0, "job_id": job_id, "already_unlocked": True}
        current_quote(owner, job_id, tier, fingerprint, parent, signature)
        return call(ledger.unlock_review, db(), owner["id"], job_id, tier, fingerprint,
                    expected_cost=payload.get("expected_cost"), recheck_of=parent, signature=signature)

    return router
