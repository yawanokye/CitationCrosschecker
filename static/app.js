/* static/app.js
   Citation Crosschecker frontend logic

   What this fixes:
   - Export files were empty because export calls did NOT include job_id
   - Adds safe defaults for Online Verification to reduce 502 timeouts
   - Robust DOM selectors so the JS won’t crash if an element id differs
*/

(() => {
  "use strict";

  // -----------------------------
  // State
  // -----------------------------
  let LAST_JOB_ID = null;      // key fix for exports
  let LAST_RESULT = null;      // optional fallback (client-side export payload)
  let IS_RUNNING = false;

  // For Online Verification safety
  const DEFAULT_MAX_VERIFY_ONLINE = 120; // prevents router 502 on big lists
  const DEFAULT_THROTTLE_ONLINE = 0.20;  // safer for rate limits

  // -----------------------------
  // DOM helpers
  // -----------------------------
  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));

  function pickFirst(selectors) {
    for (const s of selectors) {
      const el = qs(s);
      if (el) return el;
    }
    return null;
  }

  function getEl(ref) {
    if (!ref) return null;
    if (typeof ref === "string") return qs(ref);
    return ref;
  }

  function setText(el, txt) {
    el = getEl(el);
    if (!el) return;
    el.textContent = (txt ?? "").toString();
  }

  function setHTML(el, html) {
    el = getEl(el);
    if (!el) return;
    el.innerHTML = html ?? "";
  }

  function setDisabled(el, disabled) {
    el = getEl(el);
    if (!el) return;
    el.disabled = !!disabled;
  }

  function show(el) {
    el = getEl(el);
    if (!el) return;
    el.style.display = "";
  }

  function hide(el) {
    el = getEl(el);
    if (!el) return;
    el.style.display = "none";
  }

  // -----------------------------
  // UI element bindings (robust)
  // -----------------------------
  const els = {
    fileInput: pickFirst(["#file", "#fileInput", "input[type='file']"]),
    styleSelect: pickFirst(["#style", "#citation_style", "#styleSelect", "select[name='style']"]),
    verifyMode: pickFirst(["#verify_mode", "#verifyMode", "select[name='verify_mode']"]),
    throttle: pickFirst(["#throttle_s", "#throttle", "#throttleSeconds", "input[name='throttle_s']"]),
    maxVerify: pickFirst(["#max_verify", "#maxVerify", "#maxVerifyPerBatch", "input[name='max_verify']"]),
    useCrossref: pickFirst(["#use_crossref", "#useCrossref", "input[name='use_crossref']"]),
    useOpenalex: pickFirst(["#use_openalex", "#useOpenalex", "input[name='use_openalex']"]),

    runBtn: pickFirst(["#runBtn", "#runCheckBtn", "button[data-action='run-check']"]),
    runOnlineBtn: pickFirst(["#runOnlineBtn", "#runOnlineVerificationBtn", "button[data-action='run-online']"]),

    exportCsvBtn: pickFirst(["#exportCsvBtn", "button[data-action='export-csv']"]),
    exportWordBtn: pickFirst(["#exportWordBtn", "button[data-action='export-word']"]),
    exportPdfBtn: pickFirst(["#exportPdfBtn", "button[data-action='export-pdf']"]),

    banner: pickFirst(["#banner", "#alertBox", "#messageBox"]),
    bannerText: pickFirst(["#bannerText", "#alertText", "#messageText"]),

    // Metrics (optional, update if present)
    metricInText: pickFirst(["#metricInText", "[data-metric='intext']"]),
    metricRefs: pickFirst(["#metricRefs", "[data-metric='refs']"]),
    metricMissing: pickFirst(["#metricMissing", "[data-metric='missing']"]),
    metricUncited: pickFirst(["#metricUncited", "[data-metric='uncited']"]),
    metricMatchRate: pickFirst(["#metricMatchRate", "[data-metric='matchrate']"]),

    // Results containers (optional)
    summaryBox: pickFirst(["#summaryBox", "#summary", "[data-tab='summary']"]),
    missingTable: pickFirst(["#missingTable", "#missing", "[data-tab='missing']"]),
    uncitedTable: pickFirst(["#uncitedTable", "#uncited", "[data-tab='uncited']"]),
    c2rTable: pickFirst(["#c2rTable", "#intextToRef", "[data-tab='c2r']"]),
    r2cTable: pickFirst(["#r2cTable", "#refToIntext", "[data-tab='r2c']"]),
    onlineTable: pickFirst(["#onlineTable", "#onlineVerification", "[data-tab='online']"]),
    rawJsonPre: pickFirst(["#rawJson", "#rawJSON", "pre[data-role='raw-json']"])
  };

  // -----------------------------
  // Banner messaging
  // -----------------------------
  function banner(type, msg) {
    // type: "info" | "success" | "warn" | "error"
    const box = els.banner;
    const textEl = els.bannerText;

    if (!box && !textEl) {
      // last resort
      if (type === "error") console.error(msg);
      else console.log(msg);
      return;
    }

    if (box) {
      box.classList.remove("is-info", "is-success", "is-warn", "is-error");
      box.classList.add(`is-${type}`);
      show(box);
    }
    if (textEl) setText(textEl, msg);
  }

  function clearBanner() {
    if (els.banner) hide(els.banner);
    if (els.bannerText) setText(els.bannerText, "");
  }

  // -----------------------------
  // Network helpers
  // -----------------------------
  async function fetchJson(url, opts = {}) {
    const res = await fetch(url, opts);
    const text = await res.text();
    let js = null;
    try { js = text ? JSON.parse(text) : null; } catch { js = null; }
    return { res, text, js };
  }

  function parseContentDispositionFilename(cd) {
    if (!cd) return "";
    // attachment; filename="abc.docx"
    const m = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
    return m ? decodeURIComponent(m[1].replace(/\"/g, "").trim()) : "";
  }

  async function downloadFromResponse(res, fallbackName) {
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const fn = parseContentDispositionFilename(cd) || fallbackName || "download";
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fn;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  }

  // -----------------------------
  // Build FormData for /verify
  // -----------------------------
  function buildVerifyFormData({ verifyOnline }) {
    const fd = new FormData();

    const f = els.fileInput?.files?.[0];
    if (!f) throw new Error("Please choose a DOCX or PDF file.");

    fd.append("file", f);

    const styleVal = (els.styleSelect?.value || "apa").trim();
    fd.append("style", styleVal);

    fd.append("verify_online", verifyOnline ? "true" : "false");

    const verifyModeVal = (els.verifyMode?.value || "all").trim();
    fd.append("verify_mode", verifyModeVal);

    let throttleVal = (els.throttle?.value ?? "").toString().trim();
    let maxVerifyVal = (els.maxVerify?.value ?? "").toString().trim();

    // Safety defaults for Online Verification to avoid router 502
    if (verifyOnline) {
      if (!throttleVal) throttleVal = DEFAULT_THROTTLE_ONLINE.toString();
      if (!maxVerifyVal || maxVerifyVal === "0") {
        // 0 means "verify all" but that is what causes 502 on large lists
        maxVerifyVal = DEFAULT_MAX_VERIFY_ONLINE.toString();
      }
    }

    fd.append("throttle_s", throttleVal || "0.12");
    fd.append("max_verify", maxVerifyVal || "0");

    const crossref = !!els.useCrossref?.checked;
    const openalex = !!els.useOpenalex?.checked;

    fd.append("use_crossref", crossref ? "true" : "false");
    fd.append("use_openalex", openalex ? "true" : "false");

    return fd;
  }

  // -----------------------------
  // Rendering
  // -----------------------------
  function toPct(x) {
    const n = Number(x);
    if (!Number.isFinite(n)) return "";
    return `${n.toFixed(1)}%`;
  }

  function renderMetrics(data) {
    const s = data?.summary || {};
    setText(els.metricInText, s.in_text_citations_found ?? "");
    setText(els.metricRefs, s.reference_entries_found ?? "");
    setText(els.metricMissing, s.missing_in_references ?? "");
    setText(els.metricUncited, s.uncited_references ?? "");
    // If your backend already computed match rate, use it. Else compute rough.
    if (s.reference_entries_found && s.missing_in_references !== undefined) {
      const total = Number(s.in_text_citations_found || 0);
      const missing = Number(s.missing_in_references || 0);
      const ok = total > 0 ? ((total - missing) / total) * 100 : 0;
      setText(els.metricMatchRate, toPct(ok));
    }
  }

  function renderRawJson(data) {
    if (!els.rawJsonPre) return;
    setText(els.rawJsonPre, JSON.stringify(data, null, 2));
  }

  function renderSimpleListTable(containerEl, rows, columns) {
    containerEl = getEl(containerEl);
    if (!containerEl) return;

    const safeRows = Array.isArray(rows) ? rows : [];
    const cols = Array.isArray(columns) && columns.length ? columns : Object.keys(safeRows[0] || {});

    if (!cols.length) {
      setHTML(containerEl, "<div class='muted'>No data</div>");
      return;
    }

    const thead = `<thead><tr>${cols.map(c => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
    const tbody = `<tbody>${
      safeRows.map(r => `<tr>${cols.map(c => `<td>${escapeHtml(String(r?.[c] ?? ""))}</td>`).join("")}</tr>`).join("")
    }</tbody>`;

    setHTML(containerEl, `<div class="table-wrap"><table class="table">${thead}${tbody}</table></div>`);
  }

  function escapeHtml(s) {
    return (s ?? "").toString()
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderResults(data) {
    LAST_RESULT = data || null;

    renderMetrics(data);
    renderRawJson(data);

    // Summary box if present
    if (els.summaryBox) {
      const msg = data?.reference_detection_message ? escapeHtml(data.reference_detection_message) : "";
      const style = escapeHtml(data?.style || "");
      const fn = escapeHtml(data?.filename || "");
      const refsDetected = data?.references_detected ?? "";
      setHTML(
        els.summaryBox,
        `<div class="kv">
           <div><b>File</b> ${fn}</div>
           <div><b>Style</b> ${style}</div>
           <div><b>References detected</b> ${refsDetected}</div>
           <div><b>Notes</b> ${msg}</div>
         </div>`
      );
    }

    // Missing
    const missing = Array.isArray(data?.missing_in_references) ? data.missing_in_references : [];
    renderSimpleListTable(els.missingTable, missing, ["citation_in_text", "count_in_text"]);

    // Uncited
    const uncited = Array.isArray(data?.uncited_references)
      ? data.uncited_references.map(r => ({ reference: r }))
      : [];
    renderSimpleListTable(els.uncitedTable, uncited, ["reference"]);

    // Intext -> Reference
    const c2r = Array.isArray(data?.reconciliation_intext_to_reference) ? data.reconciliation_intext_to_reference : [];
    renderSimpleListTable(els.c2rTable, c2r.slice(0, 2000), ["in_text", "status", "matched_reference", "flags"]);

    // Reference -> Intext
    const r2c = Array.isArray(data?.reconciliation_reference_to_intext) ? data.reconciliation_reference_to_intext : [];
    // Show fewer because cited_by arrays can be huge
    const r2cShort = r2c.slice(0, 1000).map(r => ({
      reference: r.reference ?? "",
      times_cited: r.times_cited ?? 0,
      cited_by: Array.isArray(r.cited_by) ? r.cited_by.slice(0, 6).join(" | ") : ""
    }));
    renderSimpleListTable(els.r2cTable, r2cShort, ["reference", "times_cited", "cited_by"]);

    // Online verification
    const ovRows = Array.isArray(data?.online_verification?.rows) ? data.online_verification.rows : [];
    const ovRowsShort = ovRows.slice(0, 1500).map(r => ({
      status: r.status ?? "",
      source: r.source ?? "",
      score: r.score ?? 0,
      doi: r.doi ?? "",
      reference: r.reference ?? "",
      matched_title: r.matched_title ?? ""
    }));
    renderSimpleListTable(els.onlineTable, ovRowsShort, ["status", "source", "score", "doi", "reference", "matched_title"]);
  }

  // -----------------------------
  // Actions
  // -----------------------------
  function setRunning(on) {
    IS_RUNNING = !!on;
    setDisabled(els.runBtn, on);
    setDisabled(els.runOnlineBtn, on);
    setDisabled(els.exportCsvBtn, on);
    setDisabled(els.exportWordBtn, on);
    setDisabled(els.exportPdfBtn, on);
  }

  async function doVerify({ verifyOnline }) {
    if (IS_RUNNING) return;
    clearBanner();
    setRunning(true);

    try {
      const fd = buildVerifyFormData({ verifyOnline });

      if (verifyOnline) {
        // Inform user if we are forcing safer defaults
        const maxVerifyVal = (fd.get("max_verify") || "").toString();
        if (maxVerifyVal && maxVerifyVal !== "0") {
          banner("info", `Online verification is running with Max verify = ${maxVerifyVal} to avoid timeouts. Increase it if needed.`);
        }
      } else {
        banner("info", "Running citation check...");
      }

      const { res, js, text } = await fetchJson("/verify", { method: "POST", body: fd });

      if (!res.ok) {
        // Handle Render/router 502
        if (res.status === 502) {
          banner("error", "Server timeout (502). Reduce Max verify and try again, or enable only Crossref.");
        } else {
          banner("error", `Error (${res.status}). ${text?.slice(0, 300) || "Request failed."}`);
        }
        return;
      }

      if (!js || js.ok !== true) {
        const msg = js?.data?.error || "Unexpected response from server.";
        banner("error", msg);
        return;
      }

      LAST_JOB_ID = js.job_id || null;
      renderResults(js.data);

      if (verifyOnline) {
        banner("success", "Citation check completed. Online verification results included if it finished within the request.");
      } else {
        banner("success", "Citation check completed.");
      }
    } catch (e) {
      banner("error", e?.message || String(e));
    } finally {
      setRunning(false);
    }
  }

  async function doExport(kind) {
    if (IS_RUNNING) return;
    clearBanner();

    if (!LAST_JOB_ID && !LAST_RESULT) {
      banner("warn", "Run a check first, then export.");
      return;
    }

    setRunning(true);
    try {
      let url = "";
      let fallbackName = "export";
      if (kind === "csv") { url = "/export/csv"; fallbackName = "citation_crosscheck.csv"; }
      if (kind === "word") { url = "/export/word"; fallbackName = "citation_crosscheck.docx"; }
      if (kind === "pdf") { url = "/export/pdf"; fallbackName = "citation_crosscheck.pdf"; }

      const payload = LAST_JOB_ID
        ? { job_id: LAST_JOB_ID }
        : { result: LAST_RESULT }; // fallback only

      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const t = await res.text();
        banner("error", `Export failed (${res.status}). ${t?.slice(0, 200) || ""}`);
        return;
      }

      await downloadFromResponse(res, fallbackName);
      banner("success", "Export ready.");
    } catch (e) {
      banner("error", e?.message || String(e));
    } finally {
      setRunning(false);
    }
  }

  // -----------------------------
  // Bind events
  // -----------------------------
  function bind() {
    if (els.runBtn) {
      els.runBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        doVerify({ verifyOnline: false });
      });
    }

    if (els.runOnlineBtn) {
      els.runOnlineBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        doVerify({ verifyOnline: true });
      });
    }

    if (els.exportCsvBtn) {
      els.exportCsvBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        doExport("csv");
      });
    }

    if (els.exportWordBtn) {
      els.exportWordBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        doExport("word");
      });
    }

    if (els.exportPdfBtn) {
      els.exportPdfBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        doExport("pdf");
      });
    }
  }

  // -----------------------------
  // Init
  // -----------------------------
  document.addEventListener("DOMContentLoaded", () => {
    bind();

    // Small UX: if user set max_verify=0 and presses online, we’ll apply safe default,
    // but we can also hint in advance.
    if (els.maxVerify && els.runOnlineBtn) {
      els.runOnlineBtn.addEventListener("mouseenter", () => {
        const v = (els.maxVerify.value ?? "").toString().trim();
        if (!v || v === "0") {
          banner("info", `Tip: Online verification with Max verify = 0 can timeout. This app will use ${DEFAULT_MAX_VERIFY_ONLINE} unless you set a value.`);
        }
      });
    }
  });

})();
