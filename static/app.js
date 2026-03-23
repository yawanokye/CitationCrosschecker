/* static/app.js — Citation Crosschecker Dashboard (STABILIZED + EXPORT FIXED) */

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

window.latestResults = null;

/* -------------------------------------------------------
SCHEMA STABILIZER (🔥 CRITICAL FIX)
------------------------------------------------------- */

function unifyC2R(rows) {
    return (rows || []).map(r => ({
        status: r.status,
        citation: r.in_text || r.citation || "",
        reference: r.matched_reference || r.reference || "",
        flags: r.flags || ""
    }));
}

function unifyR2C(rows) {
    return (rows || []).map(r => ({
        reference: r.reference || r.matched_reference || "",
        times_cited: r.times_cited ?? 0,
        citations: r.cited_by || r.citations || []
    }));
}

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
EXPORT FUNCTIONS (FIXED)
------------------------------------------------------- */

function exportCSV(data) {
    if (!data) return alert("No data to export");

    const normalized = normalizeData(data);
    const c2r = unifyC2R(normalized.reconciliation_intext_to_reference);
    const r2c = unifyR2C(normalized.reconciliation_reference_to_intext);

    let csv = [];

    csv.push("=== CITATION TO REFERENCE ===");
    csv.push(`"#","Status","Citation","Reference"`);

    c2r.forEach((r, i) => {
        csv.push(`"${i+1}","${r.status}","${escapeCsv(r.citation)}","${escapeCsv(r.reference)}"`);
    });

    csv.push(``);
    csv.push("=== REFERENCE TO CITATION ===");
    csv.push(`"#","Times","Reference","Cited By"`);

    r2c.forEach((r, i) => {
        csv.push(`"${i+1}","${r.times_cited}","${escapeCsv(r.reference)}","${escapeCsv(r.citations.join("; "))}"`);
    });

    download(csv.join("\n"), "report.csv", "text/csv");
}

function exportWordFile(data) {
    if (!data) return alert("No data");

    const normalized = normalizeData(data);
    const c2r = unifyC2R(normalized.reconciliation_intext_to_reference);
    const r2c = unifyR2C(normalized.reconciliation_reference_to_intext);

    let html = `<h1>Citation Report</h1>`;

    html += `<h2>Citations</h2>`;
    html += `<table border="1"><tr><th>#</th><th>Status</th><th>Citation</th><th>Reference</th></tr>`;
    c2r.forEach((r,i)=>{
        html += `<tr><td>${i+1}</td><td>${r.status}</td><td>${esc(r.citation)}</td><td>${esc(r.reference)}</td></tr>`;
    });
    html += `</table>`;

    html += `<h2>References</h2>`;
    html += `<table border="1"><tr><th>#</th><th>Times</th><th>Reference</th></tr>`;
    r2c.forEach((r,i)=>{
        html += `<tr><td>${i+1}</td><td>${r.times_cited}</td><td>${esc(r.reference)}</td></tr>`;
    });
    html += `</table>`;

    download(html, "report.doc", "application/msword");
}

function download(content, name, type) {
    const blob = new Blob([content], {type});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
}

function escapeCsv(str) {
    return `"${String(str).replace(/"/g,'""')}"`;
}

/* -------------------------------------------------------
RENDER FIXES
------------------------------------------------------- */

function renderC2R(data) {
    const rows = unifyC2R(data?.reconciliation_intext_to_reference);

    el.c2rBody.innerHTML = rows.map((r,i)=>`
        <tr>
            <td>${i+1}</td>
            <td>${esc(r.status)}</td>
            <td>${esc(r.citation)}</td>
            <td>${esc(r.reference)}</td>
            <td>${esc(r.flags)}</td>
        </tr>
    `).join("");
}

function renderR2C(data) {
    const rows = unifyR2C(data?.reconciliation_reference_to_intext);

    el.r2cBody.innerHTML = rows.map((r,i)=>`
        <tr>
            <td>${i+1}</td>
            <td>${r.times_cited}</td>
            <td>${esc(r.reference)}</td>
            <td>${esc(r.citations.join("; "))}</td>
        </tr>
    `).join("");
}

/* -------------------------------------------------------
MASTER RENDER
------------------------------------------------------- */

function renderAll(data) {
    CURRENT_DATA = normalizeData(data);
    window.latestResults = CURRENT_DATA;

    renderC2R(CURRENT_DATA);
    renderR2C(CURRENT_DATA);

    // enable export
    const exportCsv = $("btnExportCsvTop");
    const exportWord = $("btnExportWordTop");

    if (exportCsv) exportCsv.disabled = false;
    if (exportWord) exportWord.disabled = false;
}

/* -------------------------------------------------------
RUN CHECK
------------------------------------------------------- */

async function runInitialCheck() {
    const f = el.file?.files?.[0];
    if (!f) return setStatus("Choose file", "warn");

    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", "apa");

    const res = await fetch("/verify", {method:"POST", body:fd});
    const js = await res.json();

    LAST_JOB_ID = js.job_id;
    renderAll(js);
}

/* -------------------------------------------------------
EVENTS
------------------------------------------------------- */

if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);

const exportCsv = $("btnExportCsvTop");
const exportWord = $("btnExportWordTop");

if (exportCsv) {
    exportCsv.disabled = true;
    exportCsv.addEventListener("click", () => exportCSV(window.latestResults));
}

if (exportWord) {
    exportWord.disabled = true;
    exportWord.addEventListener("click", () => exportWordFile(window.latestResults));
}

});
