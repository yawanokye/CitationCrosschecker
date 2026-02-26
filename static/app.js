/* static/app.js - robust renderer + schema compatibility */

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
    useSemanticScholar: document.getElementById("useSemanticScholar"),
    aiAssist: document.getElementById("aiAssist"),

    status: document.getElementById("status"),

    resultsCard: document.getElementById("resultsCard"),
    summaryTable: document.getElementById("summaryTable"),
    refMsg: document.getElementById("refMsg"),

    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    intextBody: document.getElementById("intextBody"),
    refIntextBody: document.getElementById("refIntextBody"),
    verifyBody: document.getElementById("verifyBody"),
  };

  const esc = (s) =>
    String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");

  // IMPORTANT: prevents [object Object] and hard crashes
  const asText = (x) => {
    if (x == null) return "";
    if (typeof x === "string") return x;
    if (typeof x === "number" || typeof x === "boolean") return String(x);
    if (typeof x === "object") {
      return (
        x.reference ||
        x.raw ||
        x.text ||
        x.citation_in_text ||
        x.title ||
        JSON.stringify(x)
      );
    }
    return String(x);
  };

  const num = (x) => {
    const n = Number(x);
    return Number.isFinite(n) ? n : 0;
  };

  const setStatus = (msg, kind = "info") => {
    if (!el.status) return;
    el.status.className = "status " + kind;
    el.status.textContent = msg;
  };

  // ---- tabs ----
  function activateTab(tabId) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    const btn = document.querySelector(`.tab[data-tab="${tabId}"]`);
    const panel = document.getElementById(tabId);
    if (btn) btn.classList.add("active");
    if (panel) panel.classList.remove("hidden");
  }

  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => activateTab(btn.dataset.tab));
  });

  // ---- renderers ----
  function renderSummary(r) {
    const missN = Array.isArray(r.missing_in_references) ? r.missing_in_references.length : num(r.missing_in_references);
    const uncN = Array.isArray(r.uncited_references) ? r.uncited_references.length : num(r.uncited_references);

    const rows = [
      ["In-text citations found", num(r.intext_citations_found)],
      ["Reference entries found", num(r.reference_entries_found)],
      ["Missing in references", missN],
      ["Uncited references", uncN],
      ["Match rate", (r.match_rate ?? 0) + "%"],
    ];

    if (r.strict_intext_count != null) rows.push(["Strict in-text count", num(r.strict_intext_count)]);
    if (r.loose_intext_count != null) rows.push(["Loose in-text count", num(r.loose_intext_count)]);

    el.summaryTable.innerHTML = rows
      .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`)
      .join("");

    el.refMsg.textContent = r.ref_heading_found
      ? `Found References heading: ${r.ref_heading_found}`
      : "References heading not detected (heuristic extraction used).";
  }

  function renderMissing(r) {
    const arr = Array.isArray(r.missing_in_references) ? r.missing_in_references : [];
    el.missingBody.innerHTML = arr.length
      ? arr
          .map((row, idx) => {
            const citation = asText(row.citation_in_text ?? row);
            const count = row.count != null ? row.count : "";
            return `<tr><td>${idx + 1}</td><td>${esc(citation)}</td><td class="num">${esc(count)}</td></tr>`;
          })
          .join("")
      : `<tr><td colspan="3">None</td></tr>`;
  }

  function renderUncited(r) {
    const arr = Array.isArray(r.uncited_references) ? r.uncited_references : [];
    el.uncitedBody.innerHTML = arr.length
      ? arr.map((row, idx) => `<tr><td>${idx + 1}</td><td>${esc(asText(row))}</td></tr>`).join("")
      : `<tr><td colspan="2">None</td></tr>`;
  }

  function renderIntextToRef(r) {
    const arr = Array.isArray(r.intext_to_reference) ? r.intext_to_reference : [];
    el.intextBody.innerHTML = arr.length
      ? arr
          .map((m, idx) => {
            return `<tr>
              <td>${idx + 1}</td>
              <td>${esc(asText(m.citation_in_text))}</td>
              <td>${esc(asText(m.reference))}</td>
              <td class="num">${esc(m.score ?? "")}</td>
              <td>${esc(m.method ?? "")}</td>
              <td>${esc(m.mode ?? "")}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="6">No matches</td></tr>`;
  }

  function renderRefToIntext(r) {
    const arr = Array.isArray(r.reference_to_intext) ? r.reference_to_intext : [];
    el.refIntextBody.innerHTML = arr.length
      ? arr
          .map((row, idx) => {
            const cited = row.cited_in_text ? "Yes" : "No";
            const cites = Array.isArray(row.citations) ? row.citations : [];
            const citeText = cites
              .slice(0, 6)
              .map((c) => `${asText(c.citation_in_text)} (${c.score ?? ""})`)
              .join("; ");
            const more = cites.length > 6 ? ` … +${cites.length - 6}` : "";
            return `<tr>
              <td>${idx + 1}</td>
              <td>${esc(asText(row.reference))}</td>
              <td>${esc(cited)}</td>
              <td>${esc(citeText + more)}</td>
            </tr>`;
          })
          .join("")
      : `<tr><td colspan="4">No data</td></tr>`;
  }

  function renderVerify(r) {
    const v = r.verify || {};
    el.verifyBody.innerHTML = v.requested
      ? `<pre>${esc(JSON.stringify(v, null, 2))}</pre>`
      : `<div class="muted">Online verification not requested.</div>`;
  }

  function renderAll(r) {
    if (!r || typeof r !== "object") {
      setStatus("No results returned.", "bad");
      return;
    }
    if (el.resultsCard) el.resultsCard.classList.remove("hidden");
    renderSummary(r);
    renderMissing(r);
    renderUncited(r);
    renderIntextToRef(r);
    renderRefToIntext(r);
    renderVerify(r);
    activateTab("tabSummary");
  }

  // ---- API ----
  async function postForm(url, fd) {
    const res = await fetch(url, { method: "POST", body: fd });
    let data = null;
    try {
      data = await res.json();
    } catch (_) {}
    if (!res.ok) {
      const msg = data?.detail || data?.error || `HTTP ${res.status}`;
      throw new Error(msg);
    }
    // handle wrappers {ok, data}
    if (data && typeof data === "object" && data.data && typeof data.data === "object") return data.data;
    return data;
  }

  function buildForm(verifyOnline) {
    const f = el.file?.files?.[0];
    if (!f) throw new Error("Choose a file first.");
    const fd = new FormData();
    fd.append("file", f);
    fd.append("style", el.style?.value || "apa");
    fd.append("verify_online", verifyOnline ? "true" : "false");
    fd.append("verify_mode", el.verifyMode?.value || "all");
    fd.append("throttle_s", el.throttle?.value || "0.25");
    fd.append("max_verify", el.maxVerify?.value || "0");
    fd.append("use_crossref", el.useCrossref?.checked ? "true" : "false");
    fd.append("use_openalex", el.useOpenAlex?.checked ? "true" : "false");
    fd.append("use_semanticscholar", el.useSemanticScholar?.checked ? "true" : "false");
    fd.append("ai_assist", el.aiAssist?.checked ? "true" : "false");
    return fd;
  }

  async function runCheck(verifyOnline) {
    try {
      setStatus("Running…", "info");
      const fd = buildForm(verifyOnline);
      const result = await postForm(verifyOnline ? "/verify" : "/check", fd);
      setStatus("Done.", "ok");
      renderAll(result);
    } catch (e) {
      console.error(e);
      setStatus(String(e.message || e), "bad");
    }
  }

  if (el.btnCheck) el.btnCheck.addEventListener("click", () => runCheck(false));
  if (el.btnVerify) el.btnVerify.addEventListener("click", () => runCheck(true));
})();
