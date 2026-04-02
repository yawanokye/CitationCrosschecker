// Auto-Fix Button Handler
async function applyAutoFix(jobId) {
    const formData = new FormData();
    formData.append('job_id', jobId);
    
    const response = await fetch('/apply-autofix', {
        method: 'POST',
        body: formData
    });
    
    const result = await response.json();
    if (result.success) {
        showFixLog(jobId);
        showNotification(`Applied ${result.fixes_applied_count} fixes`, 'success');
    }
}

// Show Fix Log
async function showFixLog(jobId) {
    const response = await fetch(`/fix-log/${jobId}`);
    const data = await response.json();
    
    // Display fixes in a modal or side panel
    displayFixLog(data);
}

// Download Fixed Document
async function downloadFixedDocument(jobId, format = 'txt') {
    window.open(`/export-fixed-document/${jobId}?format=${format}`, '_blank');
}

// Get Auto-Fix Suggestions
async function getAutoFixSuggestions(jobId) {
    const response = await fetch(`/autofix-suggestions/${jobId}`);
    const data = await response.json();
    
    if (data.available) {
        displayFixSuggestions(data);
    }
}
