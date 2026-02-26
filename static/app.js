/* static/app.js - OPTIMIZED VERSION for engine v1.4.0 */

(() => {
  "use strict";

  // ------------------------------
  // Configuration
  // ------------------------------
  const CONFIG = {
    POLL_INTERVAL: 1200,        // ms between status polls
    MAX_C2R_DISPLAY: 500,        // max rows to show in c2r table
    MAX_R2C_DISPLAY: 500,        // max rows to show in r2c table
    MAX_VERIFY_DISPLAY: 500,     // max rows to show in verify table
    CHUNK_WARNING_SIZE: 2_000_000 // show warning for files > 2MB
  };

  // ------------------------------
  // Payload normalizer (supports v1.4.0 features)
  // ------------------------------
  function toNum(x, d = 0) {
    const n = Number(x);
    return Number.isFinite(n) ? n : d;
  }

  function asText(v) {
    if (v == null) return "";
    if (typeof v === "string") return v;
    if (typeof v === "number") return String(v);
    if (typeof v === "object") {
      return (
        v.reference || v.raw || v.text || v.label || v.display || v.citation || v.citation_in_text ||
        (v.author && v.year ? `${v.author}, ${v.year}` : "") ||
        (() => { try { return JSON.stringify(v); } catch { return ""; } })()
      );
    }
    try { return String(v); } catch { return ""; }
  }

  function normalizeData(d) {
    const data = d && typeof d === "object" ? d : {};
    
    // Handle nested summary (v1.4.0 style)
    if (data.summary && typeof data.summary === "object") {
      // Add cluster info if present
      if (data.uncited_references_clusters) {
        data.summary.uncited_clusters = data.uncited_references_clusters.length;
      }
      return data;
    }

    const missingArr = Array.isArray(data.missing_in_references) ? data.missing_in_references : [];
    const uncitedArr = Array.isArray(data.uncited_references) ? data.uncited_references : [];

    data.summary = {
      in_text_citations_found: toNum(data.in_text_citations_found ?? data.intext_citations_found ?? data.intext_count ?? 0),
      reference_entries_found: toNum(data.reference_entries_found ?? data.ref_count ?? 0),
      missing_in_references: toNum(data.missing_in_references_count ?? missingArr.length),
      uncited_references: toNum(data.uncited_references_count ?? uncitedArr.length),
      match_rate: toNum(data.match_rate ?? 0),
      uncited_clusters: toNum(data.uncited_clusters ?? 0),
    };
    return data;
  }

  // ------------------------------
  // DOM Elements
  // ------------------------------
  const el = {
    file: document.getElementById("file"),
    style: document.getElementById("style"),

    btnCheck: document.getElementById("btnCheck"),
    btnVerify: document.getElementById("btnVerify"),

    verifyMode: document.getElementById("verifyMode"),
    throttle: document.getElementById("throttle"),
    maxVerify: document.getElementById("maxVerify"),
    useCrossref: document.getElementById("useCrossref"),
    useOpenAlex: document.getElementById("useOpenAlex"),
    aiAssist: document.getElementById("aiAssist"),

    status: document.getElementById("status"),
    progressBar: document.getElementById("progressBar"),

    resultsCard: document.getElementById("resultsCard"),
    dash: document.getElementById("dash"),
    verifyDash: document.getElementById("verifyDash"),
    summaryTable: document.getElementById("summaryTable"),
    refMsg: document.getElementById("refMsg"),

    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    uncitedClustersBody: document.getElementById("uncitedClustersBody"),

    c2rBody: document.getElementById("c2rBody"),
    r2cBody: document.getElementById("r2cBody"),

    verifyBody: document.getElementById("verifyBody"),

    btnExportCsvTop: document.getElementById("btnExportCsvTop"),
    btnExportWordTop: document.getElementById("btnExportWordTop"),
    
    // New elements for enhanced UI
    fileSizeWarning: document.getElementById("fileSizeWarning"),
    engineVersion: document.getElementById("engineVersion"),
    processingTime: document.getElementById("processingTime"),
    clusterInfo: document.getElementById("clusterInfo"),
  };

  // ------------------------------
  // State
  // ------------------------------
  let LAST_JOB_ID = null;
  let POLL_TIMER = null;
  let RUNNING = false;
  let START_TIME = null;
  let CURRENT_DATA = null;

  // ------------------------------
  // UI Helpers
  // ------------------------------
  function setRunning(on) {
    RUNNING = !!on;
    if (el.btnCheck) el.btnCheck.disabled = RUNNING;
    if (el.btnVerify) el.btnVerify.disabled = RUNNING;
    if (el.file) el.file.disabled = RUNNING;
    
    if (RUNNING) {
      START_TIME = Date.now();
      if (el.progressBar) {
        el.progressBar.style.width = "0%";
        el.progressBar.classList.add("active");
      }
    } else {
      if (el.progressBar) {
        el.progressBar.classList.remove("active");
      }
    }
  }

  function updateProgress(percent, message) {
    if (el.progressBar) {
      el.progressBar.style.width = `${Math.min(100, Math.max(0, percent))}%`;
    }
    if (message) setStatus(message, "muted");
  }

  function esc(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg || "";
    
    // Update ARIA for accessibility
    el.status.setAttribute("aria-live", "polite");
  }

  function fmtPct(x) {
    const n = Number(x);
    if (!Number.isFinite(n)) return "";
    return `${n.toFixed(1)}%`;
  }

  function fmtTime(ms) {
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }

  function checkFileSize() {
    const f = el.file?.files?.[0];
    if (!f || !el.fileSizeWarning) return;
    
    if (f.size > CONFIG.CHUNK_WARNING_SIZE) {
      el.fileSizeWarning.style.display = "block";
      el.fileSizeWarning.textContent = `⚠️ Large file (${(f.size / 1e6).toFixed(1)}MB). Processing may take longer.`;
    } else {
      el.fileSizeWarning.style.display = "none";
    }
  }

  // ------------------------------
  // Table Renderers
  // ------------------------------
  function clearTables() {
    if (el.missingBody) el.missingBody.innerHTML = "";
    if (el.uncitedBody) el.uncitedBody.innerHTML = "";
    if (el.uncitedClustersBody) el.uncitedClustersBody.innerHTML = "";
    if (el.c2rBody) el.c2rBody.innerHTML = "";
    if (el.r2cBody) el.r2cBody.innerHTML = "";
    if (el.verifyBody) el.verifyBody.innerHTML = "";
  }

  function renderSummaryTable(data) {
    const s = data?.summary || {};
    if (!el.summaryTable) return;

    const rows = [];
    function addRow(k, v, tooltip = "") {
      const tooltipAttr = tooltip ? ` title="${esc(tooltip)}"` : "";
      rows.push(`<tr${tooltipAttr}><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`);
    }

    addRow("In-text citations found", s.in_text_citations_found ?? "", "Unique citations in document text");
    addRow("Reference entries found", s.reference_entries_found ?? "", "Unique references in bibliography");
    addRow("Missing in references", s.missing_in_references ?? "", "Citations not found in reference list");
    addRow("Uncited references", s.uncited_references ?? "", "References never cited in text");
    
    if (s.uncited_clusters) {
      addRow("Uncited clusters", s.uncited_clusters ?? "", "Groups of duplicate uncited references");
    }
    
    addRow("Match rate", fmtPct(s.match_rate), "Percentage of citations that matched references");

    if (data?.engine_build) {
      addRow("Engine", data.engine_build, "Engine version");
    }

    el.summaryTable.innerHTML = rows.join("");
    
    // Update engine version display
    if (el.engineVersion && data?.engine_build) {
      el.engineVersion.textContent = data.engine_build;
    }
  }

  function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;
    
    if (!rows.length) {
      el.missingBody.innerHTML = `<tr><td colspan="2" class="muted">✅ No missing citations</td></tr>`;
      return;
    }
    
    el.missingBody.innerHTML = rows
      .map(
        (r) => {
          const count = r.count_in_text ?? r.count ?? 1;
          return `<tr>
            <td>${esc(r.citation_in_text || r.citation || "")}</td>
            <td class="num">${esc(count)}</td>
          </tr>`;
        }
      )
      .join("");
  }

  function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    
    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td class="muted">✅ No uncited references</td></tr>`;
      return;
    }
    
    el.uncitedBody.innerHTML = rows.map((r) => {
      const text = asText(r);
      return `<tr><td>${esc(text)}</td></tr>`;
    }).join("");
  }

  function renderUncitedClusters(data) {
    // This requires backend to provide cluster data
    const clusters = data?.uncited_references_clusters || [];
    if (!el.uncitedClustersBody || !clusters.length) return;
    
    el.uncitedClustersBody.innerHTML = clusters.map((cluster, idx) => {
      const members = cluster.members || [];
      return `
        <tr>
          <td class="num">${idx + 1}</td>
          <td>${esc(cluster.canonical || "")}</td>
          <td class="num">${members.length}</td>
          <td><button class="btn-small" onclick="toggleCluster(${idx})">Show</button></td>
        </tr>
      `;
    }).join("");
  }

  function renderC2R(data) {
    const rows = data?.reconciliation_intext_to_reference || [];
    if (!el.c2rBody) return;
    
    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="3" class="muted">No citation data</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_C2R_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.c2rBody.innerHTML = showRows
      .map((r) => {
        const st = r.status || "unknown";
        const flags = r.flags ? ` <span class="flag">(${esc(r.flags)})</span>` : "";
        return `<tr>
          <td class="badge ${esc(st)}">${esc(st)}</td>
          <td>${esc(r.in_text || "")}${flags}</td>
          <td>${esc(r.matched_reference || "")}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.c2rBody.innerHTML += `<tr><td colspan="3" class="muted">... and ${remaining} more (truncated)</td></tr>`;
    }
  }

  function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;
    
    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="3" class="muted">No reference data</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_R2C_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.r2cBody.innerHTML = showRows
      .map((r) => {
        const times = r.times_cited ?? 0;
        const citedBy = Array.isArray(r.cited_by) ? r.cited_by.join("; ") : "";
        const dupClass = r.duplicate_of_cited ? "duplicate" : "";
        const clusterInfo = r.cluster_id ? ` <span class="badge">C${r.cluster_id}</span>` : "";
        
        return `<tr class="${dupClass}">
          <td class="num">${esc(times)}</td>
          <td>${esc(r.reference || "")}${clusterInfo}</td>
          <td>${esc(citedBy)}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.r2cBody.innerHTML += `<tr><td colspan="3" class="muted">... and ${remaining} more (truncated)</td></tr>`;
    }
    
    // Update cluster info display
    if (el.clusterInfo) {
      const clustered = rows.filter(r => r.cluster_id).length;
      const duplicates = rows.filter(r => r.duplicate_of_cited).length;
      el.clusterInfo.textContent = `Clustered: ${clustered} | Duplicates merged: ${duplicates}`;
    }
  }

  function renderVerify(data) {
    const ov = data?.online_verification || {};
    const rows = ov?.rows || [];
    const sum = ov?.summary || {};

    if (el.verifyDash) {
      const total = sum.total ?? rows.length ?? 0;
      el.verifyDash.innerHTML = `
        <div class="kpi"><div class="k">Verified</div><div class="v">${esc(sum.verified ?? 0)}</div></div>
        <div class="kpi"><div class="k">Likely</div><div class="v">${esc(sum.likely ?? 0)}</div></div>
        <div class="kpi"><div class="k">Needs review</div><div class="v">${esc(sum.needs_review ?? 0)}</div></div>
        <div class="kpi"><div class="k">Not found</div><div class="v">${esc(sum.not_found ?? 0)}</div></div>
        <div class="kpi"><div class="k">Offline</div><div class="v">${esc(sum.offline ?? 0)}</div></div>
        <div class="kpi"><div class="k">Total</div><div class="v">${esc(total)}</div></div>
      `;
    }

    if (!el.verifyBody) return;
    if (!rows.length) {
      el.verifyBody.innerHTML = `<tr><td colspan="5" class="muted">No online verification data yet.</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_VERIFY_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.verifyBody.innerHTML = showRows
      .map((r) => {
        const st = r.status || "";
        return `<tr>
          <td class="badge ${esc(st)}">${esc(st)}</td>
          <td>${esc(r.reference_full || "")}</td>
          <td>${esc(r.found_title || "")}</td>
          <td>${esc(r.found_doi || "")}</td>
          <td class="num">${esc(r.score ?? "")}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.verifyBody.innerHTML += `<tr><td colspan="5" class="muted">... and ${remaining} more (truncated)</td></tr>`;
    }
  }

  function renderAll(data) {
    if (!data) return;
    CURRENT_DATA = normalizeData(data);
    clearTables();

    if (el.resultsCard) el.resultsCard.style.display = "block";
    if (el.refMsg) el.refMsg.textContent = data.reference_detection_message || "";

    renderSummaryTable(CURRENT_DATA);
    renderMissing(CURRENT_DATA);
    renderUncited(CURRENT_DATA);
    renderUncitedClusters(CURRENT_DATA);
    renderC2R(CURRENT_DATA);
    renderR2C(CURRENT_DATA);
    renderVerify(CURRENT_DATA);

    // Update processing time
    if (el.processingTime && START_TIME) {
      el.processingTime.textContent = fmtTime(Date.now() - START_TIME);
    }

    // Enable export buttons
    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = !LAST_JOB_ID;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = !LAST_JOB_ID;
  }

  // ------------------------------
  // API Calls
  // ------------------------------
  async function postVerify(verifyOnline) {
    if (RUNNING) return;

    const f = el.file?.files?.[0];
    if (!f) {
      setStatus("📁 Choose a DOCX or PDF file first.", "warn");
      return;
    }

    // Check file size
    if (f.size > 10 * 1024 * 1024) { // 10MB
      if (!confirm(`File size is ${(f.size / 1e6).toFixed(1)}MB. Large files may take longer. Continue?`)) {
        return;
      }
    }

    setRunning(true);
    updateProgress(5, "Uploading file...");

    try {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("style", el.style?.value || "apa");
      fd.append("verify_online", verifyOnline ? "true" : "false");
      fd.append("verify_mode", el.verifyMode?.value || "all");

      const throttleVal = Number(el.throttle?.value || 0.12);
      fd.append("throttle_s", String(throttleVal));
      fd.append("max_verify", String(Number(el.maxVerify?.value || 0)));

      fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");
      fd.append("ai_assist", el.aiAssist?.checked ? "true" : "false");

      const aiOn = !!el.aiAssist?.checked;
      const modeMsg = verifyOnline ? "online verification" : "";
      const aiMsg = aiOn ? "AI assist" : "";
      const statusMsg = [modeMsg, aiMsg].filter(Boolean).join(" and ");
      
      setStatus(
        statusMsg ? `Running check with ${statusMsg}...` : "Running check...",
        "muted"
      );

      updateProgress(20, "Processing document...");

      const res = await fetch("/verify", { 
        method: "POST", 
        body: fd,
        signal: AbortSignal.timeout(300000) // 5 minute timeout
      });
      
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Server error (${res.status}): ${txt.slice(0, 200)}`);
      }

      updateProgress(60, "Parsing results...");

      const js = await res.json();
      if (!js || js.ok !== true) {
        throw new Error(js?.data?.error || "Unexpected server response.");
      }

      LAST_JOB_ID = js.job_id || null;
      updateProgress(80, "Rendering results...");
      renderAll(js.data);

      if (verifyOnline || aiOn) {
        setStatus("✅ Initial check complete. Background tasks running...", "success");
        startPollingOnline();
      } else {
        updateProgress(100, "✅ Complete!");
        setStatus("✅ Done.", "success");
        setTimeout(() => setRunning(false), 500);
      }
    } catch (e) {
      console.error(e);
      setStatus(`❌ Error: ${e?.message || String(e)}`, "warn");
      setRunning(false);
      updateProgress(0, "");
    }
  }

  // ------------------------------
  // Polling
  // ------------------------------
  function stopPollingOnline() {
    if (POLL_TIMER) {
      clearInterval(POLL_TIMER);
      POLL_TIMER = null;
    }
  }

  function startPollingOnline() {
    stopPollingOnline();
    if (!LAST_JOB_ID) return;

    let pollCount = 0;
    const MAX_POLLS = 300; // 5 minutes at 1s interval

    POLL_TIMER = setInterval(async () => {
      pollCount++;
      
      if (pollCount > MAX_POLLS) {
        setStatus("⚠️ Polling timeout - background tasks may still be running", "warn");
        stopPollingOnline();
        return;
      }

      try {
        const res = await fetch(
          `/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`
        );
        if (!res.ok) return;

        const js = await res.json();
        if (!js || js.ok !== true) return;

        // Update progress based on state
        let progress = 80;
        let statusMsg = "";

        // AI status
        if (js.ai && js.ai.state) {
          if (js.ai.state === "running") {
            progress = 85 + (js.ai.progress || 0) * 10;
            statusMsg = js.ai.message || "AI assist running...";
          } else if (js.ai.state === "done") {
            progress = 95;
            statusMsg = "AI assist complete";
          }
        }

        // Online verification status
        const online = js.online || {};
        if (online.state === "running") {
          progress = 85 + (online.progress || 0) * 10;
          statusMsg = online.message || "Online verification running...";
        } else if (online.state === "done") {
          progress = 98;
          statusMsg = "Online verification complete";
        }

        updateProgress(progress, statusMsg);

        // Update results if available
        if (js.result && Object.keys(js.result).length) {
          renderAll(js.result);
        }

        // Determine if done
        const aiState = (js.ai || {}).state || "idle";
        const onlineState = (online || {}).state || "idle";
        const aiFinished = ["done", "error", "skipped", "idle"].includes(aiState);
        const onlineFinished = ["done", "error", "idle"].includes(onlineState);

        if (aiFinished && onlineFinished) {
          updateProgress(100, "✅ All background tasks complete!");
          setStatus("✅ Complete.", "success");
          stopPollingOnline();
          setTimeout(() => setRunning(false), 500);
        }
      } catch (e) {
        // Silent fail for polling errors
        console.debug("Polling error (non-critical):", e);
      }
    }, CONFIG.POLL_INTERVAL);
  }

  // ------------------------------
  // Export functions
  // ------------------------------
  async function exportCsv() {
    if (!LAST_JOB_ID) return;
    
    try {
      setStatus("Exporting CSV...", "muted");
      const res = await fetch("/export/csv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: LAST_JOB_ID }),
      });
      
      if (!res.ok) throw new Error("Export failed");
      
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `citation_check_${new Date().toISOString().slice(0,10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("✅ CSV exported", "success");
    } catch (e) {
      setStatus(`❌ Export failed: ${e.message}`, "warn");
    }
  }

  async function exportWord() {
    if (!LAST_JOB_ID) return;
    
    try {
      setStatus("Exporting Word document...", "muted");
      const res = await fetch("/export/word", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: LAST_JOB_ID }),
      });
      
      if (!res.ok) throw new Error("Export failed");
      
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `citation_check_${new Date().toISOString().slice(0,10)}.docx`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setStatus("✅ Word document exported", "success");
    } catch (e) {
      setStatus(`❌ Export failed: ${e.message}`, "warn");
    }
  }

  // ------------------------------
  // Event Listeners
  // ------------------------------
  function initEventListeners() {
    if (el.btnCheck) el.btnCheck.addEventListener("click", () => postVerify(false));
    if (el.btnVerify) el.btnVerify.addEventListener("click", () => postVerify(true));
    if (el.btnExportCsvTop) el.btnExportCsvTop.addEventListener("click", exportCsv);
    if (el.btnExportWordTop) el.btnExportWordTop.addEventListener("click", exportWord);
    
    if (el.file) {
      el.file.addEventListener("change", checkFileSize);
    }

    // Keyboard shortcuts
    document.addEventListener("keydown", (e) => {
      // Ctrl+Enter to run check
      if (e.ctrlKey && e.key === "Enter" && !RUNNING) {
        e.preventDefault();
        postVerify(false);
      }
      // Ctrl+Shift+Enter to run with verification
      if (e.ctrlKey && e.shiftKey && e.key === "Enter" && !RUNNING) {
        e.preventDefault();
        postVerify(true);
      }
    });
  }

  // ------------------------------
  // Initialize
  // ------------------------------
  function init() {
    initEventListeners();
    setStatus("✅ Ready. Select a file and click Check.", "muted");
    checkFileSize();
    
    // Set default values if not present
    if (el.throttle && !el.throttle.value) el.throttle.value = "0.12";
    if (el.maxVerify && !el.maxVerify.value) el.maxVerify.value = "0";
    
    // Check for URL parameters
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('job')) {
      LAST_JOB_ID = urlParams.get('job');
      if (LAST_JOB_ID) {
        setStatus("🔄 Resuming previous job...", "muted");
        startPollingOnline();
      }
    }
  }

  // Start when DOM ready
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  // Expose some functions globally for debugging
  window.__citationApp = {
    getJobId: () => LAST_JOB_ID,
    getData: () => CURRENT_DATA,
    refresh: () => renderAll(CURRENT_DATA),
  };
})();
