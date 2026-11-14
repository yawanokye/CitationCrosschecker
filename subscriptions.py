"""Prepaid subscriptions, email sessions and an atomic, auditable credit ledger.

PostgreSQL uses account row locks. SQLite is supported for isolated integration
tests, with BEGIN IMMEDIATE providing the same serialized accounting boundary.
No manuscript text, filenames or detailed reports are copied into these tables.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
CREDIT_COSTS = {"article": 2, "research_paper": 4, "thesis": 7, "phd": 10}
PLANS = {
    "individual": {"name": "Individual", "monthly_credits": 12, "prices": {1: 49, 3: 135, 12: 490}},
    "researcher": {"name": "Researcher", "monthly_credits": 30, "prices": {1: 99, 3: 270, 12: 990}},
}


class PortalError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def now_utc():
    return datetime.now(UTC)


def stamp(value):
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def date(value):
    return datetime.fromisoformat(value).astimezone(UTC)


def add_months(value, months):
    """Always derive boundaries from the original anchor, including Jan 31."""
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return value.replace(year=year, month=month + 1,
                         day=min(value.day, calendar.monthrange(year, month + 1)[1]))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Cursor:
    def __init__(self, cursor, sqlite=False):
        self.cursor, self.sqlite = cursor, sqlite

    def execute(self, sql, args=()):
        if self.sqlite:
            sql = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        self.cursor.execute(sql, args)
        return self

    def one(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def all(self):
        return [dict(row) for row in self.cursor.fetchall()]


@contextmanager
def transaction(database_url):
    if not database_url:
        raise PortalError("Customer accounts are temporarily unavailable.", 503)
    local = database_url.startswith("sqlite:///")
    if local:
        conn = sqlite3.connect(database_url[len("sqlite:///"):], timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
    else:
        from access_control import get_conn
        conn = get_conn(database_url)
    cursor = conn.cursor()
    try:
        yield Cursor(cursor, local)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ci_accounts (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ci_login_codes (
        email TEXT PRIMARY KEY, code_hash TEXT NOT NULL, expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS ci_sessions (
        token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ci_limits (
        key_hash TEXT NOT NULL, bucket BIGINT NOT NULL, count INTEGER NOT NULL,
        PRIMARY KEY(key_hash,bucket))""",
    """CREATE TABLE IF NOT EXISTS ci_orders (
        reference TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        kind TEXT NOT NULL, plan_key TEXT NOT NULL, months INTEGER NOT NULL,
        credits INTEGER NOT NULL, amount_subunit INTEGER NOT NULL, currency TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL, paid_at TEXT,
        starts_at TEXT, ends_at TEXT)""",
    """CREATE INDEX IF NOT EXISTS ci_orders_account ON ci_orders(account_id,created_at)""",
    """CREATE TABLE IF NOT EXISTS ci_credit_lots (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        source TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, units INTEGER NOT NULL,
        remaining INTEGER NOT NULL CHECK(remaining >= 0),
        available_at TEXT NOT NULL, expires_at TEXT)""",
    """CREATE INDEX IF NOT EXISTS ci_lots_account ON ci_credit_lots(account_id,expires_at)""",
    """CREATE TABLE IF NOT EXISTS ci_reports (
        job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        recheck_of TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ci_grants (
        job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        tier_key TEXT NOT NULL, credits INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        signature TEXT NOT NULL DEFAULT '{}',
        recheck_of TEXT UNIQUE, created_at TEXT NOT NULL, recheck_until TEXT NOT NULL,
        refunded_at TEXT)""",
    """CREATE INDEX IF NOT EXISTS ci_grants_account ON ci_grants(account_id,created_at)""",
    """CREATE TABLE IF NOT EXISTS ci_spends (
        job_id TEXT NOT NULL REFERENCES ci_grants(job_id),
        lot_id TEXT NOT NULL REFERENCES ci_credit_lots(id), units INTEGER NOT NULL,
        PRIMARY KEY(job_id,lot_id))""",
    """CREATE TABLE IF NOT EXISTS ci_ledger (
        id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        event TEXT NOT NULL, units INTEGER NOT NULL, source TEXT NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(event,source))""",
    """CREATE TABLE IF NOT EXISTS ci_analysis_recovery (
        source_job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES ci_accounts(id),
        purchase_id BIGINT, tier_key TEXT NOT NULL, replacement_job_id TEXT,
        status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS ci_recovery_replacement ON ci_analysis_recovery(replacement_job_id)""",
]


