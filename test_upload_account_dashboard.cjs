/* Account creation is offered before upload, without starting an upload. */
const fs=require('node:fs'),assert=require('node:assert/strict');
const {JSDOM}=require('jsdom');
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const upload=fs.readFileSync('templates/new_analyse.html','utf8');
function page(signedIn){
 const dom=new JSDOM(upload,{url:'https://citeintegrity.test/analyse?recheck_of=original-job',runScripts:'outside-only'});
 dom.window.HTMLElement.prototype.scrollIntoView=function(){};
 const requests=[];
 dom.window.fetch=async url=>{requests.push(url);return {ok:true,json:async()=>signedIn?{signed_in:true,email:'author@example.test',balance:12}:{signed_in:false}};};
 dom.window.eval(fs.readFileSync('static/upload_account.js','utf8'));
 return {dom,requests};
}
(async()=>{
 const {dom,requests}=page(false),doc=dom.window.document;
 await tick();
 assert.ok(doc.getElementById('uploadAccountPrompt').compareDocumentPosition(doc.getElementById('fileArea')) & dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
 for(const [id,mode] of [['uploadCreateAccount','create'],['uploadSignIn','signin']]){
  const url=new URL(doc.getElementById(id).href);
  assert.equal(url.pathname,'/account');assert.equal(url.searchParams.get('mode'),mode);
  assert.equal(url.searchParams.get('next'),'/analyse?recheck_of=original-job');
 }
 assert.deepEqual(requests,['/api/account'],'Account choices must not upload or consume credits');
 doc.getElementById('uploadAsGuest').click();assert.equal(doc.getElementById('uploadAccountPrompt').hidden,true);
 assert.equal(requests.length,1);dom.window.close();
 const signed=page(true);await tick();
 const sd=signed.dom.window.document;
 assert.equal(sd.getElementById('uploadCreateAccount').hidden,true);
 assert.equal(sd.getElementById('uploadSignIn').hidden,true);
 assert.equal(sd.getElementById('uploadAsGuest').hidden,true);
 assert.equal(sd.getElementById('uploadMyAccount').hidden,false);
 assert.match(sd.getElementById('uploadAccountDescription').textContent,/author@example.test/);
 assert.match(sd.getElementById('uploadAccountDescription').textContent,/12/);signed.dom.window.close();

 const account=new JSDOM(fs.readFileSync('templates/account.html','utf8'),{url:'https://citeintegrity.test/account?mode=create&next=%2Fanalyse',runScripts:'outside-only'});
 account.window.fetch=async url=>({ok:true,json:async()=>url==='/api/account/plans'?{plans:[],payments_available:true}:{signed_in:false}});
 account.window.eval(fs.readFileSync('static/customer_portal.js','utf8').replace('  let plans, state, selected;','  window.testReturn = safeUploadReturn;\n  let plans, state, selected;'));
 await tick();
 assert.equal(account.window.document.getElementById('loginTitle').textContent,'Create your free account');
 assert.equal(account.window.document.getElementById('accountReturnNote').hidden,false);
 const safe=account.window.testReturn;
 assert.equal(safe('/analyse?recheck_of=original-job'),'/analyse?recheck_of=original-job');
 assert.equal(safe('/analyse?redirect=https://evil.example'),'/analyse');
 for(const url of ['https://evil.example','//evil.example','/\\evil.example','/account','/api/account/logout'])assert.equal(safe(url),'');
 account.window.close();
 console.log('Pre-upload account checks passed: Create account, Sign in, guest choice, authenticated state, preserved recheck and safe upload return.');
})().catch(error=>{console.error(error);process.exitCode=1;});
