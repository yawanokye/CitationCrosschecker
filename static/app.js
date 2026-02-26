/* static/app.js - FULL FILE (polling online verification until complete) */

(() => {
  "use strict";

  // ------------------------------
  // Payload compatibility
  // Some engine versions return a nested {summary:{...}}.
  // Others return summary fields at the top level.
  // This normalizer keeps the UI working across versions.
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
    if (data.summary && typeof data.summary === "object") return data;

    const missingArr = Array.isArray(data.missing_in_references) ? data.missing_in_references : [];
    const uncitedArr = Array.isArray(data.uncited_references) ? data.uncited_references : [];

    data.summary = {
      in_text_citations_found: toNum(data.in_text_citations_found ?? data.intext_citations_found ?? data.intext_count ?? 0),
      reference_entries_found: toNum(data.reference_entries_found ?? data.ref_count ?? 0),
      missing_in_references: toNum(data.missing_in_references_count ?? missingArr.length),
      uncited_references: toNum(data.uncited_references_count ?? uncitedArr.length),
      match_rate: toNum(data.match_rate ?? 0),
      strict_intext_count: toNum(data.strict_intext_count ?? 0),
      loose_intext_count: toNum(data.loose_intext_count ?? 0),
    };
    return data;
  }

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
  };

  let LAST_JOB_ID = null;
  let POLL_TIMER = null;
  let RUNNING = false;

  function setRunning(on) {
    RUNNING = !!on;
    if (el.btnCheck) el.btnCheck.disabled = RUNNING;
    if (el.btnVerify) el.btnVerify.disabled = RUNNING;
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
  }

  function fmtPct(x) {
    const n = Number(x);
    if (!Number.isFinite(n)) return "";
    return `${n.toFixed(1)}%`;
  }

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
    function addRow(k, v) {
      rows.push(`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`);
    }

    addRow("In-text citations found", s.in_text_citations_found ?? "");
    addRow("Reference entries found", s.reference_entries_found ?? "");
    addRow("Missing in references", s.missing_in_references ?? "");
    addRow("Uncited references", s.uncited_references ?? "");
    addRow("Match rate", fmtPct(s.match_rate));
    if (data?.ai_assist?.enabled) {
      addRow("AI added citations", String(data.ai_assist.added_citations || 0));
    }

    el.summaryTable.innerHTML = rows.join("");
  }

  function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;
    if (!rows.length) {
      el.missingBody.innerHTML = `<tr><td colspan="2" class="muted">None</td></tr>`;
      return;
    }
    el.missingBody.innerHTML = rows
      .map(
        (r) =>
          `<tr><td>${esc(r.citation_in_text || "")}</td><td class="num">${esc(
            r.count_in_text ?? ""
          )}</td></tr>`
      )
      .join("");
  }

  function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td class="muted">None</td></tr>`;
      return;
    }
    el.uncitedBody.innerHTML = rows.map((r) => `<tr><td>${esc(asText(r))}</td></tr>`).join("");
  }

  function renderC2R(data) {
    const rows = data?.reconciliation_intext_to_reference || [];
    if (!el.c2rBody) return;
    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="3" class="muted">No rows</td></tr>`;
      return;
    }
    el.c2rBody.innerHTML = rows
      .slice(0, 500)
      .map((r) => {
        const st = r.status || "";
        return `<tr>
          <td class="badge ${esc(st)}">${esc(st)}</td>
          <td>${esc(r.in_text || "")}</td>
          <td>${esc(r.matched_reference || "")}</td>
        </tr>`;
      })
      .join("");
  }

  function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;
    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="3" class="muted">No rows</td></tr>`;
      return;
    }
    el.r2cBody.innerHTML = rows
      .slice(0, 500)
      .map((r) => {
        const times = r.times_cited ?? 0;
        const citedBy = Array.isArray(r.cited_by) ? r.cited_by.join("; ") : "";
        return `<tr>
          <td class="num">${esc(times)}</td>
          <td>${esc(r.reference || "")}</td>
          <td>${esc(citedBy)}</td>
        </tr>`;
      })
      .join("");
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
      el.verifyBody.innerHTML = `<tr><td colspan="5" class="muted">No online verification rows yet.</td></tr>`;
      return;
    }
    el.verifyBody.innerHTML = rows
      .slice(0, 500)
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
  }

  function renderAll(data) {
    if (!data) return;
    data = normalizeData(data);
    clearTables();

    if (el.resultsCard) el.resultsCard.style.display = "block";
    if (el.refMsg) el.refMsg.textContent = data.reference_detection_message || "";

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
      fd.append("ai_assist", el.aiAssist?.checked ? "true" : "false");

      const aiOn = !!el.aiAssist?.checked;
      setStatus(
        verifyOnline && aiOn
          ? "Running check, starting AI assist and online verification..."
          : verifyOnline
          ? "Running check and starting online verification..."
          : aiOn
          ? "Running check and starting AI assist..."
          : "Running check...",
        "muted"
      );

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

      if (verifyOnline || aiOn) {
        setStatus("Check done. Background tasks running, progress will update.", "muted");
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
        const res = await fetch(
          `/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`
        );
        if (!res.ok) return;

        const js = await res.json();
        if (!js || js.ok !== true) return;

        // If AI assist patched results, refresh dashboard
        if (js.result && Object.keys(js.result).length) {
          renderAll(js.result);
        }

        // Show AI status message if present
        if (js.ai && js.ai.state && js.ai.state !== "idle") {
          const msg = js.ai.message || `AI assist: ${js.ai.state}`;
          if (js.ai.state === "running") setStatus(msg, "muted");
          if (js.ai.state === "done") setStatus(msg, "success");
          if (js.ai.state === "error") setStatus(msg, "warn");
          if (js.ai.state === "skipped") setStatus(msg, "muted");
        }

        const online = js.online || {};
        if (online.state === "running") {
          setStatus(online.message || "Online verification running...", "muted");
          renderVerify(js.result || {});
        } else if (online.state === "done") {
          setStatus("Online verification completed.", "success");
          renderVerify(js.result || {});
          stopPollingOnline();
        } else if (online.state === "error") {
          setStatus(online.message || "Online verification error.", "warn");
          stopPollingOnline();
        }

        // Stop polling when both are done/idle/skipped
        const aiState = (js.ai || {}).state || "idle";
        const onlineState = (online || {}).state || "idle";
        const aiFinished = aiState === "done" || aiState === "error" || aiState === "skipped" || aiState === "idle";
        const onlineFinished = onlineState === "done" || onlineState === "error" || onlineState === "idle";
        if (aiFinished && onlineFinished) stopPollingOnline();
      } catch (e) {
        // ignore transient polling errors
      }
    }, 1200);
  }

  async function exportCsv() {
    if (!LAST_JOB_ID) return;
    const res = await fetch("/export/csv", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: LAST_JOB_ID }),
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "citation_crosscheck.csv";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  async function exportWord() {
    if (!LAST_JOB_ID) return;
    const res = await fetch("/export/word", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: LAST_JOB_ID }),
    });
    if (!res.ok) return;
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "citation_crosscheck.docx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  // wire buttons
  if (el.btnCheck) el.btnCheck.addEventListener("click", () => postVerify(false));
  if (el.btnVerify) el.btnVerify.addEventListener("click", () => postVerify(true));
  if (el.btnExportCsvTop) el.btnExportCsvTop.addEventListener("click", exportCsv);
  if (el.btnExportWordTop) el.btnExportWordTop.addEventListener("click", exportWord);

  setStatus("Ready.", "muted");
})();
