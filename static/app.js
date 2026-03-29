/* static/app.js — Citation Crosschecker Dashboard (WITH ALL FIXES) */

document.addEventListener("DOMContentLoaded", function () {

"use strict";

/* -------------------------------------------------------
CONFIG
------------------------------------------------------- */

const CONFIG = {
    POLL_INTERVAL: 1500,
    MAX_VERIFY_DISPLAY: 500,
    RETRY_DELAY: 30000  // 30 seconds
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
    estimatedRemaining: $("estimatedRemaining")
};

let LAST_JOB_ID = null;
let POLL_TIMER = null;
let CURRENT_DATA = null;
let VERIFICATION_IN_PROGRESS = false;
let RETRY_COUNT = 0;

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
    console.log(`[Status] ${msg} (${tone})`);
}

/* -------------------------------------------------------
STATUS POLLING FUNCTIONS (DEFINED EARLY)
------------------------------------------------------- */

async function fetchStatus() {
    if (!LAST_JOB_ID) return;
    
    try {
        const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}`);
        const js = await res.json();

        // ✅ RESET RETRY COUNT ON SUCCESS
        RETRY_COUNT = 0;

        // Update progress from online status
        if (js.online) {
            const progress = js.online.progress || 0;
            const total = js.online.total || 0;
            const percentage = js.online.percentage || 0;
            const state = js.online.state || "processing";
            
            if (total > 0) {
                updateProgress(progress, total, state, 
                    `Verifying: ${progress}/${total} (${percentage}%)`);
            }
        }

        if (js.result) {
            renderAll(js.result);
        }

        if (js.online?.state === "completed") {
            setStatus("Online verification complete", "good");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            stopPolling();
        }

        if (js.online?.state === "error") {
            setStatus(js.online?.message || "Verification failed", "warn");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            updateProgress(0, 0, "error", js.online?.message || "Verification failed");
            stopPolling();
        }

    } catch (err) {
        console.error("Status fetch error:", err);

        // ✅ NEW RETRY LOGIC (ONLY CHANGE)
        if (RETRY_COUNT < 5) {
            RETRY_COUNT++;

            setStatus(`⚠️ Connection issue... retrying (${RETRY_COUNT}/5)`, "warn");

            setTimeout(() => {
                fetchStatus();
            }, CONFIG.RETRY_DELAY);

        } else {
            setStatus("❌ Connection lost. Verification may still be running in background.", "warn");

            stopPolling();
            VERIFICATION_IN_PROGRESS = false;

            if (el.btnVerify) el.btnVerify.disabled = false;
        }
    }
}

function startPolling() {
    if (POLL_TIMER) clearInterval(POLL_TIMER);
    POLL_TIMER = setInterval(fetchStatus, CONFIG.POLL_INTERVAL);
}

function stopPolling() {
    if (POLL_TIMER) {
        clearInterval(POLL_TIMER);
        POLL_TIMER = null;
    }
}

/* -------------------------------------------------------
REMAINING CODE UNCHANGED
------------------------------------------------------- */

// (Everything else remains exactly as in your original file)

});

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
UNIQUE CITATION DEDUPLICATION (WITH COUNT)
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

function exportVerificationCSV(data) {
    if (!data) {
        alert("No verification data to export. Run verification first.");
        return;
    }

    const ov = data?.online_verification || {};
    const rows = ov.rows || [];
    const sum = ov.summary || {};

    if (rows.length === 0) {
        alert("No verification results available. Run online verification first.");
        return;
    }

    let csv = [];

    csv.push("=== ONLINE VERIFICATION REPORT ===");
    csv.push(`"Generated","${new Date().toLocaleString()}"`);
    csv.push(`"Job ID","${data.job_id || LAST_JOB_ID || 'N/A'}"`);
    csv.push(``);
    
    csv.push("=== SUMMARY ===");
    csv.push(`"Verified","${sum.verified || 0}"`);
    csv.push(`"Likely","${sum.likely || 0}"`);
    csv.push(`"Needs Review","${sum.needs_review || 0}"`);
    csv.push(`"Not Found","${sum.not_found || 0}"`);
    csv.push(`"Offline","${sum.offline || 0}"`);
    csv.push(`"Total Processed","${sum.total || rows.length}"`);
    csv.push(``);
    
    csv.push("=== VERIFICATION DETAILS ===");
    csv.push(`"#","Status","Source","Score","DOI","Matched Year","Matched Authors","Matched Title","Query Used"`);
    
    rows.forEach((r, idx) => {
        csv.push(`"${idx + 1}","${r.status || ''}","${escapeCsv(r.source || '—')}","${r.score || '—'}","${r.doi || '—'}","${r.matched_year || '—'}","${escapeCsv(r.matched_authors || '—')}","${escapeCsv((r.matched_title || '').substring(0, 100))}","${escapeCsv(r.query_used || '—')}"`);
    });

    const blob = new Blob([csv.join("\n")], { type: "text/csv;charset=utf-8;" });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.setAttribute("href", url);
    link.setAttribute("download", `verification_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
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
    
    csv.push("=== ACII SCORE (Academic Citation Integrity Index) ===");
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
        csv.push(`"${idx + 1}","${escapeCsv(ref.reference || ref)}"`);
    });
    csv.push(``);

    csv.push("=== CITATION TO REFERENCE MAPPING (UNIQUE CITATIONS) ===");
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
    const c2rRaw = normalized.reconciliation_intext_to_reference || [];
    const r2c = normalized.reconciliation_reference_to_intext || [];
    const acii = normalized.acii || {};
    const ov = normalized.online_verification || {};
    
    const uniqueCitations = getUniqueCitationsWithCount(c2rRaw);
    const timestamp = new Date().toLocaleString();
    const aciiRating = getACIIRating(acii.ACII);

    let html = `<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Citation Crosscheck Report</title>
    <style>
        body { font-family: 'Times New Roman', Times, serif; margin: 2.54cm 3.17cm; line-height: 1.5; font-size: 12pt; }
        h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; font-size: 24pt; }
        h2 { color: #34495e; margin-top: 25px; border-left: 4px solid #3498db; padding-left: 10px; font-size: 18pt; }
        h3 { color: #555; margin-top: 20px; font-size: 14pt; }
        table { border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 10pt; }
        th, td { border: 1px solid #ddd; padding: 8px 12px; text-align: left; vertical-align: top; }
        th { background-color: #f2f2f2; font-weight: bold; }
        .summary-table { width: auto; min-width: 300px; }
        .badge { display: inline-block; padding: 2px 6px; border-radius: 4px; font-size: 9pt; }
        .badge.verified { background: #27ae60; color: white; }
        .badge.not_found { background: #e74c3c; color: white; }
        .badge.likely { background: #f39c12; color: white; }
        .badge.matched { background: #27ae60; color: white; }
        .acii-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; }
        .acii-score { font-size: 48px; font-weight: bold; }
        .footer { margin-top: 30px; font-size: 9pt; color: #7f8c8d; text-align: center; border-top: 1px solid #ddd; padding-top: 15px; }
        .page-break { page-break-before: always; }
        .uncited-row { background-color: #fff3cd; }
    </style>
</head>
<body>
    <h1>📊 Citation Crosscheck Report</h1>
    <p><strong>Generated:</strong> ${timestamp}</p>
    <p><strong>File:</strong> ${esc(normalized.filename || 'N/A')}</p>
    <p><strong>Style:</strong> ${esc(normalized.style || 'APA/Harvard')}</p>
    
    <h2>📈 Summary</h2>
    <table class="summary-table">
        <thead><tr><th>Metric</th><th>Value</th></tr></thead>
        <tbody>
            <tr><td>Total in-text citations (occurrences)</td><td><strong>${s.in_text_citations_found || 0}</strong></td></tr>
            <tr><td>Unique citations</td><td><strong>${uniqueCitations.length}</strong></td></tr>
            <tr><td>Reference entries found</td><td>${s.reference_entries_found || 0}</td></tr>
            <tr><td>Missing in references (unique)</td><td><strong>${s.missing_in_references || 0}</strong></td></tr>
            <tr><td>Uncited references</td><td><strong>${s.uncited_references || 0}</strong></td></tr>
            <tr><td>Match rate</td><td><strong>${s.match_rate || 0}%</strong></td></tr>
        </tbody>
    </table>
    
    <h2>📊 ACII Score</h2>
    <div class="acii-card">
        <div class="acii-score">${acii.ACII || '—'}</div>
        <div>${aciiRating.text}</div>
        <div style="font-size:12px; margin-top:10px;">${aciiRating.description}</div>
    </div>`;

    // ACII Components
    if (acii.components) {
        html += `
    <h2>📊 ACII Components</h2>
    <table>
        <thead><tr><th>Component</th><th>Score</th><th>Category</th><th>Remark</th></tr></thead>
        <tbody>
            <tr><td>Verification Integrity</td><td>${acii.components.verification_integrity?.score || '—'}</td><td>${acii.components.verification_integrity?.category || '—'}</td><td>${esc(acii.components.verification_integrity?.remark || '—')}</td></tr>
            <tr><td>Citation Concentration</td><td>${acii.components.citation_concentration?.score || '—'}</td><td>${acii.components.citation_concentration?.category || '—'}</td><td>${esc(acii.components.citation_concentration?.remark || '—')}</td></tr>
            <tr><td>Author Diversity</td><td>${acii.components.author_diversity?.score || '—'}</td><td>${acii.components.author_diversity?.category || '—'}</td><td>${esc(acii.components.author_diversity?.remark || '—')}</td></tr>
            <tr><td>Temporal Balance</td><td>${acii.components.temporal_balance?.score || '—'}</td><td>${acii.components.temporal_balance?.category || '—'}</td><td>${esc(acii.components.temporal_balance?.remark || '—')}</td></tr>
        </tbody>
    </table>`;
    }

    // Missing Citations
    html += `
    <h2>❌ Missing Citations</h2>`;
    if (missing.length > 0) {
        html += `
    <table>
        <thead><tr><th>#</th><th>Citation</th><th>Occurrences</th></tr></thead>
        <tbody>
            ${missing.map((item, idx) => {
                const citation = (typeof item === 'string') ? item : (item.citation_in_text || item);
                const count = (typeof item === 'string') ? 1 : (item.count_in_text || 1);
                return `<tr><td>${idx + 1}</td><td>${esc(citation)}</td><td>${count}</td></tr>`;
            }).join('')}
        </tbody>
    </table>`;
    } else {
        html += `<p>✅ No missing citations found!</p>`;
    }

    // Uncited References
    html += `
    <h2>📚 Uncited References</h2>`;
    if (uncited.length > 0) {
        html += `
    <table>
        <thead><tr><th>#</th><th>Reference</th></tr></thead>
        <tbody>
            ${uncited.map((ref, idx) => `<tr class="uncited-row"><td>${idx + 1}</td><td>${esc(ref.reference || ref)}</td></tr>`).join('')}
        </tbody>
    </table>`;
    } else {
        html += `<p>✅ All references are cited!</p>`;
    }

    // Citation to Reference Mapping
    html += `
    <h2>🔗 Citation to Reference Mapping</h2>
    <table>
        <thead><tr><th>#</th><th>Status</th><th>Citation</th><th>Count</th><th>Matched Reference</th><th>Flags</th></tr></thead>
        <tbody>
            ${uniqueCitations.slice(0, 50).map((item, idx) => {
                let statusClass = item.status === 'matched' ? 'matched' : 'not_found';
                return `<tr>
                    <td>${idx + 1}</td>
                    <td><span class="badge ${statusClass}">${esc(item.status || '')}</span></td>
                    <td>${esc(item.citation)}</td>
                    <td><strong>${item.count}</strong></td>
                    <td>${esc(item.matched_reference || '—')}</td>
                    <td>${esc(item.flags || '—')}</td>
                </tr>`;
            }).join('')}
        </tbody>
    </table>`;

    // Reference to Citation Mapping (INCLUDES UNCITED)
    html += `
    <h2>📖 Reference to Citation Mapping</h2>
    <table>
        <thead><tr><th>#</th><th>Times Cited</th><th>Reference</th><th>Cited By (sample)</th></tr></thead>
        <tbody>
            ${r2c.map((item, idx) => {
                const timesCited = item.times_cited || 0;
                const citedBySample = (item.cited_by || []).slice(0, 3).join("; ");
                const uncitedClass = timesCited === 0 ? 'class="uncited-row"' : '';
                const statusText = timesCited === 0 ? ' (UNCITED)' : '';
                return `<tr ${uncitedClass}>
                    <td>${idx + 1}</td>
                    <td><strong>${timesCited}</strong>${statusText}</td>
                    <td>${esc(item.reference || '')}</td>
                    <td>${esc(citedBySample || '—')}</td>
                </tr>`;
            }).join('')}
        </tbody>
    </table>`;

    // Online Verification Results (if available)
    if (ov.rows && ov.rows.length > 0) {
        html += `
    <div class="page-break"></div>
    <h2>🌐 Online Verification Results</h2>
    <table>
        <thead><tr><th>#</th><th>Status</th><th>Source</th><th>Score</th><th>DOI</th><th>Year</th><th>Authors</th><th>Matched Title</th></tr></thead>
        <tbody>
            ${ov.rows.slice(0, 100).map((r, idx) => {
                let badgeClass = r.status === 'verified' ? 'verified' : (r.status === 'likely' ? 'likely' : (r.status === 'needs_review' ? 'needs_review' : 'not_found'));
                return `<tr>
                    <td>${idx + 1}</td>
                    <td><span class="badge ${badgeClass}">${esc(r.status || '')}</span></td>
                    <td>${esc(r.source || '—')}</td>
                    <td>${esc(r.score || '—')}</td>
                    <td>${esc(r.doi || '—')}</td>
                    <td>${esc(r.matched_year || '—')}</td>
                    <td>${esc(r.matched_authors || '—')}</td>
                    <td>${esc((r.matched_title || '').substring(0, 60))}</td>
                </tr>`;
            }).join('')}
        </tbody>
    </table>`;
    }

    html += `
    <div class="footer">
        <p>Report generated by Citation Integrity Analyzer</p>
        <p>${esc(normalized.reference_detection_message || '')}</p>
    </div>
</body>
</html>`;

    const blob = new Blob([html], { type: "application/msword" });
    const link = document.createElement("a");
    const url = URL.createObjectURL(blob);
    link.setAttribute("href", url);
    link.setAttribute("download", `citation_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.doc`);
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    
    console.log("Word report exported successfully with complete data");
}

