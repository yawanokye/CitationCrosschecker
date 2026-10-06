const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/new_results.html', 'utf8');
function section(start, end) { return html.slice(html.indexOf(start), html.indexOf(end, html.indexOf(start))); }
const messages = [];
const source = {title:'The intended paper', authors:[{family:'García',given:'Ana'},{family:'Smith',given:'Jane'}], year:'2018',
 publication_dates:{'published-online':{year:'2017',date:'2017-06-07'},'published-print':{year:'2018',date:'2018-12'}},
 date_review_required:true, author_review_required:true, metadata_warnings:['Check the full author list.'],
 url:'https://example.org/paper', opened_by_user:true, authors_complete:true,
 formatted_reference:'García, A., & Smith, J. (2018). The intended paper.'};
const item = {id:'source_verification-1', category:'source_verification', source_candidates:[source]};
let yearChecked=false, authorChecked=false, submitted;
const scope = {querySelector: selector => selector.startsWith('input') ? {value:'0'} :
 selector.startsWith('.candidate-year-confirm') ? {checked:yearChecked} :
 selector.startsWith('.candidate-author-confirm') ? {checked:authorChecked} : {checked:true}};
const button = {closest: () => scope};
const context = vm.createContext({latestResults:{correction_plan:{items:[item]}}, CSS:{escape:String},
 document:{querySelectorAll:()=>[]}, esc:value => String(value??''), setStatus:message => messages.push(message), confirm:()=>true,
 saveCorrectionDecision:async (...args) => {submitted=args; return true;}});
vm.runInContext(section('const candidateReviewStates', 'function correctionLocationText'), context);
vm.runInContext(section('async function approveCorrectionSource', 'async function approveAllReferenceFormatting'), context);
const rendered=context.renderEvidenceCandidate(item,source,0);
assert.match(rendered,/García, Ana; Smith, Jane/);
assert.match(rendered,/published-online: 2017-06-07/);
assert.match(rendered,/published-print: 2018-12/);
assert.match(rendered,/candidate-year-confirm/);
assert.match(rendered,/candidate-author-confirm/);
assert.match(rendered,/Check \/ edit full author list and publication year/);
(async()=>{
 await context.approveCorrectionSource(item.id,item.category,button);
 assert.equal(submitted,undefined); assert.match(messages.at(-1),/publication version/);
 yearChecked=true;
 await context.approveCorrectionSource(item.id,item.category,button);
 assert.equal(submitted,undefined); assert.match(messages.at(-1),/all authors/);
 authorChecked=true;
 source.authors_complete=false;
 await context.approveCorrectionSource(item.id,item.category,button);
 assert.equal(submitted,undefined); assert.match(messages.at(-1),/incomplete/);
 source.authors_complete=true;
 await context.approveCorrectionSource(item.id,item.category,button);
 assert.equal(submitted[3].approved_source.year_choice_confirmed,true);
 assert.equal(submitted[3].approved_source.author_list_confirmed,true);
 console.log('Full author display, date provenance and explicit metadata approval checks passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
