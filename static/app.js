/* static/app.js — Citation Crosschecker Dashboard (FIXED VERSION WITH EXPORTS) */

document.addEventListener("DOMContentLoaded", function () {

"use strict";

/* -------------------------------------------------------
CONFIG
------------------------------------------------------- */

const CONFIG = {
    POLL_INTERVAL: 1200,
    MAX_VERIFY_DISPLAY: 500
};

/* -------------------------------------------------------
DOM HELPERS
------------------------------------------------------- */

const $ = (id) => document.getElementById(id);

const el = {
    file: $("file"),
    style: $("style"),
    btnCheck: $("btnCheck"),
    btnVerify: $("btnVerify"),
    status: $("status"),
    resultsCard: $("resultsCard"),
    summaryTable: $("summaryTable"),
    refMsg: $("refMsg"),
    missingBody: $("missingBody"),
    uncitedBody: $("uncitedBody"),
    c2rBody: $("c2rBody"),
    r2cBody: $("r2cBody"),
    verifyDash: $("verifyDash"),
    verifyBody: $("verifyBody"),
    aciiCard: $("aciiCard"),
    aciiValue: $("aciiValue")
};

let LAST_JOB_ID = null;
let POLL_TIMER = null;
let CURRENT_DATA = null;

// Store latest results for export
window.latestResults = null;

/* -------------------------------------------------------
UTILITY
------------------------------------------------------- */

function esc(s) {
    return String(s ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
}

function toNum(x, d = 0) {
    const n = Number(x);
    return Number.isFinite(n) ? n : d;
}

function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg || "";
}

/* -------------------------------------------------------
TAB NAVIGATION (FIXED)
------------------------------------------------------- */

const tabs = document.querySelectorAll(".tab");
const panes = document.querySelectorAll(".tabPane");

// Initially hide all panes except the first
panes.forEach((pane, index) => {
    if (index === 0) {
        pane.classList.add("active");
    } else {
        pane.classList.remove("active");
    }
});

tabs.forEach(tab => {
    tab.addEventListener("click", () => {
        const target = tab.dataset.tab;

        tabs.forEach(t => t.classList.remove("active"));
        panes.forEach(p => p.classList.remove("active"));

        tab.classList.add("active");

        const pane = document.getElementById(target);
        if (pane) pane.classList.add("active");
    });
});

/* -------------------------------------------------------
EXPORT FUNCTIONS
------------------------------------------------------- */

function exportCSV(data) {
    if (!data) {
        alert("No data to export. Run a check first.");
        return;
    }

    const normalized = normalizeData(data);
    const s = normalized.summary || {};
    const missing = normalized.missing_in_references || [];
    const uncited = normalized.uncited_references || [];
    const c2r = normalized.reconciliation_intext_to_reference || [];
    const r2c = normalized.reconciliation_reference_to_intext || [];

    // Create CSV content
    let csv = [];

    // Summary section
    csv.push("=== SUMMARY ===");
    csv.push(`"Metric","Value"`);
    csv.push(`"In-text citations found","${s.in_text_citations_found || 0}"`);
    csv.push(`"Reference entries found","${s.reference_entries_found || 0}"`);
    csv.push(`"Missing in references","${s.missing_in_references || 0}"`);
    csv.push(`"Uncited references","${s.uncited_references || 0}"`);
    csv.push(`"Match rate","${s.match_rate || 0}"`);
    csv.push(``);

    // Missing citations section
    csv.push("=== MISSING CITATIONS ===");
    csv.push(`"#","Citation","Count"`);
    missing.forEach((item, idx) => {
        const citation = (typeof item === 'string') ? item : (item.citation_in_text || item);
        const count = (typeof item === 'string') ? 1 : (item.count_in_text || 1);
        csv.push(`"${idx + 1}","${escapeCsv(citation)}","${count}"`);
    });
    csv.push(``);

    // Uncited references section
    csv.push("=== UNCITED REFERENCES ===");
    csv.push(`"#","Reference"`);
    uncited.forEach((ref, idx) => {
        csv.push(`"${idx + 1}","${escapeCsv(ref)}"`);
    });
    csv.push(``);

    // Citation to Reference mapping
    csv.push("=== CITATION TO REFERENCE MAPPING ===");
    csv.push(`"#","Status","Citation","Matched Reference","Flags"`);
    const uniqueC2r = deduplicateCitations(c2r);
    uniqueC2r.forEach((item, idx) => {
        csv.push(`"${idx + 1}","${item.status || ''}","${escapeCsv(item.in_text || '')}","${escapeCsv(item.matched_reference || '')}","${item.flags || ''}"`);
    });
    csv.push(``);

    // Reference to Citation mapping
    csv.push("=== REFERENCE TO CITATION MAPPING ===");
    csv.push(`"#","Times Cited","Reference","Cited By"`);
    r2c.forEach((item, idx) => {
        const citedBy = (item.cited_by || []).slice(0, 3).join("; ");
        csv.push(`"${idx + 1}","${item.times_cited || 0}","${escapeCsv(item.reference || '')}","${escapeCsv(citedBy)}"`);
    });

    // Download file
    const blob = new Blob([csv.join("\n")], { type: "text/csv;charset=utf-8;" });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.setAttribute("href", url);
    link.setAttribute("download", `citation_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

function exportWordFile(data) {
    if (!data) {
        alert("No data to export. Run a check first.");
        return;
    }

    const normalized = normalizeData(data);
    const s = normalized.summary || {};
    const missing = normalized.missing_in_references || [];
    const uncited = normalized.uncited_references || [];
    const c2r = deduplicateCitations(normalized.reconciliation_intext_to_reference || []);
    const r2c = normalized.reconciliation_reference_to_intext || [];
    const timestamp = new Date().toLocaleString();

    // Build HTML content for Word
    let html = `<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Citation Crosscheck Report</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; }
        h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }
        h2 { color: #34495e; margin-top: 25px; border-left: 4px solid #3498db; padding-left: 10px; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 20px; }
        th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; vertical-align: top; }
        th { background-color: #f2f2f2; font-weight: bold; }
        tr:hover { background-color: #f5f5f5; }
        .summary-table { width: auto; }
        .badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
        .badge.verified { background: #27ae60; color: white; }
        .badge.not_found { background: #e74c3c; color: white; }
        .footer { margin-top: 30px; font-size: 11px; color: #7f8c8d; text-align: center; border-top: 1px solid #ddd; padding-top: 15px; }
    </style>
</head>
<body>
    <h1>📊 Citation Crosscheck Report</h1>
    <p><strong>Generated:</strong> ${timestamp}</p>
    <p><strong>File:</strong> ${esc(normalized.filename || 'N/A')}</p>
    <p><strong>Style:</strong> ${esc(normalized.style || 'APA/Harvard')}</p>
    
    <h2>📈 Summary</h2>
    <table class="summary-table">
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>In-text citations found</td><td>${s.in_text_citations_found || 0}</td></tr>
        <tr><td>Reference entries found</td><td>${s.reference_entries_found || 0}</td></tr>
        <tr><td>Missing in references</td><td><strong style="color: ${s.missing_in_references > 0 ? '#e67e22' : '#27ae60'}">${s.missing_in_references || 0}</strong></td></tr>
        <tr><td>Uncited references</td><td><strong style="color: ${s.uncited_references > 0 ? '#e67e22' : '#27ae60'}">${s.uncited_references || 0}</strong></td></tr>
        <tr><td>Match rate</td><td><strong style="color: ${s.match_rate >= 80 ? '#27ae60' : '#e67e22'}">${s.match_rate || 0}%</strong></td></tr>
    </table>
    
    <h2>❌ Missing Citations</h2>
    ${missing.length > 0 ? `
    <table>
        <thead><tr><th>#</th><th>Citation</th><th>Count</th></tr></thead>
        <tbody>
            ${missing.map((item, idx) => {
                const citation = (typeof item === 'string') ? item : (item.citation_in_text || item);
                const count = (typeof item === 'string') ? 1 : (item.count_in_text || 1);
                return `<tr><td>${idx + 1}</td><td>${esc(citation)}</td><td>${count}</td></tr>`;
            }).join('')}
        </tbody>
    </table>
    ` : '<p>✅ No missing citations found!</p>'}
    
    <h2>📌 Uncited References</h2>
    ${uncited.length > 0 ? `
    <table>
        <thead><tr><th>#</th><th>Reference</th></tr></thead>
        <tbody>
            ${uncited.slice(0, 50).map((ref, idx) => `<tr><td>${idx + 1}</td><td>${esc(ref.substring(0, 200))}${ref.length > 200 ? '...' : ''}</td></tr>`).join('')}
            ${uncited.length > 50 ? `<tr><td colspan="2">... and ${uncited.length - 50} more</td></tr>` : ''}
        </tbody>
    </table>
    ` : '<p>✅ All references are cited!</p>'}
    
    <h2>📝 Citation to Reference Mapping</h2>
    ${c2r.length > 0 ? `
    <table>
        <thead><tr><th>#</th><th>Status</th><th>Citation</th><th>Matched Reference</th><th>Flags</th></tr></thead>
        <tbody>
            ${c2r.map((item, idx) => `
                <tr>
                    <td>${idx + 1}</td>
                    <td>${item.status === 'matched' ? '✓ Matched' : '✗ Not Found'}</td>
                    <td>${esc(item.in_text || '')}</td>
                    <td>${esc((item.matched_reference || '').substring(0, 150))}${(item.matched_reference || '').length > 150 ? '...' : ''}</td>
                    <td>${esc(item.flags || '')}</td>
                </tr>
            `).join('')}
        </tbody>
    </table>
    ` : '<p>No mapping available.</p>'}
    
    <h2>📖 Reference to Citation Mapping</h2>
    ${r2c.length > 0 ? `
    <table>
        <thead><tr><th>#</th><th>Times Cited</th><th>Reference</th><th>Cited By</th></tr></thead>
        <tbody>
            ${r2c.slice(0, 100).map((item, idx) => `
                <tr>
                    <td>${idx + 1}</td>
                    <td>${item.times_cited || 0}</td>
                    <td>${esc((item.reference || '').substring(0, 150))}${(item.reference || '').length > 150 ? '...' : ''}</td>
                    <td>${esc((item.cited_by || []).slice(0, 2).join("; "))}</td>
                </tr>
            `).join('')}
            ${r2c.length > 100 ? `<tr><td colspan="4">... and ${r2c.length - 100} more references</td></tr>` : ''}
        </tbody>
    </table>
    ` : '<p>No mapping available.</p>'}
    
    <div class="footer">
        <p>Report generated by Citation Crosschecker | Engine: ${esc(normalized.engine_build || 'N/A')}</p>
        <p>${esc(normalized.reference_detection_message || '')}</p>
    </div>
</body>
</html>`;

    // Download Word file
    const blob = new Blob([html], { type: "application/msword" });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.setAttribute("href", url);
    link.setAttribute("download", `citation_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.doc`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
}

function escapeCsv(str) {
    if (!str) return '';
    // Escape quotes and wrap in quotes if contains comma, newline, or quote
    const escaped = String(str).replace(/"/g, '""');
    if (escaped.includes(',') || escaped.includes('\n') || escaped.includes('"')) {
        return `"${escaped}"`;
    }
    return escaped;
}

function deduplicateCitations(c2rRows) {
    const uniqueRows = [];
    const seen = new Set();

    c2rRows.forEach(row => {
        const citeText = row.in_text || '';
        const citeKey = citeText.toLowerCase().replace(/[^a-z0-9]/g, '');
        
        if (!seen.has(citeKey)) {
            seen.add(citeKey);
            uniqueRows.push(row);
        }
    });
    
    return uniqueRows;
}

/* -------------------------------------------------------
ACII HELPERS
------------------------------------------------------- */

function aciiCategory(score) {
    score = Number(score);
    if (score >= 90) return "Excellent";
    if (score >= 80) return "Very Good";
    if (score >= 70) return "Good";
    if (score >= 60) return "Moderate";
    if (score >= 50) return "Weak";
    return "Poor";
}

function aciiRemark(metric, score) {
    if (metric === "verification")
        return score + "% verified in scholarly databases";
    if (metric === "concentration")
        return "Measures whether citations rely heavily on few authors";
    if (metric === "diversity")
        return "Measures diversity of authors represented";
    if (metric === "temporal")
        return "Measures spread of publication years";
    return "";
}

/* -------------------------------------------------------
DATA NORMALIZATION
------------------------------------------------------- */

function normalizeData(payload) {
    const data = payload?.data || payload?.result || payload || {};
    const s = data.summary || {};

    data.summary = {
        in_text_citations_found: toNum(
            s.in_text_citations_found ??
            data.intext_count
        ),
        reference_entries_found: toNum(
            s.reference_entries_found ??
            data.reference_entries_found
        ),
        missing_in_references: toNum(
            s.missing_in_references ??
            (data.missing_in_references || []).length
        ),
        uncited_references: toNum(
            s.uncited_references ??
            (data.uncited_references || []).length
        ),
        match_rate: toNum(
            s.match_rate ??
            data.match_rate
        )
    };

    return data;
}

/* -------------------------------------------------------
SUMMARY
------------------------------------------------------- */

function renderSummaryTable(data) {
    const s = data?.summary || {};

    if (!el.summaryTable) return;

    el.summaryTable.innerHTML = `
        <tr><td>In-text citations</td><td>${esc(s.in_text_citations_found)}</td></tr>
        <tr><td>References</td><td>${esc(s.reference_entries_found)}</td></tr>
        <tr><td>Missing</td><td>${esc(s.missing_in_references)}</td></tr>
        <tr><td>Uncited</td><td>${esc(s.uncited_references)}</td></tr>
        <tr><td>Match rate</td><td>${esc(s.match_rate)}</td></tr>
    `;
}

/* -------------------------------------------------------
ACII
------------------------------------------------------- */

function renderACII(data) {
    const acii = data?.acii;

    if (!acii) return;

    if (el.aciiCard) el.aciiCard.style.display = "block";

    if (el.aciiValue)
        el.aciiValue.textContent = acii.ACII ?? "--";

    const c = acii.components || {};

    /* verification */
    if ($("aciiV"))
        $("aciiV").textContent = c.verification_integrity?.score ?? "";
    if ($("aciiVcat"))
        $("aciiVcat").textContent = c.verification_integrity?.category ?? "";
    if ($("aciiVremark"))
        $("aciiVremark").textContent = c.verification_integrity?.remark ?? "";

    /* concentration */
    if ($("aciiC"))
        $("aciiC").textContent = c.citation_concentration?.score ?? "";
    if ($("aciiCcat"))
        $("aciiCcat").textContent = c.citation_concentration?.category ?? "";
    if ($("aciiCremark"))
        $("aciiCremark").textContent = c.citation_concentration?.remark ?? "";

    /* diversity */
    if ($("aciiA"))
        $("aciiA").textContent = c.author_diversity?.score ?? "";
    if ($("aciiAcat"))
        $("aciiAcat").textContent = c.author_diversity?.category ?? "";
    if ($("aciiAremark"))
        $("aciiAremark").textContent = c.author_diversity?.remark ?? "";

    /* temporal */
    if ($("aciiT"))
        $("aciiT").textContent = c.temporal_balance?.score ?? "";
    if ($("aciiTcat"))
        $("aciiTcat").textContent = c.temporal_balance?.category ?? "";
    if ($("aciiTremark"))
        $("aciiTremark").textContent = c.temporal_balance?.remark ?? "";
}

/* -------------------------------------------------------
MISSING
------------------------------------------------------- */

function renderMissing(data) {
    const rows = data?.missing_in_references || [];

    if (!el.missingBody) return;

    if (!rows.length) {
        el.missingBody.innerHTML = `<tr><td colspan="3">None</td></tr>`;
        return;
    }

    el.missingBody.innerHTML = rows.map((r, i) => `
        <tr>
            <td>${i + 1}</td>
            <td>${esc(r.citation_in_text || r)}</td>
            <td>${esc(r.count_in_text || "")}</td>
        </tr>
    `).join("");
}

/* -------------------------------------------------------
UNCITED
------------------------------------------------------- */

function renderUncited(data) {
    const rows = data?.uncited_references || [];

    if (!el.uncitedBody) return;

    if (!rows.length) {
        el.uncitedBody.innerHTML = `<tr><td colspan="2">None</td></tr>`;
        return;
    }

    el.uncitedBody.innerHTML = rows.map((r, i) => `
        <tr>
            <td>${i + 1}</td>
            <td>${esc(r.reference || r)}</td>
        </tr>
    `).join("");
}

/* -------------------------------------------------------
IN-TEXT → REFERENCE (FIXED FOR UNIQUENESS)
------------------------------------------------------- */

function renderC2R(data) {
    const rows = data?.reconciliation_intext_to_reference || [];

    const uniqueRows = deduplicateCitations(rows);

    if (!el.c2rBody) return;

    if (!uniqueRows.length) {
        el.c2rBody.innerHTML = `<tr><td colspan="5">No mapping available</td></tr>`;
        return;
    }

    el.c2rBody.innerHTML = uniqueRows.map((r, i) => {
        let statusClass = '';
        if (r.status === 'matched') statusClass = 'verified';
        else if (r.status === 'not_found') statusClass = 'not_found';
        
        return `
        <tr>
            <td>${i + 1}</td>
            <td><span class="badge ${statusClass}">${esc(r.status || '')}</span></td>
            <td>${esc(r.in_text || '')}</td>
            <td>${esc(r.matched_reference || '')}</td>
            <td>${esc(r.flags || '')}</td>
        </tr>
    `}).join("");
}

/* -------------------------------------------------------
REFERENCE → IN-TEXT
------------------------------------------------------- */

function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];

    if (!el.r2cBody) return;

    if (!rows.length) {
        el.r2cBody.innerHTML = `<tr><td colspan="4">No mapping available</td></tr>`;
        return;
    }

    el.r2cBody.innerHTML = rows.map((r, i) => `
        <tr>
            <td>${i + 1}</td>
            <td>${esc(r.times_cited ?? 0)}</td>
            <td>${esc(r.reference || '')}</td>
            <td>${esc((r.cited_by || []).slice(0, 3).join("; "))}</td>
        </tr>
    `).join("");
}

/* -------------------------------------------------------
ONLINE VERIFICATION (FIXED DISPLAY)
------------------------------------------------------- */

function renderVerify(data) {
    const ov = data?.online_verification || {};
    const rows = ov.rows || [];
    const sum = ov.summary || {};

    if (el.verifyDash) {
        el.verifyDash.innerHTML = `
            <div class="kpi">✅ Verified: ${sum.verified ?? 0}</div>
            <div class="kpi">🔍 Likely: ${sum.likely ?? 0}</div>
            <div class="kpi">⚠️ Needs Review: ${sum.needs_review ?? 0}</div>
            <div class="kpi">❌ Not Found: ${sum.not_found ?? 0}</div>
            <div class="kpi">📡 Offline: ${sum.offline ?? 0}</div>
        `;
    }

    if (!el.verifyBody) return;

    if (!rows.length) {
        el.verifyBody.innerHTML = `<tr><td colspan="9">No verification results. Click "Run Online Verification" to start.</td></tr>`;
        return;
    }

    el.verifyBody.innerHTML = rows
        .slice(0, CONFIG.MAX_VERIFY_DISPLAY)
        .map((r, i) => {
            let badgeClass = '';
            if (r.status === 'verified') badgeClass = 'verified';
            else if (r.status === 'likely') badgeClass = 'likely';
            else if (r.status === 'needs_review') badgeClass = 'needs_review';
            else if (r.status === 'not_found') badgeClass = 'not_found';
            else if (r.status === 'offline') badgeClass = 'offline';

            return `
            <tr>
                <td>${i + 1}</td>
                <td><span class="badge ${badgeClass}">${esc(r.status || '')}</span></td>
                <td>${esc(r.source || '—')}</td>
                <td>${esc(r.score || '—')}</td>
                <td>${esc(r.doi || '—')}</td>
                <td>${esc(r.matched_year || '—')}</td>
                <td>${esc(r.matched_authors || '—')}</td>
                <td>${esc((r.matched_title || '').substring(0, 50))}${(r.matched_title || '').length > 50 ? '…' : ''}</td>
                <td>${esc(r.query_used || '—')}</td>
            </tr>
        `}).join("");
}

/* -------------------------------------------------------
MASTER RENDER
------------------------------------------------------- */

function renderAll(data) {
    if (!data) return;

    CURRENT_DATA = normalizeData(data);
    
    // Store for exports
    window.latestResults = CURRENT_DATA;
    
    // Enable export buttons
    const exportCsv = document.getElementById("btnExportCsvTop");
    const exportWord = document.getElementById("btnExportWordTop");
    if (exportCsv) exportCsv.disabled = false;
    if (exportWord) exportWord.disabled = false;

    if (el.resultsCard) el.resultsCard.style.display = "block";

    renderSummaryTable(CURRENT_DATA);
    renderACII(CURRENT_DATA);
    renderMissing(CURRENT_DATA);
    renderUncited(CURRENT_DATA);
    renderC2R(CURRENT_DATA);
    renderR2C(CURRENT_DATA);
    renderVerify(CURRENT_DATA);
}

/* -------------------------------------------------------
RUN INITIAL CHECK
------------------------------------------------------- */

async function runInitialCheck() {
    const f = el.file?.files?.[0];

    if (!f) {
        setStatus("Please choose a file first", "warn");
        return;
    }

    setStatus("Analyzing document...");

    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");

    try {
        const res = await fetch("/verify", { method: "POST", body: fd });
        const js = await res.json();

        LAST_JOB_ID = js.job_id;
        renderAll(js);
        setStatus("Analysis complete", "good");
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
    }
}

/* -------------------------------------------------------
RUN ONLINE VERIFICATION
------------------------------------------------------- */

async function runOnlineVerification() {
    if (!LAST_JOB_ID) {
        setStatus("Run document check first", "warn");
        return;
    }

    setStatus("Starting online verification...");

    const fd = new FormData();
    fd.append("job_id", LAST_JOB_ID);

    try {
        await fetch("/verify-online", { method: "POST", body: fd });
        startPolling();
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
    }
}

/* -------------------------------------------------------
POLLING
------------------------------------------------------- */

function startPolling() {
    if (POLL_TIMER) clearInterval(POLL_TIMER);

    POLL_TIMER = setInterval(async () => {
        try {
            const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}`);
            const js = await res.json();

            if (js.result) renderAll(js.result);

            if (js.online?.state === "done") {
                clearInterval(POLL_TIMER);
                setStatus("Online verification complete", "good");
            }

            if (js.online?.state === "error") {
                clearInterval(POLL_TIMER);
                setStatus(js.online?.message || "Verification failed", "warn");
            }
        } catch (err) {
            console.error("Polling error:", err);
        }
    }, CONFIG.POLL_INTERVAL);
}

/* -------------------------------------------------------
BUTTON EVENTS
------------------------------------------------------- */

if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
if (el.btnVerify) el.btnVerify.addEventListener("click", runOnlineVerification);

// Export buttons - enable after results are loaded
const exportCsv = document.getElementById("btnExportCsvTop");
const exportWord = document.getElementById("btnExportWordTop");

// Initially disabled
if (exportCsv) exportCsv.disabled = true;
if (exportWord) exportWord.disabled = true;

// Add click handlers
if (exportCsv) {
    exportCsv.addEventListener("click", () => {
        if (!window.latestResults) {
            alert("Run a check first to export data.");
            return;
        }
        exportCSV(window.latestResults);
    });
}

if (exportWord) {
    exportWord.addEventListener("click", () => {
        if (!window.latestResults) {
            alert("Run a check first to export data.");
            return;
        }
        exportWordFile(window.latestResults);
    });
}

});
