/* static/app.js — Citation Crosschecker with Progress Tracking */

document.addEventListener("DOMContentLoaded", function () {

"use strict";

/* -------------------------------------------------------
CONFIG
------------------------------------------------------- */

const CONFIG = {
    POLL_INTERVAL: 1500,  // Increased for better UX
    MAX_VERIFY_DISPLAY: 500,
    DEBUG: true,
    RETRY_DELAY: 30000  // 30 seconds retry delay for busy server
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
    btnExportVerify: $("btnExportVerify"),
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
    aciiValue: $("aciiValue"),
    aciiDescription: $("aciiDescription"),
    verifyProgress: $("verifyProgress"),
    progressBar: $("progressBar"),
    progressText: $("progressText"),
    queueStatus: $("queueStatus"),
    serverStatus: $("serverStatus"),
    debugPanel: $("debugPanel")
};

let LAST_JOB_ID = null;
let POLL_TIMER = null;
let CURRENT_DATA = null;
let VERIFICATION_IN_PROGRESS = false;

window.latestResults = null;

/* -------------------------------------------------------
DEBUG LOGGING
------------------------------------------------------- */

function debugLog(message, data = null) {
    if (!CONFIG.DEBUG) return;
    
    const timestamp = new Date().toLocaleTimeString();
    const logEntry = `[${timestamp}] ${message}`;
    console.log(logEntry, data || '');
    
    if (el.debugPanel) {
        const logDiv = document.createElement('div');
        logDiv.className = 'debug-entry';
        logDiv.style.fontSize = '11px';
        logDiv.style.fontFamily = 'monospace';
        logDiv.style.borderBottom = '1px solid #eee';
        logDiv.style.padding = '2px 0';
        logDiv.textContent = logEntry;
        el.debugPanel.appendChild(logDiv);
        
        while (el.debugPanel.children.length > 100) {
            el.debugPanel.removeChild(el.debugPanel.firstChild);
        }
        el.debugPanel.scrollTop = el.debugPanel.scrollHeight;
    }
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
    debugLog(`Status: ${msg} (${tone})`);
}

/* -------------------------------------------------------
QUEUE STATUS DISPLAY
------------------------------------------------------- */

async function updateQueueStatus() {
    try {
        const response = await fetch('/queue/status');
        const data = await response.json();
        
        if (el.queueStatus) {
            const busyClass = data.is_busy ? 'busy' : 'ready';
            el.queueStatus.innerHTML = `
                <div class="queue-info ${busyClass}">
                    <span>📊 Queue: ${data.queue_size} waiting</span>
                    <span>⏳ Pending: ${data.pending_jobs}</span>
                    <span>⚙️ Processing: ${data.processing_jobs}</span>
                    <span class="server-status ${busyClass}">
                        ${data.is_busy ? '⚠️ Server Busy' : '✅ Server Ready'}
                    </span>
                </div>
            `;
        }
        
        return data;
    } catch (err) {
        debugLog("Queue status error: " + err.message);
        return null;
    }
}

/* -------------------------------------------------------
PROGRESS BAR UPDATE
------------------------------------------------------- */

function updateProgress(progress, total, status = "processing") {
    if (!el.progressBar || !el.progressText) return;
    
    const percentage = total > 0 ? Math.round((progress / total) * 100) : 0;
    
    el.progressBar.style.width = `${percentage}%`;
    el.progressBar.setAttribute('aria-valuenow', percentage);
    
    if (status === "completed") {
        el.progressBar.style.backgroundColor = "#27ae60";
        el.progressText.textContent = `✅ Complete! ${progress}/${total} citations verified`;
        if (el.verifyProgress) {
            setTimeout(() => {
                el.verifyProgress.style.display = "none";
            }, 3000);
        }
    } else if (status === "error") {
        el.progressBar.style.backgroundColor = "#e74c3c";
        el.progressText.textContent = `❌ Error during verification`;
    } else {
        el.progressBar.style.backgroundColor = "#3498db";
        el.progressText.textContent = `🔍 Verifying citations: ${progress}/${total} (${percentage}%)`;
        if (el.verifyProgress) el.verifyProgress.style.display = "block";
    }
}

/* -------------------------------------------------------
ACII SCORE DESCRIPTION
------------------------------------------------------- */

function getACIIRating(score) {
    score = Number(score);
    if (score >= 90) return { text: "Excellent", class: "excellent", description: "Outstanding citation integrity. The document demonstrates exceptional scholarly rigor with well-verified, diverse, and temporally balanced citations." };
    if (score >= 80) return { text: "Very Good", class: "very-good", description: "Strong citation integrity. Most citations are verified with good author diversity and temporal distribution." };
    if (score >= 70) return { text: "Good", class: "good", description: "Satisfactory citation integrity. Citations are generally reliable with adequate author representation." };
    if (score >= 60) return { text: "Moderate", class: "moderate", description: "Adequate citation integrity. Some citations may require verification or improvement in diversity." };
    if (score >= 50) return { text: "Weak", class: "weak", description: "Below average citation integrity. Significant room for improvement in verification and diversity." };
    return { text: "Poor", class: "poor", description: "Low citation integrity. Many citations are unverified or lack author diversity." };
}

/* -------------------------------------------------------
UNIQUE CITATION DEDUPLICATION
------------------------------------------------------- */

function getUniqueCitationsWithCount(c2rRows) {
    const uniqueMap = new Map();
    
    c2rRows.forEach(row => {
        const citeText = row.in_text || '';
        const normalizedCite = citeText.toLowerCase().replace(/\s+/g, ' ').trim();
        const key = `${normalizedCite}|${row.status}|${row.matched_reference || ''}`;
        
        if (uniqueMap.has(key)) {
            const existing = uniqueMap.get(key);
            existing.count++;
        } else {
            uniqueMap.set(key, {
                citation: citeText,
                status: row.status,
                matched_reference: row.matched_reference,
                flags: row.flags,
                count: 1
            });
        }
    });
    
    return Array.from(uniqueMap.values());
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
RENDER FUNCTIONS
------------------------------------------------------- */

function renderSummaryTable(data) {
    const s = data?.summary || {};

    if (!el.summaryTable) return;

    el.summaryTable.innerHTML = `
        <tr><td>In-text citations (occurrences)</td><td>${esc(s.in_text_citations_found)}</td></tr>
        <tr><td>References</td><td>${esc(s.reference_entries_found)}</td></tr>
        <tr><td>Missing (unique)</td><td>${esc(s.missing_in_references)}</td></tr>
        <tr><td>Uncited</td><td>${esc(s.uncited_references)}</td></tr>
        <tr><td>Match rate</td><td>${esc(s.match_rate)}%</td></tr>
    `;
}

function renderACII(data) {
    const acii = data?.acii;
    if (!acii) return;

    if (el.aciiCard) el.aciiCard.style.display = "block";

    if (el.aciiValue) {
        const score = acii.ACII ?? "--";
        el.aciiValue.textContent = score;
        
        if (el.aciiDescription && score !== "--") {
            const rating = getACIIRating(score);
            el.aciiDescription.innerHTML = `<strong>${rating.text}</strong><br><small>${rating.description}</small>`;
            el.aciiDescription.className = `acii-desc ${rating.class}`;
        }
    }

    const c = acii.components || {};
    if ($("aciiV")) $("aciiV").textContent = c.verification_integrity?.score ?? "";
    if ($("aciiVcat")) $("aciiVcat").textContent = c.verification_integrity?.category ?? "";
    if ($("aciiC")) $("aciiC").textContent = c.citation_concentration?.score ?? "";
    if ($("aciiCcat")) $("aciiCcat").textContent = c.citation_concentration?.category ?? "";
    if ($("aciiA")) $("aciiA").textContent = c.author_diversity?.score ?? "";
    if ($("aciiAcat")) $("aciiAcat").textContent = c.author_diversity?.category ?? "";
    if ($("aciiT")) $("aciiT").textContent = c.temporal_balance?.score ?? "";
    if ($("aciiTcat")) $("aciiTcat").textContent = c.temporal_balance?.category ?? "";
}

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

function renderC2R(data) {
    const c2rRaw = data?.reconciliation_intext_to_reference || [];
    const uniqueCitations = getUniqueCitationsWithCount(c2rRaw);

    if (!el.c2rBody) return;

    if (!uniqueCitations.length) {
        el.c2rBody.innerHTML = `<tr><td colspan="6">No mapping available</td></tr>`;
        return;
    }

    el.c2rBody.innerHTML = uniqueCitations.map((item, i) => {
        let statusClass = '';
        if (item.status === 'matched') statusClass = 'verified';
        else if (item.status === 'not_found') statusClass = 'not_found';
        
        return `
            <tr>
                <td>${i + 1}</td>
                <td><span class="badge ${statusClass}">${esc(item.status || '')}</span></td>
                <td>${esc(item.citation)}</td>
                <td style="text-align:center"><strong>${item.count}</strong></td>
                <td>${esc(item.matched_reference || '')}</td>
                <td>${esc(item.flags || '')}</td>
            </tr>
        `;
    }).join("");
}

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
            `;
        }).join("");
}

function renderAll(data) {
    if (!data) return;

    CURRENT_DATA = normalizeData(data);
    window.latestResults = CURRENT_DATA;
    
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
    debugLog("Starting document analysis");

    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");

    try {
        const res = await fetch("/verify", { method: "POST", body: fd });
        const js = await res.json();
        
        // Check for server busy response
        if (res.status === 503) {
            setStatus(js.message || "Server is busy, please wait...", "warn");
            debugLog("Server busy, will retry", js);
            setTimeout(runInitialCheck, CONFIG.RETRY_DELAY);
            return;
        }

        LAST_JOB_ID = js.job_id;
        renderAll(js);
        setStatus("Analysis complete", "good");
        debugLog(`Document analysis complete. Job ID: ${LAST_JOB_ID}`);
        
        // Update queue status
        updateQueueStatus();
        
        // Enable verify button
        if (el.btnVerify) el.btnVerify.disabled = false;
        
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
        debugLog(`Error: ${err.message}`);
    }
}

/* -------------------------------------------------------
RUN ONLINE VERIFICATION (WITH PROGRESS)
------------------------------------------------------- */

async function runOnlineVerification() {
    if (!LAST_JOB_ID) {
        setStatus("Run document check first", "warn");
        return;
    }
    
    if (VERIFICATION_IN_PROGRESS) {
        setStatus("Verification already in progress...", "warn");
        return;
    }

    setStatus("Starting online verification...");
    debugLog(`Starting verification for job: ${LAST_JOB_ID}`);
    
    // Reset progress display
    updateProgress(0, 0, "processing");
    VERIFICATION_IN_PROGRESS = true;
    
    // Disable verify button during processing
    if (el.btnVerify) el.btnVerify.disabled = true;

    const fd = new FormData();
    fd.append("job_id", LAST_JOB_ID);

    try {
        const res = await fetch("/verify-online", { method: "POST", body: fd });
        const js = await res.json();
        
        // Check for server busy response
        if (res.status === 503) {
            setStatus(js.message || "Server is busy, please wait...", "warn");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            setTimeout(runOnlineVerification, CONFIG.RETRY_DELAY);
            return;
        }
        
        if (js.started) {
            setStatus("Verification in progress...", "info");
            startPolling();
        } else if (js.completed) {
            // Already completed
            setStatus("Verification already completed", "good");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            fetchStatus();
        } else {
            setStatus(js.message || "Verification could not start", "warn");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
        }
        
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
        debugLog(`Verification error: ${err.message}`);
        VERIFICATION_IN_PROGRESS = false;
        if (el.btnVerify) el.btnVerify.disabled = false;
    }
}

/* -------------------------------------------------------
STATUS POLLING WITH PROGRESS
------------------------------------------------------- */

async function fetchStatus() {
    if (!LAST_JOB_ID) return;
    
    try {
        const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}`);
        const js = await res.json();
        
        // Update progress bar
        if (js.progress) {
            updateProgress(js.progress.current, js.progress.total, js.online?.state);
        } else if (js.online) {
            updateProgress(js.online.progress || 0, js.online.total || 0, js.online.state);
        }
        
        // Update queue status
        if (js.queue) {
            updateQueueStatusDisplay(js.queue);
        }
        
        if (js.result) {
            renderAll(js.result);
        }
        
        if (js.online?.state === "done") {
            setStatus("Online verification complete", "good");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            updateProgress(js.online.total || 0, js.online.total || 0, "completed");
            stopPolling();
            if (el.btnExportVerify) el.btnExportVerify.disabled = false;
            debugLog("Verification completed successfully");
        }
        
        if (js.online?.state === "error") {
            setStatus(js.online?.message || "Verification failed", "warn");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            updateProgress(0, 0, "error");
            stopPolling();
            debugLog(`Verification error: ${js.online?.message}`);
        }
        
    } catch (err) {
        console.error("Status fetch error:", err);
    }
}

