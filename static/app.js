// static/app.js
let lastResult = null;

function $(id) { return document.getElementById(id); }

function setStatus(text, kind = "muted") {
  const el = $("status");
  if (!el) return;
  el.textContent = text || "";
  el.className = `status ${kind}`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function badge(status) {
  const s = (status || "").toLowerCase();
  let cls = "chip";
  if (s.includes("matched") || s === "verified") cls = "chip good";
  else if (s.includes("ambiguous") || s === "likely" || s === "needs_review") cls = "chip warn";
  else if (s.includes("not_found") || s.includes("offline")) cls = "chip bad";
  return `<span class="${cls}">${escapeHtml(status || "")}</span>`;
}

function showResults() {
  const card = $("resultsCard");
  if (card) card.style.display = "block";
  if ($("btnExportCsvTop")) $("btnExportCsvTop").disabled = false;
  if ($("btnExportWordTop")) $("btnExportWordTop").disabled = false;
}

function lockButtons(lock) {
  if ($("btnCheck")) $("btnCheck").disabled = lock;
  if ($("btnVerify")) $("btnVerify").disabled = lock;
  if ($("btnExportCsvTop")) $("btnExportCsvTop").disabled = lock || !lastResult;
  if ($("btnExportWordTop")) $("btnExportWordTop").disabled = lock || !lastResult;
}

function renderDashboard(ui) {
  const d = (ui && ui.dashboard) ? ui.dashboard : {};
  const dash = $("dash");
  if (!dash) return;

  dash.innerHTML = `
    <div class="metric"><div class="k">In-text</div><div class="v">${d.in_text_citations_found ?? 0}</div></div>
    <div class="metric"><div class="k">References</div><div class="v">${d.reference_entries_found ?? 0}</div></div>
    <div class="metric"><div class="k">Missing</div><div class="v">${d.missing_in_references ?? 0}</div></div>
    <div class="metric"><div class="k">Uncited</div><div class="v">${d.uncited_references ?? 0}</div></div>
    <div class="metric"><div class="k">Match rate</div><div class="v">${d.match_rate_pct ?? 0}%</div></div>
  `;
}

function renderSummary(result) {
  const s = (result && result.summary) ? result.summary : {};
  const tbl = $("summaryTable");
  if (!tbl) return;

  const items = [
    ["Filename", result.filename || ""],
    ["Style", result.style || ""],
    ["Text length", result.text_length ?? ""],
    ["Main text length", result.main_text_length ?? ""],
    ["References detected (raw)", result.references_detected ?? ""],
    ["In-text citations found", s.in_text_citations_found ?? 0],
    ["Reference entries found", s.reference_entries_found ?? 0],
    ["Missing in references", s.missing_in_references ?? 0],
    ["Uncited references", s.uncited_references ?? 0],
    ["Elapsed (s)", result.elapsed_seconds ?? ""],
    ["Max verify used", result.max_verify_used ?? ""],
  ].filter(([_, v]) => v !== "" && v !== null && v !== undefined);

  tbl.innerHTML = items.map(([k, v]) => `
    <tr>
      <td class="kcol">${escapeHtml(k)}</td>
      <td>${escapeHtml(String(v ?? ""))}</td>
    </tr>
  `).join("");

  const refMsg = $("refMsg");
  if (refMsg) refMsg.textContent = result.reference_detection_message || "";
}

function renderMissing(ui) {
  const body = $("missingBody");
  if (!body) return;

  const rows = (ui && ui.missing_rows) ? ui.missing_rows : [];
  body.innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(r.citation_in_text)}</td>
        <td>${escapeHtml(r.count_in_text)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="3" class="muted">No missing items.</td></tr>`;
}

function renderUncited(ui) {
  const body = $("uncitedBody");
  if (!body) return;

  const rows = (ui && ui.uncited_rows) ? ui.uncited_rows : [];
  body.innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(r.reference)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="2" class="muted">No uncited references.</td></tr>`;
}

function renderC2R(ui) {
  const body = $("c2rBody");
  if (!body) return;

  const rows = (ui && ui.c2r_rows) ? ui.c2r_rows : [];
  body.innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${badge(r.status)}</td>
        <td>${escapeHtml(r.in_text)}</td>
        <td>${escapeHtml(r.matched_reference)}</td>
        <td>${escapeHtml(r.flags || "")}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="5" class="muted">No rows.</td></tr>`;
}

function renderR2C(ui) {
  const body = $("r2cBody");
  if (!body) return;

  const rows = (ui && ui.r2c_rows) ? ui.r2c_rows : [];
  body.innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(String(r.times_cited ?? 0))}</td>
        <td>${escapeHtml(r.reference)}</td>
        <td>${escapeHtml(r.cited_by || "")}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="4" class="muted">No rows.</td></tr>`;
}

function renderVerify(result, ui) {
  const ov = (result && result.online_verification) ? result.online_verification : { summary: {}, rows: [] };
  const sum = (ov && ov.summary) ? ov.summary : {};
  const rows = (ui && ui.verify_rows) ? ui.verify_rows : [];

  const dash = $("verifyDash");
  if (dash) {
    dash.innerHTML = `
      <div class="metric"><div class="k">Verified</div><div class="v">${sum.verified ?? 0}</div></div>
      <div class="metric"><div class="k">Likely</div><div class="v">${sum.likely ?? 0}</div></div>
      <div class="metric"><div class="k">Review</div><div class="v">${sum.needs_review ?? 0}</div></div>
      <div class="metric"><div class="k">Not found</div><div class="v">${sum.not_found ?? 0}</div></div>
      <div class="metric"><div class="k">Offline</div><div class="v">${sum.offline ?? 0}</div></div>
    `;
  }

  const body = $("verifyBody");
  if (!body) return;

  body.innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${badge(r.status)}</td>
        <td>${escapeHtml(r.source)}</td>
        <td>${escapeHtml(String(r.score ?? ""))}</td>
        <td>${escapeHtml(r.doi)}</td>
        <td>${escapeHtml(String(r.matched_year ?? ""))}</td>
        <td>${escapeHtml(r.matched_authors ?? "")}</td>
        <td>${escapeHtml(r.matched_title ?? "")}</td>
        <td>${escapeHtml(r.query_used ?? "")}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="9" class="muted">No online verification results yet.</td></tr>`;
}

function setActiveTab(tabId) {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tabId));
  document.querySelectorAll(".tabPane").forEach(p => p.classList.toggle("active", p.id === tabId));
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  setActiveTab(tab.dataset.tab);
});

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function postForm(endpoint, formData) {
  const maxAttempts = 4;

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    let res;

    try {
      res = await fetch(endpoint, { method: "POST", body: formData });
    } catch (err) {
      if (attempt === maxAttempts) {
        return { ok: false, data: { error: "Network error. Check your internet or the server is restarting." } };
      }
      await sleep(350 * attempt);
      continue;
    }

    const ct = (res.headers.get("content-type") || "").toLowerCase();

    if (ct.includes("application/json")) {
      let data = null;
      try { data = await res.json(); } catch (e) { data = { error: "Server returned invalid JSON." }; }
      if (!res.ok) return { ok: false, data };
      if (data && data.error) return { ok: false, data };
      return { ok: true, data };
    }

    // Retry common Render wake-up / gateway issues
    const isRetryable = [502, 503, 504].includes(res.status);
    if (isRetryable && attempt < maxAttempts) {
      setStatus(`Server waking up, retrying (${attempt}/${maxAttempts})…`, "muted");
      await sleep(650 * attempt);
      continue;
    }

    let text = "";
    try { text = await res.text(); } catch (_) { text = ""; }

    let msg = "Server error.";
    if (res.status === 502) msg = "Server timeout (502). Reduce Max Verify and try again.";
    else if (res.status === 503) msg = "Server unavailable (503). The app may be restarting.";
    else if (res.status === 504) msg = "Gateway timeout (504). Reduce Max Verify and try again.";
    else if (res.status === 413) msg = "File too large (413). Upload a smaller file.";
    else if (res.status === 429) msg = "Too many requests (429). Increase throttle and retry.";
    else if (res.status >= 500) msg = `Server error (${res.status}). Check Render logs.`;

    const short = (text || "").replace(/\s+/g, " ").trim();
    if (short && short.length < 220 && !short.toLowerCase().includes("<html")) msg = short;

    return { ok: false, data: { error: msg } };
  }

  return { ok: false, data: { error: "Request failed." } };
}

function requireFile() {
  const inp = $("file");
  const f = inp ? inp.files[0] : null;
  if (!f) {
    alert("Select a DOCX or PDF first.");
    return null;
  }
  return f;
}

async function runCheck() {
  const f = requireFile();
  if (!f) return;

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  lockButtons(true);
  setStatus("Running check…", "muted");

  const resp = await postForm("/check", form);
  lockButtons(false);

  if (!resp.ok) {
    setStatus(resp.data?.error || "Check failed.", "bad");
    return;
  }

  lastResult = resp.data;
  const ui = lastResult._ui || {};

  renderDashboard(ui);
  renderSummary(lastResult);
  renderMissing(ui);
  renderUncited(ui);
  renderC2R(ui);
  renderR2C(ui);
  renderVerify(lastResult, ui);

  showResults();
  setStatus("Done.", "good");
}

async function runVerify() {
  const f = requireFile();
  if (!f) return;

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);
  form.append("verify_mode", $("verifyMode").value);
  form.append("use_crossref", $("useCrossref").checked ? "true" : "false");
  form.append("use_openalex", $("useOpenalex").checked ? "true" : "false");
  form.append("throttle_s", $("throttle").value);
  form.append("max_verify", $("maxVerify").value);

  lockButtons(true);
  setStatus("Running online verification…", "muted");

  const resp = await postForm("/verify", form);
  lockButtons(false);

  if (!resp.ok) {
    setStatus(resp.data?.error || "Online verification failed.", "bad");
    return;
  }

  lastResult = resp.data;
  const ui = lastResult._ui || {};

  renderDashboard(ui);
  renderSummary(lastResult);
  renderMissing(ui);
  renderUncited(ui);
  renderC2R(ui);
  renderR2C(ui);
  renderVerify(lastResult, ui);

  showResults();
  setStatus("Online verification complete.", "good");
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

async function exportCsv() {
  const f = requireFile();
  if (!f) return;

  // Export includes online verification when it exists in lastResult
  const includeVerify = !!(lastResult && lastResult.online_verification && (lastResult.online_verification.summary?.total ?? 0) > 0);

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  if (includeVerify) {
    form.append("verify_online", "true");
    form.append("verify_mode", $("verifyMode").value);
    form.append("use_crossref", $("useCrossref").checked ? "true" : "false");
    form.append("use_openalex", $("useOpenalex").checked ? "true" : "false");
    form.append("throttle_s", $("throttle").value);
    form.append("max_verify", $("maxVerify").value);
  } else {
    form.append("verify_online", "false");
  }

  setStatus(includeVerify ? "Exporting CSV with verification…" : "Exporting CSV…", "muted");
  const res = await fetch("/export/csv", { method: "POST", body: form });
  if (!res.ok) {
    setStatus("Export failed.", "bad");
    return;
  }
  const blob = await res.blob();
  downloadBlob(blob, "citation_report.csv");
  setStatus("CSV exported.", "good");
}

async function exportWord() {
  const f = requireFile();
  if (!f) return;

  const includeVerify = !!(lastResult && lastResult.online_verification && (lastResult.online_verification.summary?.total ?? 0) > 0);

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  if (includeVerify) {
    form.append("verify_online", "true");
    form.append("verify_mode", $("verifyMode").value);
    form.append("use_crossref", $("useCrossref").checked ? "true" : "false");
    form.append("use_openalex", $("useOpenalex").checked ? "true" : "false");
    form.append("throttle_s", $("throttle").value);
    form.append("max_verify", $("maxVerify").value);
  } else {
    form.append("verify_online", "false");
  }

  setStatus(includeVerify ? "Exporting Word with verification…" : "Exporting Word…", "muted");
  const res = await fetch("/export/word", { method: "POST", body: form });
  if (!res.ok) {
    setStatus("Export failed.", "bad");
    return;
  }
  const blob = await res.blob();
  downloadBlob(blob, "citation_report.docx");
  setStatus("Word exported.", "good");
}

(function init() {
  const btnCheck = $("btnCheck");
  const btnVerify = $("btnVerify");
  const btnCsv = $("btnExportCsvTop");
  const btnWord = $("btnExportWordTop");

  if (btnCheck) btnCheck.addEventListener("click", runCheck);
  if (btnVerify) btnVerify.addEventListener("click", runVerify);
  if (btnCsv) btnCsv.addEventListener("click", exportCsv);
  if (btnWord) btnWord.addEventListener("click", exportWord);

  setStatus("Ready.", "muted");
})();
