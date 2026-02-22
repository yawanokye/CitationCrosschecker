// static/app.js
let lastResult = null;

function $(id){ return document.getElementById(id); }

function setStatus(text, kind="muted"){
  const el = $("status");
  el.textContent = text || "";
  el.className = `status ${kind}`;
}

function escapeHtml(str){
  return String(str ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

function badge(status){
  const s = (status || "").toLowerCase();
  let cls = "chip";
  if (s.includes("matched") || s === "verified") cls = "chip good";
  else if (s.includes("ambiguous") || s === "likely" || s === "needs_review") cls = "chip warn";
  else if (s.includes("not_found") || s.includes("offline")) cls = "chip bad";
  return `<span class="${cls}">${escapeHtml(status || "")}</span>`;
}

function showResults(){
  $("resultsCard").style.display = "block";
  $("btnExportCsvTop").disabled = false;
  $("btnExportWordTop").disabled = false;
}

function renderDashboard(ui){
  const d = ui.dashboard || {};
  $("dash").innerHTML = `
    <div class="metric"><div class="k">In-text</div><div class="v">${d.in_text_citations_found ?? 0}</div></div>
    <div class="metric"><div class="k">References</div><div class="v">${d.reference_entries_found ?? 0}</div></div>
    <div class="metric"><div class="k">Missing</div><div class="v">${d.missing_in_references ?? 0}</div></div>
    <div class="metric"><div class="k">Uncited</div><div class="v">${d.uncited_references ?? 0}</div></div>
    <div class="metric"><div class="k">Match rate</div><div class="v">${d.match_rate_pct ?? 0}%</div></div>
  `;
}

function renderSummary(result, ui){
  const msg = result.reference_detection_message || "";
  const d = ui.dashboard || {};
  $("summary").innerHTML = `
    <div class="muted">${escapeHtml(msg)}</div>
    <div class="muted">Generated: ${escapeHtml(d.timestamp || "")}</div>
  `;
}

function renderMissing(ui){
  const rows = ui.missing_rows || [];
  $("missingBody").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(r.citation_in_text)}</td>
        <td>${escapeHtml(String(r.count_in_text ?? ""))}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="3" class="muted">No missing citations.</td></tr>`;
}

function renderUncited(ui){
  const rows = ui.uncited_rows || [];
  $("uncitedBody").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(r.reference)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="2" class="muted">No uncited references.</td></tr>`;
}

function renderC2R(ui){
  const rows = ui.c2r_rows || [];
  $("c2rBody").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${badge(r.status)}</td>
        <td>${escapeHtml(r.in_text)}</td>
        <td>${escapeHtml(r.matched_reference)}</td>
        <td>${escapeHtml(r.flags)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="5" class="muted">No reconciliation rows.</td></tr>`;
}

function renderR2C(ui){
  const rows = ui.r2c_rows || [];
  $("r2cBody").innerHTML = rows.length
    ? rows.map(r => `
      <tr>
        <td>${escapeHtml(r.no)}</td>
        <td>${escapeHtml(String(r.times_cited ?? 0))}</td>
        <td>${escapeHtml(r.reference)}</td>
        <td>${escapeHtml(r.cited_by)}</td>
      </tr>
    `).join("")
    : `<tr><td colspan="4" class="muted">No rows.</td></tr>`;
}

function renderVerify(result, ui){
  const ov = result.online_verification || {summary:{}, rows:[]};
  const sum = ov.summary || {};
  const rows = ui.verify_rows || [];

  $("verifyDash").innerHTML = `
    <div class="metric"><div class="k">Verified</div><div class="v">${sum.verified ?? 0}</div></div>
    <div class="metric"><div class="k">Likely</div><div class="v">${sum.likely ?? 0}</div></div>
    <div class="metric"><div class="k">Review</div><div class="v">${sum.needs_review ?? 0}</div></div>
    <div class="metric"><div class="k">Not found</div><div class="v">${sum.not_found ?? 0}</div></div>
    <div class="metric"><div class="k">Offline</div><div class="v">${sum.offline ?? 0}</div></div>
  `;

  $("verifyBody").innerHTML = rows.length
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

function setActiveTab(tabId){
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === tabId));
  document.querySelectorAll(".tabPane").forEach(p => p.classList.toggle("active", p.id === tabId));
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  setActiveTab(tab.dataset.tab);
});

async function postForm(endpoint, formData){
  let res;
  try{
    res = await fetch(endpoint, { method:"POST", body: formData });
  }catch(err){
    return { ok:false, data:{ error: "Network error. Check your internet or the server is restarting." } };
  }

  const ct = (res.headers.get("content-type") || "").toLowerCase();

  // JSON
  if (ct.includes("application/json")){
    let data = null;
    try{
      data = await res.json();
    }catch(e){
      data = { error: "Server returned invalid JSON." };
    }

    // FastAPI validation errors: {detail:[...]}
    if (!res.ok && res.status === 422 && data && data.detail){
      const msg = Array.isArray(data.detail)
        ? data.detail.map(d => `${(d.loc || []).slice(-1)[0] || "field"}: ${d.msg}`).join(" | ")
        : "Validation error (422).";
      return { ok:false, data:{ error: msg } };
    }

    if (!res.ok) return { ok:false, data };
    if (data && data.error) return { ok:false, data };
    return { ok:true, data };
  }

  // HTML / text (Render 502 pages)
  const text = await res.text();

  let msg = "Server error.";
  if (res.status === 502) msg = "Server error (502). The server likely restarted (memory) or timed out. Use batch mode or reduce Max Verify.";
  else if (res.status === 503) msg = "Server unavailable (503). The app may be restarting (cold start). Try again in 10–20 seconds.";
  else if (res.status === 504) msg = "Gateway timeout (504). Use batch mode or reduce Max Verify.";
  else if (res.status === 413) msg = "File too large (413). Upload a smaller file.";
  else if (res.status === 429) msg = "Too many requests (429). Increase throttle and retry.";

  const hint = (text || "").replace(/\s+/g, " ").trim().slice(0, 120);
  if (hint) msg += `\n\nHint: ${hint}`;

  return { ok:false, data:{ error: msg } };
}

function lockButtons(lock){
  $("btnCheck").disabled = lock;
  $("btnVerify").disabled = lock;
  $("btnExportCsvTop").disabled = lock || !lastResult;
  $("btnExportWordTop").disabled = lock || !lastResult;
}

function requireFile(){
  const f = $("file").files?.[0];
  if (!f){
    setStatus("Choose a file first.", "warn");
    return null;
  }
  return f;
}

async function runCheck(){
  const f = requireFile();
  if (!f) return;

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  lockButtons(true);
  setStatus("Running check…", "muted");

  const { ok, data } = await postForm("/check", form);

  lockButtons(false);

  if (!ok){
    setStatus("Check failed.", "bad");
    alert(data?.error || "Server error");
    return;
  }

  lastResult = data;
  showResults();

  const ui = data._ui || {};
  renderDashboard(ui);
  renderSummary(data, ui);
  renderMissing(ui);
  renderUncited(ui);
  renderC2R(ui);
  renderR2C(ui);

  setActiveTab("summaryPane");
  setStatus("Check complete.", "good");
}

async function runVerify(){
  const f = requireFile();
  if (!f) return;

  const style = $("style").value;
  const verifyMode = $("verifyMode").value;
  const useCrossref = $("useCrossref").checked ? "true" : "false";
  const useOpenAlex = $("useOpenAlex").checked ? "true" : "false";
  const throttle = String(parseFloat($("throttle").value || "0"));

  let mvInput = parseInt($("maxVerify").value || "0", 10);
  const verifyAll = (!mvInput || mvInput <= 0);

  const batchSize = verifyAll ? 60 : mvInput;

  let offset = 0;
  let mergedVerifyRows = [];
  let mergedCounts = { verified:0, likely:0, needs_review:0, not_found:0, offline:0 };
  let totalSelected = null;

  lockButtons(true);
  setStatus(verifyAll ? "Verifying all references in batches…" : "Running online verification…", "muted");

  // Ensure base check exists
  let baseResult = lastResult;
  if (!baseResult){
    const checkForm = new FormData();
    checkForm.append("file", f);
    checkForm.append("style", style);

    const c = await postForm("/check", checkForm);
    if (!c.ok){
      lockButtons(false);
      setStatus("Check failed.", "bad");
      alert(c.data?.error || "Server error");
      return;
    }
    baseResult = c.data;
    lastResult = baseResult;
    showResults();
  }

  while (true){
    const form = new FormData();
    form.append("file", f);
    form.append("style", style);
    form.append("verify_mode", verifyMode);
    form.append("use_crossref", useCrossref);
    form.append("use_openalex", useOpenAlex);
    form.append("throttle_s", throttle);
    form.append("max_verify", String(batchSize));
    form.append("offset", String(offset));

    const { ok, data } = await postForm("/verify", form);

    if (!ok){
      lockButtons(false);
      setStatus("Online verification failed.", "bad");
      alert(data?.error || "Server error");
      return;
    }

    const ov = data.online_verification || {};
    totalSelected = (ov.total_selected ?? totalSelected);
    const nextOffset = ov.next_offset;

    const sum = (ov.summary || {});
    mergedCounts.verified += (sum.verified || 0);
    mergedCounts.likely += (sum.likely || 0);
    mergedCounts.needs_review += (sum.needs_review || 0);
    mergedCounts.not_found += (sum.not_found || 0);
    mergedCounts.offline += (sum.offline || 0);

    const ui = data._ui || {};
    const batchRows = (ui.verify_rows || []);
    mergedVerifyRows = mergedVerifyRows.concat(batchRows.map(r => ({...r})));

    const done = (nextOffset === null || nextOffset === undefined);
    const doneCount = offset + (ov.batch_size || batchRows.length || 0);
    const total = (totalSelected ?? doneCount);
    setStatus(`Verified ${Math.min(doneCount, total)}/${total} references…`, "muted");

    if (done){
      const finalResult = baseResult;

      finalResult.online_verification = {
        summary: { ...mergedCounts, total: mergedCounts.verified + mergedCounts.likely + mergedCounts.needs_review + mergedCounts.not_found + mergedCounts.offline },
        total_selected: totalSelected ?? mergedVerifyRows.length,
        offset: 0,
        batch_size: mergedVerifyRows.length,
        next_offset: null,
      };

      finalResult._ui = finalResult._ui || {};
      finalResult._ui.verify_rows = mergedVerifyRows.map((r, i) => ({...r, no: (i + 1)}));
      finalResult._ui.verify_total = mergedVerifyRows.length;
      finalResult._ui.verify_truncated = false;

      lastResult = finalResult;

      showResults();
      renderDashboard(finalResult._ui);
      renderSummary(finalResult, finalResult._ui);
      renderMissing(finalResult._ui);
      renderUncited(finalResult._ui);
      renderC2R(finalResult._ui);
      renderR2C(finalResult._ui);
      renderVerify(finalResult, finalResult._ui);

      setActiveTab("verifyPane");
      setStatus("Online verification complete.", "good");
      lockButtons(false);
      return;
    }

    offset = nextOffset;
  }
}

async function downloadFromEndpoint(endpoint){
  const f = requireFile();
  if (!f) return;

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  const res = await fetch(endpoint, { method:"POST", body: form });
  if (!res.ok){
    const t = await res.text();
    alert((t || "").slice(0, 250) || "Download failed");
    return;
  }

  const blob = await res.blob();
  const cd = res.headers.get("content-disposition") || "";
  let filename = "";
  const m = cd.match(/filename="([^"]+)"/);
  if (m) filename = m[1];
  if (!filename){
    filename = endpoint.includes("csv") ? "citation_report.csv" : "citation_report.docx";
  }

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

$("btnCheck").addEventListener("click", runCheck);
$("btnVerify").addEventListener("click", runVerify);

$("btnExportCsvTop").addEventListener("click", () => downloadFromEndpoint("/export/csv"));
$("btnExportWordTop").addEventListener("click", () => downloadFromEndpoint("/export/word"));

setActiveTab("summaryPane");
setStatus("Ready.", "muted");
