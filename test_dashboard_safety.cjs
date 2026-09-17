/* DOM-level assertions for summary rendering, without a network or live job. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html', 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
  if (match[1].trim()) new vm.Script(match[1]);
}
function section(start, end) { return html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start))); }
const nodes = Object.fromEntries(['publicationKpiGrid', 'publicationCoverageNote', 'globalPublicationSafety'].map(id => [id, {innerHTML: '', textContent: '', style: {}}]));
const context = vm.createContext({$: id => nodes[id], esc: x => String(x ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;')});
vm.runInContext(section('function renderPublicationKpis(data)', 'function renderKpis(data)'), context);
vm.runInContext(section('function renderGlobalPublicationSafety(data)', 'function renderACII(data)'), context);
const summary = {retracted_or_withdrawn: 5, expression_of_concern: 1, corrected: 1, reinstated: 1, publication_notice: 1,
  other_update: 0, no_recorded_event: 11, unchecked: 0, checked: 20, total: 20, complete: true, data_versions: ['2026-09-15']};
context.renderPublicationKpis({summary: {reference_entries_found: 20}, online_verification: {rows: [{}, {}, {}, {}, {}]}, source_risk_review: {publication_summary: summary}});
assert.equal((nodes.publicationKpiGrid.innerHTML.match(/class="kpi"/g) || []).length, 8);
assert.match(nodes.publicationCoverageNote.textContent, /20 of 20 references checked/);
assert.match(nodes.publicationKpiGrid.innerHTML, />5<\/div><div class="kpi-label">Retractions/);
assert.match(nodes.publicationKpiGrid.innerHTML, />1<\/div><div class="kpi-label">Expressions/);
context.renderPublicationKpis({summary: {reference_entries_found: 20}});
assert.match(nodes.publicationKpiGrid.innerHTML, /—/);
assert.match(nodes.publicationCoverageNote.textContent, /Coverage incomplete/);
const risks = Array.from({length: 4}, () => ({risk: 'retracted_or_withdrawn', priority: 'critical'}));
risks.push({risk: 'expression_of_concern', priority: 'critical'});
context.renderGlobalPublicationSafety({source_risk_review: {risks}});
assert.match(nodes.globalPublicationSafety.innerHTML, /4 active retraction\/withdrawal finding\(s\); 1 expression/);
assert.doesNotMatch(nodes.globalPublicationSafety.innerHTML, /5 active retraction/);
console.log('Dashboard JavaScript syntax and safety-card rendering checks passed.');
