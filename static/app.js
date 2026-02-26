/* static/app.js - commercial UI glue (Mode C compatible)
   - Renders strict vs loose counts if present
   - Robust against missing fields
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

    status: document.getElementById("status"),

    resultsCard: document.getElementById("resultsCard"),
    dash: document.getElementById("dash"),
    verifyDash: document.getElementById("verifyDash"),
    summaryTable: document.getElementById("summaryTable"),
    refMsg: document.getElementById("refMsg"),

    missingBody: document.getElementById("missingBody"),
    uncitedBody: document.getElementById("uncitedBody"),
    it2refBody: document.getElementById("it2refBody"),
    ref2itBody: document.getElementById("ref2itBody"),
    verifyBody: document.getElementById("verifyBody"),

    tabBtns: Array.from(document.querySelectorAll(".tab")),
  };

  const setStatus = (msg, kind = "") => {
    if (!el.status) return;
    el.status.textContent = msg || "";
    el.status.className = "status" + (kind ? ` status-${kind}` : "");
  };

  const disableTabs = () => {
    el.tabBtns.forEach((b) => {
      b.setAttribute("aria-disabled", "true");
      b.classList.add("disabled");
    });
  };

  const enableTabs = () => {
    el.tabBtns.forEach((b) => {
      b.removeAttribute("aria-disabled");
      b.classList.remove("disabled");
    });
  };

  const showTab = (id) => {
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
    const panel = document.getElementById(id);
    if (panel) panel.classList.remove("hidden");

    el.tabBtns.forEach((b) => {
      if (b.dataset.tab === id) b.classList.add("active");
      else b.classList.remove("active");
    });
  };

  const safeNum = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };

  const addSummaryRow = (label, value) => {
    const tr = document.createElement("tr");
    const td1 = document.createElement("td");
    const td2 = document.createElement("td");
    td1.textContent = label;
    td2.textContent = String(value);
    tr.appendChild(td1);
    tr.appendChild(td2);
    el.summaryTable.appendChild(tr);
  };

  const renderSummary = (data) => {
    const s = (data && data.summary) || {};

    el.summaryTable.innerHTML = "";

    // Always show these core fields
    addSummaryRow("In-text citations found", safeNum(s.in_text_citations_found));
    addSummaryRow("Reference entries found", safeNum(s.reference_entries_found));
    addSummaryRow("Missing in references", safeNum(s.missing_in_references));
    addSummaryRow("Uncited references", safeNum(s.uncited_references));
    addSummaryRow("Match rate", `${safeNum(s.match_rate).toFixed(1)}%`);

    // Optional diagnostics
    if (s.strict_intext_count !== undefined) {
      addSummaryRow("Strict in-text count", safeNum(s.strict_intext_count));
    }
    if (s.loose_intext_count !== undefined) {
      addSummaryRow("Loose in-text count", safeNum(s.loose_intext_count));
    }

    if (el.refMsg) {
      el.refMsg.textContent = data.reference_detection_message || "";
    }
  };

  const renderMissing = (data) => {
    const rows = (data && data.missing_in_references) || [];
    el.missingBody.innerHTML = "";
    rows.forEach((r, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${escapeHtml(r.citation_in_text || "")}</td>
        <td>${safeNum(r.count_in_text)}</td>
      `;
      el.missingBody.appendChild(tr);
    });
  };

  const renderUncited = (data) => {
    const rows = (data && data.uncited_references) || [];
    el.uncitedBody.innerHTML = "";

    rows.forEach((ref, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${escapeHtml(String(ref || ""))}</td>
      `;
      el.uncitedBody.appendChild(tr);
    });
  };

  const renderIntextToRef = (data) => {
    const rows = (data && data.reconciliation_intext_to_reference) || [];
    el.it2refBody.innerHTML = "";
    rows.forEach((r, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${escapeHtml(r.in_text || "")}</td>
        <td>${escapeHtml(r.matched_reference || "")}</td>
        <td>${escapeHtml(r.status || "")}</td>
      `;
      el.it2refBody.appendChild(tr);
    });
  };

  const renderRefToIntext = (data) => {
    const rows = (data && data.reconciliation_reference_to_intext) || [];
    el.ref2itBody.innerHTML = "";
    rows.forEach((r, idx) => {
      const citedBy = Array.isArray(r.cited_by) ? r.cited_by.join("; ") : "";
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${escapeHtml(r.reference || "")}</td>
        <td>${safeNum(r.times_cited)}</td>
        <td>${escapeHtml(citedBy)}</td>
      `;
      el.ref2itBody.appendChild(tr);
    });
  };

  const renderVerify = (data) => {
    const ov = (data && data.online_verification) || { rows: [] };
    const rows = ov.rows || [];

    el.verifyBody.innerHTML = "";
    rows.forEach((r, idx) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${idx + 1}</td>
        <td>${escapeHtml(r.reference || "")}</td>
        <td>${escapeHtml(r.status || "")}</td>
        <td>${escapeHtml(r.source || "")}</td>
        <td>${escapeHtml(r.note || "")}</td>
      `;
      el.verifyBody.appendChild(tr);
    });
  };

  const escapeHtml = (s) =>
    String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");

  const postForm = async (url, formData) => {
    const res = await fetch(url, {
      method: "POST",
      body: formData,
    });
    const ct = (res.headers.get("content-type") || "").toLowerCase();
    const payload = ct.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = typeof payload === "string" ? payload : payload?.detail || "Request failed";
      throw new Error(msg);
    }
    return payload;
  };

  const runCheck = async (verifyOnline) => {
    disableTabs();
    setStatus("Working...", "busy");

    const f = el.file?.files?.[0];
    if (!f) {
      setStatus("Choose a file first.", "warn");
      return;
    }

    const fd = new FormData();
    fd.append("file", f);
    if (el.style) fd.append("style", el.style.value || "apa");

    // Keep stable: backend accepts verify_online even if false
    fd.append("verify_online", verifyOnline ? "1" : "0");

    if (verifyOnline) {
      fd.append("verify_mode", el.verifyMode?.value || "all");
      fd.append("throttle_s", el.throttle?.value || "0.12");
      fd.append("max_verify", el.maxVerify?.value || "0");
      fd.append("use_crossref", el.useCrossref?.checked ? "1" : "0");
      fd.append("use_openalex", el.useOpenAlex?.checked ? "1" : "0");
    }

    const data = await postForm("/verify", fd);

    renderSummary(data);
    renderMissing(data);
    renderUncited(data);
    renderIntextToRef(data);
    renderRefToIntext(data);
    renderVerify(data);

    if (el.resultsCard) el.resultsCard.classList.remove("hidden");

    enableTabs();
    showTab("tab-summary");
    setStatus("Done.", "ok");
  };

  el.tabBtns.forEach((b) => {
    b.addEventListener("click", () => {
      if (b.classList.contains("disabled")) return;
      showTab(b.dataset.tab);
    });
  });

  if (el.btnCheck) {
    el.btnCheck.addEventListener("click", async () => {
      try {
        await runCheck(false);
      } catch (e) {
        setStatus(String(e?.message || e), "err");
      }
    });
  }

  if (el.btnVerify) {
    el.btnVerify.addEventListener("click", async () => {
      try {
        await runCheck(true);
      } catch (e) {
        setStatus(String(e?.message || e), "err");
      }
    });
  }

  // initial
  disableTabs();
  showTab("tab-summary");
})();
