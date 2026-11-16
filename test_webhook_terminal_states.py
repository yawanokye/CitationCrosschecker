"""Exercise payment idempotency and terminal-state protection with SQLite."""
import json
import hashlib
import hmac
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# The local test environment has no PostgreSQL driver. SQL is executed against
# SQLite through a small parameter adapter, not against a production database.
try:
    import psycopg2  # noqa: F401
except ModuleNotFoundError:
    psycopg2=types.ModuleType("psycopg2");psycopg2.__path__=[]
    extras=types.ModuleType("psycopg2.extras");extras.RealDictCursor=object
    psycopg2.extras=extras
    sys.modules["psycopg2"]=psycopg2;sys.modules["psycopg2.extras"]=extras

import access_control
import paystack_payments
import subscriptions


class Cursor:
    def __init__(self,conn):self.cursor=conn.cursor()
    def __enter__(self):return self
    def __exit__(self,*args):self.cursor.close()
    def execute(self,sql,args=()):self.cursor.execute(sql.replace("%s","?"),args)
    def fetchone(self):
        row=self.cursor.fetchone()
        return dict(row) if row is not None else None


class Connection:
    def __init__(self,path):
        self.conn=sqlite3.connect(path);self.conn.row_factory=sqlite3.Row
        self.conn.create_function("NOW",0,lambda:"2026-11-15T12:00:00Z")
    def __enter__(self):return self
    def __exit__(self,*args):self.conn.close()
    def cursor(self):return Cursor(self.conn)
    def commit(self):self.conn.commit()


class TerminalStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="cite-payment-test-")
        self.path=str(Path(self.tmp.name)/"payments.db")
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE purchases (id INTEGER PRIMARY KEY,provider_reference TEXT UNIQUE,status TEXT,paid_at TEXT,amount REAL,currency TEXT,user_email TEXT,payment_provider TEXT)")
            for index,status in enumerate(["pending","paid","active","refunded","revoked","cancelled"],1):
                conn.execute("INSERT INTO purchases(id,provider_reference,status,paid_at,amount,currency,user_email,payment_provider) VALUES(?,?,?,?,?,?,?,?)",
                    (index,"CIT-"+status,status,"original" if status in {"paid","active"} else None,10,"GHS","researcher@example.test","paystack"))
        self.connpatch=patch.object(access_control,"get_conn",side_effect=lambda _:Connection(self.path));self.connpatch.start()

    def tearDown(self):self.connpatch.stop();self.tmp.cleanup()

    def test_paid_and_pending_are_safe_but_terminal_states_are_never_reopened(self):
        for original,expected in [("pending","paid"),("paid","paid"),("active","active")]:
            row=access_control.mark_purchase_paid("unused",provider_reference="CIT-"+original)
            self.assertEqual(row["status"],expected)
            if original!="pending":self.assertEqual(row["paid_at"],"original")
        for status in ("refunded","revoked","cancelled"):
            self.assertIsNone(access_control.mark_purchase_paid("unused",provider_reference="CIT-"+status))
        with sqlite3.connect(self.path) as conn:
            for status in ("refunded","revoked","cancelled"):
                self.assertEqual(conn.execute("SELECT status FROM purchases WHERE provider_reference=?",("CIT-"+status,)).fetchone()[0],status)

    def test_duplicate_refunded_webhook_is_acknowledged_without_credit(self):
        raw=json.dumps({"event":"charge.success","data":{"reference":"CIT-refunded","amount":1000,"currency":"GHS",
             "customer":{"email":"researcher@example.test"},"metadata":{"source_app":"citeintegrity"}}}).encode()
        with patch.object(paystack_payments,"verify_paystack_webhook_signature",return_value=True), \
             patch.object(paystack_payments,"_attach_preview_job_to_purchase") as attach:
            result=paystack_payments.handle_paystack_webhook(database_url="unused",raw_body=raw,signature="signed")
        self.assertEqual(result["status_code"],200)
        self.assertTrue(result["terminal_purchase"])
        self.assertFalse(result["purchase_activated"])
        attach.assert_not_called()

    def test_cancelled_purchase_remains_pending_reconciliation(self):
        raw=json.dumps({"event":"charge.success","data":{"reference":"CIT-cancelled","amount":1000,"currency":"GHS",
             "customer":{"email":"researcher@example.test"},"metadata":{"source_app":"citeintegrity"}}}).encode()
        with patch.object(paystack_payments,"verify_paystack_webhook_signature",return_value=True), \
             patch.object(paystack_payments,"_attach_preview_job_to_purchase") as attach:
            result=paystack_payments.handle_paystack_webhook(database_url="unused",raw_body=raw,signature="signed")
        self.assertEqual(result["status_code"],503)
        attach.assert_not_called()

    def test_callback_cannot_report_a_refunded_purchase_as_activated(self):
        verification={"ok":True,"verified":True,"reference":"CIT-refunded","amount":1000,"currency":"GHS",
                      "customer_email":"researcher@example.test","metadata":{"source_app":"citeintegrity"}}
        with patch.object(paystack_payments,"verify_paystack_transaction",return_value=verification):
            result=paystack_payments.verify_and_activate_purchase(database_url="unused",reference="CIT-refunded")
        self.assertFalse(result["ok"])
        self.assertFalse(result["activated"])

    def test_signed_event_crosses_both_handlers_without_duplicate_or_refund_reactivation(self):
        projectready_root=str(Path(__file__).parent.parent/"projectready-review")
        if not (Path(projectready_root)/"app"/"payments"/"paystack.py").is_file():
            self.skipTest("Cross-product integration requires the accompanying ProjectReady checkout.")
        if projectready_root not in sys.path:sys.path.insert(0,projectready_root)
        from app.payments import paystack as projectready
        secret="sk_test_shared_only"
        def event(reference):
            body=json.dumps({"event":"charge.success","data":{"id":123,"status":"success","reference":reference,
                "amount":1000,"currency":"GHS","customer":{"email":"researcher@example.test"},
                "metadata":{"source_app":"citeintegrity"}}}).encode()
            return body,hmac.new(secret.encode(),body,hashlib.sha512).hexdigest()
        def deliver(body,signature,data):
            result=paystack_payments.handle_paystack_webhook(database_url="unused",raw_body=body,signature=signature)
            return result.get("status_code")==200 and result.get("ok") and (result.get("purchase_activated") or result.get("terminal_purchase"))
        with patch.object(projectready,"PAYSTACK_SECRET_KEY",secret),patch.object(paystack_payments,"PAYSTACK_GH_SECRET_KEY",secret), \
             patch.object(projectready,"_forward_citeintegrity_event",side_effect=deliver), \
             patch.object(projectready,"record_event_once",side_effect=[True,False,True]):
            body,signature=event("CIT-pending")
            self.assertEqual(projectready.handle_paystack_webhook(raw_body=body,signature=signature)["status_code"],200)
            self.assertTrue(projectready.handle_paystack_webhook(raw_body=body,signature=signature)["duplicate"])
            body,signature=event("CIT-refunded")
            self.assertEqual(projectready.handle_paystack_webhook(raw_body=body,signature=signature)["status_code"],200)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute("SELECT status FROM purchases WHERE provider_reference=?",("CIT-pending",)).fetchone()[0],"paid")
            self.assertEqual(conn.execute("SELECT status FROM purchases WHERE provider_reference=?",("CIT-refunded",)).fetchone()[0],"refunded")

    def test_subscription_retries_do_not_duplicate_credits_or_reopen_refunds(self):
        db="sqlite:///"+str(Path(self.tmp.name)/"portal.db")
        subscriptions.init_portal_tables(db)
        with subscriptions.transaction(db) as cursor:
            cursor.execute("INSERT INTO ci_accounts(id,email,created_at) VALUES(%s,%s,%s)",("owner","researcher@example.test","2026-11-15T00:00:00+00:00"))
        pending=subscriptions.new_order(db,"owner",topup=2)
        def verified(order):return {"status":"success","reference":order["reference"],"amount":order["amount_subunit"],"currency":"GHS",
            "customer":{"email":"researcher@example.test"},"metadata":{"portal_order":order["reference"],"source_app":"citeintegrity"}}
        first=subscriptions.activate_order(db,pending["reference"],verified(pending))
        second=subscriptions.activate_order(db,pending["reference"],verified(pending))
        self.assertEqual(first["status"],"paid");self.assertEqual(second["status"],"paid")
        terminal=subscriptions.new_order(db,"owner",topup=2)
        with subscriptions.transaction(db) as cursor:cursor.execute("UPDATE ci_orders SET status='refunded' WHERE reference=%s",(terminal["reference"],))
        with self.assertRaises(subscriptions.PortalError):subscriptions.activate_order(db,terminal["reference"],verified(terminal))
        with subscriptions.transaction(db) as cursor:
            lots=cursor.execute("SELECT count(*) AS n FROM ci_credit_lots WHERE account_id=%s",("owner",)).one()["n"]
            saved=cursor.execute("SELECT status FROM ci_orders WHERE reference=%s",(terminal["reference"],)).one()
        self.assertEqual(lots,1);self.assertEqual(saved["status"],"refunded")


if __name__ == "__main__":unittest.main()
