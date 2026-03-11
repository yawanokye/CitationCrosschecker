/* static/app.js — Citation Crosschecker Dashboard */

(() => {
  "use strict";

  const CONFIG = {
    POLL_INTERVAL: 1200,
    MAX_VERIFY_DISPLAY: 500
  };

  function toNum(x, d = 0) {
    const n = Number(x);
    return Number.isFinite(n) ? n : d;
  }

  function esc(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;");
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

    resultsCard: document.getElementById("resultsCard"),
    summaryTable: document.getElementById("summaryTable"),

    verifyDash: document.getElementById("verifyDash"),
    verifyBody: document.getElementById("verifyBody"),

    /* ACII */
    aciiCard: document.getElementById("aciiCard"),
    aciiValue: document.getElementById("aciiValue"),
    aciiV: document.getElementById("aciiV"),
    aciiC: document.getElementById("aciiC"),
    aciiA: document.getElementById("aciiA"),
    aciiT: document.getElementById("aciiT")
  };

  let LAST_JOB_ID = null;
  let POLL_TIMER = null;
  let CURRENT_DATA = null;

  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg || "";
  }

  /* ---------------------------------------------------
     Robust data normalisation (fixes 0 values problem)
  --------------------------------------------------- */

  function normalizeData(d) {

    const data = d && typeof d === "object" ? d : {};

    const missingArr = Array.isArray(data.missing_in_references)
      ? data.missing_in_references
      : [];

    const uncitedArr = Array.isArray(data.uncited_references)
      ? data.uncited_references
      : [];

    const s = data.summary || {};

    data.summary = {

      in_text_citations_found:
        toNum(
          s.in_text_citations_found ??
          s.intext_citations_found ??
          s.intext_count ??
          data.intext_count
        ),

      reference_entries_found:
        toNum(
          s.reference_entries_found ??
          s.references_found ??
          s.ref_count ??
          data.reference_entries_found
        ),

      missing_in_references:
        toNum(
          s.missing_in_references ??
          s.missing ??
          data.missing_in_references_count ??
          missingArr.length
        ),

      uncited_references:
        toNum(
          s.uncited_references ??
          data.uncited_references_count ??
          uncitedArr.length
        ),

      match_rate:
        toNum(
          s.match_rate ??
          data.match_rate
        )
    };

    return data;
  }

  /* ---------------------------------------------------
     SUMMARY TABLE
  --------------------------------------------------- */

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

  /* ---------------------------------------------------
     ACII DISPLAY
  --------------------------------------------------- */

  function renderACII(data) {

    const acii = data?.acii;

    if (!acii) return;

    if (el.aciiCard)
      el.aciiCard.style.display = "block";

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

  /* ---------------------------------------------------
     VERIFICATION DASHBOARD
  --------------------------------------------------- */

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

      el.verifyBody.innerHTML =
        `<tr><td colspan="8">No verification results</td></tr>`;

      return;
    }

    el.verifyBody.innerHTML = rows
      .slice(0, CONFIG.MAX_VERIFY_DISPLAY)
      .map(r => `
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
      `).join("");
  }

  /* ---------------------------------------------------
     MAIN RENDER
  --------------------------------------------------- */

  function renderAll(data) {

    if (!data) return;

    CURRENT_DATA = normalizeData(data);

    if (el.resultsCard)
      el.resultsCard.style.display = "block";

    renderSummaryTable(CURRENT_DATA);
    renderACII(CURRENT_DATA);
    renderVerify(CURRENT_DATA);
  }

  /* ---------------------------------------------------
     INITIAL CHECK
  --------------------------------------------------- */

  async function runInitialCheck() {

    const f = el.file?.files?.[0];

    if (!f) {
      setStatus("Please choose a file first", "warn");
      return;
    }

    setStatus("Analyzing document...");

    const fd = new FormData();

    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");

    const res = await fetch("/verify", {
      method: "POST",
      body: fd
    });

    const js = await res.json();

    LAST_JOB_ID = js.job_id;

    renderAll(js.data);

    setStatus("Analysis complete", "success");
  }

  /* ---------------------------------------------------
     ONLINE VERIFICATION
  --------------------------------------------------- */

  async function runOnlineVerification() {

    if (!LAST_JOB_ID) {
      setStatus("Run document check first", "warn");
      return;
    }

    setStatus("Starting online verification...");

    const fd = new FormData();

    fd.append("job_id", LAST_JOB_ID);

    await fetch("/verify-online", {
      method: "POST",
      body: fd
    });

    startPolling();
  }

  /* ---------------------------------------------------
     POLLING
  --------------------------------------------------- */

  function startPolling() {

    if (POLL_TIMER)
      clearInterval(POLL_TIMER);

    POLL_TIMER = setInterval(async () => {

      const res = await fetch(
        `/online/status?job_id=${encodeURIComponent(LAST_JOB_ID)}&include_result=1`
      );

      const js = await res.json();

      if (js.result)
        renderAll(js.result);

      if (js.online?.state === "done") {

        clearInterval(POLL_TIMER);

        setStatus("Online verification complete", "success");
      }

    }, CONFIG.POLL_INTERVAL);
  }

  /* ---------------------------------------------------
     BUTTON EVENTS
  --------------------------------------------------- */

  if (el.btnCheck)
    el.btnCheck.addEventListener("click", runInitialCheck);

  if (el.btnVerify)
    el.btnVerify.addEventListener("click", runOnlineVerification);

})();
