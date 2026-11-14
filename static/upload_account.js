/* Give customers an account choice before selecting a document. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id),prompt=$('uploadAccountPrompt');
  if(!prompt)return;
  const current=new URL(location.href),next=new URL('/analyse',location.origin);
  const recheck=current.searchParams.get('recheck_of');
  const recovery=current.searchParams.get('recover_job');
  if(recheck && /^[a-zA-Z0-9_-]{1,100}$/.test(recheck))next.searchParams.set('recheck_of',recheck);
  if(recovery && /^[a-zA-Z0-9_-]{1,100}$/.test(recovery))next.searchParams.set('recover_job',recovery);
  if(recovery){
    $('uploadAccountTitle').textContent='Recover your paid analysis';
    $('uploadAccountDescription').textContent='Sign in with the payment email, then upload the same manuscript using its original filename. Your verified payment will be reused. Failed credit reviews use their restored credits.';
    $('uploadAsGuest').hidden=true;
  }
  for(const [id,mode] of [['uploadCreateAccount','create'],['uploadSignIn','signin']]) {
    const url=new URL('/account',location.origin);url.searchParams.set('mode',mode);url.searchParams.set('next',next.pathname+next.search);$(id).href=url.pathname+url.search;
  }
  $('uploadAsGuest').addEventListener('click',()=>{
    prompt.hidden=true;$('fileArea')?.scrollIntoView({behavior:'smooth',block:'center'});
  });
  fetch('/api/account',{cache:'no-store'}).then(response=>response.ok?response.json():null).then(account=>{
    if(!account?.signed_in || !prompt.isConnected)return;
    $('uploadAccountTitle').textContent='Your account is ready for this upload';
    $('uploadAccountDescription').textContent=recovery ? `Signed in as ${account.email}. Upload the same manuscript using its original filename to complete recovery. No new checkout is required.` : `Signed in as ${account.email}. Your new reports will appear in My Account. Available credits: ${account.balance}.`;
    for(const id of ['uploadCreateAccount','uploadSignIn','uploadAsGuest'])$(id).hidden=true;
    $('uploadMyAccount').hidden=false;
  }).catch(()=>{});
})();