/* -------------------------------------------------------
RENDER FUNCTIONS
------------------------------------------------------- */

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

    // Verification Integrity
    if ($("aciiV")) $("aciiV").textContent = c.verification_integrity?.score ?? "";
    if ($("aciiVcat")) $("aciiVcat").textContent = c.verification_integrity?.category ?? "";
    if ($("aciiVremark")) $("aciiVremark").textContent = c.verification_integrity?.remark ?? "Percentage of references verified in scholarly databases";

    // Citation Concentration
    if ($("aciiC")) $("aciiC").textContent = c.citation_concentration?.score ?? "";
    if ($("aciiCcat")) $("aciiCcat").textContent = c.citation_concentration?.category ?? "";
    if ($("aciiCremark")) $("aciiCremark").textContent = c.citation_concentration?.remark ?? "Measures whether citations rely heavily on few authors";

    // Author Diversity
    if ($("aciiA")) $("aciiA").textContent = c.author_diversity?.score ?? "";
    if ($("aciiAcat")) $("aciiAcat").textContent = c.author_diversity?.category ?? "";
    if ($("aciiAremark")) $("aciiAremark").textContent = c.author_diversity?.remark ?? "Measures diversity of authors represented in the reference list";

    // Temporal Balance
    if ($("aciiT")) $("aciiT").textContent = c.temporal_balance?.score ?? "";
    if ($("aciiTcat")) $("aciiTcat").textContent = c.temporal_balance?.category ?? "";
    if ($("aciiTremark")) $("aciiTremark").textContent = c.temporal_balance?.remark ?? "Measures spread of references across publication years";
}