def init_portal_tables(database_url):
    with transaction(database_url) as cur:
        for sql in SCHEMA:
            cur.execute(sql)


def _account_lock(cur, account_id):
    row = cur.execute("SELECT * FROM ci_accounts WHERE id=%s FOR UPDATE", (account_id,)).one()
    if not row:
        raise PortalError("Please sign in again.", 401)
    return row


def rate_limit(database_url, key, limit, seconds=60, now=None):
    current = now or now_utc()
    bucket = int(current.timestamp()) // seconds * seconds
    with transaction(database_url) as cur:
        row = cur.execute("""INSERT INTO ci_limits(key_hash,bucket,count) VALUES(%s,%s,1)
            ON CONFLICT(key_hash,bucket) DO UPDATE SET count=ci_limits.count+1 RETURNING count""",
                          (digest(key), bucket)).one()
        cur.execute("DELETE FROM ci_limits WHERE bucket < %s", (int(current.timestamp()) - 86400,))
    if row["count"] > limit:
        raise PortalError("Too many attempts. Please wait before trying again.", 429)


def request_code(database_url, email, send, now=None):
    email = str(email or "").strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise PortalError("Enter a valid email address.")
    current = now or now_utc()
    rate_limit(database_url, "email-code:" + email, 3, 900, current)
    code = f"{secrets.randbelow(1000000):06d}"
    with transaction(database_url) as cur:
        cur.execute("""INSERT INTO ci_login_codes(email,code_hash,expires_at,attempts)
            VALUES(%s,%s,%s,0) ON CONFLICT(email) DO UPDATE SET
            code_hash=excluded.code_hash,expires_at=excluded.expires_at,attempts=0""",
                    (email, digest(email + ":" + code), stamp(current + timedelta(minutes=10))))
        # Sending before commit prevents an undelivered code replacing a usable one.
        send(email, code)
    return {"ok": True, "message": "Check your email for a six-digit sign-in code. It expires in 10 minutes."}


def verify_code(database_url, email, code, now=None):
    current = now or now_utc()
    email = str(email or "").strip().lower()[:254]
    valid = False
    token = secrets.token_urlsafe(32)
    with transaction(database_url) as cur:
        row = cur.execute("SELECT * FROM ci_login_codes WHERE email=%s FOR UPDATE", (email,)).one()
        if row and row["attempts"] < 5 and row["expires_at"] > stamp(current):
            cur.execute("UPDATE ci_login_codes SET attempts=attempts+1 WHERE email=%s", (email,))
            valid = hmac.compare_digest(row["code_hash"], digest(email + ":" + str(code or "")))
        if valid:
            cur.execute("DELETE FROM ci_login_codes WHERE email=%s", (email,))
            cur.execute("""INSERT INTO ci_accounts(id,email,created_at) VALUES(%s,%s,%s)
                ON CONFLICT(email) DO NOTHING""", (secrets.token_hex(16), email, stamp(current)))
            account = cur.execute("SELECT * FROM ci_accounts WHERE email=%s", (email,)).one()
            cur.execute("DELETE FROM ci_sessions WHERE expires_at <= %s", (stamp(current),))
            cur.execute("INSERT INTO ci_sessions(token_hash,account_id,expires_at) VALUES(%s,%s,%s)",
                        (digest(token), account["id"], stamp(current + timedelta(days=30))))
    # Raise after committing failed-attempt counters.
    if not valid:
        raise PortalError("The code is invalid or expired. Request a new code if needed.", 401)
    return token, account


def session_account(database_url, token, now=None):
    if not token or not database_url:
        return None
    with transaction(database_url) as cur:
        return cur.execute("""SELECT a.* FROM ci_sessions s JOIN ci_accounts a ON a.id=s.account_id
            WHERE s.token_hash=%s AND s.expires_at>%s""", (digest(token), stamp(now or now_utc()))).one()


def logout(database_url, token):
    with transaction(database_url) as cur:
        cur.execute("DELETE FROM ci_sessions WHERE token_hash=%s", (digest(token),))


