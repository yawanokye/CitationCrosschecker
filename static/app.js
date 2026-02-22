/* static/app.js - FULL FILE
   Matches your index.html IDs exactly.
   Fixes:
   - Run buttons not working (wrong selectors)
   - Export empty (export now sends job_id)
*/

(() => {
  "use strict";

  // -----------------------------
  // DOM
  // -----------------------------
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

    status: document.getElementById("status"),

    resultsCard: document.getElementById("resultsCard"),
    dash: document.getElementById("dash"),
    verifyDash: document.getElementById("verifyDash"),
    summary: document.getElementById("summary"),
    summaryTable: document.getElementById("summaryTable"),
    refMsg: document.getElementById("refMsg"),

    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    c2rBody: document.getElementById("c2rBody"),
    r2cBody: document.getElementById("r2cBody"),
    verifyBody: document.getElementById("verifyBody"),

    btnExportCsvTop: document.getElementById("btnExportCsvTop"),
    btnExportWordTop: document.getElementById("btnExportWordTop"),

    tabs: Array.from(document.querySelectorAll(".tab")),
    panes: Array.from(document.querySelectorAll(".tabPane")),
  };

  // -----------------------------
  // State
  // -----------------------------
  let LAST_JOB_ID = null;
  let LAST_RESULT = null;
  let RUNNING = false;

  // Safety defaults for online verification (prevents 502 on big lists)
  const SAFE_ONLINE_THROTTLE = 0.20;
  const SAFE_ONLINE_MAX_VERIFY = 120;

  // -----------------------------
  // Utils
  // -----------------------------
  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg;
  }

  function setRunning(on) {
    RUNNING = !!on;
    if (el.btnCheck) el.btnCheck.disabled = on;
    if (el.btnVerify) el.btnVerify.disabled = on;
    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = on || !LAST_JOB_ID;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = on || !LAST_JOB_ID;
  }

  function esc(s) {
    return (s ?? "")
      .toString()
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function fmtNum(x) {
    const n = Number(x);
    if (!Number.isFinite(n)) return "0";
    return n.toLocaleString();
  }

  function pct(x) {
    const n = Number(x);
    if (!Number.isFinite(n)) return "";
    return `${n.toFixed(1)}%`;
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "download";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  function parseFilenameFromCD(cd) {
    if (!cd) return "";
    const m = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
    return m ? decodeURIComponent(m[1].replace(/\"/g, "").trim()) : "";
  }

  // -----------------------------
  // Tabs
  // -----------------------------
  function activateTab(paneId) {
    el.tabs.forEach((t) => {
      const active = t.dataset.tab === paneId;
      t.classList.toggle("active", active);
    });
    el.panes.forEach((p) => {
      const active = p.id === paneId;
      p.classList.toggle("active", active);
    });
  }

  function bindTabs() {
    el.tabs.forEach((t) => {
      t.addEventListener("click", () => {
        activateTab(t.dataset.tab);
      });
    });
  }

  // -----------------------------
  // Rendering
  // -----------------------------
  function showResults() {
    if (el.resultsCard) el.resultsCard.style.display = "";
  }

  function renderDashboard(data) {
    const s = data?.summary || {};

    const inText = Number(s.in_text_citations_found || 0);
    const refs = Number(s.reference_entries_found || 0);
    const missing = Number(s.missing_in_references || 0);
    const uncited = Number(s.uncited_references || 0);

    const matchRate = inText > 0 ? ((inText - missing) / inText) * 100 : 0;

    if (el.dash) {
      el.dash.innerHTML = `
        <div class="kpi"><div class="k">In-text</div><div class="v">${fmtNum(inText)}</div></div>
        <div class="kpi"><div class="k">References</div><div class="v">${fmtNum(refs)}</div></div>
        <div class="kpi"><div class="k">Missing</div><div class="v">${fmtNum(missing)}</div></div>
        <div class="kpi"><div class="k">Uncited</div><div class="v">${fmtNum(uncited)}</div></div>
        <div class="kpi"><div class="k">Match rate</div><div class="v">${pct(matchRate)}</div></div>
      `;
    }
  }

  function renderSummaryTable(data) {
    const s = data?.summary || {};
    const rows = [
      ["In-text citations found", s.in_text_citations_found],
      ["Reference entries found", s.reference_entries_found],
      ["Missing in references", s.missing_in_references],
      ["Uncited references", s.uncited_references],
      ["Style", data?.style || ""],
      ["Main text length", data?.main_text_length ?? ""],
      ["Total text length", data?.text_length ?? ""],
      ["References detected (raw)", data?.references_detected ?? ""],
      ["Verify mode used", data?.verify_mode_used ?? ""],
    ];

    if (el.summaryTable) {
      el.summaryTable.innerHTML = rows
        .map(
          (r) => `
            <tr>
              <td>${esc(r[0])}</td>
              <td>${esc(r[1] ?? "")}</td>
            </tr>
          `
        )
        .join("");
    }

    if (el.refMsg) {
      el.refMsg.textContent = data?.reference_detection_message || "";
    }
  }

  function renderMissing(data) {
    const missing = Array.isArray(data?.missing_in_references) ? data.missing_in_references : [];
    if (!el.missingBody) return;

    if (!missing.length) {
      el.missingBody.innerHTML = `<tr><td colspan="3" class="muted">None</td></tr>`;
      return;
    }

    el.missingBody.innerHTML = missing
      .slice(0, 5000)
      .map(
        (m, i) => `
          <tr>
            <td>${i + 1}</td>
            <td>${esc(m.citation_in_text || "")}</td>
            <td>${esc(m.count_in_text ?? 0)}</td>
          </tr>
        `
      )
      .join("");
  }

  function renderUncited(data) {
    const uncited = Array.isArray(data?.uncited_references) ? data.uncited_references : [];
    if (!el.uncitedBody) return;

    if (!uncited.length) {
      el.uncitedBody.innerHTML = `<tr><td colspan="2" class="muted">None</td></tr>`;
      return;
    }

    el.uncitedBody.innerHTML = uncited
      .slice(0, 5000)
      .map(
        (r, i) => `
          <tr>
            <td>${i + 1}</td>
            <td>${esc(r)}</td>
          </tr>
        `
      )
      .join("");
  }

  function renderC2R(data) {
    const rows = Array.isArray(data?.reconciliation_intext_to_reference)
      ? data.reconciliation_intext_to_reference
      : [];
    if (!el.c2rBody) return;

    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="5" class="muted">No rows</td></tr>`;
      return;
    }

    el.c2rBody.innerHTML = rows
      .slice(0, 5000)
      .map(
        (r, i) => `
          <tr>
            <td>${i + 1}</td>
            <td>${esc(r.status || "")}</td>
            <td>${esc(r.in_text || "")}</td>
            <td>${esc(r.matched_reference || "")}</td>
            <td>${esc(r.flags || "")}</td>
          </tr>
        `
      )
      .join("");
  }

  function renderR2C(data) {
    const rows = Array.isArray(data?.reconciliation_reference_to_intext)
      ? data.reconciliation_reference_to_intext
      : [];
    if (!el.r2cBody) return;

    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="4" class="muted">No rows</td></tr>`;
      return;
    }

    el.r2cBody.innerHTML = rows
      .slice(0, 3000)
      .map((r, i) => {
        const citedBy = Array.isArray(r.cited_by) ? r.cited_by.slice(0, 6).join(" | ") : "";
        return `
          <tr>
            <td>${i + 1}</td>
            <td>${esc(r.times_cited ?? 0)}</td>
            <td>${esc(r.reference || "")}</td>
            <td>${esc(citedBy)}</td>
          </tr>
        `;
      })
      .join("");
  }

  function renderVerify(data) {
    const ov = data?.online_verification || {};
    const summary = ov.summary || {};
    const rows = Array.isArray(ov.rows) ? ov.rows : [];

    if (el.verifyDash) {
      const parts = [
        ["Verified", summary.verified ?? 0],
        ["Likely", summary.likely ?? 0],
        ["Needs review", summary.needs_review ?? 0],
        ["Not found", summary.not_found ?? 0],
        ["Offline", summary.offline ?? 0],
        ["Total", summary.total ?? 0],
      ];
      el.verifyDash.innerHTML = parts
        .map((p) => `<div class="kpi"><div class="k">${esc(p[0])}</div><div class="v">${fmtNum(p[1])}</div></div>`)
        .join("");
    }

    if (!el.verifyBody) return;

    if (!rows.length) {
      el.verifyBody.innerHTML = `<tr><td colspan="9" class="muted">No online verification rows (not run or timed out).</td></tr>`;
      return;
    }

    el.verifyBody.innerHTML = rows
      .slice(0, 5000)
      .map(
        (r, i) => `
          <tr>
            <td>${i + 1}</td>
            <td>${esc(r.status || "")}</td>
            <td>${esc(r.source || "")}</td>
            <td>${esc(r.score ?? 0)}</td>
            <td>${esc(r.doi || "")}</td>
            <td>${esc(r.matched_year || "")}</td>
            <td>${esc(r.matched_authors || "")}</td>
            <td>${esc(r.matched_title || "")}</td>
            <td>${esc(r.query_used || "")}</td>
          </tr>
        `
      )
      .join("");
  }

  function renderAll(data) {
    LAST_RESULT = data || null;
    showResults();
    renderDashboard(data);
    renderSummaryTable(data);
    renderMissing(data);
    renderUncited(data);
    renderC2R(data);
    renderR2C(data);
    renderVerify(data);

    // Enable exports once we have a job id
    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = !LAST_JOB_ID;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = !LAST_JOB_ID;
  }

  // -----------------------------
  // Requests
  // -----------------------------
  async function postVerify({ verifyOnline }) {
    if (RUNNING) return;

    const f = el.file?.files?.[0];
    if (!f) {
      setStatus("Choose a DOCX or PDF file first.", "warn");
      return;
    }

    setRunning(true);

    try {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("style", el.style?.value || "apa");

      fd.append("verify_online", verifyOnline ? "true" : "false");
      fd.append("verify_mode", el.verifyMode?.value || "all");

      let throttleVal = Number(el.throttle?.value || 0.12);
      if (!Number.isFinite(throttleVal) || throttleVal < 0) throttleVal = 0.12;

      let maxVerifyVal = Number(el.maxVerify?.value || 0);
      if (!Number.isFinite(maxVerifyVal) || maxVerifyVal < 0) maxVerifyVal = 0;

      // Safety for online: avoid router 502
      if (verifyOnline) {
        if (!throttleVal || throttleVal < 0.12) throttleVal = SAFE_ONLINE_THROTTLE;
        if (!maxVerifyVal || maxVerifyVal === 0) maxVerifyVal = SAFE_ONLINE_MAX_VERIFY;
      }

      fd.append("throttle_s", String(throttleVal));
      fd.append("max_verify", String(maxVerifyVal));

      fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");

      setStatus(verifyOnline ? "Running online verification..." : "Running check...", "muted");

      const res = await fetch("/verify", { method: "POST", body: fd });

      if (!res.ok) {
        const txt = await res.text();
        if (res.status === 502) {
          setStatus("Server timeout (502). Reduce Max verify or increase Throttle.", "warn");
        } else {
          setStatus(`Error (${res.status}). ${txt.slice(0, 200)}`, "warn");
        }
        return;
      }

      const js = await res.json();
      if (!js || js.ok !== true) {
        const msg = js?.data?.error || "Unexpected server response.";
        setStatus(msg, "warn");
        return;
      }

      LAST_JOB_ID = js.job_id || null;
      renderAll(js.data);

      setStatus("Done.", "success");
    } catch (e) {
      console.error(e);
      setStatus(`Error: ${e?.message || String(e)}`, "warn");
    } finally {
      setRunning(false);
    }
  }

  async function exportKind(kind) {
    if (RUNNING) return;
    if (!LAST_JOB_ID) {
      setStatus("Run a check first, then export.", "warn");
      return;
    }

    setRunning(true);
    try {
      const url = kind === "csv" ? "/export/csv" : kind === "word" ? "/export/word" : "/export/pdf";
      const fallback =
        kind === "csv" ? "citation_crosscheck.csv" : kind === "word" ? "citation_crosscheck.docx" : "citation_crosscheck.pdf";

      setStatus(`Exporting ${kind.toUpperCase()}...`, "muted");

      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: LAST_JOB_ID }),
      });

      if (!res.ok) {
        const t = await res.text();
        setStatus(`Export failed (${res.status}). ${t.slice(0, 160)}`, "warn");
        return;
      }

      const cd = res.headers.get("Content-Disposition") || "";
      const fn = parseFilenameFromCD(cd) || fallback;

      const blob = await res.blob();
      // Basic empty-file protection
      if (blob.size < 20) {
        setStatus("Export returned an empty file. This usually means the server did not receive job_id or job expired.", "warn");
        return;
      }

      downloadBlob(blob, fn);
      setStatus("Export ready.", "success");
    } catch (e) {
      console.error(e);
      setStatus(`Export error: ${e?.message || String(e)}`, "warn");
    } finally {
      setRunning(false);
    }
  }

  // -----------------------------
  // Bind events
  // -----------------------------
  function bind() {
    if (!el.btnCheck || !el.btnVerify) {
      console.error("Buttons not found. Check IDs in HTML.");
      return;
    }

    bindTabs();

    el.btnCheck.addEventListener("click", (ev) => {
      ev.preventDefault();
      postVerify({ verifyOnline: false });
    });

    el.btnVerify.addEventListener("click", (ev) => {
      ev.preventDefault();
      postVerify({ verifyOnline: true });
    });

    if (el.btnExportCsvTop) {
      el.btnExportCsvTop.addEventListener("click", (ev) => {
        ev.preventDefault();
        exportKind("csv");
      });
    }

    if (el.btnExportWordTop) {
      el.btnExportWordTop.addEventListener("click", (ev) => {
        ev.preventDefault();
        exportKind("word");
      });
    }

    // Exports disabled until first successful run
    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = true;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = true;

    setStatus("Ready.", "muted");
    console.log("app.js bound OK");
  }

  // -----------------------------
  // Init
  // -----------------------------
  window.addEventListener("error", (e) => {
    console.error("Global error:", e?.error || e);
    setStatus("A page script error occurred. Open console for details.", "warn");
  });

  document.addEventListener("DOMContentLoaded", bind);
})();
