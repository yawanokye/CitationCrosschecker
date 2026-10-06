/* Run production dashboard functions with simulated DOM and saved results. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html', 'utf8');
function section(start, end) {
  const from = html.indexOf(start);
  assert.ok(from >= 0, start);
  const to = html.indexOf(end, from);
  assert.ok(to > from, end);
  return html.slice(from, to);
}
const nodes = {
  manualVerificationBox: {innerHTML: '', querySelectorAll: () => []},
  manualVerifySearch: {value: '', addEventListener: () => {}},
  manualVerifyStatus: {value: '', addEventListener: () => {}},
  manualVerifySort: {value: 'priority', addEventListener: () => {}},
};
const reference = 'smith, J. (2021). Research methods. Example Press.';
const source = {title:'Research methods', authors:['Smith, Jane'], year:'2022', publisher:'Example Press',
  url:'https://example.org/research-methods', formatted_reference:'Smith, J. (2022). Research methods. Example Press.',
  citation_edits_preview:[{original_text:'smith (2021)', proposed_replacement:'Smith (2022)'}]};
const item = {id:'source_verification-1', category:'source_verification', evidence:reference, original_text:reference,
  extracted_reference:{title:'Research methods', year:'2021'}, source_candidates:[source], decision:'pending'};
const rows = [{reference, status:'needs_review'}, {reference:'Missing work', status:'not_found'},
  {reference:'Unavailable work', status:'offline'}, {reference:'Confirmed work', status:'verified'}];
const messages = [];
const context = vm.createContext({
  $: id => nodes[id], document: {getElementById: id => nodes[id]},
  latestResults:{online_verification:{rows}, correction_plan:{items:[item]}}, manualVerifyRows:[], manualSearchCandidates:{},
  esc: value => String(value ?? '').replaceAll('<','&lt;'), hasPaidAccess: () => true,
  formatBadge: value => String(value), renderSourceSearchReport: () => '',
  manualDecisionLabel: value => value, applyManualDecisionUI: () => {},
  rerenderManualVerificationList: () => {},
  googleScholarUrl: () => '', googleSearchUrl: () => '', doiSearchUrl: () => '', titleSearchUrl: () => '',
  CSS:{escape: value => String(value)}, setStatus:(message) => messages.push(message), confirm: () => true,
});
vm.runInContext(section('function manualQueryFromRow', 'function googleScholarUrl'), context);
vm.runInContext(section('function normaliseManualReferenceKey', 'function rerenderManualVerificationList'), context);
vm.runInContext(section('function renderEvidenceCandidate', 'function correctionLocationText'), context);
vm.runInContext(section('function renderManualVerification', 'async function runManualSourceSearch'), context);
vm.runInContext(section('async function approveCorrectionSource', 'async function approveAllReferenceFormatting'), context);
vm.runInContext(section('function verificationCoverageCounts', 'function previewNoticeHtml'), context);
context.getSafeRows = value => Array.isArray(value) ? value : [];
context.previewTotal = (data, key, fallback=0) => data.preview_coverage?.[key]?.total ?? fallback;
vm.runInContext(section('function renderVerification(data)', 'function csvCell'), context);
nodes.verifyKpis = {innerHTML:''};
context.renderVerification({online_verification:{rows:[rows[0]]}, reference_resolution_summary:{verified:3, metadata_differences:2, needs_review:4, not_found:12, lookup_failed:7}});
assert.match(nodes.verifyKpis.innerHTML, />12<\/div><div class="kpi-label">Not found/);
assert.match(nodes.verifyKpis.innerHTML, />7<\/div><div class="kpi-label">Lookup failed/);
assert.doesNotMatch(nodes.verifyKpis.innerHTML, /Not found \/ failed/);
context.renderManualVerification(context.latestResults);
const markup = nodes.manualVerificationBox.innerHTML;
assert.match(markup, /References needing human review \(1\)/);
assert.match(markup, /References not found in searched indexes \(1\)/);
assert.match(markup, /Reference lookups failed or unavailable \(1\)/);
assert.equal((markup.match(/class="manual-card"/g) || []).length, 3);
assert.match(markup, /Extracted reference details, not independently verified/);
assert.match(markup, /Find sources/);
assert.match(markup, /Approve selected source for Track Changes/);
assert.match(markup, /Associated in-text citation edits for approval/);
assert.match(markup, /smith \(2021\) → Smith \(2022\)/);

// Preserve the review filter when results are rerendered.
nodes.manualVerifyStatus.value = 'needs_review';
context.renderManualVerification(context.latestResults);
assert.equal(nodes.manualVerifyStatus.value, 'needs_review');
assert.match(nodes.manualVerificationBox.innerHTML, /References not found in searched indexes \(0\)/);

const scope = {querySelector: selector => selector.startsWith('input') ? {value:'0'} : {checked:true}};
const button = {closest: () => scope, isConnected: true};
let approvedPayload;
context.saveCorrectionDecision = async (...args) => { approvedPayload=args; return true; };
(async () => {
  await context.approveCorrectionSource(item.id, item.category, button);
  assert.equal(approvedPayload, undefined);
  assert.match(messages.at(-1), /Open the candidate source/);
  source.opened_by_user = true;
  await context.approveCorrectionSource(item.id, item.category, button);
  assert.equal(approvedPayload[3].action, 'replace_reference');
  assert.equal(approvedPayload[3].proposed_replacement, source.formatted_reference);
  assert.equal(approvedPayload[3].approved_source.identity_confirmed, true);
  item.decision = 'accepted';
  item.proposed_replacement = source.formatted_reference;
  context.renderManualVerification(context.latestResults);
  assert.match(nodes.manualVerificationBox.innerHTML, /✅ Approved for Track Changes/);
  console.log('Reference review grouping, source previews, approval gates and confirmation checks passed.');
})().catch(error => { console.error(error); process.exitCode=1; });
