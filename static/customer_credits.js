/* Add the account credit choice without disturbing the existing checkout UI. */
(() => {
  'use strict';
  const match=location.pathname.match(/\/(?:new\/)?results\/([^/]+)/);
  if(!match) return;
  const job=decodeURIComponent(match[1]), panel=document.getElementById('paywallPanel');
  if(!panel) return;
  let pending=false, cached=null, fetched=0;
  async function mount() {
    if(pending || document.getElementById('customerCreditChoice') || !panel.querySelector('#planBox')) return;
    const box=document.createElement('div'); box.id='customerCreditChoice'; box.className='paywall-mini'; box.style.marginTop='14px';
    panel.querySelector('#planBox').after(box);
    const signIn=document.createElement('a'); signIn.href='/account'; signIn.textContent='Have a subscription or credits? Sign in to your account'; box.append(signIn);
    pending=true;
    try {
      if(!cached || Date.now()-fetched > 30000) {
        const response=await fetch(`/api/account/reports/${encodeURIComponent(job)}/quote`,{cache:'no-store'});
        if(!response.ok) return;
        cached=await response.json(); fetched=Date.now();
      }
      if(!box.isConnected) return;
      const quote=cached; box.replaceChildren();
      const text=document.createElement('p');
      text.textContent=quote.duplicate_of ? 'This manuscript already has an available review. No additional credits are needed.' : quote.already_unlocked ? 'This review is already unlocked. Refresh to see the complete report.' : `This Full Review costs ${quote.cost} credits. Your balance is ${quote.balance} credits.${quote.recheck_of ? ' Your included same-manuscript recheck applies.' : ''}`;
      box.append(text);
      const button=document.createElement('button'); button.className='btn btn-green';
      button.textContent=quote.duplicate_of ? 'Open existing review' : quote.already_unlocked ? 'Refresh review' : `Unlock with ${quote.cost} credits`;
      if(quote.duplicate_of || quote.already_unlocked) button.onclick=() => location.href=`/results/${encodeURIComponent(quote.duplicate_of || job)}`;
      else if(!quote.can_unlock) { button.textContent='Get more credits'; button.onclick=() => location.href='/account#plans'; }
      else button.onclick=async () => {
        button.disabled=true;
        try {
          const response=await fetch(`/api/account/reports/${encodeURIComponent(job)}/unlock`,{method:'POST',headers:{'Content-Type':'application/json','X-CI-Portal':'1'},body:JSON.stringify({expected_cost:quote.cost,recheck_of:quote.recheck_of || ''})});
          const outcome=await response.json(); if(!response.ok) throw new Error(outcome.detail || 'The review could not be unlocked.');
          location.href=`/results/${encodeURIComponent(outcome.job_id || job)}`;
        } catch(e) { text.textContent=e.message; cached=null; button.disabled=false; }
      };
      box.append(button);
      const account=document.createElement('a'); account.href='/account'; account.textContent='My Account'; account.style.marginLeft='15px'; box.append(account);
    } finally { pending=false; }
  }
  new MutationObserver(mount).observe(panel,{childList:true});
  mount();
})();
