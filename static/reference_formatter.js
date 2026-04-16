// =========================================
// reference_formatter.js — CiteIntegrity Pro Frontend
// Aligned with existing main.py backend
// =========================================

const form = document.getElementById("formatterForm");
const styleSelect = document.getElementById("style");
const sourceTypeSelect = document.getElementById("source_type");
const formattedOutput = document.getElementById("formattedOutput");
const copyBtn = document.getElementById("copyBtn");
const warningsBox = document.getElementById("warningsBox");
const warningsList = document.getElementById("warningsList");
const formatBtn = document.getElementById("formatBtn");
const rawReference = document.getElementById("raw_reference");

const statsPanel = document.getElementById("statsPanel");
const totalRefs = document.getElementById("totalRefs");
const doiCount = document.getElementById("doiCount");
const avgConfidence = document.getElementById("avgConfidence");
const needsReview = document.getElementById("needsReview");
const repairSummary = document.getElementById("repairSummary");
const infoBox = document.getElementById("infoBox");
const downloadTxtBtn = document.getElementById("downloadTxtBtn");
const downloadCsvBtn = document.getElementById("downloadCsvBtn");
const downloadJsonBtn = document.getElementById("downloadJsonBtn");
const autoEnhance = document.getElementById("auto_enhance");
const autoFindDoi = document.getElementById("auto_find_doi");
const repairModal = document.getElementById("repairModal");
const modalContent = document.getElementById("modalContent");
const closeModalBtn = document.getElementById("closeModalBtn");

let currentRepairResults = [];
let currentFormattedText = "";
let currentRawText = "";

if (!form) {
    console.error("formatterForm not found");
}

// =========================================
// UI HELPERS
// =========================================
function resetPanels() {
    if (warningsList) warningsList.innerHTML = "";
    if (warningsBox) warningsBox.classList.add("hidden");
    if (statsPanel) statsPanel.classList.add("hidden");
    if (infoBox) infoBox.classList.add("hidden");
}

function setLoading(isLoading, message = "🔍 Processing references...") {
    if (formatBtn) {
        formatBtn.disabled = isLoading;
        formatBtn.textContent = isLoading
            ? "⏳ Processing..."
            : "🚀 Format & Repair References";
    }

    if (formattedOutput && isLoading) {
        formattedOutput.innerHTML = `<span style="color:#19b36b;">${escapeHtml(message)}</span>`;
    }
}

function showError(message) {
    if (formattedOutput) {
        formattedOutput.innerHTML = `<span style="color:#ef4444;">${escapeHtml(message)}</span>`;
    }
}

