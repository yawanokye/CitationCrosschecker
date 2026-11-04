/* Exercise the production dashboard's request lifecycle with controlled time. */
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {JSDOM,VirtualConsole}=require('jsdom');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const requests=[], errors=[], timers=new Map();
let timerId=0, hidden=false, clock=1000000;
const vc=new VirtualConsole();
vc.on('jsdomError',error=>{if(!['css-parsing','not-implemented'].includes(error.type))errors.push(error);});
const source=fs.readFileSync('templates/new_results.html','utf8').replaceAll('{{ job_id }}','job')
 .replace('    init();',`window.pollingTest={
    render(data){latestResults=data;renderPaywall(data);},
    loadPlanRecommendation,fetchJsonNoCache,fetchVerificationStatus,stopPollingStatus,
    start(){verificationInProgress=true;startPollingStatus();},
    state(){return {enabled:statusPollingEnabled,inFlight:statusRequestInFlight,errors:statusErrorCount};}
 };`);
const dom=new JSDOM(source,{url:'https://citeintegrity.test/results/job',runScripts:'dangerously',
 pretendToBeVisual:true,virtualConsole:vc,beforeParse(window){
  window.CSS={escape:String};
  window.HTMLElement.prototype.scrollIntoView=function(){};
  window.Date.now=()=>clock;
  window.setTimeout=(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;};
  window.clearTimeout=id=>timers.delete(id);
  window.fetch=(url,options={})=>new Promise(resolve=>requests.push({url:String(url),options,resolve,settled:false}));
 }});
const {window}=dom;
Object.defineProperty(window.document,'hidden',{configurable:true,get:()=>hidden});
const $=id=>window.document.getElementById(id);
const count=part=>requests.filter(request=>request.url.includes(part)).length;
const pending=part=>requests.find(request=>!request.settled&&request.url.includes(part));
function respond(request,body,status=200,retry=0){
 assert.ok(request,'Expected pending request');request.settled=true;
 request.resolve({ok:status>=200&&status<300,status,headers:{get:()=>String(retry)},json:async()=>body});
}
function plan(currency){return {tiers:[{name:'Article',tier_key:'article',recommended:true,
 selected_currency:currency,selected_amount:10,selected_price_display:currency+' 10',max_words:15000,max_references:50}]};}
