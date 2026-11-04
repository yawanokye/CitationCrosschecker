"""Operator recovery using a Checkout Session ID obtained from Stripe.

Run in the deployed service environment. Never accepts a manual paid flag.
No charge is created. Confirmation email is sent only with the explicit flag.
"""
import argparse
import json
import os
from urllib.parse import quote

from access_control import get_conn, init_commercial_tables
from payment_notifications import send_payment_confirmation
from stripe_payments import verify_and_activate_stripe_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--expect-job", default="")
    parser.add_argument("--send-confirmation", action="store_true")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        parser.error("DATABASE_URL is required in the service environment.")
    init_commercial_tables(database_url)
    result = verify_and_activate_stripe_session(database_url=database_url,
        session_id=args.session_id, fallback_job_id=args.expect_job)
    output = {key:result.get(key) for key in ("activated", "message", "job_id", "provider_reference")}
    if result.get("activated"):
        job_id = result["job_id"]
        with get_conn(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""SELECT status,
                    COALESCE((result->'privacy'->>'content_deleted')::boolean, false)
                    OR COALESCE((result->>'result_deleted')::boolean, false) AS deleted
                    FROM jobs WHERE job_id=%s""", (job_id,))
                job = cursor.fetchone()
        available = bool(job and job["status"] == "completed" and not job["deleted"])
        output["report_available"] = available
        if available:
            base = os.environ.get("APP_BASE_URL", "https://citeintegrity.org").rstrip("/")
            output["results_url"] = f"{base}/results/{quote(job_id, safe='')}"
            output["download_url"] = f"{base}/api/report-package/{quote(job_id, safe='')}?delete_after=0"
            if args.send_confirmation:
                output["confirmation"] = send_payment_confirmation(database_url, result["purchase"]["id"])
        else:
            output["message"] = "Payment verified. Stored report is unavailable. Arrange recovery or reprocessing without a second charge."
    print(json.dumps(output, indent=2))
    return 0 if result.get("activated") else 1


if __name__ == "__main__":
    raise SystemExit(main())
