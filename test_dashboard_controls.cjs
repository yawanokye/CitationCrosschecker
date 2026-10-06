/* Exercise the production dashboard's actual event handlers in a DOM.
   Development dependency: jsdom 27. Run with node test_dashboard_controls.cjs. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
const tick = () => new Promise(resolve => setImmediate(resolve));
const html = fs.readFileSync('templates/new_results.html', 'utf8');
assert.ok(html.includes('    init();'));
const errors = [];
const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => {
  if (error.type !== 'css-parsing' && error.type !== 'not-implemented') errors.push(error);
});
const testPage = html.replace('    init();', `
    setupTabs(); setupActions();
    window.dashboardTest = {
        render(data) {
            latestResults = hydrateFinalDefaults(normalizeData(data));
            renderStudentGuidance(latestResults);
            renderManualVerification(latestResults);
            renderClaimSupport(latestResults);
        },
        data: () => latestResults,
        saveManualDecision,
    };`);
const requests = [];
let decisionResponder;
let manualResponder;
const dom = new JSDOM(testPage, {
  url:'https://citeintegrity.test/new/results/test-job', runScripts:'dangerously',
  pretendToBeVisual:true, virtualConsole,
  beforeParse(window) {
    window.CSS = {escape: value => String(value)};
    window.confirm = () => true;
    window.HTMLElement.prototype.scrollIntoView = function () {};
    window.fetch = async (url, options = {}) => {
      const request = {url:String(url), body:options.body ? JSON.parse(options.body) : {}};
      requests.push(request);
      if (request.url.endsWith('/manual-verify/decision')) return manualResponder(request);
      if (request.url.endsWith('/decision')) return decisionResponder(request);
      return {ok:true, status:200, json:async()=>({ok:true})};
    };
  },
});
const {window} = dom;
window.document.addEventListener('click', event => {
  if (event.target.closest('a')) event.preventDefault();
});
const source = {
  title:'Nursing education study', authors:['Smith, Jane','Jones, John'], year:'2026',
  doi:'10.1234/example', url:'https://publisher.example/article',
  citation_text:'Smith and Jones (2026)',
  formatted_reference:'Smith, J., & Jones, J. (2026). Nursing education study.',
  authors_complete:true, date_review_required:true, author_review_required:true,
};
const referenceItem = {
  id:'source_verification-1', category:'source_verification', decision:'pending',
  evidence:'Smith, J. (2024). Nursing education study.', title:'Check reference metadata',
  source_candidates:[source], confidence:'high',
};
const claimItem = {
  id:'claim_support-1', category:'claim_support', decision:'pending',
  title:'Check claim support', evidence:'Simulation improves nursing skills.',
  source_candidates:[{...source}], confidence:'high',
};
const makeData = () => ({
  access:{paid:true}, summary:{reference_entries_found:1},
  correction_plan:{items:JSON.parse(JSON.stringify([referenceItem,claimItem]))},
  online_verification:{rows:[{reference:referenceItem.evidence, status:'needs_review'}]},
  claim_support:[{claim:claimItem.evidence, status:'needs_review'}],
});
const query = selector => {
  const element = window.document.querySelector(selector);
  assert.ok(element, selector);
  return element;
};
function review(card, {year=true, authors=true} = {}) {
  card.querySelector('.candidate-source-choice').click();
  card.querySelector('.candidate-source-link').click();
  card.querySelector('.candidate-fit-confirm').click();
  if (year) card.querySelector('.candidate-year-confirm').click();
  if (authors) card.querySelector('.candidate-author-confirm').click();
}
function response(ok, data, status = ok ? 200 : 422) {
  return {ok, status, json:async()=>data};
}
(async () => {
  await tick();
  assert.deepEqual(errors, [], 'Dashboard must parse and bind without JavaScript errors');
  // Claim Support renderer is also exercised for integrations retaining its table.
  const claimTable=window.document.createElement('table');
  claimTable.innerHTML='<tbody id="claimBody"></tbody>';
  window.document.body.append(claimTable);
  const dashboard = window.dashboardTest;
  assert.ok(dashboard);
  dashboard.render(makeData());
  const workspace = query('.guidance-item[data-correction-id="source_verification-1"]');
  const manual = query('.manual-card');
  review(manual);
  workspace.querySelector('.candidate-source-choice').click();
  assert.ok(manual.querySelector('.candidate-source-choice').checked,
    'Workspace choice must not clear the manual-review choice');
  assert.notEqual(workspace.querySelector('.candidate-source-choice').name,
    manual.querySelector('.candidate-source-choice').name);
  assert.equal(manual.querySelectorAll('label label').length, 0, 'No nested candidate labels');

  // Refresh returns fresh provider objects without the local opened flag.
  dashboard.render(makeData());
  let current = query('.manual-card');
  for (const selector of ['.candidate-source-choice','.candidate-fit-confirm','.candidate-year-confirm','.candidate-author-confirm']) {
    assert.ok(current.querySelector(selector).checked, `${selector} preserved after refresh`);
  }
  assert.ok(dashboard.data().correction_plan.items[0].source_candidates[0].opened_by_user);

  const reordered=makeData();
  reordered.correction_plan.items[0].source_candidates.unshift({...source, title:'A different source', doi:'10.1234/another'});
  dashboard.render(reordered);
  assert.equal(query('.manual-card .candidate-source-choice:checked').value, '1');
  assert.ok(query('.manual-card .candidate-fit-confirm[data-source-index="1"]').checked);
  dashboard.render(makeData());
  current=query('.manual-card');

  // A 422 rejection is visible inside the panel where the user clicked.
  decisionResponder = async () => response(false, {error:'The original DOCX is no longer available. Upload it again.'});
  current.querySelector('.manual-approve-source').click();
  await tick(); await tick();
  assert.match(current.querySelector('.approval-error').textContent, /Upload it again/);
  assert.equal(current.querySelector('.approval-error').getAttribute('role'), 'alert');
  assert.equal(dashboard.data().correction_plan.items[0].decision, 'pending');
  assert.equal(current.querySelector('.manual-approve-source').disabled, false);

  // HTTP 200 without a saved correction plan must not show a false approval.
  decisionResponder=async()=>response(true, {});
  current.querySelector('.manual-approve-source').click();
  await tick(); await tick();
  assert.match(current.querySelector('.approval-error').textContent, /did not return the updated correction/);
  assert.equal(dashboard.data().correction_plan.items[0].decision, 'pending');
  assert.equal(current.querySelector('.manual-approve-source').disabled, false);

  // Repeated clicks and a refresh while saving cannot start duplicate requests.
  let finish;
  decisionResponder = () => new Promise(resolve => {finish=resolve;});
  const before = requests.filter(row => row.url.includes('/corrections/') && row.url.endsWith('/decision')).length;
  current.querySelector('.manual-approve-source').click();
  query('.guidance-item[data-correction-id="source_verification-1"] .approve-correction-source').click();
  dashboard.render(makeData());
  assert.ok(query('.manual-approve-source').disabled);
  assert.equal(requests.filter(row => row.url.includes('/corrections/') && row.url.endsWith('/decision')).length, before+1);
  const accepted = makeData().correction_plan;
  Object.assign(accepted.items[0], {decision:'accepted', application_status:'ready_for_track_changes', approved_source:source, approved_action:'replace_reference'});
  finish(response(true, {ok:true, correction_plan:accepted}));
  await tick(); await tick();
  assert.match(query('.manual-reference-resolution').textContent, /Approved for Track Changes/);
  assert.match(query('.guidance-item[data-correction-id="source_verification-1"] .approval-error').textContent, /marked accepted/);

  // Changed bibliographic metadata requires fresh confirmation.
  const changed = makeData();
  changed.correction_plan.items[0].source_candidates[0].year='2025';
  dashboard.render(changed);
  current=query('.manual-card');
  assert.equal(current.querySelector('.candidate-fit-confirm').checked, false);
  assert.equal(current.querySelector('.candidate-year-confirm').checked, false);

  // Claim approval and Reject/Ignore use their actual click bindings.
  const claimCard = query('.claim-direct-actions[data-id="claim_support-1"]');
  review(claimCard);
  decisionResponder = async request => {
    const plan=JSON.parse(JSON.stringify(dashboard.data().correction_plan));
    Object.assign(plan.items.find(item => item.id===request.body.item_id), {decision:request.body.decision, application_status:'ready_for_track_changes'});
    return response(true, {ok:true, correction_plan:plan});
  };
  claimCard.querySelector('.claim-approve-source').click();
  await tick(); await tick();
  assert.equal(dashboard.data().correction_plan.items[1].decision, 'accepted');
  assert.equal(requests.at(-1).body.action, 'add_supporting_citation');
  dashboard.render(makeData());
  query('.guidance-item[data-correction-id="source_verification-1"] .correction-decision[data-decision="rejected"]').click();
  await tick(); await tick();
  assert.equal(dashboard.data().correction_plan.items[0].decision, 'rejected');
  query('.guidance-item[data-correction-id="source_verification-1"] .correction-decision[data-decision="ignored"]').click();
  await tick(); await tick();
  assert.equal(dashboard.data().correction_plan.items[0].decision, 'ignored');

  // A failed manual decision leaves the choices enabled and does not claim success.
  dashboard.render(makeData());
  manualResponder = async () => response(false, {detail:'Open an evidence source before recording this decision.'});
  query('.manual-action[data-action="manual-verified"]').click();
  await tick(); await tick();
  current=query('.manual-card');
  assert.match(current.querySelector('.manual-decision-feedback').textContent, /Open an evidence source/);
  assert.equal(current.dataset.manualDecision, undefined);
  assert.equal(current.querySelector('.manual-action[data-action="manual-verified"]').disabled, false);
  manualResponder = async () => response(true, {ok:true});
  query('.manual-action[data-action="manual-verified"]').click();
  await tick(); await tick();
  assert.equal(query('.manual-card').dataset.manualDecision, 'manual_verified');
  assert.match(query('.manual-decision-feedback').textContent, /Manual decision recorded/);

  // A server refresh can reorder rows. The saved decision must follow its reference.
  dashboard.render(makeData());
  manualResponder = async () => {
    const result=makeData();
    result.online_verification.rows.unshift({reference:'A different unresolved reference', status:'needs_review'});
    return response(true, {ok:true, result});
  };
  query('.manual-action[data-action="manual-verified"]').click();
  await tick(); await tick();
  assert.equal(query('.manual-card[data-manual-index="0"]').dataset.manualDecision, undefined);
  assert.equal(query('.manual-card[data-manual-index="1"]').dataset.manualDecision, 'manual_verified');

  // Feature chips are keyboard-accessible native buttons that navigate to a panel.
  query('.output-feature-tip[data-feature-pane="voicePane"]').click();
  assert.ok(query('#voicePane').classList.contains('active'));
  query('.output-feature-tip[data-feature-action="fix-next"]').click();
  assert.ok(query('#correctionPane').classList.contains('active'));
  assert.equal(query('.output-feature-tip').tagName, 'BUTTON');
  assert.deepEqual(errors, [], 'No event handler errors');
  dom.window.close();
  console.log('Dashboard DOM controls passed: cross-panel selection, refresh, metadata reset, 422 feedback, duplicate guards, approval success, claims, Reject/Ignore, manual decisions and feature navigation.');
})().catch(error => {dom.window.close(); console.error(error); process.exitCode=1;});