function normalizeData(payload) {
    const data = payload?.data || payload?.result || payload || {};
    const s = data.summary || {};

    data.summary = {
        in_text_citations_found: toNum(s.in_text_citations_found ?? data.intext_count),
        reference_entries_found: toNum(s.reference_entries_found ?? data.reference_entries_found),
        missing_in_references: toNum(s.missing_in_references ?? (data.missing_in_references || []).length),
        uncited_references: toNum(s.uncited_references ?? (data.uncited_references || []).length),
        match_rate: toNum(s.match_rate ?? data.match_rate)
    };

    return data;
}

function renderSummaryTable(data) {
    const s = data?.summary || {};
    if (!el.summaryTable) return;

    el.summaryTable.innerHTML = `
         <tr><td style="width:220px;">In-text citations (occurrences)</td><td>${esc(s.in_text_citations_found)}</td>
        </tr>
         <tr><td>References</td><td>${esc(s.reference_entries_found)}</td>
        </tr>
         <tr><td>Missing (unique)</td><td>${esc(s.missing_in_references)}</td>
        </tr>
         <tr><td>Uncited</td><td>${esc(s.uncited_references)}</td>
        </tr>
         <tr><td>Match rate</td><td>${esc(s.match_rate)}%</td>
        </tr>
    `;
}

