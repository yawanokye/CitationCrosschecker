let lastResult = null;

function $(id){ return document.getElementById(id); }

function setStatus(text){
  $("status").textContent = text || "";
}

function showSpinner(on){
  $("spinner").style.display = on ? "inline-block" : "none";
}

function badge(status){
  const s = (status || "").toLowerCase();
  let cls = "";
  if (s.includes("matched") || s === "verified") cls = "good";
  else if (s.includes("ambiguous") || s === "likely" || s === "needs_review") cls = "warn";
  else if (s.includes("not_found") || s.includes("offline")) cls = "bad";
  return `<span class="badge ${cls}">${escapeHtml(status || "")}</span>`;
}

function escapeHtml(str){
  return String(str ?? "")
    .replaceAll("&","&amp;")
    .replaceAll("<","&lt;")
    .replaceAll(">","&gt;")
    .replaceAll('"',"&quot;")
    .replaceAll("'","&#039;");
}

function showResults(){
  $("resultsCard").style.display = "block";
  $("btnExportCsvTop").disabled = false;
  $("btnExportWordTop").disabled = false;
}

function renderDashboard(ui){
  const d = ui.dashboard || {};
  $("dash").innerHTML = `
    <div class="metric"><div class="k">In-text citations</div><div class="v">${d.in_text_citations_found ?? 0}</div></div>
    <div class="metric"><div class="k">Reference entries</div><div class="v">${d.reference_entries_found ?? 0}</div></div>
    <div class="metric"><div class="k">Missing in references</div><div class="v">${d.missing_in_references ?? 0}</div></div>
    <div class="metric"><div class="k">Uncited references</div><div class="v">${d.uncited_references ?? 0}</div></div>
    <div class="metric"><div class="k">Match rate</div><div class="v">${d.match_rate_pct ?? 0}%</div></div>
  `;
}

function renderSummary(result, ui){
  const s = result.summary || {};
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
  ];
  $("summaryTable").innerHTML = items.map(([k,v]) => `
    <tr>
      <td style="width:260px;color:var(--muted)">${escapeHtml(k)}</td>
      <td>${escapeHtml(String(v ?? ""))}</td>
    </tr>
  `).join("");

  $("refMsg").textContent = result.reference_detection_message || "";
}

function renderMissing(ui){
  const rows = ui.missing_rows || [];
  if (!rows.length){
    $("missingBody").innerHTML = `<tr><td colspan="3" class="muted">No missing items.</td></tr>`;
    return;
  }
  $("missingBody").innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.no)}</td>
      <td>${escapeHtml(r.count_in_text)}</td>
      <td>${escapeHtml(r.citation_in_text)}</td>
    </tr>
  `).join("");
}

function renderUncited(ui){
  const rows = ui.uncited_rows || [];
  if (!rows.length){
    $("uncitedBody").innerHTML = `<tr><td colspan="2" class="muted">No uncited references.</td></tr>`;
    return;
  }
  $("uncitedBody").innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.no)}</td>
      <td>${escapeHtml(r.reference)}</td>
    </tr>
  `).join("");
}

function renderC2R(ui){
  const rows = ui.c2r_rows || [];
  if (!rows.length){
    $("c2rBody").innerHTML = `<tr><td colspan="5" class="muted">No rows.</td></tr>`;
    return;
  }
  $("c2rBody").innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.no)}</td>
      <td>${badge(r.status)}</td>
      <td>${escapeHtml(r.in_text)}</td>
      <td>${escapeHtml(r.matched_reference)}</td>
      <td class="muted">${escapeHtml(r.flags || "")}</td>
    </tr>
  `).join("");
}

function renderR2C(ui){
  const rows = ui.r2c_rows || [];
  if (!rows.length){
    $("r2cBody").innerHTML = `<tr><td colspan="4" class="muted">No rows.</td></tr>`;
    return;
  }
  $("r2cBody").innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.no)}</td>
      <td>${escapeHtml(String(r.times_cited ?? 0))}</td>
      <td>${escapeHtml(r.reference)}</td>
      <td class="muted">${escapeHtml(r.cited_by || "")}</td>
    </tr>
  `).join("");
}

