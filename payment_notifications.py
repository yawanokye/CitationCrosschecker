"""Durable Stripe purchase acknowledgements using the existing SMTP settings.

The purchase row is the outbox. A row lock prevents concurrent normal sends.
SMTP cannot guarantee exactly-once delivery across a send/commit crash.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from contextlib import contextmanager
from email.message import EmailMessage
from email.utils import formataddr
from urllib.parse import quote

from access_control import get_conn

log = logging.getLogger(__name__)


def smtp_ready() -> bool:
    return all(os.environ.get(key, "").strip() for key in (
        "SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL",
    ))


def build_confirmation(purchase: dict) -> EmailMessage:
    base = os.environ.get("APP_BASE_URL", "https://citeintegrity.org").rstrip("/")
    job = quote(str(purchase["preview_job_id"]), safe="")
    report_url = f"{base}/results/{job}?payment=success"
    download_url = f"{base}/api/report-package/{job}?delete_after=0"
    amount = f"{str(purchase.get('currency') or 'USD').upper()} {float(purchase['amount']):.2f}"
    msg = EmailMessage()
    msg["From"] = formataddr((os.environ.get("PAYMENT_EMAIL_FROM_NAME", "CiteIntegrity"), os.environ["SMTP_FROM_EMAIL"]))
    msg["To"] = str(purchase["user_email"])
    msg["Subject"] = "CiteIntegrity payment confirmation and reference check"
    msg["Message-ID"] = f"<citeintegrity-payment-{int(purchase['id'])}@citeintegrity.org>"
    msg.set_content(
        "Thank you for your payment to CiteIntegrity.\n\n"
        f"Payment confirmed: {amount}\n"
        f"Payment reference: {purchase['provider_reference']}\n"
        f"Manuscript: {purchase.get('preview_file_name') or 'Your uploaded manuscript'}\n\n"
        f"Open your reference check and follow its progress:\n{report_url}\n\n"
        f"Download the report package once processing is complete:\n{download_url}\n\n"
        "Please download promptly. Manuscript content and detailed reports are temporary "
        "and follow the retention period shown in your dashboard. Payment confirmation "
        "does not mean every reference lookup has finished.\n\n"
        "If you need help, contact support@citeintegrity.org with the payment reference. "
        "Please do not pay again for this result.\n\nCiteIntegrity Support\n"
    )
    return msg


@contextmanager
def _smtp_connection():
    # A blank port should use the same default as the contact form. Do not
    # weaken certificate validation or fall back to unencrypted delivery.
    port = int(os.environ.get("SMTP_PORT", "").strip() or "587")
    # The two standard submission ports use different TLS handshakes. Some mail
    # hosts close the connection when the wrong one is configured. Retry the
    # other secure port only before any message has been submitted. A provider
    # that explicitly rejects authentication is not retried.
    alternate = {25: 587, 465: 587, 587: 465}.get(port)
    ports = [port] + ([alternate] if alternate else [])
    context = ssl.create_default_context()
    for index, candidate in enumerate(ports):
        factory = smtplib.SMTP_SSL if candidate == 465 else smtplib.SMTP
        kwargs = {"context": context} if candidate == 465 else {}
        stage = "connect"
        try:
            with factory(os.environ["SMTP_HOST"].strip(), candidate, timeout=15, **kwargs) as server:
                if candidate != 465:
                    stage = "tls"
                    server.starttls(context=context)
                stage = "authentication"
                server.login(os.environ["SMTP_USERNAME"].strip(), os.environ["SMTP_PASSWORD"])
                stage = "message"
                yield server
                return
        except Exception as exc:
            log.warning("Customer email transport failed stage=%s port=%s error=%s",
                        stage, candidate, mail_error_kind(exc))
            retryable = ((stage in {"connect", "tls"}
                          and isinstance(exc, (OSError, smtplib.SMTPServerDisconnected))
                          and not isinstance(exc, ssl.SSLCertVerificationError))
                         or (stage == "authentication" and isinstance(exc, smtplib.SMTPServerDisconnected)))
            if not retryable or index == len(ports) - 1:
                raise


def mail_error_kind(exc: Exception) -> str:
    """Return non-sensitive diagnostics, never provider text or credentials."""
    code = getattr(exc, "smtp_code", None)
    return f"{type(exc).__name__} smtp_code={code}" if isinstance(code, int) else type(exc).__name__


def probe_smtp_connection() -> bool:
    """Check the configured transport without sending a message or logging secrets."""
    if not smtp_ready():
        missing = [key for key in ("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "SMTP_FROM_EMAIL")
                   if not os.environ.get(key, "").strip()]
        log.warning("Customer email transport unavailable; missing settings: %s", ", ".join(missing))
        return False
    try:
        with _smtp_connection():
            pass
    except Exception as exc:
        log.warning("Customer email transport check failed: %s", mail_error_kind(exc))
        return False
    log.info("Customer email transport check succeeded")
    return True


def _send(message: EmailMessage) -> None:
    with _smtp_connection() as server:
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def send_payment_confirmation(database_url: str, purchase_id: int) -> dict:
    if not smtp_ready():
        return {"sent": False, "reason": "smtp_not_configured"}
    try:
        with get_conn(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT * FROM purchases WHERE id=%s AND payment_provider='stripe'
                       AND status IN ('paid','active') AND confirmation_sent_at IS NULL
                       AND (confirmation_attempted_at IS NULL OR confirmation_attempted_at < NOW() - INTERVAL '5 minutes')
                       FOR UPDATE SKIP LOCKED""", (purchase_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return {"sent": False, "reason": "already_sent_or_not_due"}
                purchase = dict(row)
                job_id = str(purchase.get("preview_job_id") or "")
                cursor.execute(
                    """SELECT 1 FROM purchase_runs pr JOIN purchases p ON p.id=pr.purchase_id
                       JOIN jobs j ON j.job_id=pr.job_id
                       WHERE pr.job_id=%s AND p.status IN ('paid','active')
                       AND j.status='completed'
                       AND COALESCE((j.result->'privacy'->>'content_deleted')::boolean, false)=false
                       AND COALESCE((j.result->>'result_deleted')::boolean, false)=false
                       AND (p.expires_at IS NULL OR p.expires_at > NOW()) LIMIT 1""", (job_id,),
                )
                if not job_id or not cursor.fetchone():
                    return {"sent": False, "reason": "report_access_not_ready"}
                cursor.execute("UPDATE purchases SET confirmation_attempted_at=NOW() WHERE id=%s", (purchase_id,))
                try:
                    _send(build_confirmation(purchase))
                except Exception as exc:
                    # Exception text may contain private SMTP information.
                    log.warning("Payment confirmation delivery failed: %s", type(exc).__name__)
                    conn.commit()
                    return {"sent": False, "reason": "delivery_failed"}
                cursor.execute("UPDATE purchases SET confirmation_sent_at=NOW() WHERE id=%s", (purchase_id,))
            conn.commit()
        return {"sent": True}
    except Exception as exc:
        log.warning("Payment confirmation deferred: %s", type(exc).__name__)
        return {"sent": False, "reason": "delivery_deferred"}


def deliver_pending_confirmations(database_url: str, limit: int = 20) -> int:
    if not database_url or not smtp_ready():
        return 0
    with get_conn(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """SELECT p.id FROM purchases p JOIN jobs j ON j.job_id=p.preview_job_id
                   WHERE p.payment_provider='stripe' AND p.status IN ('paid','active')
                   AND p.confirmation_sent_at IS NULL AND j.status='completed'
                   AND COALESCE((j.result->'privacy'->>'content_deleted')::boolean, false)=false
                   AND COALESCE((j.result->>'result_deleted')::boolean, false)=false
                   AND (p.expires_at IS NULL OR p.expires_at > NOW())
                   AND (p.confirmation_attempted_at IS NULL OR p.confirmation_attempted_at < NOW() - INTERVAL '5 minutes')
                   ORDER BY p.created_at LIMIT %s""", (max(1, min(100, limit)),),
            )
            ids = [row["id"] for row in cursor.fetchall()]
    return sum(bool(send_payment_confirmation(database_url, item).get("sent")) for item in ids)
