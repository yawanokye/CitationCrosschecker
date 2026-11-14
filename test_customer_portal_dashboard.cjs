/* Exercise the actual account and credit-choice scripts in a DOM. */
const fs=require('node:fs');
const assert=require('node:assert/strict');
const {JSDOM,VirtualConsole}=require('jsdom');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const errors=[],requests=[];
const vc=new VirtualConsole();
vc.on('jsdomError',error=>{if(!['css-parsing','not-implemented'].includes(error.type))errors.push(error);});
const catalogue={payments_available:true,plans:[
 {plan_key:'individual',name:'Individual',monthly_credits:12,terms:[{months:1,amount:49},{months:3,amount:135},{months:12,amount:490}]},
 {plan_key:'researcher',name:'Researcher',monthly_credits:30,terms:[{months:1,amount:99},{months:3,amount:270},{months:12,amount:990}]}]};
let signedIn=false;
const account={signed_in:true,email:'author@example.test',balance:12,subscription:{plan_key:'individual',credits:12,ends_at:'2027-02-28T10:00:00Z'},next_credit_release:null,scheduled:[],credit_lots:[{remaining:12,expires_at:'2027-03-31T10:00:00Z'}],payments:[{reference:'CISUB-TEST',kind:'subscription',plan_key:'individual',months:1,amount_subunit:4900,status:'paid',created_at:'2027-01-31T10:00:00Z'}],ledger:[{event:'release',units:12,created_at:'2027-01-31T10:00:00Z'}]};
const dom=new JSDOM(fs.readFileSync('templates/account.html','utf8'),{url:'https://citeintegrity.test/account',runScripts:'outside-only',virtualConsole:vc,pretendToBeVisual:true});
const w=dom.window,$=id=>w.document.getElementById(id);
w.HTMLElement.prototype.scrollIntoView=function(){};
w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
w.HTMLDialogElement.prototype.close=function(){this.open=false;};
w.fetch=async(url,options={})=>{
 const body=options.body ? JSON.parse(options.body) : undefined;
 requests.push({url:String(url),body,options});
 let data={ok:true};
 if(url==='/api/account/plans')data=catalogue;
 else if(url==='/api/account')data=signedIn ? account : {signed_in:false};
 else if(url==='/api/account/sign-in')data={message:'Check your email for a code.'};
 else if(url==='/api/account/verify-code')signedIn=true;
 else if(url==='/api/account/reports')data={reports:[{job_id:'job',file_name:'<img src=x onerror=alert(1)>',status:'completed',unlocked:true,created_at:'2027-01-31T10:00:00Z',results_url:'/results/job',download_url:'/api/report-package/job?delete_after=0',recheck_until:'2099-01-01T00:00:00Z'}]};
 else if(url==='/api/account/checkout')data={checkout_url:'https://checkout.paystack.com/test'};
 else if(url==='/api/account/recovery')data={paid:true,upload_required:true,upload_url:'/analyse?recover_job=job',message:'Payment confirmed. Recover without another payment.'};
 return {ok:true,status:200,json:async()=>data};
};
function submit(id,button){$(id).dispatchEvent(new w.SubmitEvent('submit',{bubbles:true,cancelable:true,submitter:button || $(id).querySelector('button')}));}
w.eval(fs.readFileSync('static/customer_portal.js','utf8'));
(async()=>{
 await tick();await tick();
 assert.equal($('loginPanel').hidden,false);
 assert.match($('planCards').textContent,/GHS 49/);
 assert.match($('planCards').textContent,/GHS 99/);
 $('term').value='12';$('term').dispatchEvent(new w.Event('change'));
 assert.match($('planCards').textContent,/GHS 490/);assert.match($('planCards').textContent,/GHS 990/);
 w.document.querySelector('.choose-plan').click();
 assert.equal($('checkoutDialog').open,false);
 assert.equal(requests.filter(r=>r.url==='/api/account/checkout').length,0);
 $('email').value='author@example.test';submit('emailForm');await tick();
 assert.equal($('codeForm').hidden,false);assert.equal($('emailForm').hidden,true);
 $('code').value='123456';submit('codeForm');await tick();await tick();
 assert.equal($('loginPanel').hidden,true);assert.equal($('balance').textContent,'12');
 assert.equal($('accountEmail').textContent,'author@example.test');
 assert.equal($('reports').querySelector('img'),null,'Report names must be escaped');
 assert.match($('reports').textContent,/Download report/);
 assert.match($('payments').textContent,/GHS 49.00/);
 w.document.querySelector('.recover-analysis').click();
 assert.equal($('recoveryReference').value,'job');
 submit('recoveryForm');await tick();await tick();
 assert.match($('recoveryResult').textContent,/without another payment/);
 assert.match($('recoveryResult').querySelector('a').href,/recover_job=job/);
 assert.deepEqual(requests.find(r=>r.url==='/api/account/recovery').body,{reference:'job'});
 w.document.querySelector('.choose-plan').click();
 assert.equal($('checkoutDialog').open,true);
 assert.equal($('checkoutAmount').textContent,'GHS 490');
 assert.equal($('manualRenewal').checked,false,'Prepaid renewal acceptance must not be preselected');
 $('manualRenewal').checked=true;submit('checkoutForm');await tick();
 const checkout=requests.find(r=>r.url==='/api/account/checkout');
 assert.deepEqual(checkout.body,{plan_key:'individual',months:12,accept_manual_renewal:true});
 assert.equal(checkout.options.headers['X-CI-Portal'],'1');
 assert.equal($('confirmCheckout').disabled,false);
 assert.deepEqual(errors,[]);dom.window.close();

 const credits=new JSDOM('<div id="paywallPanel"><div><div id="planBox"></div></div></div>',{url:'https://citeintegrity.test/results/job',runScripts:'outside-only',virtualConsole:vc});
 const cw=credits.window;let quoteCalls=0,unlockBody;
 cw.fetch=async(url,options={})=>{
  if(String(url).endsWith('/quote')){quoteCalls++;return {ok:true,json:async()=>({cost:4,balance:12,can_unlock:true})};}
  unlockBody=JSON.parse(options.body);return {ok:true,json:async()=>({job_id:'job'})};
 };
 cw.eval(fs.readFileSync('static/customer_credits.js','utf8'));await tick();
 const box=cw.document.getElementById('customerCreditChoice');assert.match(box.textContent,/costs 4 credits/);
 assert.match(box.textContent,/balance is 12 credits/);assert.equal(quoteCalls,1);
 box.querySelector('button').click();await tick();assert.deepEqual(unlockBody,{expected_cost:4,recheck_of:''});
 assert.equal(quoteCalls,1);credits.window.close();assert.deepEqual(errors,[]);
 console.log('Customer portal DOM checks passed: sign-in, plan terms, prices, receipts, safe report links, manual-renewal consent and pre-unlock credit confirmation.');
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
