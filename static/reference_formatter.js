// =========================================
// reference_formatter.js — CiteIntegrity Pro Frontend
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

// New UI Elements for enhanced features
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

// State management
let currentRepairResults = null;
let currentFormattedText = '';
let currentRawText = '';

if (!form) {
    console.error("formatterForm not found");
}

// =========================================
// 1. MAIN FORM SUBMIT HANDLER
// =========================================
if (form) {
    form.addEventListener("submit", async function (e) {
    e.preventDefault();

    const rawText = rawReference.value.trim();

    if (!rawText) {
        formattedOutput.textContent = "⚠️ Please paste at least one reference.";
        warningsList.innerHTML = "";
        warningsBox.classList.add("hidden");
        if (statsPanel) statsPanel.classList.add("hidden");
        if (infoBox) infoBox.classList.add("hidden");
        return;
    }

    // Store raw text for later exports
    currentRawText = rawText;

    // Show loading state
    formattedOutput.textContent = "🔍 Processing references with OpenAlex integration...";
    formattedOutput.classList.add("loading");
    formatBtn.disabled = true;
    if (formatBtn) formatBtn.textContent = "⏳ Processing...";
    warningsList.innerHTML = "";
    warningsBox.classList.add("hidden");
    if (statsPanel) statsPanel.classList.add("hidden");
    if (infoBox) infoBox.classList.add("hidden");

    // Prepare form data with enhanced fields
    const formData = new FormData(form);
    
    // Add new parameters for enhanced features
    if (autoEnhance) formData.append("auto_enhance", autoEnhance.checked);
    if (autoFindDoi) formData.append("auto_find_doi", autoFindDoi.checked);
    
    // Add source type if not already in form
    if (sourceTypeSelect && !formData.has("source_type")) {
        formData.append("source_type", sourceTypeSelect.value);
    }

    try {
        const response = await fetch("/api/format-reference", {
            method: "POST",
            body: formData
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            formattedOutput.textContent = data.message || "❌ Formatting failed.";
            return;
        }

        // Store results for exports
        currentFormattedText = data.formatted || "";
        currentRepairResults = data.repair_results || null;
        
        // Display formatted output
        formattedOutput.textContent = currentFormattedText || "No output returned.";
        
        // Format with line breaks for better readability
        if (currentFormattedText && currentFormattedText.includes('\n')) {
            formattedOutput.innerHTML = currentFormattedText.replace(/\n/g, '<br>');
        }

        // Display warnings
        if (data.warnings && data.warnings.length > 0) {
            warningsList.innerHTML = "";
            data.warnings.forEach(function (warning) {
                const li = document.createElement("li");
                li.textContent = warning;
                warningsList.appendChild(li);
            });
            warningsBox.classList.remove("hidden");
        } else {
            warningsBox.classList.add("hidden");
        }

        // Display enhanced statistics
        if (data.total_references && statsPanel) {
            statsPanel.classList.remove("hidden");
            totalRefs.textContent = data.total_references;
            doiCount.textContent = data.references_with_doi || 0;
            avgConfidence.textContent = `${Math.round(data.average_confidence || 0)}%`;
            needsReview.textContent = data.needs_review || 0;
        }

        // Display repair summary with OpenAlex info
        if (data.repair_results && data.repair_results.length > 0 && infoBox) {
            infoBox.classList.remove("hidden");
            
            const totalActions = data.repair_results.reduce((sum, r) => sum + (r.repair_log?.length || 0), 0);
            const doiFound = data.repair_results.filter(r => r.has_doi && !r.original.includes('10.')).length;
            const openAlexMatches = data.repair_results.filter(r => 
                r.repair_log && r.repair_log.some(log => log.includes('OpenAlex'))
            ).length;
            
            let summaryHtml = `<p>✅ Processed ${data.total_references} reference(s)</p>`;
            if (totalActions > 0) {
                summaryHtml += `<p>🔧 Applied ${totalActions} automatic repair(s)</p>`;
            }
            if (doiFound > 0) {
                summaryHtml += `<p>🌐 Found ${doiFound} new DOI(s) via OpenAlex/Crossref</p>`;
            }
            if (openAlexMatches > 0) {
                summaryHtml += `<p>⭐ ${openAlexMatches} reference(s) enhanced with OpenAlex metadata</p>`;
            }
            
            // Show confidence distribution
            const highConfidence = data.repair_results.filter(r => r.confidence >= 80).length;
            const mediumConfidence = data.repair_results.filter(r => r.confidence >= 50 && r.confidence < 80).length;
            const lowConfidence = data.repair_results.filter(r => r.confidence < 50).length;
            
            if (highConfidence > 0 || mediumConfidence > 0) {
                summaryHtml += `<p>📊 Confidence: ${highConfidence} high, ${mediumConfidence} medium, ${lowConfidence} low</p>`;
            }
            
            repairSummary.innerHTML = summaryHtml;
        }

        // Show side-by-side repair button if there are issues
        if (data.needs_review > 0 && data.repair_results) {
            addRepairButton();
        }

    } catch (error) {
        console.error("Format error:", error);
        formattedOutput.textContent = "❌ An error occurred while formatting the reference(s). Please check console for details.";
    } finally {
        formattedOutput.classList.remove("loading");
        formatBtn.disabled = false;
        if (formatBtn) formatBtn.textContent = "🚀 Format & Repair References";
    }
});

// =========================================
// 2. COPY TO CLIPBOARD
// =========================================
copyBtn.addEventListener("click", async function () {
    const text = formattedOutput.textContent.trim();

    if (!text || text === "Your formatted references will appear here." || text.includes("Please paste")) {
        showTemporaryMessage(copyBtn, "Nothing to copy", 1200);
        return;
    }

    try {
        await navigator.clipboard.writeText(currentFormattedText || text);
        showTemporaryMessage(copyBtn, "✓ Copied!", 1200);
    } catch (error) {
        console.error("Copy failed:", error);
        showTemporaryMessage(copyBtn, "❌ Copy failed", 1200);
    }
});

// =========================================
// 3. EXPORT FUNCTIONS
// =========================================

// Download TXT
if (downloadTxtBtn) {
    downloadTxtBtn.addEventListener("click", function () {
        if (currentFormattedText) {
            const blob = new Blob([currentFormattedText], { type: "text/plain" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, "-");
            a.download = `references_${styleSelect?.value || "apa7"}_${timestamp}.txt`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showTemporaryMessage(downloadTxtBtn, "✓ Downloaded!", 1000);
        } else {
            alert("No formatted content to download. Please format references first.");
        }
    });
}

// Download CSV (enhanced with full repair data)
if (downloadCsvBtn) {
    downloadCsvBtn.addEventListener("click", function () {
        if (currentRepairResults && currentRepairResults.length > 0) {
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
                "Source",
                "Volume",
                "Issue",
                "Pages",
                "Needs Review"
            ];
            
            const rows = currentRepairResults.map(r => [
                escapeCsv(r.original),
                escapeCsv(r.formatted),
                r.confidence,
                escapeCsv((r.issues || []).join("; ")),
                r.has_doi ? "Yes" : "No",
                escapeCsv((r.repair_log || []).join("; ")),
                escapeCsv(r.parsed?.doi || ""),
                escapeCsv(r.parsed?.year || ""),
                escapeCsv(r.parsed?.authors || ""),
                escapeCsv(r.parsed?.title || ""),
                escapeCsv(r.parsed?.source || ""),
                escapeCsv(r.parsed?.volume || ""),
                escapeCsv(r.parsed?.issue || ""),
                escapeCsv(r.parsed?.pages || ""),
                r.needs_review ? "Yes" : "No"
            ]);
            
            const csvContent = [headers, ...rows].map(row => row.join(",")).join("\n");
            const blob = new Blob(["\uFEFF" + csvContent], { type: "text/csv;charset=utf-8;" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, "-");
            a.download = `reference_report_${timestamp}.csv`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showTemporaryMessage(downloadCsvBtn, "✓ CSV Saved!", 1000);
        } else {
            alert("No repair results available. Please format references first.");
        }
    });
}

// Download JSON (complete structured data)
if (downloadJsonBtn) {
    downloadJsonBtn.addEventListener("click", function () {
        if (currentRepairResults && currentRepairResults.length > 0) {
            const exportData = {
                timestamp: new Date().toISOString(),
                style: styleSelect?.value || "apa7",
                source_type: sourceTypeSelect?.value || "journal",
                auto_enhance: autoEnhance?.checked || false,
                auto_find_doi: autoFindDoi?.checked || false,
                total_references: currentRepairResults.length,
                references: currentRepairResults,
                raw_input: currentRawText
            };
            
            const jsonContent = JSON.stringify(exportData, null, 2);
            const blob = new Blob([jsonContent], { type: "application/json" });
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            const timestamp = new Date().toISOString().slice(0, 19).replace(/:/g, "-");
            a.download = `reference_export_${timestamp}.json`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showTemporaryMessage(downloadJsonBtn, "✓ JSON Saved!", 1000);
        } else {
            alert("No repair results available. Please format references first.");
        }
    });
}

// =========================================
// 4. SIDE-BY-SIDE REPAIR UI
// =========================================

function addRepairButton() {
    // Remove existing button if present
    const existingBtn = document.getElementById("repairBtn");
    if (existingBtn) existingBtn.remove();
    
    const repairBtn = document.createElement("button");
    repairBtn.id = "repairBtn";
    repairBtn.textContent = "🔧 Open Side-by-Side Repair";
    repairBtn.style.cssText = "width: 100%; margin-top: 15px; background: #764ba2;";
    repairBtn.onclick = openRepairModal;
    
    form.parentNode.insertBefore(repairBtn, form.nextSibling);
}

function openRepairModal() {
    if (!currentRepairResults || currentRepairResults.length === 0) {
        alert("No repair results available. Please format references first.");
        return;
    }
    
    if (repairModal && modalContent) {
        let modalHtml = '<div style="max-height: 70vh; overflow-y: auto;">';
        
        currentRepairResults.forEach((result, idx) => {
            const needsAttention = result.needs_review || result.confidence < 70;
            const borderColor = needsAttention ? '#f59e0b' : '#19b36b';
            
            modalHtml += `
                <div style="border: 2px solid ${borderColor}; border-radius: 8px; padding: 15px; margin-bottom: 20px; background: #fafafa;">
                    <h3 style="margin-top: 0;">Reference ${idx + 1}</h3>
                    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 15px;">
                        <div>
                            <strong style="color: #dc2626;">📄 Original:</strong>
                            <textarea id="original_${idx}" class="original-textarea" style="width: 100%; height: 100px; margin-top: 5px; padding: 8px; font-family: monospace; font-size: 12px; border: 1px solid #ddd; border-radius: 4px;" readonly>${escapeHtml(result.original)}</textarea>
                        </div>
                        <div>
                            <strong style="color: #19b36b;">✅ Suggested Fix:</strong>
                            <textarea id="suggested_${idx}" class="suggested-textarea" style="width: 100%; height: 100px; margin-top: 5px; padding: 8px; font-family: monospace; font-size: 12px; border: 1px solid #ddd; border-radius: 4px;">${escapeHtml(result.formatted)}</textarea>
                        </div>
                    </div>
                    <div style="margin-top: 10px;">
                        <strong>Confidence: ${result.confidence}%</strong>
                        <div style="background: #e5e7eb; height: 6px; border-radius: 3px; margin-top: 5px;">
                            <div style="background: ${result.confidence >= 80 ? '#19b36b' : result.confidence >= 50 ? '#f59e0b' : '#dc2626'}; width: ${result.confidence}%; height: 6px; border-radius: 3px;"></div>
                        </div>
                    </div>
                    ${result.issues && result.issues.length > 0 ? `
                        <div style="margin-top: 10px;">
                            <strong>⚠️ Issues:</strong>
                            <ul style="margin: 5px 0 0 20px;">
                                ${result.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join('')}
                            </ul>
                        </div>
                    ` : ''}
                    ${result.repair_log && result.repair_log.length > 0 ? `
                        <div style="margin-top: 10px; font-size: 12px; color: #6b7280;">
                            <strong>🔧 Repairs applied:</strong> ${result.repair_log.join('; ')}
                        </div>
                    ` : ''}
                    ${result.alternative_dois && result.alternative_dois.length > 0 ? `
                        <div style="margin-top: 10px;">
                            <strong>🔍 Alternative DOIs found:</strong>
                            <select id="doi_select_${idx}" style="margin-left: 10px; padding: 4px 8px;">
                                <option value="">Select alternative DOI</option>
                                ${result.alternative_dois.map(doi => `<option value="${doi.doi}">${doi.doi} (${doi.final_confidence}% confidence)</option>`).join('')}
                            </select>
                            <button onclick="applyAlternativeDoi(${idx})" style="margin-left: 10px; padding: 4px 12px; background: #19b36b; color: white; border: none; border-radius: 4px; cursor: pointer;">Apply</button>
                        </div>
                    ` : ''}
                    <div style="margin-top: 10px; display: flex; gap: 10px;">
                        <button onclick="acceptSuggestion(${idx})" style="flex: 1; padding: 8px; background: #19b36b; color: white; border: none; border-radius: 4px; cursor: pointer;">✅ Accept Fix</button>
                        <button onclick="keepOriginal(${idx})" style="flex: 1; padding: 8px; background: #6b7280; color: white; border: none; border-radius: 4px; cursor: pointer;">⏸️ Keep Original</button>
                        <button onclick="manualEdit(${idx})" style="flex: 1; padding: 8px; background: #0284c7; color: white; border: none; border-radius: 4px; cursor: pointer;">✏️ Manual Edit</button>
                    </div>
                </div>
            `;
        });
        
        modalHtml += '</div>';
        modalContent.innerHTML = modalHtml;
        repairModal.style.display = "block";
    }
}

// Global functions for modal interactions
window.acceptSuggestion = function(idx) {
    const suggestedTextarea = document.getElementById(`suggested_${idx}`);
    if (suggestedTextarea) {
        const acceptedText = suggestedTextarea.value;
        updateFormattedOutput(acceptedText, idx);
        showTemporaryMessage(null, "✓ Fix accepted!", 1000);
    }
};

window.keepOriginal = function(idx) {
    const originalTextarea = document.getElementById(`original_${idx}`);
    if (originalTextarea) {
        updateFormattedOutput(originalTextarea.value, idx);
        showTemporaryMessage(null, "✓ Original kept", 1000);
    }
};

window.manualEdit = function(idx) {
    const suggestedTextarea = document.getElementById(`suggested_${idx}`);
    if (suggestedTextarea) {
        const newValue = prompt("Edit reference manually:", suggestedTextarea.value);
        if (newValue !== null) {
            suggestedTextarea.value = newValue;
            updateFormattedOutput(newValue, idx);
            showTemporaryMessage(null, "✓ Manual edit applied", 1000);
        }
    }
};

window.applyAlternativeDoi = function(idx) {
    const select = document.getElementById(`doi_select_${idx}`);
    if (select && select.value) {
        // This would trigger a re-fetch with the new DOI
        showTemporaryMessage(null, `Applying DOI: ${select.value}...`, 2000);
        // In production, you'd call an API to re-fetch metadata for this DOI
    }
};

function updateFormattedOutput(newText, referenceIdx) {
    // Update the main formatted output
    if (currentRepairResults && currentRepairResults[referenceIdx]) {
        currentRepairResults[referenceIdx].formatted = newText;
        currentRepairResults[referenceIdx].manually_edited = true;
        
        // Rebuild the full formatted text
        const allFormatted = currentRepairResults.map(r => r.formatted).join('\n\n');
        currentFormattedText = allFormatted;
        formattedOutput.innerHTML = allFormatted.replace(/\n/g, '<br>');
        formattedOutput.textContent = allFormatted;
    }
}

// Close modal
if (closeModalBtn) {
    closeModalBtn.addEventListener("click", function() {
        if (repairModal) repairModal.style.display = "none";
    });
}

// Close modal when clicking outside
window.addEventListener("click", function(e) {
    if (repairModal && e.target === repairModal) {
        repairModal.style.display = "none";
    }
});

// =========================================
// 5. HELPER FUNCTIONS
// =========================================

function escapeHtml(str) {
    if (!str) return '';
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function escapeCsv(str) {
    if (!str) return '';
    if (typeof str !== 'string') str = String(str);
    if (str.includes(',') || str.includes('"') || str.includes('\n') || str.includes('\r')) {
        return `"${str.replace(/"/g, '""')}"`;
    }
    return str;
}

function showTemporaryMessage(element, message, duration = 1200) {
    if (!element) {
        // Show in a temporary toast
        const toast = document.createElement('div');
        toast.textContent = message;
        toast.style.cssText = 'position: fixed; bottom: 20px; right: 20px; background: #19b36b; color: white; padding: 10px 20px; border-radius: 8px; z-index: 10000; animation: fadeOut 2s forwards;';
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
// 6. KEYBOARD SHORTCUTS
// =========================================
rawReference.addEventListener("keydown", function(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault();
        form.dispatchEvent(new Event("submit"));
    }
});

// =========================================
// 7. LOAD EXAMPLE ON FIRST VISIT
// =========================================
if (rawReference && !rawReference.value) {
    rawReference.placeholder = "Paste one or more references (one per line)...\n\nExample:\nSmith, J. (2020). Understanding AI. Journal of Technology, 15(2), 45-67.\nJohnson, M. (2019). Machine learning basics. https://doi.org/10.1234/example.2020.001\nBrown, R. (2021). Data science trends. Data Mining Review, 8(1), 112-128.";
}

console.log("CiteIntegrity Pro frontend loaded with OpenAlex integration");