function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;
    if (!rows.length) { el.missingBody.innerHTML = `<tr><td colspan="3">None</td>`; return; }
    el.missingBody.innerHTML = rows.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.citation_in_text || r)}</td><td>${esc(r.count_in_text || "")}</td></tr>`).join("");
}

function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    if (!rows.length) { el.uncitedBody.innerHTML = `<tr><td colspan="2">None</td>`; return; }
    el.uncitedBody.innerHTML = rows.map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r.reference || r)}</td></tr>`).join("");
}

function renderC2R(data) {
    const c2rRaw = data?.reconciliation_intext_to_reference || [];
    const uniqueCitations = getUniqueCitationsWithCount(c2rRaw);
    if (!el.c2rBody) return;
    if (!uniqueCitations.length) { el.c2rBody.innerHTML = `<tr><td colspan="6">No mapping available</td>`; return; }
    el.c2rBody.innerHTML = uniqueCitations.map((item, i) => {
        let statusClass = item.status === 'matched' ? 'verified' : (item.status === 'not_found' ? 'not_found' : '');
        return `<tr>
            <td>${i + 1}</td>
            <td><span class="badge ${statusClass}">${esc(item.status || '')}</span></td>
            <td style="max-width:300px;">${esc(item.citation)}</td>
            <td style="text-align:center"><strong>${item.count}</strong></td>
            <td style="max-width:400px;">${esc(item.matched_reference || '')}</td>
            <td>${esc(item.flags || '')}</td>
        </tr>`;
    }).join("");
}