def catalogue():
    return {"plans": [{"plan_key": key, "name": plan["name"], "monthly_credits": plan["monthly_credits"],
                        "terms": [{"months": months, "amount": amount, "currency": "GHS"}
                                  for months, amount in plan["prices"].items()]}
                       for key, plan in PLANS.items()],
            "credit_costs": CREDIT_COSTS, "topup_price": 5, "topup_minimum": 2,
            "currency": "GHS", "renewal": "manual", "rollover_months": 1,
            "recheck_days": 14, "rechecks_included": 1}


def new_order(database_url, account_id, *, plan_key="", months=1, topup=0, now=None):
    if type(topup) is not int or type(months) is not int:
        raise PortalError("Choose a valid plan term or whole number of credits.")
    if topup:
        if plan_key or not 2 <= topup <= 1000:
            raise PortalError("Top-ups require between 2 and 1,000 whole credits.")
        kind, credits, amount, months = "topup", topup, topup * 500, 0
    else:
        plan = PLANS.get(plan_key)
        if not plan or months not in plan["prices"]:
            raise PortalError("Choose a monthly, quarterly or annual plan.")
        kind, credits, amount = "subscription", plan["monthly_credits"], plan["prices"][months] * 100
    ref = "CISUB-" + secrets.token_hex(16).upper()
    with transaction(database_url) as cur:
        _account_lock(cur, account_id)
        cur.execute("""INSERT INTO ci_orders(reference,account_id,kind,plan_key,months,credits,
            amount_subunit,currency,status,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,'GHS','pending',%s)""",
                    (ref, account_id, kind, plan_key, months, credits, amount, stamp(now or now_utc())))
        return cur.execute("SELECT * FROM ci_orders WHERE reference=%s", (ref,)).one()


def order(database_url, reference):
    with transaction(database_url) as cur:
        return cur.execute("""SELECT o.*,a.email FROM ci_orders o JOIN ci_accounts a ON a.id=o.account_id
            WHERE o.reference=%s""", (reference,)).one()


def _ledger(cur, account_id, event, units, source, current):
    cur.execute("""INSERT INTO ci_ledger(id,account_id,event,units,source,created_at)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event,source) DO NOTHING""",
                (secrets.token_hex(16), account_id, event, units, source, stamp(current)))


def _lot(cur, account_id, source, kind, units, available, expires):
    inserted = cur.execute("""INSERT INTO ci_credit_lots(id,account_id,source,kind,units,remaining,available_at,expires_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source) DO NOTHING RETURNING id""",
                           (secrets.token_hex(16), account_id, source, kind, units, units,
                            stamp(available), stamp(expires) if expires else None)).one()
    if inserted and kind != "refund":
        _ledger(cur, account_id, "release" if kind == "subscription" else "topup", units, source, available)
    return bool(inserted)


def _release(cur, account_id, current):
    orders = cur.execute("""SELECT * FROM ci_orders WHERE account_id=%s AND kind='subscription'
        AND status='paid' AND starts_at<=%s ORDER BY starts_at""", (account_id, stamp(current))).all()
    for item in orders:
        anchor = date(item["starts_at"])
        for cycle in range(item["months"]):
            available = add_months(anchor, cycle)
            if available > current:
                break
            source = f"{item['reference']}:{cycle}"
            if _lot(cur, account_id, source, "subscription", item["credits"],
                    available, add_months(anchor, cycle + 2)):
                # Across different plan terms, including clipped month ends,
                # retain at most one allowance from earlier subscription lots.
                older = cur.execute("""SELECT * FROM ci_credit_lots WHERE account_id=%s
                    AND kind='subscription' AND available_at<%s AND expires_at>%s AND remaining>0
                    ORDER BY available_at,id""", (account_id, stamp(available), stamp(available))).all()
                surplus = max(0, sum(lot["remaining"] for lot in older) - item["credits"])
                expired = surplus
                for lot in older:
                    remove = min(surplus, lot["remaining"])
                    surplus -= remove
                    expiry = min(date(lot["expires_at"]), add_months(anchor, cycle + 1))
                    cur.execute("UPDATE ci_credit_lots SET remaining=remaining-%s,expires_at=%s WHERE id=%s",
                                (remove, stamp(expiry), lot["id"]))
                if expired:
                    _ledger(cur, account_id, "rollover_expiry", -expired, source, available)


