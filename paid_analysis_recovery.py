"""Recover a paid analysis without creating another payment or spending a recheck.

Payment ownership is tied to a verified portal email. Recovery cases hold IDs
only. Existing temporary file storage is reused, or the customer reuploads the
same manuscript. A replacement cash entitlement is attached after successful
analysis and validation of the paid document limits.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import subscriptions as ledger
from entitlements import DOCUMENT_TIERS, validate_paid_package_for_document
from paystack_payments import verify_and_activate_purchase
from stripe_payments import verify_and_activate_stripe_session
from subscription_portal import COOKIE, require_same_origin, manuscript_signature, signatures_match, verify_payment


def _current(purchase):
    expiry = purchase.get("expires_at")
    if not expiry:
        return True
    if isinstance(expiry, str):
        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry > ledger.now_utc()


def payment_for_owner(database_url, email, identifier):
    with ledger.transaction(database_url) as cur:
        return cur.execute("""SELECT p.* FROM purchases p LEFT JOIN purchase_runs pr ON pr.purchase_id=p.id
            WHERE LOWER(p.user_email)=%s AND (p.provider_reference=%s OR p.preview_job_id=%s
            OR p.stripe_session_id=%s OR pr.job_id=%s) ORDER BY p.created_at DESC LIMIT 1""",
                           (email, identifier, identifier, identifier, identifier)).one()


def _confirmed_payment(database_url, email, identifier):
    purchase = payment_for_owner(database_url, email, identifier)
    if not purchase:
        return None
    if purchase.get("status") not in {"paid", "active"}:
        provider = purchase.get("payment_provider")
        if provider == "paystack":
            verify_and_activate_purchase(database_url=database_url,
                reference=purchase["provider_reference"], market=purchase.get("market") or "ghana",
                source="customer_analysis_recovery")
        elif provider == "stripe" and purchase.get("stripe_session_id"):
            verify_and_activate_stripe_session(database_url=database_url,
                session_id=purchase["stripe_session_id"], fallback_job_id=purchase.get("preview_job_id") or "")
        purchase = payment_for_owner(database_url, email, identifier)
    if not purchase or purchase.get("status") not in {"paid", "active"}:
        raise ledger.PortalError("Your payment is still awaiting verification. Please do not pay again. Try recovery shortly or contact support with your receipt.", 409)
    return purchase


def _job(cur, job_id):
    job = cur.execute("SELECT * FROM jobs WHERE job_id=%s", (job_id,)).one()
    if job and isinstance(job.get("result"), str):
        try:
            job["result"] = json.loads(job["result"])
        except ValueError:
            job["result"] = {}
    return job


def case_for_owner(database_url, account_id, source_job):
    with ledger.transaction(database_url) as cur:
        case = cur.execute("SELECT * FROM ci_analysis_recovery WHERE source_job_id=%s AND account_id=%s",
                           (source_job, account_id)).one()
    if not case:
        raise ledger.PortalError("No recovery was prepared for this account. Open My Account and recover the paid analysis first.", 404)
    return case


def recovery_plan(database_url, owner, identifier, load_job):
    identifier = str(identifier or "").strip()
    if not identifier or len(identifier) > 255:
        raise ledger.PortalError("Enter your payment reference or analysis ID.")
    if identifier.startswith("CISUB-"):
        order=ledger.order(database_url,identifier)
        if not order or order["account_id"]!=owner["id"]:
            raise ledger.PortalError("No payment was found for this verified email.",404)
        if order["status"]!="paid":
            verify_payment(database_url,identifier)
        return {"action":"upload","upload_url":"/analyse","message":"Your subscription or top-up payment is confirmed. Failed analyses do not consume credits. You can upload your manuscript again without making another payment."}
    purchase = _confirmed_payment(database_url, owner["email"], identifier)
    credit = None
    if purchase:
        source = str(purchase.get("preview_job_id") or "")
        if not source:
            raise ledger.PortalError("Payment is confirmed, but the original analysis could not be identified. Contact support with your receipt. Please do not pay again.", 409)
    else:
        with ledger.transaction(database_url) as cur:
            credit = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s AND account_id=%s", (identifier, owner["id"])).one()
        if not credit:
            with ledger.transaction(database_url) as cur:
                linked=cur.execute("SELECT job_id FROM ci_reports WHERE account_id=%s AND job_id=%s",(owner["id"],identifier)).one()
            failed=load_job(identifier) if linked else None
            if failed and failed.get("status") in {"failed","error"}:
                return {"action":"upload","upload_url":"/analyse","message":"This analysis failed before any review credits were used. Upload the manuscript again. No recovery payment is required."}
            raise ledger.PortalError("No payment or credit review was found for this verified email. Sign in with the email used at checkout, or contact support. Please do not pay again.", 404)
        source = identifier
    job = load_job(source) or {}
    result = job.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    deleted = bool(result.get("result_deleted") or (result.get("privacy") or {}).get("content_deleted"))
    status = str(job.get("status") or "missing").lower()
    if purchase and not _current(purchase):
        raise ledger.PortalError("Payment is confirmed, but its self-service access period has ended. Contact support to recover the failed review. Please do not pay again.", 409)
    if status in {"queued", "processing", "running"} and not deleted:
        return {"action":"open", "job_id":source, "message":"Payment is confirmed. Your analysis is still in progress. You do not need to pay again."}
    if status == "completed" and not deleted:
        verification = result.get("verification") or {}
        incomplete = verification.get("state") in {"failed", "error", "incomplete", "idle", "queued", "running"}
        if incomplete and (result.get("references_raw") or result.get("main_text")):
            return {"action":"verification", "job_id":source, "message":"Payment is confirmed. Retrying incomplete verification without another charge."}
        return {"action":"open", "job_id":source, "message":"Payment is confirmed. Your available review can be reopened without another charge."}
    if credit:
        if status not in {"failed", "error"}:
            raise ledger.PortalError("This credit review's temporary content is no longer available. Use an eligible included recheck, or contact support. Please do not pay again.", 409)
        ledger.refund_failed_review(database_url, source)
    recheck_of=""
    if credit and not credit.get("credits"):
        with ledger.transaction(database_url) as cur:
            report=cur.execute("SELECT recheck_of FROM ci_reports WHERE job_id=%s AND account_id=%s",(source,owner["id"])).one()
        recheck_of=(report or {}).get("recheck_of") or ""
        original_grant=ledger.grant(database_url,recheck_of) if recheck_of else None
        if not original_grant or original_grant["recheck_until"]<=ledger.stamp(ledger.now_utc()):
            raise ledger.PortalError("The failed included recheck needs support recovery because its access period has ended. Please do not pay again.",409)
    tier = purchase.get("document_tier") if purchase else credit["tier_key"]
    with ledger.transaction(database_url) as cur:
        ledger._account_lock(cur, owner["id"])
        existing = cur.execute("SELECT * FROM ci_analysis_recovery WHERE source_job_id=%s FOR UPDATE", (source,)).one()
        if existing and existing["account_id"] != owner["id"]:
            raise ledger.PortalError("This recovery belongs to a different account.", 403)
        if existing and existing.get("replacement_job_id"):
            replacement = _job(cur, existing["replacement_job_id"])
            if replacement and replacement.get("status") not in {"failed", "error"}:
                return {"action":"open", "job_id":replacement["job_id"], "message":"Your recovery is already available or in progress. No second recovery job was created."}
        if not existing:
            cur.execute("""INSERT INTO ci_analysis_recovery(source_job_id,account_id,purchase_id,tier_key,status,updated_at)
                VALUES(%s,%s,%s,%s,'upload_required',%s)""", (source, owner["id"],purchase["id"] if purchase else None,tier,ledger.stamp(ledger.now_utc())))
    ledger.link_report(database_url, owner["id"], source)
    return {"action":"retry", "source_job_id":source,
            "recheck_of":recheck_of,
            "file_name":job.get("file_name") or (purchase or {}).get("preview_file_name") or "",
            "result":result, "credit_refund":bool(credit),
            "message":"Your failed-review credits have been restored. Retry with the same manuscript and use the restored credits after analysis." if credit else "Payment is confirmed. Recover the same manuscript without another payment or using your included recheck."}


def _same_file(original, replacement):
    def key(value):
        stem = Path(str(value or "")).stem.lower()
        stem = re.sub(r"\b(revised|revision|corrected|final|edited|updated|copy|retry|recovery|version|v\d+)\b", " ", stem)
        return re.sub(r"[^a-z0-9]+", "", stem)
    left, right = key(original), key(replacement)
    return bool(left and right) and (left == right or left in right or right in left)


def reserve_replacement(database_url, owner, source, new_job, filename, words):
    current = ledger.now_utc()
    with ledger.transaction(database_url) as cur:
        ledger._account_lock(cur, owner["id"])
        case = cur.execute("SELECT * FROM ci_analysis_recovery WHERE source_job_id=%s AND account_id=%s FOR UPDATE", (source, owner["id"])).one()
        if not case:
            raise ledger.PortalError("Prepare recovery in My Account before uploading.", 404)
        if case.get("replacement_job_id"):
            previous = _job(cur, case["replacement_job_id"])
            if previous and previous.get("status") not in {"failed", "error"}:
                raise ledger.PortalError("Recovery is already in progress. Open it from My Account instead of uploading again.", 409)
            if not previous and ledger.date(case["updated_at"]) > current-timedelta(minutes=10):
                raise ledger.PortalError("Recovery is already being prepared. Please wait before trying again.", 409)
        original = _job(cur, source) or {}
        original_name = original.get("file_name") or ""
        if case.get("purchase_id"):
            purchase = cur.execute("SELECT * FROM purchases WHERE id=%s FOR UPDATE", (case["purchase_id"],)).one()
            if not purchase or str(purchase.get("user_email") or "").lower() != owner["email"] or purchase.get("status") not in {"paid", "active"} or not _current(purchase):
                raise ledger.PortalError("The payment is not available for self-service recovery. Contact support. Please do not pay again.", 409)
            original_name = original_name or purchase.get("preview_file_name") or ""
        if not _same_file(original_name, filename):
            raise ledger.PortalError("Upload the same manuscript using its original filename to recover this paid analysis.", 409)
        tier = DOCUMENT_TIERS.get(case["tier_key"])
        if not tier or words > tier["max_words"]:
            raise ledger.PortalError("This replacement exceeds the paid document tier. Contact support before proceeding. Please do not pay again.", 422)
        cur.execute("""UPDATE ci_analysis_recovery SET replacement_job_id=%s,status='uploading',
            attempts=attempts+1,updated_at=%s WHERE source_job_id=%s""", (new_job,ledger.stamp(current),source))
    return case


def release_failed_upload(database_url, source, replacement):
    with ledger.transaction(database_url) as cur:
        cur.execute("""UPDATE ci_analysis_recovery SET replacement_job_id=NULL,status='upload_required',updated_at=%s
            WHERE source_job_id=%s AND replacement_job_id=%s AND status='uploading'""", (ledger.stamp(ledger.now_utc()),source,replacement))


def recovery_queued(database_url, source, replacement):
    with ledger.transaction(database_url) as cur:
        cur.execute("""UPDATE ci_analysis_recovery SET status='queued',updated_at=%s
            WHERE source_job_id=%s AND replacement_job_id=%s""", (ledger.stamp(ledger.now_utc()),source,replacement))


def finalise_replacement(database_url, job_id, result):
    with ledger.transaction(database_url) as cur:
        case = cur.execute("SELECT * FROM ci_analysis_recovery WHERE replacement_job_id=%s FOR UPDATE", (job_id,)).one()
        if not case:
            return None
        current_job=cur.execute("SELECT status FROM jobs WHERE job_id=%s",(job_id,)).one()
        if not current_job or current_job["status"]!="completed":
            return {"status":"pending","message":"Recovery is still processing. Your original payment is unchanged."}
        if not case.get("purchase_id"):
            original_grant=cur.execute("SELECT credits FROM ci_grants WHERE job_id=%s",(case["source_job_id"],)).one()
            if original_grant and not original_grant["credits"]:
                return {"status":"recheck_restored","message":"Your included recheck is available again. Use the included recheck to unlock this recovery without additional credits."}
            return {"status":"credits_restored", "message":"Credits for the failed analysis were restored. Use those credits to unlock this completed review. There is no additional recovery fee."}
        purchase = cur.execute("SELECT * FROM purchases WHERE id=%s FOR UPDATE", (case["purchase_id"],)).one()
        if not purchase or purchase.get("status") not in {"paid", "active"} or not _current(purchase):
            return {"status":"review_required", "message":"Your payment record requires support review before this recovery can be unlocked. Please do not pay again."}
        summary = result.get("summary") or {}
        original=_job(cur,case["source_job_id"]) or {}
        prior_signature=manuscript_signature(original.get("result") or {},case["account_id"])
        if prior_signature and not signatures_match(prior_signature,manuscript_signature(result,case["account_id"])):
            return {"status":"review_required","message":"This replacement could not be matched to the original manuscript. Contact support to recover the paid analysis. Please do not pay again."}
        refs = int(summary.get("reference_entries_found") or len(result.get("references_raw") or []))
        citations = int(summary.get("in_text_citations_found") or len(result.get("in_text_citations") or []))
        words = int(summary.get("word_count") or result.get("word_count") or (result.get("preflight") or {}).get("word_count") or 0)
        limits = validate_paid_package_for_document(case["tier_key"],refs,citations,words)
        if not limits.get("allowed"):
            return {"status":"review_required", "message":"The replacement exceeds the original paid tier. Contact support to complete recovery. Please do not pay again."}
        cur.execute("""INSERT INTO purchase_runs(purchase_id,job_id,reference_count,citation_count)
            VALUES(%s,%s,%s,%s) ON CONFLICT(job_id) DO NOTHING""", (purchase["id"],job_id,refs,citations))
        linked = cur.execute("SELECT purchase_id FROM purchase_runs WHERE job_id=%s", (job_id,)).one()
        if not linked or linked["purchase_id"] != purchase["id"]:
            raise ledger.PortalError("This replacement is already linked to a different purchase.", 409)
        # No analyses_used increment: a failed paid run is being replaced.
        cur.execute("UPDATE ci_analysis_recovery SET status='completed',updated_at=%s WHERE source_job_id=%s", (ledger.stamp(ledger.now_utc()),case["source_job_id"]))
        return {"status":"recovered", "message":"Paid analysis recovered. Your original payment unlocks this review. No additional payment or recheck was consumed."}


def create_recovery_router(database_url, load_job, retry_original, retry_verification):
    router = APIRouter()

    @router.post("/api/account/recovery")
    async def recover(request: Request, payload: dict = Body(...)):
        require_same_origin(request)
        db = database_url()
        try:
            owner = await run_in_threadpool(ledger.session_account,db,request.cookies.get(COOKIE,""))
            if not owner:
                raise ledger.PortalError("Sign in with the email used for payment to recover your analysis.",401)
            await run_in_threadpool(ledger.rate_limit,db,"analysis-recovery:"+owner["id"],5,300)
            plan = await run_in_threadpool(recovery_plan,db,owner,payload.get("reference"),load_job)
            if plan["action"]=="upload":
                return {"ok":True,"paid":True,"upload_required":True,"upload_url":plan["upload_url"],"message":plan["message"]}
            if plan["action"] == "verification":
                await retry_verification(plan["job_id"])
            elif plan["action"] == "retry":
                outcome = await retry_original(request,plan)
                return {"ok":True,"paid":True,"message":plan["message"],**outcome}
            return {"ok":True,"paid":True,"job_id":plan["job_id"],"results_url":"/results/"+quote(plan["job_id"],safe=""),"message":plan["message"]}
        except ledger.PortalError as exc:
            raise HTTPException(exc.status,str(exc))
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503,"Recovery is temporarily unavailable. Your payment is not being charged again. Please retry shortly or contact support with your receipt.")

    return router
