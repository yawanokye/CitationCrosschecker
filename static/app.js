/* static/app.js - OPTIMIZED VERSION with Tab Navigation + ACII Support */

(() => {
  "use strict";

  const CONFIG = {
    POLL_INTERVAL: 1200,
    MAX_C2R_DISPLAY: 500,
    MAX_R2C_DISPLAY: 500,
    MAX_VERIFY_DISPLAY: 500,
    CHUNK_WARNING_SIZE: 2_000_000
  };

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
        v.reference || v.raw || v.text || v.label || v.display ||
        v.citation || v.citation_in_text || v.reference_apa ||
        (v.author && v.year ? `${v.author}, ${v.year}` : "") ||
        (() => { try { return JSON.stringify(v); } catch { return ""; } })()
      );
    }
    try { return String(v); } catch { return ""; }
  }

  function normalizeData(d) {
    const data = d && typeof d === "object" ? d : {};

    const missingArr = Array.isArray(data.missing_in_references) ? data.missing_in_references : [];
    const uncitedArr = Array.isArray(data.uncited_references) ? data.uncited_references : [];

    data.summary = {
      in_text_citations_found: toNum(data.in_text_citations_found ?? 0),
      reference_entries_found: toNum(data.reference_entries_found ?? 0),
      missing_in_references: toNum(data.missing_in_references_count ?? missingArr.length),
      uncited_references: toNum(data.uncited_references_count ?? uncitedArr.length),
      match_rate: toNum(data.match_rate ?? 0),
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

    status: document.getElementById("status"),
    progressBar: document.getElementById("progressBar"),

    resultsCard: document.getElementById("resultsCard"),
    dash: document.getElementById("dash"),
    verifyDash: document.getElementById("verifyDash"),
    summaryTable: document.getElementById("summaryTable"),

    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    c2rBody: document.getElementById("c2rBody"),
    r2cBody: document.getElementById("r2cBody"),
    verifyBody: document.getElementById("verifyBody"),

    btnExportCsvTop: document.getElementById("btnExportCsvTop"),
    btnExportWordTop: document.getElementById("btnExportWordTop"),

    /* ACII elements */
    aciiCard: document.getElementById("aciiCard"),
    aciiValue: document.getElementById("aciiValue"),
    aciiV: document.getElementById("aciiV"),
    aciiC: document.getElementById("aciiC"),
    aciiA: document.getElementById("aciiA"),
    aciiT: document.getElementById("aciiT"),
  };

  let LAST_JOB_ID = null;
  let POLL_TIMER = null;
  let RUNNING = false;
  let CURRENT_DATA = null;

  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg || "";
  }

  function esc(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
  }

  function renderSummaryTable(data) {

    const s = data?.summary || {};

    if (!el.summaryTable) return;

    el.summaryTable.innerHTML = `
      <tr><td>In-text citations</td><td>${esc(s.in_text_citations_found)}</td></tr>
      <tr><td>References</td><td>${esc(s.reference_entries_found)}</td></tr>
      <tr><td>Missing</td><td>${esc(s.missing_in_references)}</td></tr>
      <tr><td>Uncited</td><td>${esc(s.uncited_references)}</td></tr>
      <tr><td>Match rate</td><td>${esc(s.match_rate)}</td></tr>
    `;
  }

  /* ------------------------------
     ACII Renderer
  ------------------------------ */

  function renderACII(data) {

    const acii = data?.acii;

    if (!acii) return;

    if (el.aciiCard) el.aciiCard.style.display = "block";

    if (el.aciiValue)
      el.aciiValue.textContent = acii.ACII ?? "--";

    const c = acii.components || {};

    if (el.aciiV)
      el.aciiV.textContent = c.verification_integrity ?? "";

    if (el.aciiC)
      el.aciiC.textContent = c.citation_concentration ?? "";

    if (el.aciiA)
      el.aciiA.textContent = c.author_diversity ?? "";

    if (el.aciiT)
      el.aciiT.textContent = c.temporal_balance ?? "";
  }

  function renderVerify(data) {

    const ov = data?.online_verification || {};
    const rows = ov.rows || [];
    const sum = ov.summary || {};

    if (el.verifyDash) {

      el.verifyDash.innerHTML = `
        <div class="kpi">Verified ${sum.verified ?? 0}</div>
        <div class="kpi">Likely ${sum.likely ?? 0}</div>
        <div class="kpi">Needs Review ${sum.needs_review ?? 0}</div>
        <div class="kpi">Not Found ${sum.not_found ?? 0}</div>
        <div class="kpi">Offline ${sum.offline ?? 0}</div>
      `;
    }

    if (!rows.length) {
      el.verifyBody.innerHTML = `<tr><td colspan="8">No verification data</td></tr>`;
      return;
    }

    el.verifyBody.innerHTML = rows
      .slice(0, CONFIG.MAX_VERIFY_DISPLAY)
      .map((r) => {

        return `
        <tr>
          <td>${esc(r.status)}</td>
          <td>${esc(r.source)}</td>
          <td>${esc(r.score)}</td>
          <td>${esc(r.doi)}</td>
          <td>${esc(r.matched_year)}</td>
          <td>${esc(r.author)}</td>
          <td>${esc(r.matched_title)}</td>
          <td>${esc(r.reference)}</td>
        </tr>
        `;
      })
      .join("");
  }

  function renderAll(data) {

    if (!data) return;

    CURRENT_DATA = normalizeData(data);

    if (el.resultsCard) el.resultsCard.style.display = "block";

    renderSummaryTable(CURRENT_DATA);

    /* ACII */
    renderACII(CURRENT_DATA);

    renderVerify(CURRENT_DATA);
  }

  async function runInitialCheck() {

    if (RUNNING) return;

    const f = el.file?.files?.[0];

    if (!f) {
      setStatus("Choose a file first", "warn");
      return;
    }

    RUNNING = true;

    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");

    const res = await fetch("/verify", { method: "POST", body: fd });

    const js = await res.json();

    LAST_JOB_ID = js.job_id;

    renderAll(js.data);

    RUNNING = false;
  }

  async function runOnlineVerification() {

    if (!LAST_JOB_ID) {
      setStatus("Run initial check first", "warn");
      return;
    }

    const fd = new FormData();
    fd.append("job_id", LAST_JOB_ID);

    await fetch("/verify-online", { method: "POST", body: fd });

    startPolling();
  }

  function startPolling() {

    if (POLL_TIMER) clearInterval(POLL_TIMER);

    POLL_TIMER = setInterval(async () => {

      const res = await fetch(
        `/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`
      );

      const js = await res.json();

      if (js.result) {
        renderAll(js.result);
      }

      if (js.online?.state === "done") {
        clearInterval(POLL_TIMER);
      }

    }, CONFIG.POLL_INTERVAL);
  }

  if (el.btnCheck) el.btnCheck.addEventListener("click", runInitialCheck);
  if (el.btnVerify) el.btnVerify.addEventListener("click", runOnlineVerification);

})();
