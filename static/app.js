/* static/app.js — Citation Crosschecker Dashboard (Stable Version) */

document.addEventListener("DOMContentLoaded", function () {

"use strict";

/* -------------------------------------------------------
CONFIG
------------------------------------------------------- */

const CONFIG = {
    POLL_INTERVAL: 1200,
    MAX_VERIFY_DISPLAY: 500
};

/* -------------------------------------------------------
DOM HELPERS
------------------------------------------------------- */

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
aciiValue: $("aciiValue")

};

let LAST_JOB_ID = null;
let POLL_TIMER = null;
let CURRENT_DATA = null;

/* -------------------------------------------------------
UTILITY
------------------------------------------------------- */

function esc(s) {
return String(s ?? "")
.replaceAll("&","&amp;")
.replaceAll("<","&lt;")
.replaceAll(">","&gt;");
}

function toNum(x,d=0){
const n = Number(x);
return Number.isFinite(n)?n:d;
}

function setStatus(msg,tone="muted"){
if(!el.status) return;
el.status.className = `status ${tone}`;
el.status.textContent = msg || "";
}

/* -------------------------------------------------------
ACII HELPERS
------------------------------------------------------- */

function aciiCategory(score){

score = Number(score);

if(score >= 90) return "Excellent";
if(score >= 80) return "Very Good";
if(score >= 70) return "Good";
if(score >= 60) return "Moderate";
if(score >= 50) return "Weak";

return "Poor";
}

function aciiRemark(metric,score){

if(metric==="verification")
return score+"% verified in scholarly databases";

if(metric==="concentration")
return "Measures whether citations rely heavily on few authors";

if(metric==="diversity")
return "Measures diversity of authors represented";

if(metric==="temporal")
return "Measures spread of publication years";

return "";
}

/* -------------------------------------------------------
TAB NAVIGATION
------------------------------------------------------- */

const tabs = document.querySelectorAll(".tab");
const panes = document.querySelectorAll(".tabPane");

tabs.forEach(tab=>{
tab.addEventListener("click",()=>{

const target = tab.dataset.tab;

tabs.forEach(t=>t.classList.remove("active"));
panes.forEach(p=>p.classList.remove("active"));

tab.classList.add("active");

const pane = document.getElementById(target);
if(pane) pane.classList.add("active");

});
});

/* -------------------------------------------------------
DATA NORMALIZATION
------------------------------------------------------- */

function normalizeData(payload){

const data = payload?.data || payload?.result || payload || {};
const s = data.summary || {};

data.summary = {

in_text_citations_found: toNum(
s.in_text_citations_found ??
data.intext_count
),

reference_entries_found: toNum(
s.reference_entries_found ??
data.reference_entries_found
),

missing_in_references: toNum(
s.missing_in_references ??
(data.missing_in_references || []).length
),

uncited_references: toNum(
s.uncited_references ??
(data.uncited_references || []).length
),

match_rate: toNum(
s.match_rate ??
data.match_rate
)

};

return data;

}

/* -------------------------------------------------------
SUMMARY
------------------------------------------------------- */

function renderSummaryTable(data){

const s = data?.summary || {};

if(!el.summaryTable) return;

el.summaryTable.innerHTML = `
<tr><td>In-text citations</td><td>${esc(s.in_text_citations_found)}</td></tr>
<tr><td>References</td><td>${esc(s.reference_entries_found)}</td></tr>
<tr><td>Missing</td><td>${esc(s.missing_in_references)}</td></tr>
<tr><td>Uncited</td><td>${esc(s.uncited_references)}</td></tr>
<tr><td>Match rate</td><td>${esc(s.match_rate)}</td></tr>
`;

}

/* -------------------------------------------------------
ACII
------------------------------------------------------- */

function renderACII(data){

const acii = data?.acii;

if(!acii) return;

if(el.aciiCard) el.aciiCard.style.display="block";
if(el.aciiValue) el.aciiValue.textContent = acii.ACII ?? "--";

const c = acii.components || {};

/* scores */

$("aciiV").textContent = c.verification_integrity ?? "";
$("aciiC").textContent = c.citation_concentration ?? "";
$("aciiA").textContent = c.author_diversity ?? "";
$("aciiT").textContent = c.temporal_balance ?? "";

/* categories */

$("aciiVcat").textContent = aciiCategory(c.verification_integrity);
$("aciiCcat").textContent = aciiCategory(c.citation_concentration);
$("aciiAcat").textContent = aciiCategory(c.author_diversity);
$("aciiTcat").textContent = aciiCategory(c.temporal_balance);

/* remarks */

$("aciiVremark").textContent =
aciiRemark("verification",c.verification_integrity);

$("aciiCremark").textContent =
aciiRemark("concentration",c.citation_concentration);

$("aciiAremark").textContent =
aciiRemark("diversity",c.author_diversity);

$("aciiTremark").textContent =
aciiRemark("temporal",c.temporal_balance);

}

/* -------------------------------------------------------
MASTER RENDER
------------------------------------------------------- */

function renderAll(data){

if(!data) return;

CURRENT_DATA = normalizeData(data);

if(el.resultsCard) el.resultsCard.style.display="block";

renderSummaryTable(CURRENT_DATA);
renderACII(CURRENT_DATA);
renderMissing(CURRENT_DATA);
renderUncited(CURRENT_DATA);
renderC2R(CURRENT_DATA);
renderR2C(CURRENT_DATA);
renderVerify(CURRENT_DATA);

}
