const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html', 'utf8');
const from = html.indexOf('function correctionApiError');
const to = html.indexOf('async function findCorrectionSources', from);
assert.ok(from >= 0 && to > from);
let responseBody = {error:'Approval not recorded: The original DOCX is no longer available. Upload the manuscript again.'};
const alerts = [], statusMessages = [];
const card = {querySelector:()=>({style:{}, setAttribute:()=>{}, set textContent(value){alerts.push(value);}}), querySelectorAll:()=>[]};
const result = {correction_plan:{items:[]}};
const context = vm.createContext({
  JOB_ID:'job', latestResults:result, CSS:{escape:String},
  document:{querySelector:()=>card, querySelectorAll:()=>[card]}, console:{error:()=>{}},
  setStatus:message=>statusMessages.push(message),
  renderStudentGuidance:()=>{}, renderClaimSupport:()=>{}, renderManualVerification:()=>{},
  fetch:async()=>({ok:false,status:422,json:async()=>responseBody}),
});
vm.runInContext(html.slice(html.indexOf('const candidateReviewStates'),html.indexOf('function renderEvidenceCandidate')),context);
vm.runInContext(html.slice(from,to),context);
(async()=>{
  assert.equal(await context.saveCorrectionDecision('item','accepted',''),false);
  assert.match(alerts.at(-1),/Upload the manuscript again/);
  assert.doesNotMatch(alerts.at(-1),/Could not save correction decision/);
  assert.deepEqual(result.correction_plan,{items:[]});
  responseBody={detail:'The exact passage was not found uniquely in editable Word text.'};
  await context.saveCorrectionDecision('item','accepted','');
  assert.match(alerts.at(-1),/exact passage/);
  responseBody={detail:[{msg:'Field required',loc:['body','item_id']}]};
  await context.saveCorrectionDecision('item','accepted','');
  assert.match(alerts.at(-1),/Field required/);
  responseBody={};
  await context.saveCorrectionDecision('item','accepted','');
  assert.match(alerts.at(-1),/HTTP 422/);
  context.fetch=async()=>({ok:true,status:200,json:async()=>({correction_plan:{items:[{id:'item',decision:'accepted'}]}})});
  assert.equal(await context.saveCorrectionDecision('item','accepted',''),true);
  assert.equal(result.correction_plan.items[0].decision,'accepted');
  assert.match(statusMessages.at(-1),/Placement checked for Word Track Changes/);
  console.log('Approval errors show exact server reasons for legacy and current response formats. Successful approval remains distinct.');
})().catch(error=>{console.error(error);process.exitCode=1;});