function updateQueueStatusDisplay(queue) {
    if (!el.queueStatus) return;
    
    const busyClass = queue.is_busy ? 'busy' : 'ready';
    el.queueStatus.innerHTML = `
        <div class="queue-info ${busyClass}">
            <span>📊 Queue: ${queue.queue_size || 0}</span>
            <span>⏳ Pending: ${queue.pending_jobs || 0}</span>
            <span>⚙️ Processing: ${queue.processing_jobs || 0}</span>
            <span class="server-status ${busyClass}">
                ${queue.is_busy ? '⚠️ Server Busy' : '✅ Server Ready'}
            </span>
        </div>
    `;
}

function startPolling() {
    if (POLL_TIMER) clearInterval(POLL_TIMER);
    POLL_TIMER = setInterval(fetchStatus, CONFIG.POLL_INTERVAL);
    debugLog("Started polling for status updates");
}

function stopPolling() {
    if (POLL_TIMER) {
        clearInterval(POLL_TIMER);
        POLL_TIMER = null;
        debugLog("Stopped polling");
    }
}

/* -------------------------------------------------------
EXPORT FUNCTIONS
------------------------------------------------------- */

function escapeCsv(str) {
    if (!str) return '';
    const escaped = String(str).replace(/"/g, '""');
    if (escaped.includes(',') || escaped.includes('\n') || escaped.includes('"')) {
        return `"${escaped}"`;
    }
    return escaped;
}

function exportCSV(data) {
    if (!data) {
        alert("No data to export. Run a check first.");
        return;
    }

    const normalized = normalizeData(data);
    const s = normalized.summary || {};
    const missing = normalized.missing_in_references || [];
    const uncited = normalized.uncited_references || [];
    const c2rRaw = normalized.reconciliation_intext_to_reference || [];
    const r2c = normalized.reconciliation_reference_to_intext || [];
    const acii = normalized.acii || {};
    const ov = normalized.online_verification || {};
    
    const uniqueCitations = getUniqueCitationsWithCount(c2rRaw);

    let csv = [];

    csv.push("=== CITATION CROSSCHECK REPORT ===");
    csv.push(`"Generated","${new Date().toLocaleString()}"`);
    csv.push(`"Job ID","${normalized.job_id || LAST_JOB_ID || 'N/A'}"`);
    csv.push(``);
    csv.push("=== SUMMARY ===");
    csv.push(`"Total in-text citations found (occurrences)","${s.in_text_citations_found || 0}"`);
    csv.push(`"Unique citations","${uniqueCitations.length}"`);
    csv.push(`"Reference entries found","${s.reference_entries_found || 0}"`);
    csv.push(`"Missing in references (unique)","${s.missing_in_references || 0}"`);
    csv.push(`"Uncited references","${s.uncited_references || 0}"`);
    csv.push(`"Match rate","${s.match_rate || 0}%"`);
    csv.push(``);
    
    csv.push("=== ACII SCORE ===");
    csv.push(`"ACII Score","${acii.ACII || '—'}"`);
    const rating = getACIIRating(acii.ACII);
    csv.push(`"Rating","${rating.text}"`);
    csv.push(`"Description","${rating.description}"`);
    csv.push(``);
    
    if (acii.components) {
        csv.push("=== ACII COMPONENTS ===");
        const comp = acii.components;
        csv.push(`"Verification Integrity","${comp.verification_integrity?.score || '—'} (${comp.verification_integrity?.category || '—'})"`);
        csv.push(`"Citation Concentration","${comp.citation_concentration?.score || '—'} (${comp.citation_concentration?.category || '—'})"`);
        csv.push(`"Author Diversity","${comp.author_diversity?.score || '—'} (${comp.author_diversity?.category || '—'})"`);
        csv.push(`"Temporal Balance","${comp.temporal_balance?.score || '—'} (${comp.temporal_balance?.category || '—'})"`);
        csv.push(``);
    }

    csv.push("=== MISSING CITATIONS ===");
    csv.push(`"#","Citation","Count"`);
    missing.forEach((item, idx) => {
        const citation = (typeof item === 'string') ? item : (item.citation_in_text || item);
        const count = (typeof item === 'string') ? 1 : (item.count_in_text || 1);
        csv.push(`"${idx + 1}","${escapeCsv(citation)}","${count}"`);
    });
    csv.push(``);

    csv.push("=== UNCITED REFERENCES ===");
    csv.push(`"#","Reference"`);
    uncited.forEach((ref, idx) => {
        csv.push(`"${idx + 1}","${escapeCsv(ref)}"`);
    });
    csv.push(``);

    csv.push("=== CITATION TO REFERENCE MAPPING ===");
    csv.push(`"#","Status","Citation","Count","Matched Reference","Flags"`);
    uniqueCitations.forEach((item, idx) => {
        csv.push(`"${idx + 1}","${item.status || ''}","${escapeCsv(item.citation)}","${item.count}","${escapeCsv(item.matched_reference || '')}","${item.flags || ''}"`);
    });
    csv.push(``);

    csv.push("=== REFERENCE TO CITATION MAPPING ===");
    csv.push(`"#","Times Cited","Reference","Cited By (sample)"`);
    r2c.forEach((item, idx) => {
        const citedBy = (item.cited_by || []).slice(0, 3).join("; ");
        csv.push(`"${idx + 1}","${item.times_cited || 0}","${escapeCsv(item.reference || '')}","${escapeCsv(citedBy)}"`);
    });
    
    if (ov.rows && ov.rows.length > 0) {
        csv.push(``);
        csv.push("=== ONLINE VERIFICATION SUMMARY ===");
        csv.push(`"Verified","${ov.summary?.verified || 0}"`);
        csv.push(`"Likely","${ov.summary?.likely || 0}"`);
        csv.push(`"Needs Review","${ov.summary?.needs_review || 0}"`);
        csv.push(`"Not Found","${ov.summary?.not_found || 0}"`);
        csv.push(`"Offline","${ov.summary?.offline || 0}"`);
    }

    const blob = new Blob([csv.join("\n")], { type: "text/csv;charset=utf-8;" });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.setAttribute("href", url);
    link.setAttribute("download", `citation_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    
    debugLog(`Exported CSV with ${uniqueCitations.length} unique citations`);
}

function exportWordFile(data) {
    // Keep your existing exportWordFile function
    alert("Word export functionality available. Use CSV for structured data.");
}

/* -------------------------------------------------------
INITIALIZATION
------------------------------------------------------- */

// Set up event listeners
if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
if (el.btnVerify) {
    el.btnVerify.disabled = true;  // Initially disabled until document check
    el.btnVerify.addEventListener("click", runOnlineVerification);
}

// Export buttons
const exportCsv = document.getElementById("btnExportCsvTop");
const exportWord = document.getElementById("btnExportWordTop");

if (exportCsv) {
    exportCsv.disabled = true;
    exportCsv.addEventListener("click", () => {
        if (!window.latestResults) {
            alert("Run a check first to export data.");
            return;
        }
        exportCSV(window.latestResults);
    });
}

if (exportWord) {
    exportWord.disabled = true;
    exportWord.addEventListener("click", () => {
        if (!window.latestResults) {
            alert("Run a check first to export data.");
            return;
        }
        exportWordFile(window.latestResults);
    });
}

// Periodic queue status update
setInterval(updateQueueStatus, 5000);
updateQueueStatus();

debugLog("Application initialized");

});
