# admin_dashboard.py
import os
import io
import csv
import json
import psycopg2
from psycopg2.extras import RealDictCursor, Json
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, Response

router = APIRouter()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ADMIN_DASHBOARD_TOKEN = os.getenv("ADMIN_DASHBOARD_TOKEN", "").strip()


def _conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg2.connect(DATABASE_URL)


def admin_allowed(request: Request) -> bool:
    token = (
        request.query_params.get("token")
        or request.headers.get("x-admin-token")
        or ""
    ).strip()

    return bool(ADMIN_DASHBOARD_TOKEN) and token == ADMIN_DASHBOARD_TOKEN


def init_commercial_dashboard_table():
    if not DATABASE_URL:
        print("[ADMIN DASHBOARD] DATABASE_URL not found. Table skipped.")
        return

    conn = _conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS commercial_dashboard_records (
            id BIGSERIAL PRIMARY KEY,
            job_id TEXT UNIQUE NOT NULL,

            file_name TEXT,
            email TEXT,

            plan_key TEXT,
            plan_name TEXT,

            currency TEXT,
            amount NUMERIC(12,2),
            amount_minor INTEGER,
            payment_reference TEXT,
            payment_status TEXT DEFAULT 'unpaid',
            paid BOOLEAN DEFAULT FALSE,

            analysis_status TEXT,

            reference_count INTEGER DEFAULT 0,
            citation_count INTEGER DEFAULT 0,
            missing_count INTEGER DEFAULT 0,
            uncited_count INTEGER DEFAULT 0,
            match_rate NUMERIC(8,2) DEFAULT 0,
            acii_score NUMERIC(8,2),

            verified_count INTEGER DEFAULT 0,
            likely_count INTEGER DEFAULT 0,
            needs_review_count INTEGER DEFAULT 0,
            not_found_count INTEGER DEFAULT 0,
            offline_count INTEGER DEFAULT 0,

            recovery_count INTEGER DEFAULT 0,
            claim_count INTEGER DEFAULT 0,

            manual_verified_count INTEGER DEFAULT 0,
            plausible_count INTEGER DEFAULT 0,
            manual_not_verified_count INTEGER DEFAULT 0,
            manual_needs_review_count INTEGER DEFAULT 0,

            certificate_generated BOOLEAN DEFAULT FALSE,

            raw_stats JSONB DEFAULT '{}'::jsonb,

            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_commercial_dashboard_records_job_id
        ON commercial_dashboard_records(job_id);
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_commercial_dashboard_records_email
        ON commercial_dashboard_records(email);
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_commercial_dashboard_records_paid
        ON commercial_dashboard_records(paid);
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_commercial_dashboard_records_updated_at
        ON commercial_dashboard_records(updated_at DESC);
    """)

    conn.commit()
    cur.close()
    conn.close()

    print("✅ Commercial admin dashboard table checked")


def _safe_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_float(value, default=0):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _count_status(rows, field_names):
    counts = {}

    for row in rows or []:
        if not isinstance(row, dict):
            continue

        status = ""
        for field in field_names:
            if row.get(field):
                status = str(row.get(field)).strip().lower()
                break

        if status:
            counts[status] = counts.get(status, 0) + 1

    return counts


def _walk_manual_decisions(obj, out=None):
    if out is None:
        out = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in {"manual_decision", "manual_decision_label", "display_status", "decision"}:
                if value:
                    out.append(str(value).strip().lower())
            else:
                _walk_manual_decisions(value, out)

    elif isinstance(obj, list):
        for item in obj:
            _walk_manual_decisions(item, out)

    return out


def extract_stats_from_result(result: dict) -> dict:
    result = result or {}
    summary = result.get("summary") or {}

    online = result.get("online_verification") or {}
    verification_rows = online.get("rows") or []
    verification_summary = online.get("summary") or {}

    recovery = result.get("recovery") or {}
    missing_recovery = recovery.get("missing_recovery") or []
    verification_recovery = recovery.get("verification_recovery") or []

    claim_rows = result.get("claim_support") or []
    if not isinstance(claim_rows, list):
        claim_rows = []

    acii = result.get("acii") or {}

    verification_counts = _count_status(
        verification_rows,
        ["status", "verification_status"]
    )

    manual_decisions = _walk_manual_decisions(result)

    return {
        "file_name": (
            result.get("file_name")
            or result.get("filename")
            or result.get("original_filename")
        ),
        "analysis_status": result.get("status") or "completed",

        "reference_count": _safe_int(
            summary.get("reference_entries_found")
            or result.get("reference_entries_found")
            or len(result.get("references_raw") or [])
        ),
        "citation_count": _safe_int(
            summary.get("in_text_citations_found")
            or result.get("in_text_citations_found")
            or len(result.get("in_text_citations") or [])
        ),
        "missing_count": _safe_int(
            summary.get("missing_in_references")
            or len(result.get("missing_in_references") or [])
        ),
        "uncited_count": _safe_int(
            summary.get("uncited_references")
            or len(result.get("uncited_references") or [])
        ),
        "match_rate": _safe_float(summary.get("match_rate")),

        "acii_score": _safe_float(
            acii.get("ACII")
            or acii.get("score")
            or result.get("acii_score"),
            default=None
        ),

        "verified_count": _safe_int(
            verification_summary.get("verified")
            if isinstance(verification_summary, dict)
            else verification_counts.get("verified", 0)
        ),
        "likely_count": _safe_int(
            verification_summary.get("likely")
            if isinstance(verification_summary, dict)
            else verification_counts.get("likely", 0)
        ),
        "needs_review_count": _safe_int(
            verification_summary.get("needs_review")
            if isinstance(verification_summary, dict)
            else verification_counts.get("needs_review", 0)
        ),
        "not_found_count": _safe_int(
            verification_summary.get("not_found")
            if isinstance(verification_summary, dict)
            else verification_counts.get("not_found", 0)
        ),
        "offline_count": _safe_int(verification_counts.get("offline", 0)),

        "recovery_count": _safe_int(len(missing_recovery) + len(verification_recovery)),
        "claim_count": _safe_int(len(claim_rows)),

        "manual_verified_count": _safe_int(manual_decisions.count("manual_verified")),
        "plausible_count": _safe_int(
            manual_decisions.count("plausible")
            + manual_decisions.count("not_indexed_but_plausible")
        ),
        "manual_not_verified_count": _safe_int(manual_decisions.count("manual_not_verified")),
        "manual_needs_review_count": _safe_int(
            manual_decisions.count("needs_review")
            + manual_decisions.count("keep_needs_review")
        ),

        "certificate_generated": bool(result.get("citation_integrity_certificate")),

        "raw_stats": {
            "summary": summary,
            "online_verification_summary": verification_summary,
            "acii": acii,
            "access": result.get("access") or {},
        },
    }


def record_dashboard(job_id: str, **fields):
    if not DATABASE_URL:
        return

    job_id = str(job_id or "").strip()
    if not job_id:
        return

    allowed = {
        "file_name",
        "email",
        "plan_key",
        "plan_name",
        "currency",
        "amount",
        "amount_minor",
        "payment_reference",
        "payment_status",
        "paid",
        "analysis_status",
        "reference_count",
        "citation_count",
        "missing_count",
        "uncited_count",
        "match_rate",
        "acii_score",
        "verified_count",
        "likely_count",
        "needs_review_count",
        "not_found_count",
        "offline_count",
        "recovery_count",
        "claim_count",
        "manual_verified_count",
        "plausible_count",
        "manual_not_verified_count",
        "manual_needs_review_count",
        "certificate_generated",
        "raw_stats",
    }

    clean = {
        key: value
        for key, value in fields.items()
        if key in allowed and value is not None
    }

    if not clean:
        return

    if "raw_stats" in clean:
        clean["raw_stats"] = Json(clean["raw_stats"] or {})

    columns = ["job_id"] + list(clean.keys())
    values = [job_id] + list(clean.values())

    placeholders = ", ".join(["%s"] * len(columns))
    columns_sql = ", ".join(columns)

    update_sql = ", ".join([
        f"{column} = EXCLUDED.{column}"
        for column in clean.keys()
    ])

    sql = f"""
        INSERT INTO commercial_dashboard_records ({columns_sql})
        VALUES ({placeholders})
        ON CONFLICT (job_id)
        DO UPDATE SET
            {update_sql},
            updated_at = NOW();
    """

    try:
        conn = _conn()
        cur = conn.cursor()
        cur.execute(sql, values)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[ADMIN DASHBOARD] Record failed for {job_id}: {e}")


def record_dashboard_from_result(job_id: str, result: dict, **extra):
    stats = extract_stats_from_result(result or {})
    stats.update(extra or {})
    record_dashboard(job_id, **stats)


@router.get("/admin/api/commercial-dashboard")
async def commercial_dashboard_api(
    request: Request,
    q: str = "",
    paid: str = "",
    limit: int = 300,
):
    if not admin_allowed(request):
        raise HTTPException(status_code=403, detail="Admin dashboard access denied.")

    limit = max(1, min(int(limit or 300), 1000))

    where = []
    params = []

    if q:
        where.append("""
            (
                job_id ILIKE %s
                OR file_name ILIKE %s
                OR email ILIKE %s
                OR payment_reference ILIKE %s
                OR plan_name ILIKE %s
            )
        """)
        like = f"%{q}%"
        params.extend([like, like, like, like, like])

    if paid in {"true", "false"}:
        where.append("paid = %s")
        params.append(paid == "true")

    where_sql = "WHERE " + " AND ".join(where) if where else ""

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute(f"""
        SELECT *
        FROM commercial_dashboard_records
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT %s
    """, params + [limit])

    rows = cur.fetchall()

    cur.execute("""
        SELECT
            COUNT(*) AS total_jobs,
            COALESCE(SUM(CASE WHEN paid THEN 1 ELSE 0 END), 0) AS paid_jobs,
            COALESCE(SUM(CASE WHEN paid THEN amount ELSE 0 END), 0) AS revenue,
            COALESCE(SUM(reference_count), 0) AS total_references,
            COALESCE(SUM(citation_count), 0) AS total_citations,
            COALESCE(SUM(verified_count), 0) AS total_verified,
            COALESCE(SUM(not_found_count), 0) AS total_not_found
        FROM commercial_dashboard_records
    """)

    totals = cur.fetchone()

    cur.close()
    conn.close()

    return {
        "ok": True,
        "totals": totals,
        "rows": rows,
    }


@router.get("/admin/api/commercial-dashboard.csv")
async def commercial_dashboard_csv(request: Request):
    if not admin_allowed(request):
        raise HTTPException(status_code=403, detail="Admin dashboard access denied.")

    conn = _conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("""
        SELECT *
        FROM commercial_dashboard_records
        ORDER BY updated_at DESC
    """)

    rows = cur.fetchall()
    cur.close()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)

    headers = [
        "updated_at",
        "job_id",
        "file_name",
        "email",
        "plan_name",
        "currency",
        "amount",
        "payment_status",
        "paid",
        "reference_count",
        "citation_count",
        "missing_count",
        "uncited_count",
        "match_rate",
        "acii_score",
        "verified_count",
        "likely_count",
        "needs_review_count",
        "not_found_count",
        "offline_count",
        "recovery_count",
        "claim_count",
        "manual_verified_count",
        "plausible_count",
        "manual_not_verified_count",
        "manual_needs_review_count",
        "certificate_generated",
        "payment_reference",
    ]

    writer.writerow(headers)

    for row in rows:
        writer.writerow([row.get(h) for h in headers])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": "attachment; filename=CiteIntegrity_Commercial_Dashboard.csv"
        },
    )


@router.get("/admin/commercial-dashboard", response_class=HTMLResponse)
async def commercial_dashboard_page(request: Request):
    if not admin_allowed(request):
        return HTMLResponse(
            """
            <html>
            <body style="font-family:Arial;padding:40px;">
                <h2>Access denied</h2>
                <p>Add your admin token to the URL:</p>
                <code>/admin/commercial-dashboard?token=YOUR_TOKEN</code>
            </body>
            </html>
            """,
            status_code=403,
        )

    return HTMLResponse("""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>CiteIntegrity Commercial Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">

    <style>
        :root {
            --navy:#0f172a;
            --green:#13855a;
            --slate:#64748b;
            --line:#e2e8f0;
            --soft:#f8fafc;
            --white:#ffffff;
        }

        * { box-sizing:border-box; }

        body {
            margin:0;
            font-family:Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
            background:var(--soft);
            color:var(--navy);
        }

        header {
            background:white;
            border-bottom:1px solid var(--line);
            padding:16px 24px;
            position:sticky;
            top:0;
            z-index:10;
        }

        .header-inner {
            max-width:1500px;
            margin:0 auto;
            display:flex;
            justify-content:space-between;
            align-items:center;
            gap:16px;
            flex-wrap:wrap;
        }

        h1 {
            font-size:24px;
            margin:0;
            letter-spacing:-0.5px;
        }

        .sub {
            color:var(--slate);
            font-size:13px;
            margin-top:4px;
        }

        main {
            max-width:1500px;
            margin:0 auto;
            padding:20px 24px 42px;
        }

        .kpis {
            display:grid;
            grid-template-columns:repeat(6, minmax(140px, 1fr));
            gap:12px;
            margin-bottom:16px;
        }

        .kpi {
            background:white;
            border:1px solid var(--line);
            border-radius:18px;
            padding:14px;
            box-shadow:0 8px 22px rgba(15,23,42,0.05);
        }

        .kpi strong {
            display:block;
            font-size:24px;
            margin-bottom:4px;
        }

        .kpi span {
            color:var(--slate);
            font-size:12px;
            font-weight:700;
        }

        .toolbar {
            display:flex;
            gap:10px;
            flex-wrap:wrap;
            margin-bottom:14px;
            background:white;
            border:1px solid var(--line);
            border-radius:18px;
            padding:12px;
        }

        input, select, button, a.btn {
            border:1px solid var(--line);
            border-radius:999px;
            padding:10px 14px;
            font:inherit;
            background:white;
        }

        button, a.btn {
            cursor:pointer;
            font-weight:800;
            text-decoration:none;
            color:var(--navy);
        }

        .primary {
            background:var(--navy);
            color:white !important;
            border-color:var(--navy);
        }

        .table-card {
            background:white;
            border:1px solid var(--line);
            border-radius:22px;
            overflow:hidden;
            box-shadow:0 8px 22px rgba(15,23,42,0.05);
        }

        .table-wrap {
            overflow:auto;
            max-height:72vh;
        }

        table {
            width:100%;
            border-collapse:collapse;
            min-width:1750px;
            font-size:12.5px;
        }

        th, td {
            padding:10px 12px;
            border-bottom:1px solid #eef2f6;
            text-align:left;
            vertical-align:top;
        }

        th {
            position:sticky;
            top:0;
            background:#f8fafc;
            z-index:2;
            font-size:11px;
            text-transform:uppercase;
            letter-spacing:0.4px;
        }

        tr:hover td { background:#fcfdff; }

        .badge {
            display:inline-flex;
            border-radius:999px;
            padding:4px 8px;
            font-size:11px;
            font-weight:900;
            white-space:nowrap;
        }

        .paid { background:#ecfdf5; color:var(--green); }
        .unpaid { background:#fff7ed; color:#c2410c; }
        .muted { color:var(--slate); }

        @media (max-width:900px) {
            .kpis { grid-template-columns:1fr 1fr; }
            main { padding:14px; }
        }
    </style>
</head>

<body>
<header>
    <div class="header-inner">
        <div>
            <h1>CiteIntegrity Commercial Dashboard</h1>
            <div class="sub">Files, emails, payment records, analysis counts and verification statistics.</div>
        </div>
        <div><a class="btn" href="/developer/access">Developer Access Control</a> <a class="btn primary" id="downloadCsv" href="#">Download CSV</a></div>
    </div>
</header>

<main>
    <section class="kpis" id="kpis"></section>

    <section class="toolbar">
        <input id="searchBox" placeholder="Search email, file, job ID, payment reference or plan" style="min-width:340px;">
        <select id="paidFilter">
            <option value="">All payment statuses</option>
            <option value="true">Paid only</option>
            <option value="false">Unpaid only</option>
        </select>
        <button class="primary" id="btnLoad">Refresh</button>
    </section>

    <section class="table-card">
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Updated</th>
                        <th>File name</th>
                        <th>Email</th>
                        <th>Plan</th>
                        <th>Amount</th>
                        <th>Payment</th>
                        <th>Job ID</th>
                        <th>Refs</th>
                        <th>Citations</th>
                        <th>Missing</th>
                        <th>Uncited</th>
                        <th>Match</th>
                        <th>ACII</th>
                        <th>Verified</th>
                        <th>Likely</th>
                        <th>Needs review</th>
                        <th>Not found</th>
                        <th>Offline</th>
                        <th>Recovery</th>
                        <th>Claims</th>
                        <th>Manual verified</th>
                        <th>Plausible</th>
                        <th>Manual not verified</th>
                        <th>Manual needs review</th>
                        <th>Certificate</th>
                    </tr>
                </thead>
                <tbody id="rowsBody">
                    <tr><td colspan="25">Loading dashboard...</td></tr>
                </tbody>
            </table>
        </div>
    </section>
</main>

<script>
const params = new URLSearchParams(window.location.search);
const TOKEN = params.get("token") || "";

function esc(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
}

function fmtDate(value) {
    if (!value) return "—";
    try { return new Date(value).toLocaleString(); }
    catch { return value; }
}

function fmtMoney(row) {
    if (!row.currency && !row.amount) return "—";
    return `${esc(row.currency || "")} ${esc(row.amount ?? "")}`;
}

function badgePaid(row) {
    if (row.paid) return '<span class="badge paid">Paid</span>';
    return '<span class="badge unpaid">Unpaid</span>';
}

async function loadDashboard() {
    const q = document.getElementById("searchBox").value || "";
    const paid = document.getElementById("paidFilter").value || "";

    const url = `/admin/api/commercial-dashboard?token=${encodeURIComponent(TOKEN)}&q=${encodeURIComponent(q)}&paid=${encodeURIComponent(paid)}&limit=500`;

    const res = await fetch(url);
    const js = await res.json();

    if (!res.ok || js.ok === false) {
        document.getElementById("rowsBody").innerHTML =
            `<tr><td colspan="25">Could not load dashboard: ${esc(js.detail || js.error || "Unknown error")}</td></tr>`;
        return;
    }

    const t = js.totals || {};

    document.getElementById("kpis").innerHTML = `
        <div class="kpi"><strong>${esc(t.total_jobs || 0)}</strong><span>Total jobs</span></div>
        <div class="kpi"><strong>${esc(t.paid_jobs || 0)}</strong><span>Paid jobs</span></div>
        <div class="kpi"><strong>${esc(t.revenue || 0)}</strong><span>Total revenue</span></div>
        <div class="kpi"><strong>${esc(t.total_references || 0)}</strong><span>References checked</span></div>
        <div class="kpi"><strong>${esc(t.total_citations || 0)}</strong><span>Citations checked</span></div>
        <div class="kpi"><strong>${esc(t.total_verified || 0)}</strong><span>Verified refs</span></div>
    `;

    const rows = js.rows || [];

    if (!rows.length) {
        document.getElementById("rowsBody").innerHTML =
            `<tr><td colspan="25">No dashboard records found.</td></tr>`;
        return;
    }

    document.getElementById("rowsBody").innerHTML = rows.map(row => `
        <tr>
            <td>${esc(fmtDate(row.updated_at))}</td>
            <td><strong>${esc(row.file_name || "—")}</strong></td>
            <td>${esc(row.email || "—")}</td>
            <td>${esc(row.plan_name || row.plan_key || "—")}</td>
            <td>${fmtMoney(row)}</td>
            <td>${badgePaid(row)}<div class="muted">${esc(row.payment_status || "")}</div></td>
            <td><code>${esc(row.job_id)}</code></td>
            <td>${esc(row.reference_count)}</td>
            <td>${esc(row.citation_count)}</td>
            <td>${esc(row.missing_count)}</td>
            <td>${esc(row.uncited_count)}</td>
            <td>${esc(row.match_rate)}%</td>
            <td>${esc(row.acii_score ?? "—")}</td>
            <td>${esc(row.verified_count)}</td>
            <td>${esc(row.likely_count)}</td>
            <td>${esc(row.needs_review_count)}</td>
            <td>${esc(row.not_found_count)}</td>
            <td>${esc(row.offline_count)}</td>
            <td>${esc(row.recovery_count)}</td>
            <td>${esc(row.claim_count)}</td>
            <td>${esc(row.manual_verified_count)}</td>
            <td>${esc(row.plausible_count)}</td>
            <td>${esc(row.manual_not_verified_count)}</td>
            <td>${esc(row.manual_needs_review_count)}</td>
            <td>${row.certificate_generated ? '<span class="badge paid">Generated</span>' : '<span class="badge unpaid">No</span>'}</td>
        </tr>
    `).join("");
}

document.getElementById("btnLoad").addEventListener("click", loadDashboard);
document.getElementById("paidFilter").addEventListener("change", loadDashboard);
document.getElementById("searchBox").addEventListener("keydown", e => {
    if (e.key === "Enter") loadDashboard();
});

document.getElementById("downloadCsv").href =
    `/admin/api/commercial-dashboard.csv?token=${encodeURIComponent(TOKEN)}`;

loadDashboard();
</script>
</body>
</html>
    """)
