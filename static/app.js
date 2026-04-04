/* static/app.js — CiteIntegrity Dashboard (FULLY FUNCTIONAL with Suggested References & DOI Links) */

document.addEventListener("DOMContentLoaded", function () {

"use strict";

/* -------------------------------------------------------
CONFIG
------------------------------------------------------- */

const CONFIG = {
    POLL_INTERVAL: 3000,
    MAX_VERIFY_DISPLAY: 500,
    RETRY_DELAY: 30000,
    MAX_POLL_ATTEMPTS: 1200,
    STALL_TIMEOUT: 300000
};

/* -------------------------------------------------------
DOM HELPERS
------------------------------------------------------- */

const $ = (id) => document.getElementById(id);

const el = {
    file: $("file"),
    autofix: $("autofix"),
    onlineVerify: $("onlineVerify"),
    btnCheck: $("btnCheck"),
    btnVerify: $("btnVerify"),
    btnApplyAutofix: $("btnApplyAutofix"),
    btnExportFixed: $("btnExportFixed"),
    status: $("status"),
    resultsCard: $("resultsCard"),
    summaryTable: $("summaryTable"),
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
    estimatedRemaining: $("estimatedRemaining"),
    fixLogPanel: $("fixLogPanel"),
    fixLogContent: $("fixLogContent"),
    fixSuggestionsPanel: $("fixSuggestionsPanel"),
    fixSuggestionsContent: $("fixSuggestionsContent"),
    aciiCenterpiece: $("aciiCenterpiece"),
    aciiScoreLarge: $("aciiScoreLarge"),
    aciiRatingBadge: $("aciiRatingBadge"),
    aciiRecommendationText: $("aciiRecommendationText"),
    stepUpload: $("stepUpload"),
    stepExtract: $("stepExtract"),
    stepMatch: $("stepMatch"),
    stepACII: $("stepACII")
};

let LAST_JOB_ID = null;
let POLL_TIMER = null;
let CURRENT_DATA = null;
let VERIFICATION_IN_PROGRESS = false;
let RETRY_COUNT = 0;
let POLL_ATTEMPT_COUNT = 0;
let LAST_PROGRESS = 0;
let LAST_PROGRESS_TIME = null;
let AUTO_FIX_APPLIED = false;
let FIX_SUGGESTIONS = null;

window.latestResults = null;

/* -------------------------------------------------------
UTILITY FUNCTIONS
------------------------------------------------------- */

function esc(s) {
    return String(s ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function toNum(x, d = 0) {
    const n = Number(x);
    return Number.isFinite(n) ? n : d;
}

function setStatus(msg, tone = "muted") {
    if (el.status) {
        el.status.className = `status ${tone}`;
        el.status.textContent = msg || "";
    }
    console.log(`[Status] ${msg}`);
}

function formatTime(seconds) {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
    return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
}

function showNotification(message, type = "info") {
    const notification = document.createElement("div");
    notification.textContent = message;
    notification.style.cssText = `
        position: fixed; bottom: 20px; right: 20px; padding: 10px 16px;
        background: ${type === "success" ? "#19b36b" : type === "error" ? "#e74c3c" : "#3498db"};
        color: white; border-radius: 8px; z-index: 10000; font-size: 12px;
        animation: slideIn 0.3s ease; box-shadow: 0 2px 10px rgba(0,0,0,0.15);
    `;
    document.body.appendChild(notification);
    setTimeout(() => notification.remove(), 3000);
}

function updateProcessFeedback(step, status) {
    const stepEl = el[`step${step}`];
    if (stepEl) {
        stepEl.className = `feedback-step ${status}`;
        if (status === "completed") stepEl.innerHTML = stepEl.innerHTML.replace("⏳", "✓");
        else if (status === "active") stepEl.innerHTML = stepEl.innerHTML.replace("⏳", "🔄");
    }
}

function resetProcessFeedback() {
    ["Upload", "Extract", "Match", "ACII"].forEach(step => {
        const stepEl = el[`step${step}`];
        if (stepEl) {
            stepEl.className = "feedback-step";
            stepEl.innerHTML = stepEl.innerHTML.replace("✓", "⏳").replace("🔄", "⏳");
        }
    });
}

/* -------------------------------------------------------
FILE AREA SETUP
------------------------------------------------------- */

function setupFileArea() {
    const fileArea = document.getElementById("fileArea");
    const fileInput = el.file;
    const fileNameSpan = document.getElementById("fileName");
    
    if (fileArea && fileInput) {
        fileArea.addEventListener("click", function(e) {
            if (e.target.classList && e.target.classList.contains("file-name")) return;
            fileInput.click();
        });
        
        fileArea.addEventListener("dragover", function(e) {
            e.preventDefault();
            fileArea.classList.add("drag-over");
        });
        
        fileArea.addEventListener("dragleave", function(e) {
            e.preventDefault();
            fileArea.classList.remove("drag-over");
        });
        
        fileArea.addEventListener("drop", function(e) {
            e.preventDefault();
            fileArea.classList.remove("drag-over");
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                fileInput.files = files;
                const changeEvent = new Event('change', { bubbles: true });
                fileInput.dispatchEvent(changeEvent);
            }
        });
        
        fileInput.addEventListener('change', function() {
            const file = fileInput.files[0];
            if (file) {
                if (file.name.toLowerCase().endsWith('.docx')) {
                    fileNameSpan.textContent = `📄 ${file.name}`;
                    setStatus("DOCX file selected. Ready to run check.", "good");
                    if (el.btnCheck) el.btnCheck.disabled = false;
                } else {
                    fileNameSpan.textContent = "";
                    setStatus("Only DOCX files are accepted.", "warn");
                    fileInput.value = '';
                    if (el.btnCheck) el.btnCheck.disabled = true;
                }
            } else {
                fileNameSpan.textContent = "";
            }
        });
    }
}

/* -------------------------------------------------------
ACII RATING & RECOMMENDATIONS
------------------------------------------------------- */

function getACIIRating(score) {
    score = Number(score);
    if (score >= 90) return { text: "Excellent", class: "excellent", description: "Outstanding citation integrity." };
    if (score >= 80) return { text: "Very Good", class: "very-good", description: "Strong citation integrity." };
    if (score >= 70) return { text: "Good", class: "good", description: "Satisfactory citation integrity." };
    if (score >= 60) return { text: "Moderate", class: "moderate", description: "Adequate citation integrity." };
    if (score >= 50) return { text: "Weak", class: "weak", description: "Below average citation integrity." };
    return { text: "Poor", class: "poor", description: "Low citation integrity." };
}

function generateACIIRecommendations(aciiData) {
    if (!aciiData) return "<div>No recommendation data available.</div>";
    const comp = aciiData.components || {};
    const recs = aciiData.recommendations || {};
    let html = "";
    if (recs.recency) html += `<div>📅 ${esc(recs.recency)}</div>`;
    if (recs.verification) html += `<div>🔍 ${esc(recs.verification)}</div>`;
    if (recs.diversity) html += `<div>👥 ${esc(recs.diversity)}</div>`;
    if (recs.priority) html += `<div class="priority">🎯 ${esc(recs.priority)}</div>`;
    if (!html) html += `<div>✨ Your citations look good!</div>`;
    return html;
}

/* -------------------------------------------------------
SUGGESTED REFERENCES FUNCTION (WITH DOI LINKS)
------------------------------------------------------- */

function appendSuggestedReferences(data) {
    console.log("[Debug] ========== appendSuggestedReferences START ==========");
    console.log("[Debug] Data type:", typeof data);
    console.log("[Debug] Data keys:", data ? Object.keys(data) : "null");
    console.log("[Debug] data.online_verification exists:", !!(data && data.online_verification));
    
    if (!data) {
        console.log("[Debug] No data for suggested references");
        return;
    }

    const container = el.fixSuggestionsContent;
    if (!container) {
        console.log("[Debug] No fixSuggestionsContent container found");
        return;
    }

    let html = "";
    let suggestionCount = 0;

    // Check for verification results that might have suggested_references
    let verifyResults = [];
    
    if (data.online_verification && data.online_verification.rows) {
        verifyResults = data.online_verification.rows;
        console.log(`[Debug] Found ${verifyResults.length} verification rows in data.online_verification`);
    } else if (data.verification && data.verification.rows) {
        verifyResults = data.verification.rows;
        console.log(`[Debug] Found verification.rows: ${verifyResults.length} rows`);
    } else if (Array.isArray(data.verify)) {
        verifyResults = data.verify;
        console.log(`[Debug] Found data.verify: ${verifyResults.length} rows`);
    } else if (Array.isArray(data)) {
        verifyResults = data;
        console.log(`[Debug] Data is array: ${verifyResults.length} rows`);
    } else if (data.rows && Array.isArray(data.rows)) {
        verifyResults = data.rows;
        console.log(`[Debug] Found data.rows: ${verifyResults.length} rows`);
    } else if (data.result && data.result.online_verification && data.result.online_verification.rows) {
        verifyResults = data.result.online_verification.rows;
        console.log(`[Debug] Found data.result.online_verification.rows: ${verifyResults.length} rows`);
    }
    
    // Also check window.latestResults as fallback
    if (verifyResults.length === 0 && window.latestResults) {
        console.log("[Debug] Checking window.latestResults...");
        if (window.latestResults.online_verification && window.latestResults.online_verification.rows) {
            verifyResults = window.latestResults.online_verification.rows;
            console.log(`[Debug] Found in window.latestResults: ${verifyResults.length} rows`);
        } else if (window.latestResults.rows) {
            verifyResults = window.latestResults.rows;
            console.log(`[Debug] Found window.latestResults.rows: ${verifyResults.length} rows`);
        }
    }

    console.log(`[Debug] Final verifyResults length: ${verifyResults.length}`);
    
    // Log the first row to see if it has suggested_references
    if (verifyResults.length > 0) {
        console.log("[Debug] First row keys:", Object.keys(verifyResults[0]));
        console.log("[Debug] First row has 'suggested_references':", 'suggested_references' in verifyResults[0]);
        if (verifyResults[0].suggested_references) {
            console.log("[Debug] First row suggested_references length:", verifyResults[0].suggested_references.length);
        }
    }

    verifyResults.forEach((row, index) => {
        if (row.suggested_references && row.suggested_references.length > 0) {
            suggestionCount++;
            console.log(`[Debug] Row ${index} has ${row.suggested_references.length} suggestions`);
            
            const originalRef = row.reference || row.matched_title || 'Unknown reference';
            
            html += `
                <div class="suggestions-section" style="margin-top: 20px; padding: 12px; background: #f8fafc; border-radius: 10px;">
                    <div class="suggestion-header" style="margin-bottom: 12px;">
                        <span style="font-weight: 600; color: #1e293b;">📚 Suggested Reference #${suggestionCount}</span>
                        <span class="badge ${row.status === 'verified' ? 'verified' : (row.status === 'likely' ? 'likely' : 'needs_review')}" style="margin-left: 10px;">
                            ${row.status || 'Unknown'}
                        </span>
                    </div>
                    <div class="suggestion-original" style="font-size: 12px; color: #64748b; margin-bottom: 10px;">
                        ❌ Original: ${esc(originalRef.substring(0, 200))}${originalRef.length > 200 ? '…' : ''}
                    </div>
                    <div style="margin-top: 8px;">
            `;

            row.suggested_references.forEach((s, idx) => {
                const confidence = s.score || 75;
                const borderColor = confidence >= 80 ? '#19b36b' : (confidence >= 70 ? '#f39c12' : '#e74c3c');
                const doiLink = s.doi ? `<a href="https://doi.org/${esc(s.doi)}" target="_blank" style="color: #19b36b; text-decoration: none;">🔗 View Article</a> <small style="color: #64748b;">(${esc(s.doi)})</small>` : '';
                
                html += `
                    <div class="suggestion-item" style="margin-top: 10px; padding: 10px; background: white; border-radius: 8px; border-left: 3px solid ${borderColor};">
                        <div class="suggestion-suggested" style="font-weight: 500; margin-bottom: 6px;">
                            ✅ ${esc(s.title || 'No title available')}
                        </div>
                        <div class="suggestion-meta" style="display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: #64748b;">
                            <span>📅 ${esc(s.year || "N/A")}</span>
                            <span>✍️ ${esc(s.authors || "Unknown author")}</span>
                            <span>📊 ${Math.round(confidence)}% match</span>
                        </div>
                        ${doiLink ? `<div class="suggestion-doi" style="margin-top: 6px; font-size: 10px;">${doiLink}</div>` : ''}
                    </div>
                `;
            });

            html += `
                    </div>
                </div>
            `;
        }
    });

    // Also check missing citations for suggestions
    const missingRows = data.missing_in_references || [];
    console.log(`[Debug] Checking ${missingRows.length} missing rows for suggestions`);
    
    missingRows.forEach((row, index) => {
        if (row.suggested_references && row.suggested_references.length > 0) {
            suggestionCount++;
            console.log(`[Debug] Missing row ${index} has ${row.suggested_references.length} suggestions`);
            
            const citationText = row.citation_in_text || row;
            
            html += `
                <div class="suggestions-section" style="margin-top: 20px; padding: 12px; background: #fef2f2; border-radius: 10px; border-left: 3px solid #e74c3c;">
                    <div class="suggestion-header" style="margin-bottom: 12px;">
                        <span style="font-weight: 600; color: #1e293b;">📚 Missing Citation Suggestion #${suggestionCount}</span>
                    </div>
                    <div class="suggestion-original" style="font-size: 12px; color: #991b1b; margin-bottom: 10px;">
                        ❌ Missing: ${esc(citationText)}
                    </div>
                    <div style="margin-top: 8px;">
            `;

            row.suggested_references.forEach((s, idx) => {
                const confidence = s.score || 75;
                const doiLink = s.doi ? `<a href="https://doi.org/${esc(s.doi)}" target="_blank" style="color: #19b36b; text-decoration: none;">🔗 View Article</a> <small style="color: #64748b;">(${esc(s.doi)})</small>` : '';
                
                html += `
                    <div class="suggestion-item" style="margin-top: 10px; padding: 10px; background: white; border-radius: 8px;">
                        <div class="suggestion-suggested" style="font-weight: 500; margin-bottom: 6px;">
                            ✅ ${esc(s.title || 'No title available')}
                        </div>
                        <div class="suggestion-meta" style="display: flex; gap: 12px; flex-wrap: wrap; font-size: 11px; color: #64748b;">
                            <span>📅 ${esc(s.year || "N/A")}</span>
                            <span>✍️ ${esc(s.authors || "Unknown author")}</span>
                            <span>📊 ${Math.round(confidence)}% match</span>
                        </div>
                        ${doiLink ? `<div class="suggestion-doi" style="margin-top: 6px; font-size: 10px;">${doiLink}</div>` : ''}
                    </div>
                `;
            });

            html += `
                    </div>
                </div>
            `;
        }
    });

    if (suggestionCount > 0) {
        console.log(`[Debug] ✅ Added ${suggestionCount} suggested references to the UI`);
        
        // Remove existing suggestions section if present
        let existingSection = container.querySelector('.suggestions-master-section');
        if (existingSection) {
            existingSection.remove();
        }
        
        // Remove any existing "no suggestions" message
        let noMsg = container.querySelector('.no-suggestions-msg');
        if (noMsg) noMsg.remove();
        
        // Create the master section
        const masterSection = document.createElement('div');
        masterSection.className = 'suggestions-master-section';
        masterSection.style.marginTop = '24px';
        masterSection.style.borderTop = '2px solid #e2e8f0';
        masterSection.style.paddingTop = '20px';
        masterSection.innerHTML = `
            <h3 style="font-size: 16px; margin-bottom: 16px; color: #1e293b; display: flex; align-items: center; gap: 8px;">
                📚 Suggested References from Online Verification
                <span style="background: #667eea; color: white; padding: 2px 8px; border-radius: 20px; font-size: 11px;">${suggestionCount}</span>
            </h3>
            ${html}
        `;
        
        container.appendChild(masterSection);
        console.log("[Debug] Master section appended to container");
    } else {
        console.log("[Debug] ❌ No suggested references found in verification results");
        // Show a message if no suggestions
        let noMsg = container.querySelector('.no-suggestions-msg');
        if (!noMsg) {
            noMsg = document.createElement('div');
            noMsg.className = 'no-suggestions-msg';
            noMsg.style.padding = '15px';
            noMsg.style.textAlign = 'center';
            noMsg.style.color = '#64748b';
            noMsg.style.fontSize = '12px';
            noMsg.innerHTML = '💡 No suggested references available. Run verification to get suggestions.';
            container.appendChild(noMsg);
        }
    }
    
    console.log("[Debug] ========== appendSuggestedReferences END ==========");
}

/* -------------------------------------------------------
AUTO-FIX FUNCTIONS
------------------------------------------------------- */

async function getAutoFixSuggestions() {
    if (!LAST_JOB_ID) { showNotification("Run document check first", "error"); return null; }
    try {
        setStatus("Fetching auto-fix suggestions...", "info");
        const response = await fetch(`/autofix-suggestions/${encodeURIComponent(LAST_JOB_ID)}`);
        const data = await response.json();
        if (data.available) {
            FIX_SUGGESTIONS = data;
            displayFixSuggestions(data);
            setStatus(`Auto-fix suggestions available (${data.auto_fixable_count} auto-fixable)`, "good");
            if (el.btnApplyAutofix) el.btnApplyAutofix.disabled = false;
            return data;
        } else {
            setStatus(data.message || "No auto-fix suggestions available", "warn");
            return null;
        }
    } catch (err) {
        console.error(err);
        setStatus("Error fetching auto-fix suggestions", "bad");
        return null;
    }
}

function displayFixSuggestions(data) {
    if (!el.fixSuggestionsPanel) return;
    const suggestions = data.suggestions || {};
    const citations = suggestions.citations || [];
    const references = suggestions.references || [];
    
    const uniqueCitations = [];
    const seen = new Set();
    for (const fix of citations) {
        if (!seen.has(fix.original)) {
            seen.add(fix.original);
            uniqueCitations.push(fix);
        }
    }
    
    const uniqueReferences = [];
    const seenRef = new Set();
    for (const fix of references) {
        const key = fix.original.substring(0, 100);
        if (!seenRef.has(key)) {
            seenRef.add(key);
            uniqueReferences.push(fix);
        }
    }
    
    el.fixSuggestionsPanel.style.display = "block";
    let html = `<div class="fix-summary"><h4>🔧 Auto-Fix Summary</h4>
        <div style="display: flex; gap: 15px; margin-bottom: 15px;">
            <span>✓ Auto-fixable: ${data.auto_fixable_count || 0}</span>
            <span>⚠️ Needs review: ${data.review_needed_count || 0}</span>
            <span>📋 Total: ${data.statistics?.total_suggestions || 0}</span>
        </div>
    </div>`;
    
    if (uniqueCitations.length > 0) {
        html += `<div class="fix-section"><h5>📝 Citation Fixes (${uniqueCitations.length})</h5>`;
        html += uniqueCitations.slice(0, 30).map(fix => `
            <div class="fix-item ${fix.confidence >= 0.85 ? 'high-conf' : 'med-conf'}">
                <div class="fix-original">❌ ${esc(fix.original)}</div>
                <div class="fix-suggested">✅ ${esc(fix.suggested)}</div>
                <div class="fix-meta">${Math.round(fix.confidence * 100)}% - ${esc(fix.reason)}</div>
            </div>
        `).join('');
        html += `</div>`;
    }
    
    if (uniqueReferences.length > 0) {
        html += `<div class="fix-section"><h5>📚 Reference Fixes (${uniqueReferences.length})</h5>`;
        html += uniqueReferences.slice(0, 15).map(fix => `
            <div class="fix-item ${fix.confidence >= 0.85 ? 'high-conf' : 'med-conf'}">
                <div class="fix-original">${esc(fix.original.substring(0, 120))}${fix.original.length > 120 ? '…' : ''}</div>
                <div class="fix-suggested">${esc(fix.suggested.substring(0, 120))}${fix.suggested.length > 120 ? '…' : ''}</div>
                <div class="fix-meta">${esc(fix.type)} - ${Math.round(fix.confidence * 100)}%</div>
            </div>
        `).join('');
        html += `</div>`;
    }
    
    if (uniqueCitations.length === 0 && uniqueReferences.length === 0) {
        html += `<div class="fix-empty">✨ No fix suggestions available. Document looks good!</div>`;
    }
    
    if (el.fixSuggestionsContent) el.fixSuggestionsContent.innerHTML = html;
    
    // APPEND SUGGESTED REFERENCES AFTER displaying fixes
    appendSuggestedReferences(window.latestResults || CURRENT_DATA);
}

async function applyAutoFix() {
    if (!LAST_JOB_ID) { showNotification("Run document check first", "error"); return; }
    if (AUTO_FIX_APPLIED) { showNotification("Auto-fix already applied", "info"); return; }
    try {
        setStatus("Applying auto-fixes...", "info");
        const formData = new FormData();
        formData.append("job_id", LAST_JOB_ID);
        const response = await fetch("/apply-autofix", { method: "POST", body: formData });
        const result = await response.json();
        if (result.success) {
            AUTO_FIX_APPLIED = true;
            setStatus(`Applied ${result.fixes_applied_count} fixes`, "good");
            showNotification(`✅ Applied ${result.fixes_applied_count} auto-fixes`, "success");
            if (el.btnExportFixed) el.btnExportFixed.disabled = false;
            await showFixLog();
        } else {
            showNotification("Failed to apply auto-fixes", "error");
        }
    } catch (err) {
        console.error(err);
        setStatus("Error applying auto-fixes", "bad");
        showNotification("Error applying auto-fixes", "error");
    }
}

async function showFixLog() {
    if (!LAST_JOB_ID) return;
    try {
        const response = await fetch(`/fix-log/${encodeURIComponent(LAST_JOB_ID)}`);
        const data = await response.json();
        if (el.fixLogPanel && data.fixes && data.fixes.length > 0) {
            el.fixLogPanel.style.display = "block";
            let html = `<div class="fix-log-list">`;
            data.fixes.forEach((fix, idx) => {
                html += `<div class="fix-log-entry">
                    <div><strong>#${idx + 1}</strong></div>
                    <div class="fix-log-original">${esc(fix.original)}</div>
                    <div class="fix-log-suggested">→ ${esc(fix.suggested)}</div>
                    <div class="fix-log-meta">${esc(fix.type)} - ${esc(fix.reason)}</div>
                </div>`;
            });
            html += `</div>`;
            if (el.fixLogContent) el.fixLogContent.innerHTML = html;
        }
    } catch (err) { console.error(err); }
}

function downloadFixedDocument() {
    if (!LAST_JOB_ID) { showNotification("Run document check first", "error"); return; }
    if (!AUTO_FIX_APPLIED) { showNotification("Please apply auto-fix first", "error"); return; }
    window.open(`/export-fixed-document/${encodeURIComponent(LAST_JOB_ID)}?format=txt`, '_blank');
    showNotification("Downloading fixed document...", "info");
}

/* -------------------------------------------------------
PROGRESS BAR
------------------------------------------------------- */

function updateProgress(progress, total, status = "processing", message = null, remainingSeconds = null) {
    if (!el.progressBar || !el.progressText) return;
    const percentage = total > 0 ? Math.round((progress / total) * 100) : 0;
    el.progressBar.style.width = `${percentage}%`;
    el.progressBar.textContent = `${percentage}%`;
    
    if (el.estimatedRemaining && remainingSeconds !== null && remainingSeconds > 0 && status === "processing") {
        el.estimatedRemaining.textContent = `⏱️ Est. remaining: ${formatTime(remainingSeconds)}`;
    } else if (el.estimatedRemaining && status !== "processing") {
        el.estimatedRemaining.textContent = "";
    }
    
    if (status === "completed") {
        el.progressBar.style.backgroundColor = "#27ae60";
        el.progressText.textContent = message || `✅ Complete! ${progress}/${total}`;
        if (el.verifyProgress) setTimeout(() => { el.verifyProgress.style.display = "none"; }, 5000);
    } else if (status === "error") {
        el.progressBar.style.backgroundColor = "#e74c3c";
        el.progressText.textContent = message || "❌ Error during verification";
    } else {
        el.progressBar.style.backgroundColor = "#3498db";
        let msg = message || `🔍 Verifying: ${progress}/${total} (${percentage}%)`;
        if (remainingSeconds) msg += ` - Est. ${formatTime(remainingSeconds)} remaining`;
        el.progressText.textContent = msg;
        if (el.verifyProgress) el.verifyProgress.style.display = "block";
    }
}

/* -------------------------------------------------------
QUEUE STATUS
------------------------------------------------------- */

async function updateQueueStatus() {
    try {
        const response = await fetch('/queue/status');
        const data = await response.json();
        const statUploads = document.getElementById("statUploads");
        const statQueue = document.getElementById("statQueue");
        if (statUploads) statUploads.textContent = data.total_jobs || 0;
        if (statQueue) statQueue.textContent = data.queue_size || 0;
        if (el.queueStatus) {
            const busyClass = data.is_busy ? 'busy' : 'ready';
            el.queueStatus.innerHTML = `<div class="queue-info ${busyClass}">
                <span>📊 Queue: ${data.queue_size || 0}</span>
                <span>⏳ Pending: ${data.pending_jobs || 0}</span>
                <span>⚙️ Processing: ${data.processing_jobs || 0}</span>
                <span>📈 Total Jobs: ${data.total_jobs || 0}</span>
            </div>`;
        }
        return data;
    } catch (err) { return null; }
}

/* -------------------------------------------------------
RESET UI
------------------------------------------------------- */

function resetVerificationUI() {
    if (el.verifyDash) {
        el.verifyDash.innerHTML = `<div class="kpi">✅ Verified: 0</div><div class="kpi">🔍 Likely: 0</div>
            <div class="kpi">⚠️ Needs Review: 0</div><div class="kpi">❌ Not Found: 0</div>`;
    }
    if (el.verifyBody) {
        el.verifyBody.innerHTML = `<tr><td colspan="9">No verification results. Click "Verify References" to start. </div> </div>`;
    }
    updateProgress(0, 0, "processing", "Ready to verify");
    if (el.verifyProgress) el.verifyProgress.style.display = "none";
    if (el.estimatedRemaining) el.estimatedRemaining.textContent = "";
    VERIFICATION_IN_PROGRESS = false;
}

/* -------------------------------------------------------
TAB NAVIGATION
------------------------------------------------------- */

const tabs = document.querySelectorAll(".tab");
const panes = document.querySelectorAll(".tabPane");

if (panes.length > 0) {
    panes.forEach((pane, index) => {
        if (index === 0) pane.classList.add("active");
        else pane.classList.remove("active");
    });
}

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
STATUS POLLING
------------------------------------------------------- */

async function fetchStatus() {
    if (!LAST_JOB_ID) return;
    POLL_ATTEMPT_COUNT++;
    if (POLL_ATTEMPT_COUNT > CONFIG.MAX_POLL_ATTEMPTS) {
        stopPolling();
        VERIFICATION_IN_PROGRESS = false;
        if (el.btnVerify) el.btnVerify.disabled = false;
        return;
    }
    try {
        const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}`);
        const js = await res.json();
        if (js.online) {
            const progress = js.online.progress || 0;
            const total = js.online.total || 0;
            const state = js.online.state || "processing";
            if (total > 0) updateProgress(progress, total, state);
            if (js.online.state === "completed") {
                setStatus("Online verification complete", "good");
                VERIFICATION_IN_PROGRESS = false;
                if (el.btnVerify) el.btnVerify.disabled = false;
                updateProgress(total, total, "completed", `✅ Complete! All ${total} citations verified`);
                stopPolling();
            }
        }
        if (js.result) renderAll(js.result);
    } catch (err) { console.error(err); }
}

function startPolling() {
    if (POLL_TIMER) clearInterval(POLL_TIMER);
    POLL_ATTEMPT_COUNT = 0;
    POLL_TIMER = setInterval(fetchStatus, CONFIG.POLL_INTERVAL);
}

function stopPolling() {
    if (POLL_TIMER) { clearInterval(POLL_TIMER); POLL_TIMER = null; }
}

/* -------------------------------------------------------
UNIQUE CITATIONS
------------------------------------------------------- */

function getUniqueCitationsWithCount(c2rRows) {
    const uniqueMap = new Map();
    c2rRows.forEach(row => {
        const citeText = row.in_text || '';
        const key = `${citeText.toLowerCase()}|${row.status}|${row.matched_reference || ''}`;
        if (uniqueMap.has(key)) uniqueMap.get(key).count++;
        else uniqueMap.set(key, { citation: citeText, status: row.status, matched_reference: row.matched_reference, flags: row.flags, count: 1 });
    });
    return Array.from(uniqueMap.values());
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

/* -------------------------------------------------------
RENDER FUNCTIONS
------------------------------------------------------- */

function renderSummaryTable(data) {
    const s = data?.summary || {};
    const summaryTable = el.summaryTable;
    if (summaryTable) {
        summaryTable.innerHTML = `
            <tr><td style="width:220px;">In-text citations (occurrences)</div><td>${esc(s.in_text_citations_found)}</div></tr>
            <tr><td style="width:220px;">References</div><td>${esc(s.reference_entries_found)}</div></tr>
            <tr><td style="width:220px;">Missing (unique)</div><td>${esc(s.missing_in_references)}</div></tr>
            <tr><td style="width:220px;">Uncited</div><td>${esc(s.uncited_references)}</div></tr>
            <tr><td style="width:220px;">Match rate</div><td>${esc(s.match_rate)}%</div></tr>
        `;
    }
}

function renderACII(data) {
    const acii = data?.acii;
    if (!acii) return;
    if (el.aciiCenterpiece) el.aciiCenterpiece.style.display = "block";
    if (el.aciiCard) el.aciiCard.style.display = "block";
    
    if (el.aciiScoreLarge) {
        const score = acii.ACII ?? "--";
        el.aciiScoreLarge.textContent = score;
        if (el.aciiRatingBadge && score !== "--") {
            const rating = getACIIRating(score);
            el.aciiRatingBadge.textContent = rating.text;
            el.aciiRatingBadge.className = `acii-rating ${rating.class}`;
        }
    }
    if (el.aciiRecommendationText) {
        el.aciiRecommendationText.innerHTML = generateACIIRecommendations(acii);
    }
    
    const comp = acii.components || {};
    if ($("aciiV")) $("aciiV").textContent = comp.verification_integrity?.score ?? "--";
    if ($("aciiVcat")) $("aciiVcat").textContent = comp.verification_integrity?.category ?? "--";
    if ($("aciiVremark")) $("aciiVremark").textContent = comp.verification_integrity?.remark ?? "--";
    if ($("aciiC")) $("aciiC").textContent = comp.citation_concentration?.score ?? "--";
    if ($("aciiCcat")) $("aciiCcat").textContent = comp.citation_concentration?.category ?? "--";
    if ($("aciiCremark")) $("aciiCremark").textContent = comp.citation_concentration?.remark ?? "--";
    if ($("aciiA")) $("aciiA").textContent = comp.author_diversity?.score ?? "--";
    if ($("aciiAcat")) $("aciiAcat").textContent = comp.author_diversity?.category ?? "--";
    if ($("aciiAremark")) $("aciiAremark").textContent = comp.author_diversity?.remark ?? "--";
    if ($("aciiT")) $("aciiT").textContent = comp.temporal_balance?.score ?? "--";
    if ($("aciiTcat")) $("aciiTcat").textContent = comp.temporal_balance?.category ?? "--";
    if ($("aciiTremark")) $("aciiTremark").textContent = comp.temporal_balance?.remark ?? "--";
}

function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;
    if (!rows.length) { el.missingBody.innerHTML = `<tr><td colspan="3">None</div></tr>`; return; }
    el.missingBody.innerHTML = rows.map((r, i) => `<tr><td style="width:50px;">${i + 1}</div><td>${esc(r.citation_in_text || r)}</div><td style="width:80px;">${esc(r.count_in_text || "")}</div></tr>`).join("");
}

function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    if (!rows.length) { el.uncitedBody.innerHTML = `<tr><td colspan="2">None</div></tr>`; return; }
    el.uncitedBody.innerHTML = rows.map((r, i) => `<tr><td style="width:50px;">${i + 1}</div><td>${esc(r.reference || r)}</div></tr>`).join("");
}

function renderC2R(data) {
    const c2rRaw = data?.reconciliation_intext_to_reference || [];
    const uniqueCitations = getUniqueCitationsWithCount(c2rRaw);
    if (!el.c2rBody) return;
    if (!uniqueCitations.length) { el.c2rBody.innerHTML = `<tr><td colspan="6">No mapping available</div></tr>`; return; }
    el.c2rBody.innerHTML = uniqueCitations.map((item, i) => `<tr>
        <td>${i + 1}</div>
        <td><span class="badge ${item.status === 'matched' ? 'matched' : 'not_found'}">${esc(item.status || '')}</span></div>
        <td style="max-width:300px;">${esc(item.citation)}</div>
        <td style="text-align:center"><strong>${item.count}</strong></div>
        <td style="max-width:400px;">${esc(item.matched_reference || '')}</div>
        <td>${esc(item.flags || '')}</div>
    </tr>`).join("");
}

function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;
    if (!rows.length) { el.r2cBody.innerHTML = `<tr><td colspan="4">No mapping available</div></td>`; return; }
    el.r2cBody.innerHTML = rows.map((r, i) => `<tr>
        <td>${i + 1}</div>
        <td>${esc(r.times_cited ?? 0)}</div>
        <td style="max-width:500px;">${esc(r.reference || '')}</div>
        <td>${esc((r.cited_by || []).slice(0, 3).join("; "))}</div>
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
            <div class="kpi">❌ Not Found: ${sum.not_found ?? 0}</div>`;
    }
    if (!el.verifyBody) return;
    if (!rows.length) { el.verifyBody.innerHTML = `<tr><td colspan="9">No verification results. Click "Verify References" to start.</div></tr>`; return; }
    
    el.verifyBody.innerHTML = rows.slice(0, CONFIG.MAX_VERIFY_DISPLAY).map((r, i) => {
        const doiLink = r.doi ? `<a href="https://doi.org/${esc(r.doi)}" target="_blank" style="color: #19b36b; text-decoration: none;">🔗 View Article</a> <small style="color: #64748b;">(${esc(r.doi)})</small>` : '';
        return `
            <tr>
                <td>${i + 1}</td>
                <td><span class="badge ${r.status === 'verified' ? 'verified' : (r.status === 'likely' ? 'likely' : 'not_found')}">${esc(r.status || '')}</span></td>
                <td>${esc(r.source || '—')}</td>
                <td>${esc(r.score || '—')}</td>
                <td>${doiLink || '—'}</td>
                <td>${esc(r.matched_year || '—')}</td>
                <td>${esc(r.matched_authors || '—')}</td>
                <td style="max-width:250px;">${esc((r.matched_title || '').substring(0, 60))}${(r.matched_title || '').length > 60 ? '…' : ''}</td>
            </tr>
        `;
    }).join("");
}

function renderAll(data) {
    if (!data) return;
    
    console.log("[Debug] renderAll called with data keys:", Object.keys(data));
    
    const normalized = normalizeData(data);
    normalized.job_id = data.job_id || LAST_JOB_ID;
    window.latestResults = normalized;
    
    // Enable export buttons
    const exportCsv = document.getElementById("btnExportCsvTop");
    const exportWord = document.getElementById("btnExportWordTop");
    if (exportCsv) exportCsv.disabled = false;
    if (exportWord) exportWord.disabled = false;
    
    if (el.resultsCard) el.resultsCard.style.display = "block";
    
    renderSummaryTable(normalized);
    renderACII(normalized);
    renderMissing(normalized);
    renderUncited(normalized);
    renderC2R(normalized);
    renderR2C(normalized);
    renderVerify(normalized);
    
    updateProcessFeedback("Upload", "completed");
    updateProcessFeedback("Extract", "completed");
    updateProcessFeedback("Match", "completed");
    updateProcessFeedback("ACII", "completed");
    
    if (normalized.autofix && normalized.autofix.suggestions) {
        FIX_SUGGESTIONS = normalized.autofix;
        displayFixSuggestions(FIX_SUGGESTIONS);
        if (el.btnApplyAutofix) el.btnApplyAutofix.disabled = false;
    }
    
    // 🔥 PASS THE ORIGINAL DATA DIRECTLY - DON'T MODIFY IT
    // The verification results are in data.online_verification
    console.log("[Debug] Checking for verification data:");
    console.log("[Debug] - data.online_verification:", !!data.online_verification);
    console.log("[Debug] - data.result?.online_verification:", !!(data.result && data.result.online_verification));
    console.log("[Debug] - window.latestResults?.online_verification:", !!(window.latestResults && window.latestResults.online_verification));
    
    // Try multiple locations to find verification results
    let verificationData = null;
    
    if (data.online_verification && data.online_verification.rows) {
        verificationData = data;
        console.log("[Debug] Using data.online_verification");
    } else if (data.result && data.result.online_verification && data.result.online_verification.rows) {
        verificationData = data.result;
        console.log("[Debug] Using data.result.online_verification");
    } else if (window.latestResults && window.latestResults.online_verification && window.latestResults.online_verification.rows) {
        verificationData = window.latestResults;
        console.log("[Debug] Using window.latestResults.online_verification");
    } else {
        verificationData = data;
        console.log("[Debug] Using original data (may not have online_verification)");
    }
    
    // Call appendSuggestedReferences with the data that has verification results
    appendSuggestedReferences(verificationData);
}

/* -------------------------------------------------------
EXPORT FUNCTIONS
------------------------------------------------------- */

function exportCSV() {
    if (!window.latestResults) { showNotification("No results to export. Run a check first.", "error"); return; }
    const data = window.latestResults;
    const s = data.summary || {};
    const missing = data.missing_in_references || [];
    const uncited = data.uncited_references || [];
    const acii = data.acii || {};
    
    let csv = [`"CiteIntegrity Report"`];
    csv.push(`"Generated","${new Date().toLocaleString()}"`);
    csv.push(`"File","${data.filename || 'N/A'}"`);
    csv.push(``);
    csv.push(`"SUMMARY"`);
    csv.push(`"In-text citations","${s.in_text_citations_found || 0}"`);
    csv.push(`"References","${s.reference_entries_found || 0}"`);
    csv.push(`"Missing","${s.missing_in_references || 0}"`);
    csv.push(`"Uncited","${s.uncited_references || 0}"`);
    csv.push(`"Match Rate","${s.match_rate || 0}%"`);
    csv.push(``);
    csv.push(`"ACII SCORE"`);
    csv.push(`"ACII","${acii.ACII || 'N/A'}"`);
    csv.push(`"Category","${acii.category || 'N/A'}"`);
    csv.push(``);
    csv.push(`"MISSING CITATIONS"`);
    csv.push(`"Citation","Count"`);
    missing.forEach(m => {
        const citation = typeof m === 'string' ? m : (m.citation_in_text || m);
        const count = typeof m === 'string' ? 1 : (m.count_in_text || 1);
        csv.push(`"${citation.replace(/"/g, '""')}",${count}`);
    });
    csv.push(``);
    csv.push(`"UNCITED REFERENCES"`);
    csv.push(`"Reference"`);
    uncited.forEach(ref => {
        const refText = typeof ref === 'string' ? ref : (ref.reference || ref);
        csv.push(`"${refText.replace(/"/g, '""')}"`);
    });
    
    const blob = new Blob([csv.join("\n")], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `citeintegrity_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    showNotification("CSV report downloaded", "success");
}

function exportWord() {
    if (!window.latestResults) { showNotification("No results to export. Run a check first.", "error"); return; }
    const data = window.latestResults;
    const s = data.summary || {};
    const missing = data.missing_in_references || [];
    const uncited = data.uncited_references || [];
    const acii = data.acii || {};
    const rating = getACIIRating(acii.ACII);
    
    let html = `<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>CiteIntegrity Report</title>
<style>
    body { font-family: 'Times New Roman', Times, serif; margin: 2.54cm 3.17cm; font-size: 12pt; }
    h1 { color: #1a2a4f; border-bottom: 2px solid #19b36b; }
    h2 { color: #1a2a4f; margin-top: 20px; border-left: 3px solid #19b36b; padding-left: 10px; }
    table { border-collapse: collapse; width: 100%; margin-bottom: 15px; }
    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
    th { background: #f2f2f2; }
    .acii-score { font-size: 36px; font-weight: bold; color: #19b36b; }
</style>
</head>
<body>
    <h1>CiteIntegrity Report</h1>
    <p><strong>Generated:</strong> ${new Date().toLocaleString()}</p>
    <p><strong>File:</strong> ${esc(data.filename || 'N/A')}</p>
    
    <h2>Summary</h2>
    <table>
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td style="width:220px;">In-text citations</div><td><strong>${s.in_text_citations_found || 0}</strong></div></tr>
        <tr><td style="width:220px;">References</div><td>${s.reference_entries_found || 0}</div></tr>
        <tr><td style="width:220px;">Missing</div><td><strong>${s.missing_in_references || 0}</strong></div></tr>
        <tr><td style="width:220px;">Uncited</div><td><strong>${s.uncited_references || 0}</strong></div></tr>
        <tr><td style="width:220px;">Match rate</div><td>${s.match_rate || 0}%</div></tr>
    </table>
    
    <h2>ACII Score</h2>
    <div class="acii-score">${acii.ACII || 'N/A'}</div>
    <p><strong>${rating.text}</strong> - ${rating.description}</p>
    
    <h2>Missing Citations</h2>
    ${missing.length ? `<ul>${missing.map(m => `<li>${esc(typeof m === 'string' ? m : (m.citation_in_text || m))}</li>`).join('')}</ul>` : '<p>None found.</p>'}
    
    <h2>Uncited References</h2>
    ${uncited.length ? `<ul>${uncited.map(ref => `<li>${esc(typeof ref === 'string' ? ref : (ref.reference || ref))}</li>`).join('')}</ul>` : '<p>None found.</p>'}
    
    <div class="footer"><p>Generated by CiteIntegrity</p></div>
</body>
</html>`;
    
    const blob = new Blob([html], { type: "application/msword" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `citeintegrity_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.doc`;
    link.click();
    URL.revokeObjectURL(link.href);
    showNotification("Word report downloaded", "success");
}

function exportVerificationReport() {
    if (!window.latestResults) { showNotification("No verification data to export.", "error"); return; }
    const ov = window.latestResults?.online_verification || {};
    const rows = ov.rows || [];
    if (!rows.length) { showNotification("No verification results available.", "error"); return; }
    
    let csv = [`"Status","Source","Score","DOI","Year","Authors","Title"`];
    rows.forEach(r => {
        csv.push(`"${r.status || ''}","${r.source || ''}","${r.score || ''}","${r.doi || ''}","${r.matched_year || ''}","${(r.matched_authors || '').substring(0, 100)}","${(r.matched_title || '').substring(0, 100)}"`);
    });
    
    const blob = new Blob([csv.join("\n")], { type: "text/csv;charset=utf-8" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `verification_report_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
    showNotification("Verification report downloaded", "success");
}

/* -------------------------------------------------------
RUN INITIAL CHECK
------------------------------------------------------- */

async function runInitialCheck() {
    const f = el.file?.files?.[0];
    if (!f) { setStatus("Please choose a file first", "warn"); return; }
    if (!f.name.toLowerCase().endsWith('.docx')) {
        setStatus("Only DOCX files are accepted.", "warn");
        return;
    }
    
    resetProcessFeedback();
    updateProcessFeedback("Upload", "active");
    if (POLL_TIMER) clearInterval(POLL_TIMER);
    VERIFICATION_IN_PROGRESS = false;
    AUTO_FIX_APPLIED = false;
    LAST_JOB_ID = null;
    resetVerificationUI();
    setStatus("Analyzing document...");
    
    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", "apa");
    fd.append("enable_autofix", (el.autofix?.checked || false).toString());
    fd.append("enable_online_verification", (el.onlineVerify?.checked || false).toString());
    
    try {
        const res = await fetch("/verify", { method: "POST", body: fd });
        if (res.status === 503) {
            setStatus("Server is busy, please wait...", "warn");
            if (RETRY_COUNT < 3) { RETRY_COUNT++; setTimeout(runInitialCheck, CONFIG.RETRY_DELAY); }
            return;
        }
        const js = await res.json();
        LAST_JOB_ID = js.job_id;
        updateProcessFeedback("Extract", "completed");
        updateProcessFeedback("Match", "active");
        renderAll(js);
        setStatus("Analysis complete", "good");
        updateQueueStatus();
        if (el.btnVerify) el.btnVerify.disabled = false;
        if (js.online_verification_started) startPolling();
    } catch (err) { setStatus("Error: " + err.message, "bad"); }
}

/* -------------------------------------------------------
RUN ONLINE VERIFICATION
------------------------------------------------------- */

async function runOnlineVerification() {
    if (!LAST_JOB_ID) { setStatus("Run document check first", "warn"); return; }
    if (VERIFICATION_IN_PROGRESS) { setStatus("Verification already in progress...", "warn"); return; }
    
    setStatus("Starting online verification...");
    VERIFICATION_IN_PROGRESS = true;
    updateProgress(0, 0, "processing", "Starting verification...");
    if (el.btnVerify) el.btnVerify.disabled = true;
    
    const fd = new FormData();
    fd.append("job_id", LAST_JOB_ID);
    
    try {
        const res = await fetch("/verify-online", { method: "POST", body: fd });
        const js = await res.json();
        if (js.started) { setStatus("Verification in progress...", "info"); startPolling(); }
        else if (js.completed) { setStatus("Verification already completed", "good"); VERIFICATION_IN_PROGRESS = false; if (el.btnVerify) el.btnVerify.disabled = false; fetchStatus(); }
        else { setStatus(js.message || "Verification could not start", "warn"); VERIFICATION_IN_PROGRESS = false; if (el.btnVerify) el.btnVerify.disabled = false; }
    } catch (err) { setStatus("Error: " + err.message, "bad"); VERIFICATION_IN_PROGRESS = false; if (el.btnVerify) el.btnVerify.disabled = false; }
}

/* -------------------------------------------------------
BUTTON EVENT LISTENERS
------------------------------------------------------- */

setupFileArea();

if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
if (el.btnVerify) { el.btnVerify.disabled = true; el.btnVerify.addEventListener("click", runOnlineVerification); }
if (el.btnApplyAutofix) { el.btnApplyAutofix.disabled = true; el.btnApplyAutofix.addEventListener("click", applyAutoFix); }
if (el.btnExportFixed) { el.btnExportFixed.disabled = true; el.btnExportFixed.addEventListener("click", downloadFixedDocument); }

// Export button listeners
const exportCsvBtn = document.getElementById("btnExportCsvTop");
const exportWordBtn = document.getElementById("btnExportWordTop");
const exportVerifyBtn = document.getElementById("btnExportVerify");

if (exportCsvBtn) exportCsvBtn.addEventListener("click", exportCSV);
if (exportWordBtn) exportWordBtn.addEventListener("click", exportWord);
if (exportVerifyBtn) exportVerifyBtn.addEventListener("click", exportVerificationReport);

setInterval(updateQueueStatus, 5000);
updateQueueStatus();

console.log("[CiteIntegrity] App initialized - All features working with Suggested References & DOI Links");

});
