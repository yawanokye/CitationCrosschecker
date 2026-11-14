"""Payment recovery regressions. Stripe, SMTP and PostgreSQL are simulated."""
import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

import access_control as access
import payment_notifications as mail
import stripe_payments as payments

PURCHASE = {"id": 41, "status": "pending", "payment_provider": "stripe", "amount": 2.99,
            "currency": "USD", "user_email": "author@example.test", "provider_reference": "CI-example",
            "preview_job_id": "report-job", "preview_file_name": "Manuscript.docx", "analyses_used": 0,
            "stripe_session_id": "cs_test_example", "access_token": "test-only-token"}
SESSION = {"id": "cs_test_example", "mode": "payment", "payment_status": "paid", "amount_total": 299,
           "currency": "usd", "customer_email": "author@example.test", "client_reference_id": "CI-example",
           "customer_details": {"email": "author@example.test"},
           "metadata": {"purchase_id": "41", "provider_reference": "CI-example", "job_id": "report-job"}}


@pytest.fixture
def gateway(monkeypatch):
    ledger = deepcopy(PURCHASE)
    monkeypatch.setattr(payments, "STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(payments, "STRIPE_WEBHOOK_SECRET", "whsec_fixture")
    retrieve = Mock(return_value=deepcopy(SESSION))
    monkeypatch.setattr(payments.stripe.checkout.Session, "retrieve", retrieve)
    monkeypatch.setattr(payments, "get_purchase_by_provider_reference", Mock(return_value=ledger))
    mark = Mock(side_effect=lambda *a, **kw: dict(ledger, status="paid"))
    monkeypatch.setattr(payments, "mark_purchase_paid", mark)
    attach = Mock(return_value={"attached": True, "purchase": dict(ledger, status="paid", analyses_used=1)})
    monkeypatch.setattr(payments, "attach_paid_purchase_to_preview_job", attach)
    monkeypatch.setattr(payments, "purchase_is_paid_for_job", Mock(return_value={"paid": True}))
    construct = Mock(return_value={"type": "checkout.session.completed", "data": {"object": deepcopy(SESSION)}})
    monkeypatch.setattr(payments.stripe.Webhook, "construct_event", construct)
    return ledger, retrieve, mark, attach, construct


def verify():
    return payments.verify_and_activate_stripe_session(database_url="fixture", session_id=SESSION["id"], fallback_job_id="report-job")


def webhook():
    return payments.handle_stripe_webhook(database_url="fixture", raw_body=b"signed-fixture", signature="fixture")


def test_verified_card_payment_unlocks_original_report(gateway):
    result = verify()
    assert result["activated"] and result["job_id"] == "report-job"
    gateway[3].assert_called_once()


@pytest.mark.parametrize("field,value", [("amount_total", 298), ("currency", "eur"),
                                         ("customer_email", "someone@example.test"), ("id", "cs_other")])
def test_mismatched_ledger_never_activates(gateway, field, value):
    gateway[1].return_value[field] = value
    assert not verify()["activated"]
    gateway[2].assert_not_called()


@pytest.mark.parametrize("key,value", [("purchase_id", "42"), ("provider_reference", "different"), ("job_id", "other-job")])
def test_metadata_conflicts_do_not_unlock_another_report(gateway, key, value):
    gateway[1].return_value["metadata"][key] = value
    assert not verify()["activated"]
    gateway[2].assert_not_called()


def test_editable_billing_email_does_not_block_paid_checkout(gateway):
    gateway[1].return_value["customer_details"]["email"] = "billing@example.test"
    assert verify()["activated"]


def test_browser_job_id_is_never_used_as_purchase_proof(gateway):
    gateway[0]["preview_job_id"] = ""
    gateway[1].return_value["metadata"]["job_id"] = ""
    assert not verify()["activated"]
    gateway[2].assert_not_called()


def test_return_url_cannot_rebind_a_purchase(gateway):
    result = payments.verify_and_activate_stripe_session(database_url="fixture", session_id=SESSION["id"], fallback_job_id="another-job")
    assert not result["activated"]
    gateway[2].assert_not_called()


@pytest.mark.parametrize("event_type", ["checkout.session.completed", "checkout.session.async_payment_succeeded"])
def test_paid_webhooks_activate(gateway, event_type):
    gateway[4].return_value["type"] = event_type
    assert webhook()["purchase_activated"]


def test_unpaid_webhook_waits_for_async_success(gateway):
    gateway[4].return_value["data"]["object"]["payment_status"] = "unpaid"
    result = webhook()
    assert result["status_code"] == 200 and not result["purchase_activated"]
    gateway[2].assert_not_called()


def test_failed_attachment_and_db_failure_are_retryable(gateway, monkeypatch):
    monkeypatch.setattr(payments, "purchase_is_paid_for_job", lambda *a, **k: {"paid": False})
    assert verify()["activated"] is False
    assert webhook()["status_code"] == 503
    gateway[3].side_effect = RuntimeError("temporary database error")
    assert webhook()["status_code"] == 503


def test_invalid_signature_never_activates(gateway):
    gateway[4].side_effect = payments.stripe.SignatureVerificationError("bad", "fixture")
    assert webhook()["status_code"] == 400
    gateway[2].assert_not_called()


def load_routes(**overrides):
    namespace = {"Request": Request, "HTTPException": HTTPException, "JSONResponse": JSONResponse,
                 "Any": Any, "Dict": Dict, "json": __import__('json'),
                 "RedirectResponse": RedirectResponse, "DATABASE_URL": "fixture", "jsonable_encoder": jsonable_encoder,
                 "run_in_threadpool": run_in_threadpool, "quote_plus": __import__('urllib.parse', fromlist=['quote_plus']).quote_plus,
                 "_checkout_rate_limited": lambda *a: False,
                 "get_checkout_purchase_by_token": Mock(return_value=deepcopy(PURCHASE)),
                 "verify_and_activate_stripe_session": Mock(return_value={"ok": True, "activated": True, "job_id": "report-job"}),
                 "purchase_is_paid_for_job": Mock(return_value={"paid": True}),
                 "load_job_record_fresh": Mock(return_value={"status": "completed", "result": {}})}
    namespace.update(overrides)
    tree = ast.parse(Path("main.py").read_text())
    names = {"recover_stripe_payment", "stripe_success", "stripe_webhook", "initialize_commercial_checkout", "_commercial_job_counts"}
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list = []
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    namespace.update(overrides)
    app = FastAPI()
    app.post("/api/payments/recover/{job_id}")(namespace["recover_stripe_payment"])
    app.get("/payment/stripe/success")(namespace["stripe_success"])
    app.post("/api/webhooks/stripe")(namespace["stripe_webhook"])
    app.post("/api/payments/initialize")(namespace["initialize_commercial_checkout"])
    return TestClient(app), namespace


def test_cookie_recovery_uses_exact_job_and_returns_download_link():
    client, ns = load_routes()
    client.cookies.set("ci_access_token", "test-only-token")
    result = client.post("/api/payments/recover/report-job", json={})
    assert result.status_code == 200 and result.json()["paid"]
    assert result.json()["download_url"].endswith("?delete_after=0")
    ns["get_checkout_purchase_by_token"].assert_called_once_with("fixture", token="test-only-token", job_id="report-job")
    assert "access_token" not in result.text and "author@" not in result.text


def test_recovery_rejects_paid_but_deleted_report():
    client, _ = load_routes(load_job_record_fresh=lambda job: {"result": {"result_deleted": True}})
    response = client.post("/api/payments/recover/report-job", json={"session_id": SESSION["id"]})
    assert response.status_code == 410 and response.json()["paid"]
    assert "do not pay again" in response.json()["message"].lower()


def test_no_saved_checkout_or_payment_does_not_unlock():
    client, ns = load_routes(get_checkout_purchase_by_token=lambda *a, **kw: {}, purchase_is_paid_for_job=lambda *a, **kw: {"paid": False})
    response = client.post("/api/payments/recover/report-job", json={})
    assert response.status_code == 200 and not response.json()["paid"]
    ns["verify_and_activate_stripe_session"].assert_not_called()


def test_confirmed_payment_with_failed_job_offers_recovery_not_fake_download():
    client,_=load_routes(load_job_record_fresh=lambda job:{"status":"failed","result":{}})
    response=client.post('/api/payments/recover/report-job',json={})
    assert response.status_code==200 and response.json()['paid']
    assert response.json()['recovery_required'] and response.json()['recovery_url']=='/account#recovery'
    assert 'download_url' not in response.json()


def test_recovery_rate_limit():
    client, ns = load_routes(_checkout_rate_limited=lambda *a: True)
    assert client.post("/api/payments/recover/report-job", json={}).status_code == 429
    ns["verify_and_activate_stripe_session"].assert_not_called()


def test_pending_return_does_not_redirect_customer_to_buy_again():
    client, _ = load_routes(verify_and_activate_stripe_session=lambda **k: {"activated": False, "job_id": "report-job"})
    response = client.get("/payment/stripe/success?session_id=cs_test_example", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/results/report-job?payment=pending"


def test_webhook_route_serializes_database_timestamps():
    client, _ = load_routes(handle_stripe_webhook=lambda **kw: {"ok": True, "status_code": 200,
        "purchase": {"paid_at": datetime(2026, 11, 3, tzinfo=timezone.utc)}})
    assert client.post("/api/webhooks/stripe", content=b"{}").status_code == 200


def test_checkout_returns_session_for_recovery_and_refuses_duplicate_payment():
    create = Mock(return_value={"ok": True, "checkout_url": "https://checkout.stripe.test", "session_id": SESSION["id"], "access_token": "fixture-token"})
    client, ns = load_routes(
        get_access_mode=lambda *a: {"mode": "payment_required"}, redis_conn=None, re=__import__('re'),
        _commercial_job_counts=lambda job: {"word_count": 100, "reference_count": 5, "citation_count": 6, "file_name": "Paper.docx"},
        resolve_payment_market=lambda code: {"provider": "stripe", "market": "international", "currency": "USD", "country_code": "GB"},
        recommend_document_tier_with_words=lambda *a: "article", initialize_citeintegrity_stripe_payment=create,
        purchase_is_paid_for_job=lambda *a, **k: {"paid": False}, os=__import__('os'))
    data = {"email": "author@example.test", "job_id": "report-job", "billing_country": "GB"}
    response = client.post("/api/payments/initialize", json=data)
    assert response.status_code == 200 and response.json()["session_id"] == SESSION["id"]
    assert "HttpOnly" in response.headers["set-cookie"]
    ns["purchase_is_paid_for_job"] = lambda *a, **k: {"paid": True}
    assert client.post("/api/payments/initialize", json=data).status_code == 409
    assert create.call_count == 1


@pytest.fixture
def smtp(monkeypatch):
    for key, value in {"SMTP_HOST": "smtp.example.test", "SMTP_USERNAME": "fixture", "SMTP_PASSWORD": "fixture",
                       "SMTP_FROM_EMAIL": "support@example.test", "APP_BASE_URL": "https://citeintegrity.test"}.items():
        monkeypatch.setenv(key, value)
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    cursor.fetchone.side_effect = [dict(PURCHASE, status="paid"), {"?column?": 1}]
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    monkeypatch.setattr(mail, "get_conn", Mock(return_value=conn))
    sender = Mock()
    monkeypatch.setattr(mail, "_send", sender)
    return cursor, conn, sender


def test_email_includes_acknowledgement_and_safe_report_download(smtp):
    assert mail.send_payment_confirmation("fixture", 41)["sent"]
    message = smtp[2].call_args.args[0]
    body = message.get_content()
    assert "USD 2.99" in body and "CI-example" in body
    assert "https://citeintegrity.test/results/report-job?payment=success" in body
    assert "https://citeintegrity.test/api/report-package/report-job?delete_after=0" in body
    assert "test-only-token" not in body
    assert any("confirmation_sent_at=NOW()" in call.args[0] for call in smtp[0].execute.call_args_list)


def test_smtp_failure_is_not_recorded_as_sent_and_can_retry(smtp):
    smtp[2].side_effect = RuntimeError("simulated SMTP failure")
    assert mail.send_payment_confirmation("fixture", 41)["reason"] == "delivery_failed"
    assert not any("confirmation_sent_at=NOW()" in call.args[0] for call in smtp[0].execute.call_args_list)
    assert smtp[1].commit.called
    smtp[2].side_effect = None
    smtp[0].fetchone.side_effect = [dict(PURCHASE, status="paid"), {"?column?": 1}]
    assert mail.send_payment_confirmation("fixture", 41)["sent"]


def test_duplicate_or_locked_notification_is_not_sent(smtp):
    smtp[0].fetchone.side_effect = [None]
    assert not mail.send_payment_confirmation("fixture", 41)["sent"]
    smtp[2].assert_not_called()
    assert "FOR UPDATE SKIP LOCKED" in smtp[0].execute.call_args.args[0]


def test_unavailable_report_does_not_get_a_false_report_email(smtp):
    smtp[0].fetchone.side_effect = [dict(PURCHASE, status="paid"), None]
    assert mail.send_payment_confirmation("fixture", 41)["reason"] == "report_access_not_ready"
    smtp[2].assert_not_called()


def test_missing_smtp_is_explicit_and_does_not_contact_server(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    assert mail.send_payment_confirmation("fixture", 41)["reason"] == "smtp_not_configured"


def test_checkout_requests_stripe_receipt_and_saves_recovery_session(monkeypatch):
    monkeypatch.setattr(payments, "STRIPE_SECRET_KEY", "sk_test_fixture")
    monkeypatch.setattr(payments, "create_pending_purchase", Mock(return_value=deepcopy(PURCHASE)))
    monkeypatch.setattr(payments, "_prelink_pending_purchase_to_job", Mock(return_value={"linked": True}))
    create = Mock(return_value=type('Session', (), {"id": SESSION["id"], "url": "https://checkout.stripe.test"})())
    monkeypatch.setattr(payments.stripe.checkout.Session, "create", create)
    conn = MagicMock()
    conn.__enter__.return_value = conn
    cursor = MagicMock()
    cursor.__enter__.return_value = cursor
    conn.cursor.return_value = cursor
    monkeypatch.setattr(payments.psycopg2, "connect", Mock(return_value=conn))
    result = payments.initialize_citeintegrity_stripe_payment(database_url="fixture", user_email=PURCHASE["user_email"],
        tier_key="article", reference_count=5, citation_count=6, job_id="report-job")
    assert result["ok"] and result["session_id"] == SESSION["id"]
    assert create.call_args.kwargs["payment_intent_data"]["receipt_email"] == PURCHASE["user_email"]
    assert "/results/report-job?payment=cancelled" in create.call_args.kwargs["cancel_url"]
    assert cursor.execute.call_args.args[1] == (SESSION["id"], 41)


def test_expired_content_cannot_start_another_checkout():
    client, namespace = load_routes(load_job_record_fresh=lambda job: {"status": "completed", "result": {"privacy": {"content_deleted": True}}})
    with pytest.raises(HTTPException) as error:
        namespace["_commercial_job_counts"]("report-job")
    assert error.value.status_code == 410
