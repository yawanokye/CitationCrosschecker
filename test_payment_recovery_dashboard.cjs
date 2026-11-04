/* Run with node test_payment_recovery_dashboard.cjs. Development dependency: jsdom 27. */
const fs = require('node:fs');
const assert = require('node:assert/strict');
const {JSDOM, VirtualConsole} = require('jsdom');
const tick = () => new Promise(resolve => setImmediate(resolve));
const errors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', e => { if (!['css-parsing','not-implemented'].includes(e.type)) errors.push(e); });
const html = fs.readFileSync('templates/new_results.html', 'utf8')
  .replaceAll('{{ job_id }}', 'report-job')
  .replace('    init();', `
    window.paymentTest = {
        restorePaymentAccess, startCheckout,
        render(data) { latestResults=data; renderPaywall(data); }
    };`);
let recoverResponse = {ok:true, status:200, body:{ok:true, paid:false, message:'No confirmed payment. Please do not pay again.'}};
let heldRecovery;
const requests = [];
const preview = {access:{paid:false}, summary:{reference_entries_found:1}, file_name:'Paper.docx'};
const dom = new JSDOM(html, {url:'https://citeintegrity.test/results/report-job', runScripts:'dangerously',
  pretendToBeVisual:true, virtualConsole:vc, beforeParse(window) {
    window.CSS = {escape:String};
    window.alert = () => {};
    window.HTMLElement.prototype.scrollIntoView = function() {};
    window.fetch = async (url, options={}) => {
      requests.push({url:String(url), options});
      if (String(url).includes('/recover/')) {
        if (heldRecovery) await heldRecovery;
        const current = recoverResponse;
        return {ok:current.ok, status:current.status, json:async()=>current.body};
      }
      if (String(url).includes('/initialize')) return {ok:true, status:200, json:async()=>({ok:true, session_id:'cs_test_new', checkout_url:'https://checkout.stripe.test'})};
      if (String(url).startsWith('/result/')) return {ok:true, status:200, json:async()=>({status:'completed', data:{...preview, access:{paid:true}}})};
      return {ok:true, status:200, json:async()=>({ok:true, tiers:[{tier_key:'article', recommended:true, name:'Article', max_words:15000, max_references:50}]})};
    };
  }});
const {window} = dom;
const $ = id => window.document.getElementById(id);
(async () => {
  await tick();
  const test = window.paymentTest;
  assert.ok(test);
  test.render(preview);
  await tick();
  assert.ok($('btnRestorePayment'));
  window.localStorage.setItem('ci_checkout:report-job', 'cs_test_existing');
  await test.restorePaymentAccess();
  assert.equal(JSON.parse(requests.find(r=>r.url.includes('/recover/')).options.body).session_id, 'cs_test_existing');
  assert.equal($('btnRestorePayment').disabled, false);
  assert.match($('paymentRecoveryNotice').textContent, /do not pay again/);
  assert.equal(window.localStorage.getItem('ci_checkout:report-job'), 'cs_test_existing');
  // Failed fulfillment stays retryable and never displays unlocked success.
  recoverResponse = {ok:false, status:503, body:{message:'Report access is being restored. Please do not pay again.'}};
  await test.restorePaymentAccess();
  assert.match($('paymentRecoveryNotice').textContent, /being restored/);
  assert.equal($('btnRestorePayment').disabled, false);
  // Concurrent clicks produce only one recovery request.
  let release;
  heldRecovery = new Promise(resolve=>{release=resolve;});
  const before = requests.filter(r=>r.url.includes('/recover/')).length;
  const running = test.restorePaymentAccess();
  await test.restorePaymentAccess();
  assert.equal(requests.filter(r=>r.url.includes('/recover/')).length, before+1);
  release(); await running; heldRecovery = null;
  // Restore remains available when new checkout is suspended.
  test.render({...preview, payments_suspended:true});
  assert.ok($('btnRestorePayment'));
  assert.equal($('btnPayUnlock'), null);
  // Provider messages are rendered as text.
  recoverResponse = {ok:false, status:503, body:{message:'<img id="unsafePaymentHtml" src="bad">'}};
  await test.restorePaymentAccess();
  assert.equal($('unsafePaymentHtml'), null);
  // Successful checkout persists the session before navigation.
  test.render(preview); await tick();
  $('payEmail').value='author@example.test';
  await test.startCheckout();
  assert.equal(window.localStorage.getItem('ci_checkout:report-job'), 'cs_test_new');
  // Verified recovery refreshes paid data and removes the stale checkout.
  recoverResponse = {ok:true, status:200, body:{ok:true, paid:true, message:'Payment confirmed. Full Review is available.'}};
  await test.restorePaymentAccess();
  assert.equal(window.localStorage.getItem('ci_checkout:report-job'), null);
  assert.match($('paymentRecoveryNotice').textContent, /Payment confirmed/);
  assert.ok(requests.some(r=>r.url.startsWith('/result/')));
  assert.match($('paywallPanel').textContent, /Full Review unlocked/);
  assert.deepEqual(errors, []);
  dom.window.close();
  console.log('Payment recovery dashboard passed: saved checkout, retry, concurrent clicks, suspension, text safety and paid refresh.');
})().catch(e=>{dom.window.close(); console.error(e); process.exitCode=1;});