def activate_order(database_url, reference, verified, now=None):
    """Only call with a server-retrieved Paystack transaction, never browser data."""
    current = now or now_utc()
    saved = order(database_url, reference)
    if not saved:
        raise PortalError("Payment reference not found.", 404)
    customer = verified.get("customer") or {}
    metadata = verified.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    matched = (verified.get("status") == "success" and verified.get("reference") == reference
               and type(verified.get("amount")) is int and verified["amount"] == saved["amount_subunit"]
               and verified.get("currency") == saved["currency"]
               and str(customer.get("email") or "").strip().lower() == saved["email"]
               and isinstance(metadata, dict) and metadata.get("portal_order") == reference
               and metadata.get("source_app") == "citeintegrity")
    if not matched:
        raise PortalError("Payment is pending or does not match this order. Please do not pay again.", 409)
    with transaction(database_url) as cur:
        _account_lock(cur, saved["account_id"])
        saved = cur.execute("SELECT * FROM ci_orders WHERE reference=%s FOR UPDATE", (reference,)).one()
        if saved["status"] == "paid":
            return saved
        if saved["kind"] == "subscription":
            latest = cur.execute("""SELECT ends_at FROM ci_orders WHERE account_id=%s
                AND kind='subscription' AND status='paid' ORDER BY ends_at DESC LIMIT 1""",
                                 (saved["account_id"],)).one()
            start = max(current, date(latest["ends_at"])) if latest else current
            end = add_months(start, saved["months"])
            cur.execute("UPDATE ci_orders SET starts_at=%s,ends_at=%s WHERE reference=%s",
                        (stamp(start), stamp(end), reference))
        else:
            _lot(cur, saved["account_id"], reference, "topup", saved["credits"], current, None)
        cur.execute("UPDATE ci_orders SET status='paid',paid_at=%s WHERE reference=%s", (stamp(current), reference))
        _release(cur, saved["account_id"], current)
        return cur.execute("SELECT * FROM ci_orders WHERE reference=%s", (reference,)).one()


def _lots(cur, account_id, current):
    return cur.execute("""SELECT * FROM ci_credit_lots WHERE account_id=%s AND remaining>0
        AND available_at<=%s AND (expires_at IS NULL OR expires_at>%s)
        ORDER BY CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END,expires_at,available_at,id""",
                       (account_id, stamp(current), stamp(current))).all()


def snapshot(database_url, account_id, now=None):
    current = now or now_utc()
    with transaction(database_url) as cur:
        account = _account_lock(cur, account_id)
        _release(cur, account_id, current)
        lots = _lots(cur, account_id, current)
        orders = cur.execute("SELECT * FROM ci_orders WHERE account_id=%s ORDER BY created_at DESC LIMIT 100",
                             (account_id,)).all()
        active = cur.execute("""SELECT * FROM ci_orders WHERE account_id=%s AND status='paid'
            AND kind='subscription' AND starts_at<=%s AND ends_at>%s ORDER BY starts_at LIMIT 1""",
                             (account_id, stamp(current), stamp(current))).all()
        scheduled = cur.execute("""SELECT * FROM ci_orders WHERE account_id=%s AND status='paid'
            AND kind='subscription' AND starts_at>%s ORDER BY starts_at""", (account_id, stamp(current))).all()
        next_release = None
        if active:
            item = active[0]
            for cycle in range(1, item["months"]):
                boundary = add_months(date(item["starts_at"]), cycle)
                if boundary > current:
                    next_release = stamp(boundary)
                    break
        return {"email": account["email"], "balance": sum(o["remaining"] for o in lots),
                "credit_lots": [{k: o[k] for k in ("kind", "remaining", "expires_at")} for o in lots],
                "subscription": active[0] if active else None, "scheduled": scheduled,
                "next_credit_release": next_release, "payments": orders,
                "renewal": "manual", "ledger": cur.execute("""SELECT event,units,source,created_at FROM ci_ledger
                    WHERE account_id=%s ORDER BY created_at DESC LIMIT 100""", (account_id,)).all()}


def link_report(database_url, account_id, job_id, recheck_of="", now=None):
    with transaction(database_url) as cur:
        _account_lock(cur, account_id)
        cur.execute("""INSERT INTO ci_reports(job_id,account_id,recheck_of,created_at) VALUES(%s,%s,%s,%s)
            ON CONFLICT(job_id) DO NOTHING""", (job_id, account_id, recheck_of, stamp(now or now_utc())))