function renderVerify(result, ui){
  const ov = result.online_verification || {summary:{}, rows:[]};
  const sum = ov.summary || {};
  const rows = ui.verify_rows || [];

  $("verifyDash").innerHTML = `
    <div class="metric"><div class="k">Verified</div><div class="v">${sum.verified ?? 0}</div></div>
    <div class="metric"><div class="k">Likely</div><div class="v">${sum.likely ?? 0}</div></div>
    <div class="metric"><div class="k">Needs review</div><div class="v">${sum.needs_review ?? 0}</div></div>
    <div class="metric"><div class="k">Not found</div><div class="v">${sum.not_found ?? 0}</div></div>
    <div class="metric"><div class="k">Offline</div><div class="v">${sum.offline ?? 0}</div></div>
  `;

  if (!rows.length){
    $("verifyBody").innerHTML = `<tr><td colspan="10" class="muted">No online verification results yet.</td></tr>`;
    return;
  }

  $("verifyBody").innerHTML = rows.map(r => `
    <tr>
      <td>${escapeHtml(r.no)}</td>
      <td>${badge(r.status)}</td>
      <td class="muted">${escapeHtml(r.source)}</td>
      <td>${escapeHtml(String(r.score ?? ""))}</td>
      <td class="muted">${escapeHtml(r.doi)}</td>
      <td class="muted">${escapeHtml(String(r.matched_year ?? ""))}</td>
      <td class="muted">${escapeHtml(r.matched_authors ?? "")}</td>
      <td>${escapeHtml(r.matched_title ?? "")}</td>
      <td class="muted">${escapeHtml(r.query_used ?? "")}</td>
      <td class="muted">${escapeHtml(r.error ?? "")}</td>
    </tr>
  `).join("");
}

function setActiveTab(tabId){
  document.querySelectorAll(".tab").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === tabId);
  });
  document.querySelectorAll(".tabPane").forEach(p => {
    p.classList.toggle("active", p.id === tabId);
  });
}

document.addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  setActiveTab(tab.dataset.tab);
});

async function postForm(endpoint, formData){
  const res = await fetch(endpoint, { method:"POST", body: formData });
  const ct = res.headers.get("content-type") || "";
  let data = null;

  if (ct.includes("application/json")) data = await res.json();
  else data = { error: await res.text() };

  if (!res.ok) return { ok:false, data };
  if (data && data.error) return { ok:false, data };
  return { ok:true, data };
}

function requireFile(){
  const f = $("file").files[0];
  if (!f){
    alert("Select a DOCX or PDF first.");
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

  $("btnCheck").disabled = true;
  $("btnVerify").disabled = true;
  showSpinner(true);
  setStatus("Running check...");

  const { ok, data } = await postForm("/check", form);

  $("btnCheck").disabled = false;
  $("btnVerify").disabled = false;
  showSpinner(false);

  if (!ok){
    setStatus("Error.");
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
  renderVerify(data, ui);

  setActiveTab("summaryPane");
  setStatus("Check complete.");
}

async function runVerify(){
  const f = requireFile();
  if (!f) return;

  const form = new FormData();
  form.append("file", f);
  form.append("style", $("style").value);

  form.append("verify_mode", $("verifyMode").value);
  form.append("use_crossref", $("useCrossref").checked ? "true" : "false");
  form.append("use_openalex", $("useOpenAlex").checked ? "true" : "false");
  form.append("throttle_s", String(parseFloat($("throttle").value || "0")));
  form.append("max_verify", String(parseInt($("maxVerify").value || "0", 10)));

  $("btnCheck").disabled = true;
  $("btnVerify").disabled = true;
  showSpinner(true);
  setStatus("Running online verification...");

  const { ok, data } = await postForm("/verify", form);

  $("btnCheck").disabled = false;
  $("btnVerify").disabled = false;
  showSpinner(false);

  if (!ok){
    setStatus("Error.");
    alert((data?.error || "Server error") + (data?.detail ? "\n\n" + data.detail : ""));
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
  renderVerify(data, ui);

  setActiveTab("verifyPane");
  setStatus("Online verification complete.");
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
    alert(t || "Download failed");
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

$("btnExportCsv").addEventListener("click", () => downloadFromEndpoint("/export/csv"));
$("btnExportWord").addEventListener("click", () => downloadFromEndpoint("/export/word"));
