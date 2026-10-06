const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html','utf8');
function section(start, end) {
  const from=html.indexOf(start), to=html.indexOf(end,from);
  assert.ok(from>=0 && to>from);
  return html.slice(from,to);
}
const nodes = {verificationCoverageNote:{textContent:''}, verifyKpis:{innerHTML:''},
  verifyBody:{innerHTML:''}, kpiGrid:{innerHTML:''}};
const messages=[], progress=[];
const context=vm.createContext({
  $:id=>nodes[id], getSafeRows:value=>Array.isArray(value)?value:[],
  previewTotal:(data,key,fallback=0)=>data.preview_coverage?.[key]?.total ?? fallback,
  esc:value=>String(value ?? '').replaceAll('<','&lt;'), hasPaidAccess:()=>true,
  showProgress:(percent,message)=>progress.push([percent,message]), setStatus:(message,type)=>messages.push([message,type]),
  getManualVerificationRows:()=>[], manualQueryFromRow:row=>row.reference,
  referenceReviewStatus:row=>row.status === 'verified' ? 'verified' : 'needs_review',
  isManualReviewRow:()=>false, formatBadge:value=>String(value), verificationDisplayStatus:row=>row.status,
  manualDecisionLabel:value=>value,
  renderPublicationKpis:()=>{}, isLockedPayload:()=>false, previewNoticeRow:()=>'',
});
vm.runInContext(section('function verificationCoverageCounts','function previewNoticeHtml'),context);
vm.runInContext(section('function hasVerificationRows','function hydrateFinalDefaults'),context);
vm.runInContext(section('function renderKpis','function renderTableFigureAudit'),context);
vm.runInContext(section('function renderVerification(data)','function csvCell'),context);
const rows=Array.from({length:118},(_,i)=>({reference:`Reference ${i}`,status:i<6?'verified':'needs_review'}));
const data={summary:{reference_entries_found:118}, online_verification:{rows,summary:{total:118,verified:6}},
 verification:{state:'completed',total:118,progress:118},
 verification_coverage:{expected:118,processed:118,matched:6,result_rows:118,pending:0,complete:true}};
context.renderKpis(data);
assert.match(nodes.kpiGrid.innerHTML,/118[\s\S]*References processed/);
assert.match(nodes.kpiGrid.innerHTML,/6[\s\S]*Sources matched/);
context.renderVerification(data);
assert.equal((nodes.verifyBody.innerHTML.match(/<tr data-status=/g)||[]).length,118);
assert.match(nodes.verificationCoverageNote.textContent,/118 of 118 references processed\. 6 source records matched/);
context.showVerificationResultStatus(data);
assert.equal(progress.at(-1)[0],100);
assert.equal(messages.at(-1)[1],'good');
// Full View has no hidden 600-row display cap.
const large={...data,online_verification:{rows:Array.from({length:650},(_,i)=>({reference:`Reference ${i}`,status:'verified'}))}};
context.renderVerification(large);
assert.equal((nodes.verifyBody.innerHTML.match(/<tr data-status=/g)||[]).length,650);
// A stale final flag must not stop polling during a rerun.
const partial={...data, online_verification:{rows:rows.slice(0,40),summary:{total:40}},
 verification:{state:'running',total:118,progress:40,final_tables_ready:true},verification_completed_at:'old',
 verification_coverage:{expected:118,processed:40,matched:6,result_rows:40,pending:78,complete:false}};
assert.equal(context.isFinalReady(partial),false);
partial.verification.state='incomplete';
assert.equal(context.isFinalReady(partial),true);
context.showVerificationResultStatus(partial);
assert.ok(progress.at(-1)[0]<100);
assert.equal(messages.at(-1)[1],'warn');
assert.match(messages.at(-1)[0],/78 references still lack a usable result/);
assert.match(messages.at(-1)[0],/Retry verification/);
// A sampled legacy payload still reports the full saved result count.
const preview={...data, verification_coverage:undefined, online_verification:{rows:rows.slice(0,10),summary:{total:118,verified:6}},preview_coverage:{online_verification:{total:118,shown:10}}};
assert.equal(context.verificationCoverageCounts(preview).processed,118);
assert.match(context.verificationCoverageText(preview),/Showing 10 result rows in this preview/);
console.log('Full View reference counts, all-row display, partial warnings and rerun readiness passed.');

// Refreshing a dashboard with a partial batch must resume live polling.
let pollStarts=0;
context.startPollingStatus=()=>{pollStarts++;};
context.verificationInProgress=false;
context.lastKnownTotal=0;
context.lastKnownProgress=0;
context.partialFinalRendered=true;
nodes.btnVerify={disabled:false};
vm.runInContext(section('function resumeVerificationPolling','async function loadExistingResult'),context);
partial.verification.state='running';
assert.equal(context.resumeVerificationPolling(partial),true);
assert.equal(pollStarts,1);
assert.equal(context.lastKnownTotal,118);
assert.equal(context.lastKnownProgress,40);
assert.equal(nodes.btnVerify.disabled,true);
context.resumeVerificationPolling(partial);
assert.equal(pollStarts,1);
assert.equal(context.resumeVerificationPolling(data),false);
assert.ok(html.includes('addEventListener("click", () => startVerification(true))'));
assert.ok(html.includes('formData.append("force", force === true ? "true" : "false")'));
console.log('Partial saved results resume polling and explicit rechecks are enabled.');
