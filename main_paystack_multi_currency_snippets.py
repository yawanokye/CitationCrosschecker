"""
main_paystack_multi_currency_snippets.py
FastAPI snippets for GHS/USD Paystack checkout.
"""

# Add imports in main.py
from fastapi import Body
from fastapi.responses import HTMLResponse, JSONResponse
from entitlements import build_plan_selection_payload, apply_entitlements_to_result
from access_control import (
    init_commercial_tables,
    purchase_is_paid_for_job,
    validate_purchase_for_new_run,
    record_purchase_run,
)
from paystack_payments import (
    initialize_citeintegrity_payment,
    verify_and_activate_purchase,
    handle_paystack_webhook,
)

# During startup/database setup:
# if DATABASE_URL:
#     init_commercial_tables(DATABASE_URL)

"""
@app.get("/api/plans/recommend/{job_id}")
async def recommend_package_for_job(job_id: str, currency: str = "GHS"):
    job = load_job_record_fresh(job_id)
    if not job:
        raise HTTPException(404, "Job not found")

    result = job.get("result") or {}
    summary = result.get("summary") or {}

    reference_count = (
        summary.get("reference_entries_found")
        or len(result.get("references_raw") or [])
        or 0
    )
    citation_count = (
        summary.get("in_text_citations_found")
        or len(result.get("in_text_citations") or [])
        or 0
    )

    return build_plan_selection_payload(
        reference_count,
        citation_count,
        selected_currency=currency,
    )
"""

"""
@app.post("/api/paystack/initialize")
async def paystack_initialize(payload: dict = Body(...)):
    user_email = (payload.get("email") or "").strip()
    tier_key = (payload.get("tier_key") or "").strip()
    selected_currency = (payload.get("currency") or "GHS").strip().upper()
    reference_count = int(payload.get("reference_count") or 0)
    citation_count = int(payload.get("citation_count") or 0)

    if not user_email or "@" not in user_email:
        raise HTTPException(400, "A valid email is required.")

    if not DATABASE_URL:
        raise HTTPException(500, "DATABASE_URL is not configured.")

    init = initialize_citeintegrity_payment(
        database_url=DATABASE_URL,
        user_email=user_email,
        tier_key=tier_key,
        reference_count=reference_count,
        citation_count=citation_count,
        selected_currency=selected_currency,
        callback_path="/payment/paystack/callback",
    )

    if not init.get("ok"):
        raise HTTPException(402, init.get("error", "Could not initialize payment."))

    return init
"""

"""
@app.get("/payment/paystack/callback")
async def paystack_callback(reference: str):
    result = verify_and_activate_purchase(
        database_url=DATABASE_URL,
        reference=reference,
    )

    if not result.get("activated"):
        return HTMLResponse(
            "<h2>Payment could not be confirmed</h2><p>Please contact support with your payment reference.</p>",
            status_code=400,
        )

    return HTMLResponse(
        "<h2>Payment successful</h2><p>Your CiteIntegrity review access has been activated. Check your email for the secure access link.</p>"
    )
"""

"""
@app.post("/webhooks/paystack")
async def paystack_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature", "")

    result = handle_paystack_webhook(
        database_url=DATABASE_URL,
        raw_body=raw_body,
        signature=signature,
    )

    return JSONResponse(result, status_code=result.get("status_code", 200))
"""

"""
# When returning result data:
access = purchase_is_paid_for_job(DATABASE_URL, job_id=job_id) if DATABASE_URL else {
    "paid": False,
    "tier_key": "",
    "currency": "GHS",
}

safe_result = apply_entitlements_to_result(
    result,
    tier_key=access.get("tier_key", ""),
    paid=access.get("paid", False),
    currency=access.get("currency", "GHS"),
)
"""