function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;
    if (!rows.length) { el.r2cBody.innerHTML = `<tr><td colspan="4">No mapping available</td>`; return; }
    el.r2cBody.innerHTML = rows.map((r, i) => `<tr>
        <td>${i + 1}</td>
        <td>${esc(r.times_cited ?? 0)}</td>
        <td style="max-width:500px;">${esc(r.reference || '')}</td>
        <td>${esc((r.cited_by || []).slice(0, 3).join("; "))}</td>
    </tr>`).join("");
}

function renderVerify(data) {
    const ov = data?.online_verification || {};
    const rows = ov.rows || [];
    const sum = ov.summary || {};

    if (el.verifyDash) {
        el.verifyDash.innerHTML = `<div class="kpi">✅ Verified: ${sum.verified ?? 0}</div>
            <div class="kpi">🔍 Likely: ${sum.likely ?? 0}</div>
            <div class="kpi">⚠️ Needs Review: ${sum.needs_review ?? 0}</div>
            <div class="kpi">❌ Not Found: ${sum.not_found ?? 0}</div>
            <div class="kpi">📡 Offline: ${sum.offline ?? 0}</div>`;
    }

    if (!el.verifyBody) return;
    if (!rows.length) { el.verifyBody.innerHTML = `<tr><td colspan="9">No verification results. Click "Run Online Verification" to start.</td>`; return; }

    el.verifyBody.innerHTML = rows.slice(0, CONFIG.MAX_VERIFY_DISPLAY).map((r, i) => {
        let badgeClass = r.status === 'verified' ? 'verified' : (r.status === 'likely' ? 'likely' : (r.status === 'needs_review' ? 'needs_review' : (r.status === 'not_found' ? 'not_found' : 'offline')));
        return `<tr>
            <td>${i + 1}</td>
            <td><span class="badge ${badgeClass}">${esc(r.status || '')}</span></td>
            <td>${esc(r.source || '—')}</td>
            <td>${esc(r.score || '—')}</td>
            <td>${esc(r.doi || '—')}</td>
            <td>${esc(r.matched_year || '—')}</td>
            <td>${esc(r.matched_authors || '—')}</td>
            <td>${esc((r.matched_title || '').substring(0, 50))}${(r.matched_title || '').length > 50 ? '…' : ''}</td>
            <td>${esc(r.query_used || '—')}</td>
        </tr>`;
    }).join("");
    
    if (el.btnExportVerify && rows.length > 0) el.btnExportVerify.disabled = false;
}

