const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
const source = fs.readFileSync('templates/new_results.html', 'utf8');
assert.ok(source.includes('    init();'));
const page = source.replaceAll('{{ job_id }}', 'test-job').replace('    init();', '    window.renderACIIForTest = renderACII;');
const errors = [];
const console = new VirtualConsole();
console.on('jsdomError', error => { if (error.type !== 'css-parsing' && error.type !== 'not-implemented') errors.push(error); });
const dom = new JSDOM(page, {url:'https://citeintegrity.test/results/test', runScripts:'dangerously', virtualConsole:console});
const {window} = dom;
const text = id => window.document.getElementById(id).textContent;
(async () => {
await new Promise(resolve => setImmediate(resolve));
window.renderACIIForTest({access:{paid:false}, summary:{reference_entries_found:20},
    acii:{locked:true, message:'Unlock Full Review for the assessment.',
          coverage:{publication_status_checked:12, expected_references:20, complete:false}}});
assert.equal(text('aciiScore'),'Locked');
assert.match(text('aciiRecommendation'),/Unlock Full Review/);
assert.match(text('aciiCoverage'),/12 of 20 references checked/);
assert.match(text('aciiCoverage'),/requires completed checks/);
window.renderACIIForTest({access:{paid:true}, summary:{reference_entries_found:20},
    acii:{ACII:82, score_available:true, category:'Good', recommendations:{priority:'Improve references'},
          coverage:{publication_status_checked:20, expected_references:20, complete:true}}});
assert.equal(text('aciiScore'),'82');
assert.match(text('aciiRecommendation'),/Improve references/);
assert.deepEqual(errors,[]);
dom.window.close();
})().catch(error => { dom.window.close(); process.stderr.write(String(error) + '\n'); process.exitCode=1; });
