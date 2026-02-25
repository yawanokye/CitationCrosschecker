/* static/app.js — FIXED:
   - Adds tab switching (previous file had none)
   - Renders table rows with correct column counts
   - Shows AI state (skipped/running/done/error) clearly
*/

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

  function setRunning(on) {
    RUNNING = !!on;
    if (el.btnCheck) el.btnCheck.disabled = RUNNING;
    if (el.btnVerify) el.btnVerify.disabled = RUNNING;
  }

  // ---------- Tabs ----------
  function initTabs() {
    const tabs = Array.from(document.querySelectorAll(".tabs .tab"));
    const panes = Array.from(document.querySelectorAll(".tabPanes .tabPane"));
    if (!tabs.length || !panes.length) return;

    function activate(tabEl) {
      const id = tabEl.getAttribute("data-tab");
      tabs.forEach(t => t.classList.toggle("active", t === tabEl));
      panes.forEach(p => p.classList.toggle("active", p.id === id));
    }

    tabs.forEach(t => {
      t.addEventListener("click", () => activate(t));
      t.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          activate(t);
        }
      });
    });
  }

  // ---------- Render helpers ----------
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
    const addRow = (k, v) => rows.push(`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`);

    addRow("In-text citations found", s.in_text_citations_found ?? "");
    addRow("Reference entries found", s.reference_entries_found ?? "");
    addRow("Missing in references", s.missing_in_references ?? "");
    addRow("Uncited references", s.uncited_references ?? "");
    addRow("Match rate", fmtPct(s.match_rate));

    const ai = data?.ai_assist || {};
    if (ai.enabled) {
      addRow("AI Assist", `enabled (added: ${ai.added_citations || 0})`);
    } else if (ai.state) {
      addRow("AI Assist", ai.state);
    }

    el.summaryTable.innerHTML = rows.join("");
  }

  function renderMissing(data) {
    const rows = data?.missing_in_references || [];
    if (!el.missingBody) return;

    if (!rows.length) {
      el.missingBody.innerHTML = `<tr><td colspan="3" class="muted">None</td></tr>`;
      return;
    }

    el.missingBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td class="num">${i + 1}</td>
        <td>${esc(r.citation_in_text || "")}</td>
        <td class="num">${esc(r.count_in_text ?? "")}</td>
      </tr>
    `).join("");
  }

  function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;

    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td colspan="2" class="muted">None</td></tr>`;
      return;
    }

    el.uncitedBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td class="num">${i + 1}</td>
        <td>${esc(r)}</td>
      </tr>
    `).join("");
  }

  function renderC2R(data) {
    const rows = data?.reconciliation_intext_to_reference || [];
    if (!el.c2rBody) return;

    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="5" class="muted">No rows</td></tr>`;
      return;
    }

    el.c2rBody.innerHTML = rows.slice(0, 800).map((r, i) => {
      const st = r.status || "";
      const flags = Array.isArray(r.flags) ? r.flags.join("; ") : (r.flags || "");
      return `
        <tr>
          <td class="num">${i + 1}</td>
          <td><span class="badge ${esc(st)}">${esc(st)}</span></td>
          <td>${esc(r.in_text || "")}</td>
          <td>${esc(r.matched_reference || "")}</td>
          <td>${esc(flags)}</td>
        </tr>
      `;
    }).join("");
  }

  function renderR2C(data) {
    const rows = data?.reconciliation_reference_to_intext || [];
    if (!el.r2cBody) return;

    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="4" class="muted">No rows</td></tr>`;
      return;
    }

    el.r2cBody.innerHTML = rows.slice(0, 800).map((r, i) => {
      const times = r.times_cited ?? 0;
      const citedBy = Array.isArray(r.cited_by) ? r.cited_by.join("; ") : (r.cited_by || "");
      return `
        <tr>
          <td class="num">${i + 1}</td>
          <td class="num">${esc(times)}</td>
          <td>${esc(r.reference || "")}</td>
          <td>${esc(citedBy)}</td>
        </tr>
      `;
    }).join("");
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
      el.verifyBody.innerHTML = `<tr><td colspan="9" class="muted">No online verification rows yet.</td></tr>`;
      return;
    }

    el.verifyBody.innerHTML = rows.slice(0, 800).map((r, i) => {
      const st = r.status || "";
      const src = r.source || "";
      const score = r.score ?? "";
      const doi = r.found_doi || "";
      const year = r.found_year || "";
      const authors = r.found_authors || "";
      const title = r.found_title || "";
      const query = r.query_used || r.query || "";
      return `
        <tr>
          <td class="num">${i + 1}</td>
          <td><span class="badge ${esc(st)}">${esc(st)}</span></td>
          <td>${esc(src)}</td>
          <td class="num">${esc(score)}</td>
          <td>${esc(doi)}</td>
          <td class="num">${esc(year)}</td>
          <td>${esc(authors)}</td>
          <td>${esc(title)}</td>
          <td>${esc(query)}</td>
        </tr>
      `;
    }).join("");
  }

  function renderAll(data) {
    if (!data) return;

    clearTables();

    if (el.resultsCard) el.resultsCard.style.display = "block";
    if (el.refMsg) el.refMsg.textContent = data.reference_detection_message || "";

    renderSummaryTable(data);
    renderMissing(data);
    renderUncited(data);
    renderC2R(data);
    renderR2C(data);
    renderVerify(data);

    if (el.btnExportCsvTop) el.btnExportCsvTop.disabled = !LAST_JOB_ID;
    if (el.btnExportWordTop) el.btnExportWordTop.disabled = !LAST_JOB_ID;
  }

  // ---------- API ----------
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

      fd.append("throttle_s", String(Number(el.throttle?.value || 0.12)));
      fd.append("max_verify", String(Number(el.maxVerify?.value || 0)));

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
        startPolling();
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

  function stopPolling() {
    if (POLL_TIMER) {
      clearInterval(POLL_TIMER);
      POLL_TIMER = null;
    }
  }

  function startPolling() {
    stopPolling();
    if (!LAST_JOB_ID) return;

    POLL_TIMER = setInterval(async () => {
      try {
        const res = await fetch(`/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`);
        if (!res.ok) return;

        const js = await res.json();
        if (!js || js.ok !== true) return;

        if (js.result && Object.keys(js.result).length) {
          renderAll(js.result);
        }

        // AI message
        const ai = js.ai || {};
        if (ai.state && ai.state !== "idle") {
          if (ai.state === "running") setStatus(ai.message || "AI assist running...", "muted");
          else if (ai.state === "done") setStatus(ai.message || "AI assist done.", "success");
          else if (ai.state === "skipped") setStatus(ai.message || "AI assist skipped.", "muted");
          else if (ai.state === "error") setStatus(ai.message || "AI assist error.", "warn");
        }

        // Online verification message
        const online = js.online || {};
        if (online.state === "running") setStatus(online.message || "Online verification running...", "muted");
        else if (online.state === "done") setStatus("Online verification completed.", "success");
        else if (online.state === "error") setStatus(online.message || "Online verification error.", "warn");

        const aiDoneish = ["idle", "done", "skipped", "error"].includes((ai.state || "idle"));
        const onDoneish = ["idle", "done", "error"].includes((online.state || "idle"));
        if (aiDoneish && onDoneish) stopPolling();
      } catch (e) {
        // keep polling silently
      }
    }, 1200);
  }

  // ---------- Export ----------
  async function exportFile(kind) {
    if (!LAST_JOB_ID) return;
    try {
      const res = await fetch(`/export/${kind}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_id: LAST_JOB_ID }),
      });
      if (!res.ok) {
        setStatus(`Export failed (${res.status}).`, "warn");
        return;
      }
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = kind === "csv" ? "citation_crosscheck.csv" : "citation_crosscheck.docx";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (e) {
      setStatus("Export failed.", "warn");
    }
  }

  // ---------- Bind ----------
  function bind() {
    initTabs();

    if (el.btnCheck) el.btnCheck.addEventListener("click", () => postVerify(false));
    if (el.btnVerify) el.btnVerify.addEventListener("click", () => postVerify(true));

    if (el.btnExportCsvTop) el.btnExportCsvTop.addEventListener("click", () => exportFile("csv"));
    if (el.btnExportWordTop) el.btnExportWordTop.addEventListener("click", () => exportFile("word"));
  }

  bind();
})();
