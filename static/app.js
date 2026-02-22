/* static/app.js - FULL FILE (polling online verification until complete) */

(() => {
  "use strict";

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

  let LAST_JOB_ID = null;
  let LAST_RESULT = null;
  let RUNNING = false;
  let POLL_TIMER = null;

  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg;
  }

  function setRunning(on) {
    RUNNING = !!on;
    el.btnCheck.disabled = on;
    el.btnVerify.disabled = on;
    el.btnExportCsvTop.disabled = on || !LAST_JOB_ID;
    el.btnExportWordTop.disabled = on || !LAST_JOB_ID;
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

  function activateTab(paneId) {
    el.tabs.forEach((t) => t.classList.toggle("active", t.dataset.tab === paneId));
    el.panes.forEach((p) => p.classList.toggle("active", p.id === paneId));
  }

  function bindTabs() {
    el.tabs.forEach((t) => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  }

  function showResults() {
    el.resultsCard.style.display = "";
  }

  function renderDashboard(data) {
    const s = data?.summary || {};
    const inText = Number(s.in_text_citations_found || 0);
    const refs = Number(s.reference_entries_found || 0);
    const missing = Number(s.missing_in_references || 0);
    const uncited = Number(s.uncited_references || 0);
    const matchRate = inText > 0 ? ((inText - missing) / inText) * 100 : 0;

    el.dash.innerHTML = `
      <div class="kpi"><div class="k">In-text</div><div class="v">${fmtNum(inText)}</div></div>
      <div class="kpi"><div class="k">References</div><div class="v">${fmtNum(refs)}</div></div>
      <div class="kpi"><div class="k">Missing</div><div class="v">${fmtNum(missing)}</div></div>
      <div class="kpi"><div class="k">Uncited</div><div class="v">${fmtNum(uncited)}</div></div>
      <div class="kpi"><div class="k">Match rate</div><div class="v">${pct(matchRate)}</div></div>
    `;
  }

  function renderSummaryTable(data) {
    const s = data?.summary || {};
    const rows = [
      ["In-text citations found", s.in_text_citations_found],
      ["Reference entries found", s.reference_entries_found],
      ["Missing in references", s.missing_in_references],
      ["Uncited references", s.uncited_references],
      ["Style", data?.style || ""],
      ["Verify mode used", data?.verify_mode_used ?? ""],
    ];
    el.summaryTable.innerHTML = rows
      .map((r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1] ?? "")}</td></tr>`)
      .join("");

    el.refMsg.textContent = data?.reference_detection_message || "";
  }

  function renderMissing(data) {
    const missing = Array.isArray(data?.missing_in_references) ? data.missing_in_references : [];
    el.missingBody.innerHTML = missing.length
      ? missing.slice(0, 5000).map((m, i) =>
          `<tr><td>${i + 1}</td><td>${esc(m.citation_in_text || "")}</td><td>${esc(m.count_in_text ?? 0)}</td></tr>`
        ).join("")
      : `<tr><td colspan="3" class="muted">None</td></tr>`;
  }

  function renderUncited(data) {
    const uncited = Array.isArray(data?.uncited_references) ? data.uncited_references : [];
    el.uncitedBody.innerHTML = uncited.length
      ? uncited.slice(0, 5000).map((r, i) => `<tr><td>${i + 1}</td><td>${esc(r)}</td></tr>`).join("")
      : `<tr><td colspan="2" class="muted">None</td></tr>`;
  }

  function renderC2R(data) {
    const rows = Array.isArray(data?.reconciliation_intext_to_reference) ? data.reconciliation_intext_to_reference : [];
    el.c2rBody.innerHTML = rows.length
      ? rows.slice(0, 5000).map((r, i) =>
          `<tr><td>${i + 1}</td><td>${esc(r.status || "")}</td><td>${esc(r.in_text || "")}</td><td>${esc(r.matched_reference || "")}</td><td>${esc(r.flags || "")}</td></tr>`
        ).join("")
      : `<tr><td colspan="5" class="muted">No rows</td></tr>`;
  }

  function renderR2C(data) {
    const rows = Array.isArray(data?.reconciliation_reference_to_intext) ? data.reconciliation_reference_to_intext : [];
    el.r2cBody.innerHTML = rows.length
      ? rows.slice(0, 3000).map((r, i) => {
          const citedBy = Array.isArray(r.cited_by) ? r.cited_by.slice(0, 6).join(" | ") : "";
          return `<tr><td>${i + 1}</td><td>${esc(r.times_cited ?? 0)}</td><td>${esc(r.reference || "")}</td><td>${esc(citedBy)}</td></tr>`;
        }).join("")
      : `<tr><td colspan="4" class="muted">No rows</td></tr>`;
  }

  function renderVerify(data) {
    const ov = data?.online_verification || {};
    const summary = ov.summary || {};
    const rows = Array.isArray(ov.rows) ? ov.rows : [];

    el.verifyDash.innerHTML = `
      <div class="kpi"><div class="k">Verified</div><div class="v">${fmtNum(summary.verified ?? 0)}</div></div>
      <div class="kpi"><div class="k">Likely</div><div class="v">${fmtNum(summary.likely ?? 0)}</div></div>
      <div class="kpi"><div class="k">Needs review</div><div class="v">${fmtNum(summary.needs_review ?? 0)}</div></div>
      <div class="kpi"><div class="k">Not found</div><div class="v">${fmtNum(summary.not_found ?? 0)}</div></div>
      <div class="kpi"><div class="k">Offline</div><div class="v">${fmtNum(summary.offline ?? 0)}</div></div>
      <div class="kpi"><div class="k">Total</div><div class="v">${fmtNum(summary.total ?? 0)}</div></div>
    `;

    el.verifyBody.innerHTML = rows.length
      ? rows.slice(0, 5000).map((r, i) =>
          `<tr>
            <td>${i + 1}</td>
            <td>${esc(r.status || "")}</td>
            <td>${esc(r.source || "")}</td>
            <td>${esc(r.score ?? 0)}</td>
            <td>${esc(r.doi || "")}</td>
            <td>${esc(r.matched_year || "")}</td>
            <td>${esc(r.matched_authors || "")}</td>
            <td>${esc(r.matched_title || "")}</td>
            <td>${esc(r.query_used || "")}</td>
          </tr>`
        ).join("")
      : `<tr><td colspan="9" class="muted">No rows yet.</td></tr>`;
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

    el.btnExportCsvTop.disabled = !LAST_JOB_ID;
    el.btnExportWordTop.disabled = !LAST_JOB_ID;
  }

  async function postVerify(verifyOnline) {
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

      const throttleVal = Number(el.throttle?.value || 0.12);
      fd.append("throttle_s", String(throttleVal));
      fd.append("max_verify", String(Number(el.maxVerify?.value || 0))); // kept for compatibility

      fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");

      setStatus(verifyOnline ? "Running check and starting online verification..." : "Running check...", "muted");

      const res = await fetch("/verify", { method: "POST", body: fd });
      if (!res.ok) {
        const txt = await res.text();
        setStatus(`Error (${res.status}). ${txt.slice(0, 200)}`, "warn");
        return;
      }

      const js = await res.json();
      if (!js || js.ok !== true) {
        setStatus(js?.data?.error || "Unexpected server response.", "warn");
        return;
      }

      LAST_JOB_ID = js.job_id || null;
      renderAll(js.data);

      if (verifyOnline) {
        setStatus("Check done. Online verification running in background, progress will update.", "muted");
        startPollingOnline();
      } else {
        setStatus("Done.", "success");
      }
    } catch (e) {
      console.error(e);
      setStatus(`Error: ${e?.message || String(e)}`, "warn");
    } finally {
      setRunning(false);
    }
  }

  function stopPollingOnline() {
    if (POLL_TIMER) {
      clearInterval(POLL_TIMER);
      POLL_TIMER = null;
    }
  }

  function startPollingOnline() {
    stopPollingOnline();
    if (!LAST_JOB_ID) return;

    POLL_TIMER = setInterval(async () => {
      try {
        const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}`);
        if (!res.ok) return;
        const js = await res.json();
        const online = js?.online || {};
        const ov = js?.online_verification || {};

        // update UI from stored result if server has it
        if (LAST_RESULT) {
          LAST_RESULT.online_verification = ov;
          renderVerify(LAST_RESULT);
        }

        const state = online.state || "idle";
        const prog = Number(online.progress || 0);
        const total = Number(online.total || 0);
        const msg = online.message || "";

        if (state === "running") {
          setStatus(`Online verification: ${prog}/${total}. ${msg}`, "muted");
        } else if (state === "done") {
          setStatus("Online verification completed.", "success");
          stopPollingOnline();
        } else if (state === "error") {
          setStatus(`Online verification error: ${msg}`, "warn");
          stopPollingOnline();
        }
      } catch (e) {
        // ignore occasional poll errors
      }
    }, 1200);
  }

  async function exportKind(kind) {
    if (RUNNING) return;
    if (!LAST_JOB_ID) {
      setStatus("Run a check first, then export.", "warn");
      return;
    }

    setRunning(true);
    try {
      const url = kind === "csv" ? "/export/csv" : "/export/word";
      const fallback = kind === "csv" ? "citation_crosscheck.csv" : "citation_crosscheck.docx";

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
      downloadBlob(blob, fn);

      setStatus("Export ready.", "success");
    } catch (e) {
      console.error(e);
      setStatus(`Export error: ${e?.message || String(e)}`, "warn");
    } finally {
      setRunning(false);
    }
  }

  function bind() {
    bindTabs();

    el.btnCheck.addEventListener("click", (ev) => {
      ev.preventDefault();
      stopPollingOnline();
      postVerify(false);
    });

    el.btnVerify.addEventListener("click", (ev) => {
      ev.preventDefault();
      stopPollingOnline();
      postVerify(true);
    });

    el.btnExportCsvTop.addEventListener("click", (ev) => {
      ev.preventDefault();
      exportKind("csv");
    });

    el.btnExportWordTop.addEventListener("click", (ev) => {
      ev.preventDefault();
      exportKind("word");
    });

    el.btnExportCsvTop.disabled = true;
    el.btnExportWordTop.disabled = true;

    setStatus("Ready.", "muted");
  }

  function parseFilenameFromCD(cd) {
    if (!cd) return "";
    const m = /filename\*?=(?:UTF-8''|")?([^\";]+)/i.exec(cd);
    return m ? decodeURIComponent(m[1].replace(/\"/g, "").trim()) : "";
  }

  document.addEventListener("DOMContentLoaded", bind);
})();