function runTimer(){
 assert.equal(timers.size,1,'Only one status timer should be queued');
 const [id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();return timer;
}
const preview={access:{paid:false},summary:{reference_entries_found:3,in_text_citations_found:4,word_count:2000}};
const running={online:{state:'running',progress:1,total:3,message:'Verifying references'}};
(async()=>{
 await tick();const api=window.pollingTest;assert.ok(api);
 // Seven rapid redraws must share one quote and preserve live form controls.
 api.render(preview);
 const email=$('payEmail');email.value='author@example.test';
 for(let i=0;i<7;i++)api.render(preview);
 assert.equal(count('/api/plans/recommend/'),1);
 assert.equal($('payEmail'),email);assert.equal($('payEmail').value,'author@example.test');
 // A slower previous-country response must not overwrite the latest selection.
 $('payCountry').value='NG';$('payCountry').dispatchEvent(new window.Event('change'));
 assert.equal(count('/api/plans/recommend/'),2);
 respond(pending('billing_country=NG'),plan('NGN'));await tick();
 assert.match($('planBox').textContent,/NGN/);
 respond(pending('billing_country=GH'),plan('GHS'));await tick();
 assert.match($('planBox').textContent,/NGN/);
 assert.equal(window.recommendedPlan.selected_currency,'NGN');
 // Revisiting a country reuses its short-lived quote.
 $('payCountry').value='GH';$('payCountry').dispatchEvent(new window.Event('change'));await tick();
 assert.equal(count('/api/plans/recommend/'),2);assert.match($('planBox').textContent,/GHS/);
 // Changed document counts refresh pricing, keeping both the email and country.
 const changed={...preview,summary:{...preview.summary,reference_entries_found:5}};
 api.render(changed);
 assert.equal($('payEmail').value,'author@example.test');assert.equal($('payCountry').value,'GH');
 assert.equal(count('/api/plans/recommend/'),3);
 respond(pending('billing_country=GH'),plan('GHS'));await tick();
 // Expired cache entries are refreshed, rather than used indefinitely.
 clock+=120001;const refresh=api.loadPlanRecommendation('GH');
 assert.equal(count('/api/plans/recommend/'),4);
 respond(pending('billing_country=GH'),plan('GHS'));await refresh;
 // A failed quote can be retried, and must not permit a stale-price checkout.
 const failed=api.loadPlanRecommendation('GH',true);
 respond(pending('billing_country=GH'),{},503);await failed;
 assert.equal($('btnPayUnlock').disabled,true);assert.equal(window.recommendedPlan,null);
 assert.ok($('btnRetryPlan'));$('btnRetryPlan').click();
 respond(pending('billing_country=GH'),plan('GHS'));await tick();
 assert.equal($('btnPayUnlock').disabled,false);
 // Two concurrent reads of the same result must share one HTTP request.
 const one=api.fetchJsonNoCache('/result/job?fresh=1');
 const two=api.fetchJsonNoCache('/result/job?fresh=1');
 assert.equal(count('/result/job'),1);
 respond(pending('/result/job'),{status:'completed',data:{value:1}});
 assert.deepEqual(await one,await two);
 // A failed shared request is removed so the next read can try again.
 const badOne=api.fetchJsonNoCache('/result/job?fresh=1').catch(error=>error.status);
 const badTwo=api.fetchJsonNoCache('/result/job?fresh=1').catch(error=>error.status);
 assert.equal(count('/result/job'),2);respond(pending('/result/job'),{},429,12);
 assert.equal(await badOne,429);assert.equal(await badTwo,429);
 const retry=api.fetchJsonNoCache('/result/job?fresh=1');
 assert.equal(count('/result/job'),3);respond(pending('/result/job'),{ok:true});await retry;
 // A slow status response must never create overlapping polls.
 for(let i=0;i<7;i++)api.start();
 assert.equal(count('/online/status'),1);assert.equal(timers.size,0);
 await api.fetchVerificationStatus();assert.equal(count('/online/status'),1);
 assert.match(pending('/online/status').url,/include_result=false/);
 respond(pending('/online/status'),running);await tick();
 assert.equal(timers.size,1);assert.equal(timers.values().next().value.delay,8000);
 runTimer();assert.equal(count('/online/status'),2);assert.equal(timers.size,0);
 await api.fetchVerificationStatus();assert.equal(count('/online/status'),2);
 respond(pending('/online/status'),running);await tick();
 assert.equal(timers.size,1);
 // Busy responses honor Retry-After and do not exhaust the failure counter.
 runTimer();respond(pending('/online/status'),{},429,12);await tick();
 assert.equal(timers.size,1);assert.equal(timers.values().next().value.delay,12000);
 assert.equal(api.state().errors,0);
 // Hidden tabs do not issue a document-status request on their timer.
 hidden=true;const before=count('/online/status');runTimer();await tick();
 assert.equal(count('/online/status'),before);assert.equal(timers.values().next().value.delay,60000);
 hidden=false;window.document.dispatchEvent(new window.Event('visibilitychange'));
 assert.equal(count('/online/status'),before+1);assert.equal(timers.size,0);
 respond(pending('/online/status'),running);await tick();
 // Stopping invalidates an in-flight response, including a late completion.
 runTimer();api.stopPollingStatus();const resultRequests=count('/result/job');
 respond(pending('/online/status'),{online:{state:'completed',progress:3,total:3}});await tick();
 assert.equal(timers.size,0);assert.equal(count('/result/job'),resultRequests);
 // Leaving the page cancels the polling lifecycle.
 api.start();assert.ok(pending('/online/status'));
 window.dispatchEvent(new window.Event('pagehide'));
 respond(pending('/online/status'),running);await tick();
 assert.equal(api.state().enabled,false);assert.equal(timers.size,0);
 assert.deepEqual(errors,[]);
 dom.window.close();
 console.log('Request lifecycle passed: quote deduplication, form preservation, country races, cache expiry, retries, shared reads, serial polls, backoff, visibility and late-response safety.');
})().catch(error=>{dom.window.close();console.error(error);process.exitCode=1;});