function renderAll(data) {
    if (!data) return;

    CURRENT_DATA = normalizeData(data);
    CURRENT_DATA.job_id = data.job_id || LAST_JOB_ID;
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
FILE VALIDATION - REJECT PDF FILES
------------------------------------------------------- */

function validateFile(file) {
    if (!file) return { valid: false, message: "No file selected" };
    
    const fileName = file.name.toLowerCase();
    const fileExtension = fileName.split('.').pop();
    
    // Reject PDF files
    if (fileExtension === 'pdf') {
        return { 
            valid: false, 
            message: "PDF files are not supported. Please convert to DOCX first: Open blank Word → File → Open → Select PDF → Click OK → Save as .docx" 
        };
    }
    
    // Accept only DOCX files
    if (fileExtension !== 'docx') {
        return { 
            valid: false, 
            message: "Only DOCX files are accepted. Please convert PDF to DOCX: Open blank Word → File → Open → Select PDF → Click OK → Save as .docx" 
        };
    }
    
    return { valid: true, message: "DOCX file selected. Ready to run check." };
}

/* -------------------------------------------------------
RUN INITIAL CHECK (WITH PDF REJECTION)
------------------------------------------------------- */

async function runInitialCheck() {
    const f = el.file?.files?.[0];
    if (!f) { setStatus("Please choose a file first", "warn"); return; }
    
    // Validate file type - REJECT PDF
    const validation = validateFile(f);
    if (!validation.valid) {
        setStatus(validation.message, "warn");
        updateProgress(0, 0, "error", validation.message);
        // Clear the file input
        el.file.value = '';
        return;
    }

    if (POLL_TIMER) { clearInterval(POLL_TIMER); POLL_TIMER = null; }
    VERIFICATION_IN_PROGRESS = false;
    LAST_JOB_ID = null;
    RETRY_COUNT = 0;
    resetVerificationUI();

    setStatus("Analyzing document...");

    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");

    try {
        const res = await fetch("/verify", { method: "POST", body: fd });
        
        // Handle backend PDF rejection
        if (res.status === 400) {
            const js = await res.json();
            const errorMsg = js.message || "Invalid file format. Please use DOCX files only.";
            setStatus(errorMsg, "warn");
            updateProgress(0, 0, "error", errorMsg);
            el.file.value = '';
            return;
        }
        
        if (res.status === 503) {
            const js = await res.json();
            setStatus(js.message || "Server is busy, please wait...", "warn");
            updateProgress(0, 0, "error", `Server busy: ${js.message || "Please wait"}`);
            if (RETRY_COUNT < 3) { RETRY_COUNT++; setTimeout(runInitialCheck, CONFIG.RETRY_DELAY); }
            else { setStatus("Server is busy. Please try again later.", "warn"); RETRY_COUNT = 0; }
            return;
        }
        
        const js = await res.json();
        LAST_JOB_ID = js.job_id;
        renderAll(js);
        setStatus("Analysis complete", "good");
        RETRY_COUNT = 0;
        updateQueueStatus();
        if (el.btnVerify) el.btnVerify.disabled = false;
        
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
    }
}

/* -------------------------------------------------------
FILE SELECTION VALIDATION
------------------------------------------------------- */

if (el.file) {
    el.file.addEventListener('change', function(e) {
        const file = e.target.files[0];
        if (file) {
            const validation = validateFile(file);
            if (!validation.valid) {
                setStatus(validation.message, "warn");
                el.file.value = ''; // Clear the file input
                if (el.btnCheck) el.btnCheck.disabled = true;
                if (el.btnVerify) el.btnVerify.disabled = true;
            } else {
                setStatus(validation.message, "good");
                if (el.btnCheck) el.btnCheck.disabled = false;
            }
        }
    });
}

/* -------------------------------------------------------
RUN ONLINE VERIFICATION
------------------------------------------------------- */

async function runOnlineVerification() {
    if (!LAST_JOB_ID) { setStatus("Run document check first", "warn"); return; }
    if (VERIFICATION_IN_PROGRESS) { setStatus("Verification already in progress...", "warn"); return; }

    setStatus("Starting online verification...");
    VERIFICATION_IN_PROGRESS = true;
    RETRY_COUNT = 0;
    
    updateProgress(0, 0, "processing", "Starting verification...");
    if (el.btnVerify) el.btnVerify.disabled = true;

    const fd = new FormData();
    fd.append("job_id", LAST_JOB_ID);

    try {
        const res = await fetch("/verify-online", { method: "POST", body: fd });
        
        if (res.status === 503) {
            const js = await res.json();
            setStatus(js.message || "Server is busy, please wait...", "warn");
            updateProgress(0, 0, "error", `Server busy: ${js.message || "Please wait"}`);
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            if (RETRY_COUNT < 3) { RETRY_COUNT++; setTimeout(runOnlineVerification, CONFIG.RETRY_DELAY); }
            return;
        }
        
        const js = await res.json();
        
        if (js.started) {
            setStatus("Verification in progress...", "info");
            startPolling();
        } else if (js.completed) {
            setStatus("Verification already completed", "good");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
            fetchStatus();
        } else {
            setStatus(js.message || "Verification could not start", "warn");
            VERIFICATION_IN_PROGRESS = false;
            if (el.btnVerify) el.btnVerify.disabled = false;
        }
        RETRY_COUNT = 0;
        
    } catch (err) {
        setStatus("Error: " + err.message, "bad");
        VERIFICATION_IN_PROGRESS = false;
        if (el.btnVerify) el.btnVerify.disabled = false;
    }
}

/* -------------------------------------------------------
EXPORT VERIFICATION RESULTS
------------------------------------------------------- */

function exportVerificationResults() {
    if (!window.latestResults) {
        alert("No verification data to export. Run verification first.");
        return;
    }
    const ov = window.latestResults?.online_verification;
    if (!ov || !ov.rows || ov.rows.length === 0) {
        alert("No verification results available. Please run online verification first.");
        return;
    }
    exportVerificationCSV(window.latestResults);
}

/* -------------------------------------------------------
BUTTON EVENTS
------------------------------------------------------- */

if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
if (el.btnVerify) { el.btnVerify.disabled = true; el.btnVerify.addEventListener("click", runOnlineVerification); }
if (el.btnExportVerify) { el.btnExportVerify.disabled = true; el.btnExportVerify.addEventListener("click", exportVerificationResults); }

const exportCsv = document.getElementById("btnExportCsvTop");
const exportWord = document.getElementById("btnExportWordTop");

if (exportCsv) {
    exportCsv.disabled = true;
    exportCsv.addEventListener("click", () => {
        if (!window.latestResults) { alert("Run a check first to export data."); return; }
        exportCSV(window.latestResults);
    });
}

if (exportWord) {
    exportWord.disabled = true;
    exportWord.addEventListener("click", () => {
        if (!window.latestResults) { alert("Run a check first to export data."); return; }
        exportWordFile(window.latestResults);
    });
}

// Periodic queue status update
setInterval(updateQueueStatus, 5000);
updateQueueStatus();

console.log("[App] Initialized successfully");

});
