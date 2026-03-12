/* static/app.js */

(() => {

"use strict";

const POLL_INTERVAL = 1200;

let JOB = null;
let TIMER = null;

const $ = id => document.getElementById(id);

const el = {

file: $("file"),
style: $("style"),

btnCheck: $("btnCheck"),
btnVerify: $("btnVerify"),

summaryTable: $("summaryTable"),

missingBody: $("missingBody"),
uncitedBody: $("uncitedBody"),
c2rBody: $("c2rBody"),
r2cBody: $("r2cBody"),

verifyBody: $("verifyBody"),
verifyDash: $("verifyDash"),

aciiValue: $("aciiValue")

};

function esc(x){
return String(x ?? "")
.replaceAll("&","&amp;")
.replaceAll("<","&lt;")
.replaceAll(">","&gt;");
}

function normalize(data){

return data.data || data.result || data;

}

function renderSummary(d){

const s = d.summary || {};

el.summaryTable.innerHTML = `
<tr><td>In-text citations</td><td>${s.in_text_citations_found}</td></tr>
<tr><td>References</td><td>${s.reference_entries_found}</td></tr>
<tr><td>Missing</td><td>${s.missing_in_references}</td></tr>
<tr><td>Uncited</td><td>${s.uncited_references}</td></tr>
<tr><td>Match rate</td><td>${s.match_rate}</td></tr>
`;

}

function renderMissing(d){

const rows = d.missing_in_references || [];

el.missingBody.innerHTML = rows.length
? rows.map((r,i)=>`
<tr>
<td>${i+1}</td>
<td>${esc(r.citation_in_text)}</td>
<td>${esc(r.count_in_text)}</td>
</tr>`).join("")
: `<tr><td colspan="3">None</td></tr>`;

}

function renderUncited(d){

const rows = d.uncited_references || [];

el.uncitedBody.innerHTML = rows.length
? rows.map((r,i)=>`
<tr>
<td>${i+1}</td>
<td>${esc(r)}</td>
</tr>`).join("")
: `<tr><td colspan="2">None</td></tr>`;

}

function renderC2R(d){

const rows = d.reconciliation_intext_to_reference || [];

el.c2rBody.innerHTML = rows.length
? rows.map((r,i)=>`
<tr>
<td>${i+1}</td>
<td>${esc(r.status)}</td>
<td>${esc(r.in_text)}</td>
<td>${esc(r.matched_reference)}</td>
<td>${esc(r.flags)}</td>
</tr>`).join("")
: `<tr><td colspan="5">None</td></tr>`;

}

function renderR2C(d){

const rows = d.reconciliation_reference_to_intext || [];

el.r2cBody.innerHTML = rows.length
? rows.map((r,i)=>`
<tr>
<td>${i+1}</td>
<td>${r.times_cited}</td>
<td>${esc(r.reference)}</td>
<td>${esc((r.cited_by||[]).slice(0,3).join("; "))}</td>
</tr>`).join("")
: `<tr><td colspan="4">None</td></tr>`;

}

function renderVerify(d){

const v = d.online_verification || {};

const rows = v.rows || [];

const sum = v.summary || {};

el.verifyDash.innerHTML = `
Verified ${sum.verified||0}
Likely ${sum.likely||0}
Needs Review ${sum.needs_review||0}
Not Found ${sum.not_found||0}
Offline ${sum.offline||0}
`;

el.verifyBody.innerHTML = rows.length
? rows.map((r,i)=>`
<tr>
<td>${i+1}</td>
<td>${esc(r.status)}</td>
<td>${esc(r.source)}</td>
<td>${esc(r.score)}</td>
<td>${esc(r.doi)}</td>
<td>${esc(r.matched_year)}</td>
<td>${esc(r.matched_authors)}</td>
<td>${esc(r.matched_title)}</td>
<td>${esc(r.query_used)}</td>
</tr>`).join("")
: `<tr><td colspan="9">No results</td></tr>`;

}

function renderACII(d){

if(!d.acii) return;

el.aciiValue.textContent = d.acii.ACII;

}

function renderAll(data){

const d = normalize(data);

renderSummary(d);
renderMissing(d);
renderUncited(d);
renderC2R(d);
renderR2C(d);
renderVerify(d);
renderACII(d);

}

async function runCheck(){

const f = el.file.files[0];

const fd = new FormData();
fd.append("file",f);
fd.append("style",el.style.value);

const res = await fetch("/verify",{method:"POST",body:fd});

const js = await res.json();

JOB = js.job_id;

renderAll(js);

}

async function runVerify(){

const fd = new FormData();
fd.append("job_id",JOB);

await fetch("/verify-online",{method:"POST",body:fd});

poll();

}

function poll(){

TIMER = setInterval(async ()=>{

const res = await fetch(`/online/status?job_id=${JOB}`);

const js = await res.json();

renderAll(js);

if(js.online.state === "done") clearInterval(TIMER);

},POLL_INTERVAL);

}

el.btnCheck.onclick = runCheck;
el.btnVerify.onclick = runVerify;

})();