def report_links(database_url, account_id):
    with transaction(database_url) as cur:
        return cur.execute("""SELECT r.*,g.credits,g.recheck_until,g.refunded_at FROM ci_reports r
            LEFT JOIN ci_grants g ON g.job_id=r.job_id
            WHERE r.account_id=%s ORDER BY r.created_at DESC LIMIT 100""", (account_id,)).all()


def grant(database_url, job_id):
    with transaction(database_url) as cur:
        return cur.execute("SELECT * FROM ci_grants WHERE job_id=%s AND refunded_at IS NULL", (job_id,)).one()


def quote_review(database_url, account_id, job_id, tier_key, fingerprint, recheck_of="", now=None):
    current = now or now_utc()
    if tier_key not in CREDIT_COSTS:
        raise PortalError("This document exceeds the self-service limits. Contact support.", 422)
    state = snapshot(database_url, account_id, current)
    with transaction(database_url) as cur:
        existing = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s AND refunded_at IS NULL", (job_id,)).one()
        if existing:
            if existing["account_id"] != account_id:
                raise PortalError("This report is already linked to another account.", 409)
            return {"cost": 0, "already_unlocked": True, "balance": state["balance"], "tier_key": tier_key}
        duplicate = cur.execute("""SELECT job_id FROM ci_grants WHERE account_id=%s AND fingerprint=%s
            AND refunded_at IS NULL AND recheck_until>%s ORDER BY created_at LIMIT 1""",
                                (account_id, fingerprint, stamp(current))).one() if fingerprint else None
        if duplicate:
            return {"cost": 0, "duplicate_of": duplicate["job_id"], "balance": state["balance"], "tier_key": tier_key}
        if recheck_of:
            original = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s AND account_id=%s",
                                   (recheck_of, account_id)).one()
            used = cur.execute("SELECT job_id FROM ci_grants WHERE recheck_of=%s AND refunded_at IS NULL",
                               (recheck_of,)).one()
            if (not original or original["recheck_of"] or original["refunded_at"]
                    or original["recheck_until"] <= stamp(current) or used
                    or CREDIT_COSTS[tier_key] > CREDIT_COSTS[original["tier_key"]]):
                raise PortalError("The included recheck is unavailable. You can unlock this review with credits.", 409)
        return {"cost": 0 if recheck_of else CREDIT_COSTS[tier_key], "balance": state["balance"],
                "tier_key": tier_key, "recheck_of": recheck_of, "already_unlocked": False,
                "can_unlock": bool(recheck_of) or state["balance"] >= CREDIT_COSTS[tier_key]}


