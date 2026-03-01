/* static/app.js - OPTIMIZED VERSION with Tab Navigation */

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
  // Payload normalizer
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
        v.reference_apa || // Add APA version
        (v.author && v.year ? `${v.author}, ${v.year}` : "") ||
        (() => { try { return JSON.stringify(v); } catch { return ""; } })()
      );
    }
    try { return String(v); } catch { return ""; }
  }

  function normalizeData(d) {
    const data = d && typeof d === "object" ? d : {};
    
    if (data.summary && typeof data.summary === "object") {
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
    };
    return data;
  }

  // ------------------------------
  // DOM Elements
  // ------------------------------
  const el = {
    // File inputs
    file: document.getElementById("file"),
    style: document.getElementById("style"),

    // Buttons
    btnCheck: document.getElementById("btnCheck"),
    btnVerify: document.getElementById("btnVerify"),

    // Options
    verifyMode: document.getElementById("verifyMode"),
    throttle: document.getElementById("throttle"),
    maxVerify: document.getElementById("maxVerify"),
    useCrossref: document.getElementById("useCrossref"),
    useOpenAlex: document.getElementById("useOpenAlex"),
    aiAssist: document.getElementById("aiAssist"),

    // Status
    status: document.getElementById("status"),
    progressBar: document.getElementById("progressBar"),

    // Results container
    resultsCard: document.getElementById("resultsCard"),
    dash: document.getElementById("dash"),
    verifyDash: document.getElementById("verifyDash"),
    summaryTable: document.getElementById("summaryTable"),
    refMsg: document.getElementById("refMsg"),

    // Tab buttons (from HTML)
    tabSummary: document.querySelector('[data-tab="summaryPane"]'),
    tabMissing: document.querySelector('[data-tab="missingPane"]'),
    tabUncited: document.querySelector('[data-tab="uncitedPane"]'),
    tabC2R: document.querySelector('[data-tab="c2rPane"]'),
    tabR2C: document.querySelector('[data-tab="r2cPane"]'),
    tabVerify: document.querySelector('[data-tab="verifyPane"]'),

    // Tab panes (content containers)
    summaryPane: document.getElementById("summaryPane"),
    missingPane: document.getElementById("missingPane"),
    uncitedPane: document.getElementById("uncitedPane"),
    c2rPane: document.getElementById("c2rPane"),
    r2cPane: document.getElementById("r2cPane"),
    verifyPane: document.getElementById("verifyPane"),

    // Table bodies
    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    c2rBody: document.getElementById("c2rBody"),
    r2cBody: document.getElementById("r2cBody"),
    verifyBody: document.getElementById("verifyBody"),

    // Export buttons
    btnExportCsvTop: document.getElementById("btnExportCsvTop"),
    btnExportWordTop: document.getElementById("btnExportWordTop"),
    
    // Additional UI elements
    fileSizeWarning: document.getElementById("fileSizeWarning"),
    engineVersion: document.getElementById("engineVersion"),
    processingTime: document.getElementById("processingTime"),
    lastUpdated: document.getElementById("lastUpdated"),
    
    // New: Job ID display (optional)
    jobId: document.getElementById("jobId"),
    verifyStatus: document.getElementById("verifyStatus"),
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
  // Tab Navigation
  // ------------------------------
  function initTabs() {
    // Define tabs and their corresponding panes
    const tabs = [
      { button: el.tabSummary, pane: el.summaryPane, name: "summary" },
      { button: el.tabMissing, pane: el.missingPane, name: "missing" },
      { button: el.tabUncited, pane: el.uncitedPane, name: "uncited" },
      { button: el.tabC2R, pane: el.c2rPane, name: "c2r" },
      { button: el.tabR2C, pane: el.r2cPane, name: "r2c" },
      { button: el.tabVerify, pane: el.verifyPane, name: "verify" }
    ];

    // Add click handlers to each tab button
    tabs.forEach(tab => {
      if (!tab.button) return;

      tab.button.addEventListener("click", (e) => {
        e.preventDefault();
        
        // Remove active class from all tab buttons
        tabs.forEach(t => {
          if (t.button) t.button.classList.remove("active");
        });

        // Hide all panes
        tabs.forEach(t => {
          if (t.pane) t.pane.classList.remove("active");
        });

        // Activate clicked tab
        tab.button.classList.add("active");
        if (tab.pane) tab.pane.classList.add("active");
        
        // Store active tab in localStorage for persistence
        try {
          localStorage.setItem("citation_active_tab", tab.name);
        } catch (e) {}
      });
    });

    // Restore last active tab or default to summary
    try {
      const savedTab = localStorage.getItem("citation_active_tab") || "summary";
      const tabToActivate = tabs.find(t => t.name === savedTab);
      if (tabToActivate && tabToActivate.button) {
        tabToActivate.button.click();
      } else if (el.tabSummary) {
        el.tabSummary.click(); // Default to summary
      }
    } catch (e) {
      // Fallback to summary tab
      if (el.tabSummary) el.tabSummary.click();
    }
  }

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
    addRow("Reference entries found", s.reference_entries_found ?? "", "References in bibliography");
    addRow("Missing in references", s.missing_in_references ?? "", "Citations not found in reference list");
    addRow("Uncited references", s.uncited_references ?? "", "References never cited in text");
    addRow("Match rate", fmtPct(s.match_rate), "Percentage of citations that matched references");

    if (LAST_JOB_ID && el.jobId) {
      addRow("Job ID", LAST_JOB_ID.substring(0, 8) + "...", "Use this ID for verification");
    }

    el.summaryTable.innerHTML = rows.join("");
    
    // Update engine version if element exists
    if (el.engineVersion) {
      el.engineVersion.textContent = data.engine_build || "v1.5.0";
    }
  }

  function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;
    
    if (!rows.length) {
      el.missingBody.innerHTML = `<tr><td colspan="3" class="muted">✅ No missing citations</td></tr>`;
      return;
    }
    
    el.missingBody.innerHTML = rows
      .map((r, index) => {
        const count = r.count_in_text ?? r.count ?? 1;
        return `<tr>
          <td class="num">${index + 1}</td>
          <td>${esc(r.citation_in_text || r.citation || "")}</td>
          <td class="num">${esc(count)}</td>
        </tr>`;
      })
      .join("");
  }

  function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    
    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td colspan="2" class="muted">✅ No uncited references</td></tr>`;
      return;
    }
    
    el.uncitedBody.innerHTML = rows.map((r, index) => {
      const text = asText(r);
      return `<tr><td class="num">${index + 1}</td><td>${esc(text)}</td></tr>`;
    }).join("");
  }

  function renderC2R(data) {
    const rows = data?.reconciliation_intext_to_reference || [];
    if (!el.c2rBody) return;
    
    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="5" class="muted">No citation data</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_C2R_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.c2rBody.innerHTML = showRows
      .map((r, index) => {
        const st = r.status || "unknown";
        const flags = r.flags ? ` (${esc(r.flags)})` : "";
        return `<tr>
          <td class="num">${index + 1}</td>
          <td class="badge ${esc(st)}">${esc(st)}</td>
          <td>${esc(r.in_text || "")}${flags}</td>
          <td>${esc(r.matched_reference || "")}</td>
          <td>${esc(r.flags || "")}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.c2rBody.innerHTML += `<tr><td colspan="5" class="muted">... and ${remaining} more (truncated)</td></tr>`;
    }
  }

  function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;
    
    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="4" class="muted">No reference data</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_R2C_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.r2cBody.innerHTML = showRows
      .map((r, index) => {
        const times = r.times_cited ?? 0;
        const citedBy = Array.isArray(r.cited_by) ? r.cited_by.join("; ") : "";
        const dupClass = r.duplicate_of_cited ? "duplicate" : "";
        const clusterInfo = r.cluster_id ? ` <span class="badge">C${r.cluster_id}</span>` : "";
        
        return `<tr class="${dupClass}">
          <td class="num">${index + 1}</td>
          <td class="num">${esc(times)}</td>
          <td>${esc(r.reference || "")}${clusterInfo}</td>
          <td>${esc(citedBy)}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.r2cBody.innerHTML += `<tr><td colspan="4" class="muted">... and ${remaining} more (truncated)</td></tr>`;
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
      el.verifyBody.innerHTML = `<tr><td colspan="10" class="muted">No online verification data yet. Click "Run Online Verification" to start.</td></tr>`;
      return;
    }
    
    const showRows = rows.slice(0, CONFIG.MAX_VERIFY_DISPLAY);
    const remaining = rows.length - showRows.length;
    
    el.verifyBody.innerHTML = showRows
      .map((r, index) => {
        const st = r.status || "";
        return `<tr>
          <td class="num">${index + 1}</td>
          <td class="badge ${esc(st)}">${esc(st)}</td>
          <td>${esc(r.source || "")}</td>
          <td class="num">${esc(r.score ?? "")}</td>
          <td>${esc(r.doi || "")}</td>
          <td class="num">${esc(r.matched_year || "")}</td>
          <td>${esc(r.author || "")}</td>
          <td>${esc(r.matched_title || "")}</td>
          <td>${esc(r.reference_apa || r.reference || "").substring(0, 100)}...</td>
          <td>${esc(r.query_used || "")}</td>
        </tr>`;
      })
      .join("");
    
    if (remaining > 0) {
      el.verifyBody.innerHTML += `<tr><td colspan="10" class="muted">... and ${remaining} more (truncated)</td></tr>`;
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
    renderC2R(CURRENT_DATA);
    renderR2C(CURRENT_DATA);
    renderVerify(CURRENT_DATA);

    // Update processing time
    if (el.processingTime && START_TIME) {
      el.processingTime.textContent = fmtTime(Date.now() - START_TIME);
    }

    // Update last updated timestamp
    if (el.lastUpdated) {
      el.lastUpdated.textContent = new Date().toLocaleTimeString();
    }

    // Update verify status
    if (el.verifyStatus && LAST_JOB_ID) {
      el.verifyStatus.textContent = `Ready for verification (Job: ${LAST_JOB_ID.substring(0, 8)}...)`;
    }

    // Enable export buttons
    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = !LAST_JOB_ID;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = !LAST_JOB_ID;
  }

  // ------------------------------
  // API Calls
  // ------------------------------
  async function runInitialCheck() {
    if (RUNNING) return;

    const f = el.file?.files?.[0];
    if (!f) {
      setStatus("📁 Choose a DOCX or PDF file first.", "warn");
      return;
    }

    if (f.size > 10 * 1024 * 1024) {
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
      fd.append("verify_mode", el.verifyMode?.value || "all");

      const throttleVal = Number(el.throttle?.value || 0.12);
      fd.append("throttle_s", String(throttleVal));

      fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");
      
      setStatus("Running initial citation check...", "muted");
      updateProgress(20, "Processing document...");

      const res = await fetch("/verify", { 
        method: "POST", 
        body: fd,
        signal: AbortSignal.timeout(300000)
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
      
      updateProgress(100, "✅ Complete!");
      setStatus(`✅ Initial check complete. Job ID: ${LAST_JOB_ID?.substring(0, 8)}... You can now run online verification.`, "success");
      setTimeout(() => setRunning(false), 500);
      
    } catch (e) {
      console.error(e);
      setStatus(`❌ Error: ${e?.message || String(e)}`, "warn");
      setRunning(false);
      updateProgress(0, "");
    }
  }

  async function runOnlineVerification() {
    if (RUNNING) return;
    
    if (!LAST_JOB_ID) {
      setStatus("❌ No job found. Run initial check first.", "warn");
      return;
    }

    setRunning(true);
    updateProgress(10, "Starting online verification...");

    try {
      const fd = new FormData();
      fd.append("job_id", LAST_JOB_ID);
      fd.append("verify_mode", el.verifyMode?.value || "all");

      const throttleVal = Number(el.throttle?.value || 0.12);
      fd.append("throttle_s", String(throttleVal));

      fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");

      setStatus("Starting online verification...", "muted");

      const res = await fetch("/verify-online", { 
        method: "POST", 
        body: fd,
        signal: AbortSignal.timeout(300000)
      });
      
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Server error (${res.status}): ${txt.slice(0, 200)}`);
      }

      const js = await res.json();
      if (!js || js.ok !== true) {
        throw new Error(js?.error || "Unexpected server response.");
      }

      updateProgress(30, "Verification started...");
      setStatus("✅ Online verification started. Progress will update automatically.", "success");
      
      // Start polling for status
      startPollingOnline();
      
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
    const MAX_POLLS = 300;

    POLL_TIMER = setInterval(async () => {
      pollCount++;
      
      if (pollCount > MAX_POLLS) {
        setStatus("⚠️ Polling timeout - tasks may still be running", "warn");
        stopPollingOnline();
        setTimeout(() => setRunning(false), 500);
        return;
      }

      try {
        const res = await fetch(
          `/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`
        );
        if (!res.ok) return;

        const js = await res.json();
        if (!js || js.ok !== true) return;

        let progress = 30;
        let statusMsg = "";

        const online = js.online || {};
        
        if (online.state === "running") {
          progress = 30 + (online.progress / online.total * 50);
          statusMsg = online.message || `Online verification: ${online.progress}/${online.total}`;
          setStatus(statusMsg, "muted");
        } else if (online.state === "done") {
          progress = 100;
          statusMsg = "Online verification completed";
          setStatus("✅ Online verification completed!", "success");
          stopPollingOnline();
          setTimeout(() => setRunning(false), 500);
        } else if (online.state === "error") {
          progress = 0;
          statusMsg = online.message || "Online verification error";
          setStatus(`❌ ${statusMsg}`, "warn");
          stopPollingOnline();
          setTimeout(() => setRunning(false), 500);
        }

        updateProgress(progress, statusMsg);

        if (js.result && Object.keys(js.result).length) {
          renderAll(js.result);
        }

        // Update verify status
        if (el.verifyStatus) {
          el.verifyStatus.textContent = `Status: ${online.state} (${online.progress || 0}/${online.total || 0})`;
        }

        if (online.state === "done" || online.state === "error") {
          stopPollingOnline();
        }
      } catch (e) {
        console.debug("Polling error:", e);
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
    if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
    if (el.btnVerify) el.btnVerify.addEventListener("click", runOnlineVerification);
    if (el.btnExportCsvTop) el.btnExportCsvTop.addEventListener("click", exportCsv);
    if (el.btnExportWordTop) el.btnExportWordTop.addEventListener("click", exportWord);
    
    if (el.file) {
      el.file.addEventListener("change", checkFileSize);
    }

    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey && e.key === "Enter" && !RUNNING) {
        e.preventDefault();
        runInitialCheck();
      }
      if (e.ctrlKey && e.shiftKey && e.key === "Enter" && !RUNNING) {
        e.preventDefault();
        runOnlineVerification();
      }
    });
  }

  // ------------------------------
  // Initialize
  // ------------------------------
  function init() {
    initTabs(); // Initialize tab navigation first
    initEventListeners();
    setStatus("✅ Ready. Select a file and click Check.", "muted");
    checkFileSize();
    
    if (el.throttle && !el.throttle.value) el.throttle.value = "0.12";
    if (el.maxVerify && !el.maxVerify.value) el.maxVerify.value = "0";
    
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('job')) {
      LAST_JOB_ID = urlParams.get('job');
      if (LAST_JOB_ID) {
        setStatus(`🔄 Resuming previous job: ${LAST_JOB_ID.substring(0, 8)}...`, "muted");
        if (el.verifyStatus) {
          el.verifyStatus.textContent = `Job loaded: ${LAST_JOB_ID.substring(0, 8)}...`;
        }
        // Check if verification is already running
        startPollingOnline();
      }
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.__citationApp = {
    getJobId: () => LAST_JOB_ID,
    getData: () => CURRENT_DATA,
    refresh: () => renderAll(CURRENT_DATA),
    runCheck: runInitialCheck,
    runVerify: runOnlineVerification,
    setTab: (tabName) => {
      const tabMap = {
        summary: el.tabSummary,
        missing: el.tabMissing,
        uncited: el.tabUncited,
        c2r: el.tabC2R,
        r2c: el.tabR2C,
        verify: el.tabVerify
      };
      const tabToClick = tabMap[tabName];
      if (tabToClick) tabToClick.click();
    }
  };
})();
