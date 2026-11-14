/* Customer portal. Prices and credit decisions are supplied by the server. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const when = value => value ? new Date(value).toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'numeric'}) : 'Not scheduled';
  function safeUploadReturn(value) {
    if (!value || !value.startsWith('/') || value.startsWith('//')) return '';
    try {
      const url=new URL(value,location.origin);
      if (url.origin!==location.origin || !['/analyse','/new/analyse'].includes(url.pathname)) return '';
      const clean=new URL(url.pathname,location.origin), recheck=url.searchParams.get('recheck_of'),recovery=url.searchParams.get('recover_job');
      if(recheck && /^[a-zA-Z0-9_-]{1,100}$/.test(recheck))clean.searchParams.set('recheck_of',recheck);
      if(recovery && /^[a-zA-Z0-9_-]{1,100}$/.test(recovery))clean.searchParams.set('recover_job',recovery);
      return clean.pathname+clean.search;
    } catch (_) { return ''; }
  }
  const params=new URLSearchParams(location.search),returnTo=safeUploadReturn(params.get('next'));
  if(params.get('mode')==='create') {
    $('loginTitle').textContent='Create your free account'; $('loginEyebrow').textContent='GET STARTED';
    $('sendCode').textContent='Send verification code'; $('verifyAccount').textContent='Verify and create account';
  } else if(params.get('mode')==='signin') {
    $('loginTitle').textContent='Sign in to your account'; $('loginEyebrow').textContent='WELCOME BACK';
    $('verifyAccount').textContent='Sign in';
  }
  $('accountReturnNote').hidden=!returnTo;
  let plans, state, selected;
  function offerRecovery(reference) {
    $('recoveryReference').value=reference;
    $('recovery').scrollIntoView({behavior:'smooth',block:'center'});
    $('recoveryReference').focus();
  }
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
      headers: body === undefined ? {} : {'Content-Type':'application/json', 'X-CI-Portal':'1'},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || data.error || 'This action could not be completed. Please try again.');
    return data;
  }
  function notice(text, error=false) { $('notice').textContent=text; $('notice').hidden=!text; $('notice').classList.toggle('error',error); }
  async function busy(button, action) { button.disabled=true; try { await action(); } catch(e) { notice(e.message,true); } finally { button.disabled=false; } }
  function renderPlans() {
    const months = Number($('term').value), term = {1:'month',3:'quarter',12:'year'}[months];
    $('planCards').innerHTML = plans.plans.map(plan => {
      const price = plan.terms.find(t => t.months === months);
      return `<article class="card plan-card ${plan.plan_key === 'researcher' ? 'featured' : ''}"><p class="eyebrow">${esc(plan.name.toUpperCase())}</p><h3>${plan.monthly_credits} credits every month</h3><p class="price">GHS ${price.amount}<small> / ${term}</small></p><p class="muted">${months === 1 ? 'One month of access' : months + ' months prepaid, credits released monthly'}</p><ul><li>Full Integrity Reviews and ACII assessment</li><li>One same-manuscript recheck per review within 14 days</li><li>One month of unused-credit rollover</li><li>Available report downloads included</li></ul><button class="primary choose-plan" data-plan="${plan.plan_key}" ${!plans.payments_available ? 'disabled' : ''}>${state?.signed_in ? 'Choose ' + esc(plan.name) : 'Sign in to choose'}</button></article>`;
    }).join('');
    document.querySelectorAll('.choose-plan').forEach(button => button.addEventListener('click', () => {
      const plan = plans.plans.find(p => p.plan_key === button.dataset.plan);
      choose({plan_key:plan.plan_key, months}, `${plan.name} · ${term}`, `${plan.monthly_credits} credits released each month.`, plan.terms.find(t => t.months === months).amount);
    }));
    $('topupButton').disabled = !plans.payments_available;
  }
  function choose(payload, title, description, amount) {
    if (!state?.signed_in) { $('loginPanel').scrollIntoView({behavior:'smooth',block:'center'}); $('email').focus(); notice('Sign in to purchase a plan or top-up.'); return; }
    selected = payload; $('checkoutTitle').textContent=title; $('checkoutDescription').textContent=description;
    $('checkoutAmount').textContent=`GHS ${amount}`; $('manualRenewal').checked=false; $('checkoutDialog').showModal();
  }
  async function reports() {
    const data = await api('/api/account/reports');
    $('reports').innerHTML = data.reports.length ? data.reports.map(row => {
      const id=encodeURIComponent(row.job_id), recheck = row.unlocked && !row.recheck_of && new Date(row.recheck_until) > new Date();
      return `<tr><td><strong>${esc(row.file_name)}</strong><small>${when(row.created_at)}${row.expires_at ? ' · Content expires ' + when(row.expires_at) : ''}</small></td><td><span class="tag">${esc(row.status === 'completed' ? row.unlocked ? 'Full Review' : 'Preview ready' : row.status)}</span></td><td class="report-actions">${row.results_url ? `<a href="${esc(row.results_url)}">${row.unlocked ? 'Open review' : 'View preview and credit cost'}</a>` : '<span class="muted">' + (row.status === 'expired' ? 'Temporary content is no longer available.' : 'Processing status: ' + esc(row.status)) + '</span>'}${row.download_url ? `<a href="${esc(row.download_url)}">Download report</a>` : ''}${recheck ? `<a href="/analyse?recheck_of=${id}">Use included recheck</a>` : ''}<button type="button" class="recover-analysis" data-ref="${esc(row.job_id)}">Recover analysis</button></td></tr>`;
    }).join('') : '<tr><td colspan="3">No reports yet. Sign in before uploading to see your new reviews here.</td></tr>';
    document.querySelectorAll('.recover-analysis').forEach(button=>button.addEventListener('click',()=>offerRecovery(button.dataset.ref)));
  }
  async function refresh() {
    state=await api('/api/account');
    $('loginPanel').hidden=state.signed_in; $('accountPanel').hidden=!state.signed_in;
    $('signOut').hidden=!state.signed_in; $('paymentPanel').hidden=!state.signed_in;
    if (state.signed_in) {
      $('accountEmail').textContent=state.email; $('balance').textContent=state.balance;
      const subscription = state.subscription;
      $('planName').textContent=subscription ? plans.plans.find(p => p.plan_key === subscription.plan_key)?.name || subscription.plan_key : 'No active plan';
      $('planAllowance').textContent=subscription ? `${subscription.credits} credits released monthly` : 'Choose a plan below or use your remaining credits.';
      $('nextRelease').textContent=when(state.next_credit_release);
      $('renewalDate').textContent=subscription ? `Current term ends ${when(subscription.ends_at)}. Renew manually to continue.` : 'Renewal is manual. There are no automatic charges.';
      $('queuedRenewal').textContent=state.scheduled.length ? `Next prepaid term starts ${when(state.scheduled[0].starts_at)}.` : '';
      const expiry=state.credit_lots.filter(l => l.expires_at).sort((a,b) => a.expires_at.localeCompare(b.expires_at))[0];
      $('balanceDetail').textContent=expiry ? `${expiry.remaining} credits expire ${when(expiry.expires_at)}. Earliest-expiring credits are used first.` : 'Credits ready for your next review';
      $('payments').innerHTML=state.payments.length ? state.payments.map(row => `<tr><td>${when(row.created_at)}<small>${esc(row.reference)}</small></td><td>${row.kind === 'topup' ? row.credits + ' credit top-up' : row.kind === 'one_off' ? esc(row.plan_key || 'Document') + ' Full Review' : esc(row.plan_key) + ' · ' + row.months + ' month term'}</td><td>${esc(row.currency || 'GHS')} ${(row.amount_subunit/100).toFixed(2)}</td><td>${esc(row.status)}</td><td>${row.status === 'pending' ? `<button class="verify-payment" data-ref="${esc(row.reference)}">Check payment</button>` : ''}</td></tr>`).join('') : '<tr><td colspan="5">Your subscription and top-up payments will appear here.</td></tr>';
      $('creditActivity').innerHTML=state.ledger.length ? state.ledger.map(row => `<tr><td>${when(row.created_at)}</td><td>${esc(row.event)}</td><td>${row.units > 0 ? '+' : ''}${row.units}</td></tr>`).join('') : '<tr><td colspan="3">No credit activity yet.</td></tr>';
      document.querySelectorAll('.verify-payment').forEach(button => button.addEventListener('click', () => busy(button, async () => { const result=await api(`/api/account/payments/${encodeURIComponent(button.dataset.ref)}/verify`,{}); notice(result.message); await refresh(); })));
      await reports();
    }
    renderPlans();
  }
  $('emailForm').addEventListener('submit', event => { event.preventDefault(); busy($('sendCode'),async () => {
    const result=await api('/api/account/sign-in',{email:$('email').value}); notice(result.message); $('emailForm').hidden=true; $('codeForm').hidden=false; $('code').focus();
  }); });
  $('codeForm').addEventListener('submit', event => { event.preventDefault(); busy(event.submitter,async () => {
    await api('/api/account/verify-code',{email:$('email').value,code:$('code').value}); $('code').value='';
    if(returnTo){window.location.href=returnTo;return;}
    notice('You are signed in. Your account is ready.'); await refresh();
  }); });
  $('changeEmail').addEventListener('click', () => { $('emailForm').hidden=false; $('codeForm').hidden=true; $('code').value=''; $('email').focus(); });
  $('signOut').addEventListener('click', () => busy($('signOut'),async () => { await api('/api/account/logout',{}); window.location.href='/account'; }));
  $('recoveryForm').addEventListener('submit',event=>{event.preventDefault();busy($('recoverAnalysis'),async()=>{
    const result=await api('/api/account/recovery',{reference:$('recoveryReference').value.trim()});
    const box=$('recoveryResult');box.replaceChildren();box.hidden=false;
    const message=document.createElement('p');message.textContent=result.message;box.append(message);
    const path=result.upload_url||result.results_url;
    if(path && path.startsWith('/')&&!path.startsWith('//')){const link=document.createElement('a');link.href=path;link.textContent=result.upload_required?'Upload the same manuscript':'Open recovered analysis';box.append(link);}
    await refresh();
  });});
  $('term').addEventListener('change',renderPlans);
  $('topup').addEventListener('input', () => { $('topupAmount').textContent=`GHS ${Number($('topup').value)*5}`; });
  $('topupForm').addEventListener('submit', event => { event.preventDefault(); const count=Number($('topup').value); choose({topup:count},`${count} credit top-up`,'Top-up credits do not expire.',count*5); });
  $('cancelCheckout').addEventListener('click', () => $('checkoutDialog').close());
  $('checkoutForm').addEventListener('submit', event => { event.preventDefault(); busy($('confirmCheckout'), async () => {
    const result=await api('/api/account/checkout',{...selected,accept_manual_renewal:$('manualRenewal').checked}); window.location.href=result.checkout_url;
  }); });
  (async () => { try { plans=await api('/api/account/plans'); await refresh(); const payment=new URLSearchParams(location.search).get('payment'); if(payment) notice(payment === 'success' ? 'Payment verified. Your account has been updated.' : 'Payment is awaiting verification. Use Check payment in your payment history. Please do not pay again.',payment !== 'success'); if(!plans.payments_available) notice('New payments are currently unavailable. Your existing credits and reports remain accessible.'); } catch(e) { notice(e.message,true); } })();
})();