def unlock_review(database_url, account_id, job_id, tier_key, fingerprint, *, expected_cost,
                  recheck_of="", signature=None, now=None):
    current = now or now_utc()
    # Keep quote and spend under the same per-account lock. Revalidate every
    # condition here, because the balance or included recheck may have changed.
    with transaction(database_url) as cur:
        _account_lock(cur, account_id)
        _release(cur, account_id, current)
        existing = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s", (job_id,)).one()
        if existing:
            if existing["account_id"] != account_id or existing["refunded_at"]:
                raise PortalError("This review cannot be unlocked again. Refresh your account.", 409)
            return {"ok": True, "cost": 0, "already_unlocked": True, "job_id": job_id}
        owner = cur.execute("SELECT account_id FROM ci_reports WHERE job_id=%s", (job_id,)).one()
        if owner and owner["account_id"] != account_id:
            raise PortalError("This report is linked to another account.", 409)
        if tier_key not in CREDIT_COSTS:
            raise PortalError("This document exceeds the self-service limits.", 422)
        duplicate = cur.execute("""SELECT job_id FROM ci_grants WHERE account_id=%s AND fingerprint=%s
            AND refunded_at IS NULL AND recheck_until>%s LIMIT 1""",
                                (account_id, fingerprint, stamp(current))).one() if fingerprint else None
        if duplicate:
            return {"ok": True, "cost": 0, "duplicate_of": duplicate["job_id"], "job_id": duplicate["job_id"]}
        until = current + timedelta(days=14)
        if recheck_of:
            original = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s AND account_id=%s", (recheck_of, account_id)).one()
            used = cur.execute("SELECT job_id FROM ci_grants WHERE recheck_of=%s AND refunded_at IS NULL", (recheck_of,)).one()
            if (not original or original["recheck_of"] or original["refunded_at"] or used
                    or original["recheck_until"] <= stamp(current)
                    or CREDIT_COSTS[tier_key] > CREDIT_COSTS[original["tier_key"]]):
                raise PortalError("The included recheck is no longer available. Refresh the credit quote.", 409)
            until = date(original["recheck_until"])
        cost = 0 if recheck_of else CREDIT_COSTS[tier_key]
        if type(expected_cost) is not int or expected_cost != cost:
            raise PortalError("The credit quote changed. Check the cost and try again.", 409)
        lots = _lots(cur, account_id, current)
        if sum(o["remaining"] for o in lots) < cost:
            raise PortalError("You need more credits. Choose a plan or buy a top-up.", 402)
        cur.execute("""INSERT INTO ci_grants(job_id,account_id,tier_key,credits,fingerprint,signature,recheck_of,created_at,recheck_until)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (job_id, account_id, tier_key, cost, fingerprint,
                json.dumps(signature or {}), recheck_of or None, stamp(current), stamp(until)))
        left = cost
        for lot in lots:
            units = min(left, lot["remaining"])
            if units:
                cur.execute("UPDATE ci_credit_lots SET remaining=remaining-%s WHERE id=%s", (units, lot["id"]))
                cur.execute("INSERT INTO ci_spends(job_id,lot_id,units) VALUES(%s,%s,%s)", (job_id, lot["id"], units))
                left -= units
        _ledger(cur, account_id, "unlock", -cost, job_id, current)
        cur.execute("""INSERT INTO ci_reports(job_id,account_id,recheck_of,created_at) VALUES(%s,%s,%s,%s)
            ON CONFLICT(job_id) DO NOTHING""", (job_id, account_id, recheck_of, stamp(current)))
        return {"ok": True, "cost": cost, "job_id": job_id}


def refund_failed_review(database_url, job_id, now=None):
    """The caller must first confirm a fatal failed/error job state server-side."""
    current = now or now_utc()
    saved = grant(database_url, job_id)
    if not saved:
        return False
    with transaction(database_url) as cur:
        _account_lock(cur, saved["account_id"])
        saved = cur.execute("SELECT * FROM ci_grants WHERE job_id=%s FOR UPDATE", (job_id,)).one()
        if saved["refunded_at"]:
            return False
        spends = cur.execute("""SELECT s.units AS spent_units,l.* FROM ci_spends s JOIN ci_credit_lots l ON l.id=s.lot_id
            WHERE s.job_id=%s""", (job_id,)).all()
        for lot in spends:
            # Return still-valid lots in place. Expired credit is reissued for a
            # month so a delayed failure cannot make a promised refund unusable.
            if lot["expires_at"] and lot["expires_at"] <= stamp(current):
                _lot(cur, saved["account_id"], f"refund:{job_id}:{lot['id']}", "refund", lot["spent_units"],
                     current, add_months(current, 1))
            else:
                cur.execute("UPDATE ci_credit_lots SET remaining=remaining+%s WHERE id=%s", (lot["spent_units"], lot["id"]))
        cur.execute("UPDATE ci_grants SET refunded_at=%s,recheck_of=NULL WHERE job_id=%s", (stamp(current), job_id))
        _ledger(cur, saved["account_id"], "refund", saved["credits"], job_id, current)
        return True


def cleanup_portal_auth(database_url, now=None):
    current = stamp(now or now_utc())
    with transaction(database_url) as cur:
        cur.execute("DELETE FROM ci_login_codes WHERE expires_at<=%s", (current,))
        cur.execute("DELETE FROM ci_sessions WHERE expires_at<=%s", (current,))
        cur.execute("""UPDATE ci_grants SET signature='{}',fingerprint='' WHERE recheck_until<=%s
            AND (fingerprint<>'' OR signature<>'{}')""", (current,))


def forget_expired_report_fingerprint(database_url, job_id):
    # Called only after the server confirms the temporary report is gone.
    # Retain the short-lived hashed similarity signature for the included recheck.
    with transaction(database_url) as cur:
        cur.execute("UPDATE ci_grants SET fingerprint='' WHERE job_id=%s", (job_id,))