function showTemporaryMessage(element, message, duration = 1200) {
    if (!element) {
        const toast = document.createElement("div");
        toast.textContent = message;
        toast.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #19b36b;
            color: white;
            padding: 10px 20px;
            border-radius: 8px;
            z-index: 10000;
            box-shadow: 0 4px 14px rgba(0,0,0,0.15);
        `;
        document.body.appendChild(toast);
        setTimeout(() => toast.remove(), duration);
        return;
    }

    const originalText = element.textContent;
    element.textContent = message;
    setTimeout(() => {
        element.textContent = originalText;
    }, duration);
}

// =========================================
// MAIN SUBMIT HANDLER
// =========================================
if (form) {
    form.addEventListener("submit", async function (e) {
        e.preventDefault();

        const rawText = rawReference ? rawReference.value.trim() : "";

        if (!rawText) {
            resetPanels();
            showError("⚠️ Please paste at least one reference.");
            return;
        }

        currentRawText = rawText;
        currentFormattedText = "";
        currentRepairResults = [];

        resetPanels();
        setLoading(true, "🔍 Processing references with OpenAlex integration...");

        try {
            const payload = new URLSearchParams({
                raw_reference: rawText,
                style: styleSelect ? styleSelect.value : "apa7",
                variant: "generic",
                source_type: sourceTypeSelect ? sourceTypeSelect.value : "journal"
            });

            console.log("Submitting formatter request:", Object.fromEntries(payload.entries()));

            const response = await fetch("/api/format-reference", {
                method: "POST",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json"
                },
                body: payload
            });

            console.log("Formatter response status:", response.status);

            let data;
            const contentType = response.headers.get("content-type") || "";

            if (contentType.includes("application/json")) {
                data = await response.json();
            } else {
                const text = await response.text();
                console.error("Non-JSON response:", text);
                throw new Error(`HTTP ${response.status}: server returned non-JSON response`);
            }

            console.log("Formatter response data:", data);

            if (!response.ok) {
                const msg = data?.detail || data?.message || "Request failed";
                throw new Error(`HTTP ${response.status}: ${msg}`);
            }

            if (!data.success) {
                throw new Error(data.message || "Formatting failed.");
            }

            currentFormattedText = data.formatted || "";
            currentRepairResults = normalizeRepairResults(rawText, data);

            renderOutput(currentFormattedText);
            renderWarnings(data.warnings || []);
            renderStats(currentRepairResults);
            renderSummary(currentRepairResults);

            if (currentRepairResults.some(r => r.needs_review)) {
                addRepairButton();
            } else {
                removeRepairButton();
            }

        } catch (error) {
            console.error("Format error:", error);
            showError(`❌ ${error.message}`);
            removeRepairButton();
        } finally {
            setLoading(false);
        }
    });
}

// =========================================
// NORMALIZATION
// =========================================
function normalizeRepairResults(rawText, data) {
    if (Array.isArray(data.repair_results) && data.repair_results.length > 0) {
        return data.repair_results.map((r, idx) => ({
            original: r.original || "",
            formatted: r.formatted || "",
            confidence: typeof r.confidence === "number" ? r.confidence : 75,
            issues: Array.isArray(r.issues) ? r.issues : [],
            repair_log: Array.isArray(r.repair_log) ? r.repair_log : [],
            has_doi: Boolean(r.has_doi),
            needs_review: Boolean(r.needs_review),
            parsed: r.parsed || {},
            index: idx
        }));
    }

    const references = rawText.split(/\n+/).map(r => r.trim()).filter(Boolean);
    const formattedRefs = (data.formatted || "").split(/\n\s*\n/).map(r => r.trim());

    return references.map((ref, idx) => ({
        original: ref,
        formatted: formattedRefs[idx] || ref,
        confidence: 75,
        issues: Array.isArray(data.warnings) ? data.warnings : [],
        repair_log: [],
        has_doi: Boolean(extractDoiFromText(ref)),
        needs_review: false,
        parsed: {
            doi: extractDoiFromText(ref),
            year: extractYearFromText(ref),
            authors: extractAuthorsFromText(ref),
            title: extractTitleFromText(ref),
            source: extractSourceFromText(ref)
        },
        index: idx
    }));
}

// =========================================
// RENDERING
// =========================================
function renderOutput(text) {
    if (!formattedOutput) return;

    if (!text) {
        formattedOutput.innerHTML = '<span style="color:#ef4444;">No output returned.</span>';
        return;
    }

    formattedOutput.innerHTML = escapeHtml(text).replace(/\n/g, "<br>");
}

function renderWarnings(warnings) {
    if (!warningsBox || !warningsList) return;

    if (!warnings || warnings.length === 0) {
        warningsBox.classList.add("hidden");
        return;
    }

    warningsList.innerHTML = "";
    warnings.forEach(warning => {
        const li = document.createElement("li");
        li.textContent = warning;
        warningsList.appendChild(li);
    });

    warningsBox.classList.remove("hidden");
}

function renderStats(results) {
    if (!statsPanel || !results || results.length === 0) return;

    const total = results.length;
    const dois = results.filter(r => r.has_doi).length;
    const avg = Math.round(
        results.reduce((sum, r) => sum + (Number(r.confidence) || 0), 0) / total
    );
    const review = results.filter(r => r.needs_review).length;

    if (totalRefs) totalRefs.textContent = String(total);
    if (doiCount) doiCount.textContent = String(dois);
    if (avgConfidence) avgConfidence.textContent = `${avg}%`;
    if (needsReview) needsReview.textContent = String(review);

    statsPanel.classList.remove("hidden");
}

function renderSummary(results) {
    if (!infoBox || !repairSummary || !results || results.length === 0) return;

    const total = results.length;
    const dois = results.filter(r => r.has_doi).length;
    const issues = results.reduce((sum, r) => sum + (r.issues?.length || 0), 0);

    let html = `<p>✅ Processed ${total} reference(s)</p>`;
    if (dois > 0) html += `<p>🌐 Found ${dois} DOI(s) in the references</p>`;
    if (issues > 0) html += `<p>⚠️ Flagged ${issues} issue(s) across the reference set</p>`;

    repairSummary.innerHTML = html;
    infoBox.classList.remove("hidden");
}

// =========================================
// CLIPBOARD
// =========================================
if (copyBtn) {
    copyBtn.addEventListener("click", async function () {
        const text = currentFormattedText || (formattedOutput ? formattedOutput.textContent.trim() : "");

        if (!text) {
            showTemporaryMessage(copyBtn, "Nothing to copy", 1200);
            return;
        }

        try {
            await navigator.clipboard.writeText(text);
            showTemporaryMessage(copyBtn, "✓ Copied!", 1200);
        } catch (error) {
            console.error("Copy failed:", error);
            showTemporaryMessage(copyBtn, "❌ Copy failed", 1200);
        }
    });
}

// =========================================
// EXPORTS
// =========================================
if (downloadTxtBtn) {
    downloadTxtBtn.addEventListener("click", function () {
        if (!currentFormattedText) {
            alert("No formatted content to download. Please format references first.");
            return;
        }

        const blob = new Blob([currentFormattedText], { type: "text/plain" });
        triggerDownload(blob, `references_${styleSelect?.value || "apa7"}_${timestamp()}.txt`);
        showTemporaryMessage(downloadTxtBtn, "✓ Downloaded!", 1000);
    });
}

if (downloadCsvBtn) {
    downloadCsvBtn.addEventListener("click", function () {
        if (!currentRepairResults || currentRepairResults.length === 0) {
            alert("No repair results available. Please format references first.");
            return;
        }

        const headers = [
            "Original Reference",
            "Formatted Reference",
            "Confidence (%)",
            "Issues",
            "Has DOI",
            "Repair Actions",
            "DOI",
            "Year",
            "Authors",
            "Title",
            "Source"
        ];

        const rows = currentRepairResults.map(r => [
            escapeCsv(r.original),
            escapeCsv(r.formatted),
            Number(r.confidence) || 75,
            escapeCsv((r.issues || []).join("; ")),
            r.has_doi ? "Yes" : "No",
            escapeCsv((r.repair_log || []).join("; ")),
            escapeCsv(r.parsed?.doi || ""),
            escapeCsv(r.parsed?.year || ""),
            escapeCsv(r.parsed?.authors || ""),
            escapeCsv(r.parsed?.title || ""),
            escapeCsv(r.parsed?.source || "")
        ]);

        const csvContent = [headers, ...rows].map(row => row.join(",")).join("\n");
        const blob = new Blob(["\uFEFF" + csvContent], { type: "text/csv;charset=utf-8;" });
        triggerDownload(blob, `reference_report_${timestamp()}.csv`);
        showTemporaryMessage(downloadCsvBtn, "✓ CSV Saved!", 1000);
    });
}

if (downloadJsonBtn) {
    downloadJsonBtn.addEventListener("click", function () {
        if (!currentRepairResults || currentRepairResults.length === 0) {
            alert("No repair results available. Please format references first.");
            return;
        }

        const exportData = {
            timestamp: new Date().toISOString(),
            style: styleSelect?.value || "apa7",
            source_type: sourceTypeSelect?.value || "journal",
            total_references: currentRepairResults.length,
            references: currentRepairResults,
            raw_input: currentRawText
        };

        const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: "application/json" });
        triggerDownload(blob, `reference_export_${timestamp()}.json`);
        showTemporaryMessage(downloadJsonBtn, "✓ JSON Saved!", 1000);
    });
}

function triggerDownload(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function timestamp() {
    return new Date().toISOString().slice(0, 19).replace(/:/g, "-");
}

// =========================================
// SIDE-BY-SIDE REPAIR UI
// =========================================
function addRepairButton() {
    const existingBtn = document.getElementById("repairBtn");
    if (existingBtn) return;

    const repairBtn = document.createElement("button");
    repairBtn.id = "repairBtn";
    repairBtn.type = "button";
    repairBtn.textContent = "🔧 Open Side-by-Side Repair";
    repairBtn.style.cssText = "width: 100%; margin-top: 15px; background: #764ba2;";
    repairBtn.addEventListener("click", openRepairModal);

    if (form && form.parentNode) {
        form.parentNode.insertBefore(repairBtn, form.nextSibling);
    }
}

function removeRepairButton() {
    const existingBtn = document.getElementById("repairBtn");
    if (existingBtn) existingBtn.remove();
}

function openRepairModal() {
    if (!currentRepairResults || currentRepairResults.length === 0) {
        alert("No repair results available. Please format references first.");
        return;
    }

    if (!repairModal || !modalContent) return;

    let modalHtml = '<div style="max-height: 70vh; overflow-y: auto;">';

    currentRepairResults.forEach((result, idx) => {
        const confidence = Number(result.confidence) || 75;
        const needsAttention = result.needs_review || confidence < 70;
        const borderColor = needsAttention ? "#f59e0b" : "#19b36b";

        modalHtml += `
            <div style="border: 2px solid ${borderColor}; border-radius: 8px; padding: 15px; margin-bottom: 20px; background: #fafafa;">
                <h3 style="margin-top: 0;">Reference ${idx + 1}</h3>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                    <div>
                        <strong style="color: #dc2626;">📄 Original:</strong>
                        <textarea id="original_${idx}" style="width: 100%; height: 100px; margin-top: 5px; padding: 8px; font-family: monospace; font-size: 12px; border: 1px solid #ddd; border-radius: 4px;" readonly>${escapeHtml(result.original)}</textarea>
                    </div>
                    <div>
                        <strong style="color: #19b36b;">✅ Suggested Fix:</strong>
                        <textarea id="suggested_${idx}" style="width: 100%; height: 100px; margin-top: 5px; padding: 8px; font-family: monospace; font-size: 12px; border: 1px solid #ddd; border-radius: 4px;">${escapeHtml(result.formatted)}</textarea>
                    </div>
                </div>
                <div style="margin-top: 10px;">
                    <strong>Confidence: ${confidence}%</strong>
                    <div style="background: #e5e7eb; height: 6px; border-radius: 3px; margin-top: 5px;">
                        <div style="background: ${confidence >= 80 ? "#19b36b" : confidence >= 50 ? "#f59e0b" : "#dc2626"}; width: ${confidence}%; height: 6px; border-radius: 3px;"></div>
                    </div>
                </div>
                ${result.issues && result.issues.length > 0 ? `
                    <div style="margin-top: 10px;">
                        <strong>⚠️ Issues:</strong>
                        <ul style="margin: 5px 0 0 20px;">
                            ${result.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join("")}
                        </ul>
                    </div>
                ` : ""}
                ${result.repair_log && result.repair_log.length > 0 ? `
                    <div style="margin-top: 10px; font-size: 12px; color: #6b7280;">
                        <strong>🔧 Repairs applied:</strong> ${escapeHtml(result.repair_log.join("; "))}
                    </div>
                ` : ""}
                <div style="margin-top: 10px; display: flex; gap: 10px;">
                    <button type="button" onclick="acceptSuggestion(${idx})" style="flex: 1; padding: 8px; background: #19b36b; color: white; border: none; border-radius: 4px; cursor: pointer;">✅ Accept Fix</button>
                    <button type="button" onclick="keepOriginal(${idx})" style="flex: 1; padding: 8px; background: #6b7280; color: white; border: none; border-radius: 4px; cursor: pointer;">⏸️ Keep Original</button>
                    <button type="button" onclick="manualEdit(${idx})" style="flex: 1; padding: 8px; background: #0284c7; color: white; border: none; border-radius: 4px; cursor: pointer;">✏️ Manual Edit</button>
                </div>
            </div>
        `;
    });

    modalHtml += "</div>";
    modalContent.innerHTML = modalHtml;
    repairModal.style.display = "block";
}

window.acceptSuggestion = function (idx) {
    const suggestedTextarea = document.getElementById(`suggested_${idx}`);
    if (!suggestedTextarea) return;
    updateFormattedOutput(suggestedTextarea.value, idx);
    showTemporaryMessage(null, "✓ Fix accepted!", 1000);
};

window.keepOriginal = function (idx) {
    const originalTextarea = document.getElementById(`original_${idx}`);
    if (!originalTextarea) return;
    updateFormattedOutput(originalTextarea.value, idx);
    showTemporaryMessage(null, "✓ Original kept", 1000);
};

window.manualEdit = function (idx) {
    const suggestedTextarea = document.getElementById(`suggested_${idx}`);
    if (!suggestedTextarea) return;

    const newValue = prompt("Edit reference manually:", suggestedTextarea.value);
    if (newValue !== null) {
        suggestedTextarea.value = newValue;
        updateFormattedOutput(newValue, idx);
        showTemporaryMessage(null, "✓ Manual edit applied", 1000);
    }
};

function updateFormattedOutput(newText, referenceIdx) {
    if (!currentRepairResults || !currentRepairResults[referenceIdx]) return;

    currentRepairResults[referenceIdx].formatted = newText;
    currentRepairResults[referenceIdx].manually_edited = true;

    currentFormattedText = currentRepairResults.map(r => r.formatted).join("\n\n");
    renderOutput(currentFormattedText);
}

if (closeModalBtn) {
    closeModalBtn.addEventListener("click", function () {
        if (repairModal) repairModal.style.display = "none";
    });
}

window.addEventListener("click", function (e) {
    if (repairModal && e.target === repairModal) {
        repairModal.style.display = "none";
    }
});

// =========================================
// PARSING HELPERS
// =========================================
function extractDoiFromText(text) {
    const doiPattern = /10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i;
    const match = text.match(doiPattern);
    return match ? match[0] : "";
}

function extractYearFromText(text) {
    const yearPattern = /\((\d{4})\)|,\s*(\d{4})/;
    const match = text.match(yearPattern);
    return match ? (match[1] || match[2]) : "";
}

function extractAuthorsFromText(text) {
    const authorPattern = /^(.+?)\s*\(\d{4}\)/;
    const match = text.match(authorPattern);
    return match ? match[1].trim() : "";
}

function extractTitleFromText(text) {
    const titlePattern = /\(\d{4}\)\.\s*(.+?)\.\s+[A-Z]/;
    const match = text.match(titlePattern);
    return match ? match[1].trim() : "";
}

function extractSourceFromText(text) {
    const sourcePattern = /\.\s*([^.,]+(?:\s+[^.,]+)*)\,\s*\d+/;
    const match = text.match(sourcePattern);
    return match ? match[1].trim() : "";
}

// =========================================
// GENERIC HELPERS
// =========================================
function escapeHtml(str) {
    if (!str) return "";
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

function escapeCsv(str) {
    if (str === null || str === undefined) return "";
    const s = String(str);
    if (s.includes(",") || s.includes('"') || s.includes("\n") || s.includes("\r")) {
        return `"${s.replace(/"/g, '""')}"`;
    }
    return s;
}

// =========================================
// KEYBOARD SHORTCUT
// =========================================
if (rawReference) {
    rawReference.addEventListener("keydown", function (e) {
        if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
            e.preventDefault();
            if (form) {
                form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
            }
        }
    });
}

console.log("CiteIntegrity Pro frontend loaded - aligned with main.py backend");
