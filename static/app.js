/* static/app.js - FULL FILE (robust rendering for uncited objects + safer fallbacks) */

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

  function setRunning(on) {
    RUNNING = !!on;
    if (el.btnCheck) el.btnCheck.disabled = RUNNING;
    if (el.btnVerify) el.btnVerify.disabled = RUNNING;
  }

  function esc(s) {
    // Defensive: allow objects/numbers without crashing UI
    let str = "";
    try {
      if (s === null || s === undefined) str = "";
      else if (typeof s === "string") str = s;
      else if (typeof s === "number" || typeof s === "boolean") str = String(s);
      else str = JSON.stringify(s);
    } catch {
      str = String(s ?? "");
    }

    return str
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

  // ---------- Helpers for mixed backend shapes ----------
  function pickRefText(r) {
    // Handles string OR object reference rows
    if (r === null || r === undefined) return "";
    if (typeof r === "string") return r;

    // common keys we might see from engine versions
    if (typeof r === "object") {
      return (
        r.reference_full ||
        r.reference ||
        r.ref ||
        r.raw ||
        r.text ||
        r.display ||
        ""
      );
    }
    return String(r);
  }

  function pickCitationText(r) {
    // missing_in_references rows: may be {citation_in_text, count_in_text} or other variants
    if (!r) return { txt: "", count: "" };
    const txt =
      r.citation_in_text ||
      r.in_text ||
      r.citation ||
      r.key ||
      (typeof r === "string" ? r : "");
    const count =
      r.count_in_text ?? r.count ?? r.times ?? r.n ?? (typeof r === "number" ? r : "");
    return { txt, count };
  }

  function safeBadgeClass(st) {
    // Avoid injecting arbitrary classes. Keep it simple.
    const s = String(st || "").toLowerCase().replace(/\s+/g, "_");
    const ok = new Set([
      "matched",
      "matched_strict",
      "matched_fuzzy",
      "matched_ai",
      "needs_review",
      "missing",
      "uncited",
      "verified",
      "likely",
      "not_found",
      "offline",
      "error",
    ]);
    return ok.has(s) ? s : "needs_review";
  }

  // ---------- Renderers ----------
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

    // Support multiple ai fields from different engine versions
    const ai = data?.ai_assist || data?.ai || null;
    if (ai && (ai.enabled || ai.state)) {
      const added =
        ai.added_citations ??
        ai.added ??
        ai.added_count ??
        ai.added_items ??
        0;
      addRow("AI Assist", ai.enabled ? `enabled (added: ${added || 0})` : (ai.state || "enabled"));
    }

    // Optional extra diagnostics (if you added them in engine)
    if (s.strict_intext_count !== undefined) addRow("Strict in-text count", s.strict_intext_count);
    if (s.loose_intext_count !== undefined) addRow("Loose in-text count", s.loose_intext_count);

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
      .map((r) => {
        const x = pickCitationText(r);
        return `<tr><td>${esc(x.txt)}</td><td class="num">${esc(x.count)}</td></tr>`;
      })
      .join("");
  }

  function renderUncited(data) {
    const rows = data?.uncited_references || [];
    if (!el.uncitedBody) return;
    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td class="muted">None</td></tr>`;
      return;
    }

    // FIX: handle objects instead of producing "[object Object]"
    el.uncitedBody.innerHTML = rows
      .map((r) => `<tr><td>${esc(pickRefText(r))}</td></tr>`)
      .join("");
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
        const st = r?.status || "";
        const cls = safeBadgeClass(st);
        return `<tr>
          <td class="badge ${esc(cls)}">${esc(st)}</td>
          <td>${esc(r?.in_text || r?.citation_in_text || "")}</td>
          <td>${esc(pickRefText(r?.matched_reference || r?.reference || ""))}</td>
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
        const times = r?.times_cited ?? r?.count ?? 0;
        const citedBy = Array.isArray(r?.cited_by) ? r.cited_by.join("; ") : (r?.cited_by || "");
        return `<tr>
          <td class="num">${esc(times)}</td>
          <td>${esc(pickRefText(r?.reference || r?.reference_full || r))}</td>
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
        const st = r?.status || "";
        const cls = safeBadgeClass(st);
        return `<tr>
          <td class="badge ${esc(cls)}">${esc(st)}</td>
          <td>${esc(r?.reference_full || r?.reference || "")}</td>
          <td>${esc(r?.found_title || "")}</td>
          <td>${esc(r?.found_doi || "")}</td>
          <td class="num">${esc(r?.score ?? "")}</td>
        </tr>`;
      })
      .join("");
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
        setStatus(`Error (${res.status}). ${txt.slice(0, 220)}`, "warn");
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

        if (js.result && Object.keys(js.result).length) {
          renderAll(js.result);
        }

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

        const aiState = (js.ai || {}).state || "idle";
        const onlineState = (online || {}).state || "idle";
        const aiFinished =
          aiState === "done" || aiState === "error" || aiState === "skipped" || aiState === "idle";
        const onlineFinished =
          onlineState === "done" || onlineState === "error" || onlineState === "idle";
        if (aiFinished && onlineFinished) stopPollingOnline();
      } catch {
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

  if (el.btnCheck) el.btnCheck.addEventListener("click", () => postVerify(false));
  if (el.btnVerify) el.btnVerify.addEventListener("click", () => postVerify(true));
  if (el.btnExportCsvTop) el.btnExportCsvTop.addEventListener("click", exportCsv);
  if (el.btnExportWordTop) el.btnExportWordTop.addEventListener("click", exportWord);

  setStatus("Ready.", "muted");
})();
