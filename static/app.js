/* static/app.js — Citation Crosschecker Dashboard */

(() => {
  "use strict";

  const CONFIG = {
    POLL_INTERVAL: 1200,
    MAX_VERIFY_DISPLAY: 500
  };

  const $ = (id) => document.getElementById(id);

  const el = {
    file: $("file"),
    style: $("style"),
    btnCheck: $("btnCheck"),
    btnVerify: $("btnVerify"),
    status: $("status"),

    resultsCard: $("resultsCard"),
    summaryTable: $("summaryTable"),
    refMsg: $("refMsg"),

    missingBody: $("missingBody"),
    uncitedBody: $("uncitedBody"),
    c2rBody: $("c2rBody"),
    r2cBody: $("r2cBody"),

    verifyDash: $("verifyDash"),
    verifyBody: $("verifyBody"),

    aciiCard: $("aciiCard"),
    aciiValue: $("aciiValue"),
    aciiInterpretation: $("aciiInterpretation"),

    aciiVScore: $("aciiVScore"),
    aciiVCategory: $("aciiVCategory"),
    aciiVRemark: $("aciiVRemark"),

    aciiCScore: $("aciiCScore"),
    aciiCCategory: $("aciiCCategory"),
    aciiCRemark: $("aciiCRemark"),

    aciiAScore: $("aciiAScore"),
    aciiACategory: $("aciiACategory"),
    aciiARemark: $("aciiARemark"),

    aciiTScore: $("aciiTScore"),
    aciiTCategory: $("aciiTCategory"),
    aciiTRemark: $("aciiTRemark")
  };

  let LAST_JOB_ID = null;
  let POLL_TIMER = null;
  let CURRENT_DATA = null;

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

  function setStatus(msg, tone = "muted") {
    if (!el.status) return;
    el.status.className = `status ${tone}`;
    el.status.textContent = msg || "";
  }

  /* -----------------------------------------
     ACII CATEGORY + REMARK LOGIC
  ----------------------------------------- */

  function category(score){

    if(score >= 90) return "Excellent";
    if(score >= 80) return "Very Good";
    if(score >= 70) return "Good";
    if(score >= 60) return "Moderate";
    if(score >= 50) return "Weak";

    return "Poor";
  }

  function interpretation(score){

    if(score >= 90) return "Excellent citation integrity";
    if(score >= 80) return "Very good citation integrity";
    if(score >= 70) return "Good citation integrity";
    if(score >= 60) return "Moderate citation integrity";
    if(score >= 50) return "Weak citation integrity";

    return "Poor citation integrity";
  }

  function remark(metric,score){

    if(metric==="verification")
      return `${score}% of references verified in scholarly databases`;

    if(metric==="concentration")
      return "Indicates how evenly citations are distributed";

    if(metric==="diversity")
      return "Reflects diversity of authors cited";

    if(metric==="temporal")
      return "Indicates spread of references across publication years";

    return "";
  }

  function badge(cat){
    const cls = cat.replace(" ","");
    return `<span class="badge ${cls}">${cat}</span>`;
  }

  /* -----------------------------------------
     TAB NAVIGATION
  ----------------------------------------- */

  const tabs = document.querySelectorAll(".tab");
  const panes = document.querySelectorAll(".tabPane");

  tabs.forEach(tab => {
    tab.addEventListener("click", () => {

      const target = tab.dataset.tab;

      tabs.forEach(t => t.classList.remove("active"));
      panes.forEach(p => p.classList.remove("active"));

      tab.classList.add("active");

      const pane = document.getElementById(target);
      if (pane) pane.classList.add("active");
    });
  });

  /* -----------------------------------------
     DATA NORMALISATION
  ----------------------------------------- */

  function normalizeData(d) {
    const data = d && typeof d === "object" ? d : {};
    const s = data.summary || {};

    data.summary = {
      in_text_citations_found: toNum(
        s.in_text_citations_found ??
        s.intext_count ??
        data.intext_count
      ),
      reference_entries_found: toNum(
        s.reference_entries_found ??
        s.ref_count ??
        data.reference_entries_found
      ),
      missing_in_references: toNum(
        s.missing_in_references ??
        data.missing_in_references_count ??
        (data.missing_in_references || []).length
      ),
      uncited_references: toNum(
        s.uncited_references ??
        data.uncited_references_count ??
        (data.uncited_references || []).length
      ),
      match_rate: toNum(
        s.match_rate ??
        data.match_rate
      )
    };

    return data;
  }

  /* -----------------------------------------
     SUMMARY
  ----------------------------------------- */

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

  /* -----------------------------------------
     ACII RENDER
  ----------------------------------------- */

  function renderACII(data){

    const acii = data?.acii;
    if (!acii) return;

    if (el.aciiCard) el.aciiCard.style.display = "block";

    const score = acii.ACII ?? "--";

    if (el.aciiValue) el.aciiValue.textContent = score;

    if (el.aciiInterpretation)
      el.aciiInterpretation.textContent = interpretation(score);

    const c = acii.components || {};

    function setRow(scoreEl,catEl,remarkEl,metric,val){

      const s = toNum(val);
      const cat = category(s);

      if(scoreEl) scoreEl.textContent = s;
      if(catEl) catEl.innerHTML = badge(cat);
      if(remarkEl) remarkEl.textContent = remark(metric,s);

    }

    setRow(el.aciiVScore,el.aciiVCategory,el.aciiVRemark,"verification",c.verification_integrity);
    setRow(el.aciiCScore,el.aciiCCategory,el.aciiCRemark,"concentration",c.citation_concentration);
    setRow(el.aciiAScore,el.aciiACategory,el.aciiARemark,"diversity",c.author_diversity);
    setRow(el.aciiTScore,el.aciiTCategory,el.aciiTRemark,"temporal",c.temporal_balance);
  }

  /* -----------------------------------------
     MISSING
  ----------------------------------------- */

  function renderMissing(data) {

    const rows = data?.missing_in_references || [];

    if (!el.missingBody) return;

    if (!rows.length) {
      el.missingBody.innerHTML = `<tr><td colspan="3">None</td></tr>`;
      return;
    }

    el.missingBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(r.citation_in_text || r.citation || r)}</td>
        <td>${esc(r.count_in_text || r.count || "")}</td>
      </tr>
    `).join("");
  }

  /* -----------------------------------------
     UNCITED
  ----------------------------------------- */

  function renderUncited(data) {

    const rows = data?.uncited_references || [];

    if (!el.uncitedBody) return;

    if (!rows.length) {
      el.uncitedBody.innerHTML = `<tr><td colspan="2">None</td></tr>`;
      return;
    }

    el.uncitedBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(r.reference || r)}</td>
      </tr>
    `).join("");
  }

  /* -----------------------------------------
     MAPPINGS
  ----------------------------------------- */

  function renderC2R(data) {

    const rows = data?.reconciliation_intext_to_reference || data?.c2r_map || [];

    if (!el.c2rBody) return;

    if (!rows.length) {
      el.c2rBody.innerHTML = `<tr><td colspan="5">No mapping available</td></tr>`;
      return;
    }

    el.c2rBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(r.status || "")}</td>
        <td>${esc(r.in_text || "")}</td>
        <td>${esc(r.matched_reference || r.reference || "")}</td>
        <td>${esc(r.flags || "")}</td>
      </tr>
    `).join("");
  }

  function renderR2C(data) {

    const rows = data?.reconciliation_reference_to_intext || data?.r2c_map || [];

    if (!el.r2cBody) return;

    if (!rows.length) {
      el.r2cBody.innerHTML = `<tr><td colspan="4">No mapping available</td></tr>`;
      return;
    }

    el.r2cBody.innerHTML = rows.map((r, i) => `
      <tr>
        <td>${i + 1}</td>
        <td>${esc(r.times_cited ?? r.count ?? 0)}</td>
        <td>${esc(r.reference || "")}</td>
        <td>${esc((r.cited_by || []).slice(0, 3).join("; "))}</td>
      </tr>
    `).join("");
  }

  /* -----------------------------------------
     ONLINE VERIFICATION
  ----------------------------------------- */

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

    if (!el.verifyBody) return;

    if (!rows.length) {
      el.verifyBody.innerHTML = `<tr><td colspan="9">No verification results</td></tr>`;
      return;
    }

    el.verifyBody.innerHTML = rows
      .slice(0, CONFIG.MAX_VERIFY_DISPLAY)
      .map((r, i) => `
        <tr>
          <td>${i + 1}</td>
          <td>${esc(r.status || "")}</td>
          <td>${esc(r.source || "")}</td>
          <td>${esc(r.score ?? "")}</td>
          <td>${esc(r.doi || "")}</td>
          <td>${esc(r.matched_year || "")}</td>
          <td>${esc(r.author || r.matched_authors || "")}</td>
          <td>${esc(r.matched_title || "")}</td>
          <td>${esc(r.query_used || "")}</td>
        </tr>
      `).join("");
  }

  /* -----------------------------------------
     MAIN RENDER
  ----------------------------------------- */

  function renderAll(data){

    if (!data) return;

    CURRENT_DATA = normalizeData(data);

    if (el.resultsCard) el.resultsCard.style.display = "block";

    renderSummaryTable(CURRENT_DATA);
    renderACII(CURRENT_DATA);
    renderMissing(CURRENT_DATA);
    renderUncited(CURRENT_DATA);
    renderC2R(CURRENT_DATA);
    renderR2C(CURRENT_DATA);
    renderVerify(CURRENT_DATA);

    if (el.refMsg && data.reference_detection_message)
      el.refMsg.textContent = data.reference_detection_message;
  }

  /* -----------------------------------------
     BUTTONS
  ----------------------------------------- */

  el.btnCheck?.addEventListener("click", runInitialCheck);
  el.btnVerify?.addEventListener("click", runOnlineVerification);

})();
