"""Real ledger transactions, boundary and concurrency tests. Gateways are fake."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from copy import deepcopy

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import subscriptions as s
import subscription_portal as portal

START = datetime(2027, 1, 31, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    value = 'sqlite:///' + str(tmp_path / 'ledger.sqlite')
    s.init_portal_tables(value)
    s.init_portal_tables(value)
    return value


def customer(db, email='author@example.test', now=START):
    delivered = []
    s.request_code(db, email, lambda *args: delivered.append(args), now)
    token, account = s.verify_code(db, email, delivered[0][1], now)
    return account, token


def verified(saved, email='author@example.test'):
    return {'status':'success', 'reference':saved['reference'], 'amount':saved['amount_subunit'],
            'currency':'GHS', 'customer':{'email':email},
            'metadata':{'source_app':'citeintegrity', 'portal_order':saved['reference']}}


def subscribe(db, owner, plan='individual', months=1, now=START):
    saved=s.new_order(db,owner['id'],plan_key=plan,months=months,now=now)
    return s.activate_order(db,saved['reference'],verified(saved,owner['email']),now)


@pytest.mark.parametrize('plan,months,amount,credits', [
    ('individual',1,4900,12),('individual',3,13500,12),('individual',12,49000,12),
    ('researcher',1,9900,30),('researcher',3,27000,30),('researcher',12,99000,30)])
def test_all_six_prices_and_monthly_allowances(db,plan,months,amount,credits):
    owner,_=customer(db)
    saved=s.new_order(db,owner['id'],plan_key=plan,months=months,now=START)
    assert saved['amount_subunit']==amount
    assert s.snapshot(db,owner['id'],START)['balance']==0
    s.activate_order(db,saved['reference'],verified(saved),START)
    state=s.snapshot(db,owner['id'],START)
    assert state['balance']==credits
    assert state['subscription']['ends_at']==s.stamp(s.add_months(START,months))
    assert state['renewal']=='manual'


@pytest.mark.parametrize('field,value',[('status','pending'),('amount',4899),('amount',4900.0),
                                      ('currency','USD'),('reference','CISUB-wrong'),
                                      ('customer',{'email':'other@example.test'}),
                                      ('metadata',{'source_app':'other'}),('metadata',[])])
def test_payment_mismatch_never_grants_credits(db,field,value):
    owner,_=customer(db)
    saved=s.new_order(db,owner['id'],plan_key='individual',now=START)
    data=verified(saved); data[field]=value
    with pytest.raises(s.PortalError): s.activate_order(db,saved['reference'],data,START)
    assert s.snapshot(db,owner['id'],START)['balance']==0


def test_callbacks_and_webhooks_activate_only_once_under_concurrency(db):
    owner,_=customer(db)
    saved=s.new_order(db,owner['id'],plan_key='individual',months=12,now=START)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _: s.activate_order(db,saved['reference'],verified(saved),START),range(20)))
    assert all(item['status']=='paid' for item in results)
    assert s.snapshot(db,owner['id'],START)['balance']==12
    with s.transaction(db) as cur:
        assert cur.execute('SELECT COUNT(*) AS count FROM ci_credit_lots').one()['count']==1


def test_january_31_leap_year_and_original_month_anchor():
    assert s.add_months(START,1).day==28
    assert s.add_months(START,2).day==31
    leap=START.replace(year=2028)
    assert s.add_months(leap,1).day==29
    assert s.add_months(leap,2).day==31


def test_monthly_releases_rollover_and_skipped_visits(db):
    owner,_=customer(db); subscribe(db,owner,months=12)
    assert s.snapshot(db,owner['id'],s.add_months(START,1)-timedelta(microseconds=1))['balance']==12
    assert s.snapshot(db,owner['id'],s.add_months(START,1))['balance']==24
    assert s.snapshot(db,owner['id'],s.add_months(START,2))['balance']==24
    assert s.snapshot(db,owner['id'],s.add_months(START,9))['balance']==24
    assert s.snapshot(db,owner['id'],s.add_months(START,12))['balance']==12
    assert s.snapshot(db,owner['id'],s.add_months(START,13))['balance']==0


def test_early_renewals_queue_and_clipped_months_never_stack_three_allowances(db):
    owner,_=customer(db)
    first=subscribe(db,owner)
    second=subscribe(db,owner,now=START+timedelta(days=2))
    assert second['starts_at']==first['ends_at']
    third=subscribe(db,owner,now=START+timedelta(days=3))
    assert third['starts_at']==second['ends_at']
    assert s.snapshot(db,owner['id'],s.date(second['starts_at']))['balance']==24
    assert s.snapshot(db,owner['id'],s.date(third['starts_at']))['balance']==24


def test_changed_plan_caps_rollover_to_one_new_allowance(db):
    owner,_=customer(db); first=subscribe(db,owner,plan='researcher')
    subscribe(db,owner,plan='individual',now=START+timedelta(days=2))
    state=s.snapshot(db,owner['id'],s.date(first['ends_at']))
    assert state['balance']==24


def test_topups_are_fixed_price_idempotent_and_do_not_expire(db):
    owner,_=customer(db)
    saved=s.new_order(db,owner['id'],topup=2,now=START)
    assert saved['amount_subunit']==1000
    for _ in range(2): s.activate_order(db,saved['reference'],verified(saved),START)
    assert s.snapshot(db,owner['id'],s.add_months(START,24))['balance']==2


@pytest.mark.parametrize('kwargs',[{'topup':1},{'topup':2.5},{'topup':True},{'topup':-1},
    {'plan_key':'individual','months':2},{'plan_key':'individual','months':True},
    {'plan_key':'individual','topup':2},{'plan_key':'fake'}])
def test_invalid_order_cannot_change_ledger(db,kwargs):
    owner,_=customer(db)
    with pytest.raises(s.PortalError): s.new_order(db,owner['id'],now=START,**kwargs)


@pytest.mark.parametrize('tier,cost',[('article',2),('research_paper',4),('thesis',7),('phd',10)])
def test_server_quote_and_unlock_match_all_tiers(db,tier,cost):
    owner,_=customer(db); subscribe(db,owner,plan='researcher')
    quote=s.quote_review(db,owner['id'],'job',tier,'fingerprint',now=START)
    assert quote['cost']==cost and quote['can_unlock']
    s.unlock_review(db,owner['id'],'job',tier,'fingerprint',expected_cost=cost,now=START)
    assert s.snapshot(db,owner['id'],START)['balance']==30-cost
    assert s.grant(db,'job')['tier_key']==tier


def test_stale_quote_insufficient_balance_and_repeat_unlock(db):
    owner,_=customer(db); subscribe(db,owner)
    with pytest.raises(s.PortalError):
        s.unlock_review(db,owner['id'],'job','phd','fp',expected_cost=2,now=START)
    assert s.snapshot(db,owner['id'],START)['balance']==12
    s.unlock_review(db,owner['id'],'job','phd','fp',expected_cost=10,now=START)
    assert s.unlock_review(db,owner['id'],'job','phd','fp',expected_cost=10,now=START)['already_unlocked']
    with pytest.raises(s.PortalError): s.unlock_review(db,owner['id'],'job2','thesis','fp2',expected_cost=7,now=START)
    assert s.snapshot(db,owner['id'],START)['balance']==2


def test_concurrent_unlocks_cannot_overspend(db):
    owner,_=customer(db); subscribe(db,owner)
    def spend(i):
        try: return s.unlock_review(db,owner['id'],f'job-{i}','phd',f'fp-{i}',expected_cost=10,now=START)
        except s.PortalError as exc: return exc.status
    with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(spend,range(8)))
    assert sum(isinstance(result,dict) for result in results)==1
    assert s.snapshot(db,owner['id'],START)['balance']==2


def test_duplicate_manuscript_returns_existing_report_without_charge(db):
    owner,_=customer(db); subscribe(db,owner)
    s.unlock_review(db,owner['id'],'job','article','fp',expected_cost=2,now=START)
    result=s.unlock_review(db,owner['id'],'same-upload','article','fp',expected_cost=2,now=START)
    assert result['duplicate_of']=='job' and result['cost']==0
    assert s.snapshot(db,owner['id'],START)['balance']==10


def test_recheck_is_once_same_tier_within_14_days_and_not_transferable(db):
    owner,_=customer(db); subscribe(db,owner)
    s.unlock_review(db,owner['id'],'original','article','fp',expected_cost=2,now=START)
    s.unlock_review(db,owner['id'],'revision','article','new-fp',expected_cost=0,recheck_of='original',now=START+timedelta(days=13))
    assert s.snapshot(db,owner['id'],START+timedelta(days=13))['balance']==10
    for job,parent in [('again','original'),('chain','revision')]:
        with pytest.raises(s.PortalError): s.unlock_review(db,owner['id'],job,'article',job,expected_cost=0,recheck_of=parent,now=START)
    other,_=customer(db,'other@example.test')
    with pytest.raises(s.PortalError): s.unlock_review(db,other['id'],'steal','article','different',expected_cost=0,recheck_of='original',now=START)


def test_recheck_expiry_and_oversize_are_enforced(db):
    owner,_=customer(db); subscribe(db,owner)
    s.unlock_review(db,owner['id'],'job','article','fp',expected_cost=2,now=START)
    with pytest.raises(s.PortalError): s.unlock_review(db,owner['id'],'larger','phd','new',expected_cost=0,recheck_of='job',now=START)
    with pytest.raises(s.PortalError): s.unlock_review(db,owner['id'],'late','article','new',expected_cost=0,recheck_of='job',now=START+timedelta(days=14))


def test_refund_restores_exact_spend_once_even_after_lot_expired(db):
    owner,_=customer(db); subscribe(db,owner)
    s.unlock_review(db,owner['id'],'job','article','fp',expected_cost=2,now=START)
    assert s.refund_failed_review(db,'job',START)
    assert not s.refund_failed_review(db,'job',START)
    assert s.snapshot(db,owner['id'],START)['balance']==12
    s.unlock_review(db,owner['id'],'job2','article','fp2',expected_cost=2,now=START)
    assert s.refund_failed_review(db,'job2',s.add_months(START,3))
    assert s.snapshot(db,owner['id'],s.add_months(START,3))['balance']==2
    assert s.grant(db,'job2') is None


def test_email_codes_single_use_attempt_limit_hashed_sessions_and_logout(db):
    delivered=[]
    s.request_code(db,'Author@Example.test',lambda *args: delivered.append(args),START)
    email,code=delivered[0]
    for _ in range(5):
        with pytest.raises(s.PortalError): s.verify_code(db,email,'incorrect',START)
    with pytest.raises(s.PortalError): s.verify_code(db,email,code,START)
    owner,token=customer(db,now=START+timedelta(minutes=1))
    with s.transaction(db) as cur:
        assert cur.execute('SELECT token_hash FROM ci_sessions').one()['token_hash']!=token
    assert s.session_account(db,token,START)['id']==owner['id']
    assert s.session_account(db,token,START+timedelta(days=31)) is None
    s.logout(db,token)
    assert s.session_account(db,token,START) is None
    with pytest.raises(s.PortalError): s.verify_code(db,email,code,START)


def test_expired_code_and_persistent_email_rate_limit(db):
    delivered=[]
    s.request_code(db,'author@example.test',lambda *args: delivered.append(args),START)
    with pytest.raises(s.PortalError): s.verify_code(db,delivered[0][0],delivered[0][1],START+timedelta(minutes=10))
    for _ in range(2): s.request_code(db,'author@example.test',lambda *args:None,START)
    with pytest.raises(s.PortalError): s.request_code(db,'author@example.test',lambda *args:None,START)


def test_revised_content_must_match_beyond_filename():
    text=' '.join(f'researchword{i}' for i in range(300))
    original=portal.manuscript_signature({'main_text':text},'account-salt')
    assert portal.signatures_match(original,portal.manuscript_signature({'main_text':text+' A short added sentence.'},'account-salt'))
    assert not portal.signatures_match(original,portal.manuscript_signature({'main_text':' '.join(f'otherword{i}' for i in range(300))},'account-salt'))
    assert not portal.signatures_match({}, {})


@pytest.fixture
def client(db,monkeypatch):
    monkeypatch.setenv('PAYMENT_COOKIE_SECURE','false')
    monkeypatch.setattr(s,'now_utc',lambda:START)
    messages=[]
    monkeypatch.setattr(portal,'send_login_code',lambda *args: messages.append(args))
    jobs={'ready':{'status':'completed','result':{'main_text':' '.join(f'word{i}' for i in range(100))}},
          'failed':{'status':'failed','result':{}}}
    def counts(job):
        if job not in jobs: raise HTTPException(404,'Not found')
        if jobs[job].get('result',{}).get('result_deleted'): raise HTTPException(410,'Content deleted')
        if jobs[job]['status']!='completed': raise HTTPException(409,'Complete the analysis first.')
        return {'reference_count':40,'citation_count':80,'word_count':2000}
    app=FastAPI(); mode={'mode':'payment_required'}
    app.include_router(portal.create_portal_router(lambda:db,counts,lambda job:deepcopy(jobs.get(job)),lambda:mode,lambda job:False))
    monkeypatch.setattr(portal,'report_metadata',lambda *args:{})
    monkeypatch.setattr(portal,'one_off_reports',lambda *args:[])
    monkeypatch.setattr(portal,'one_off_payments',lambda *args:[])
    monkeypatch.setattr(portal,'reconcile_failed_reviews',lambda *args:None)
    client=TestClient(app)
    return client,messages,mode,jobs


HEADERS={'X-CI-Portal':'1','Origin':'http://testserver'}


def login(client):
    browser,messages,_,_=client
    assert browser.post('/api/account/sign-in',json={'email':'author@example.test'},headers=HEADERS).status_code==200
    response=browser.post('/api/account/verify-code',json={'email':messages[-1][0],'code':messages[-1][1]},headers=HEADERS)
    assert response.status_code==200 and 'HttpOnly' in response.headers['set-cookie']
    return browser


def test_routes_require_signin_and_same_origin(client):
    browser,_,_,_=client
    assert not browser.get('/api/account').json()['signed_in']
    assert browser.get('/api/account/reports/ready/quote').status_code==401
    assert browser.post('/api/account/sign-in',json={'email':'author@example.test'}).status_code==403
    assert browser.post('/api/account/sign-in',json={'email':'author@example.test'},headers={**HEADERS,'Origin':'https://evil.example'}).status_code==403


def test_failed_analysis_and_forged_quote_never_spend(client,db):
    browser=login(client)
    owner=s.session_account(db,browser.cookies[portal.COOKIE],START); subscribe(db,owner)
    assert browser.post('/api/account/reports/failed/unlock',json={'expected_cost':2},headers=HEADERS).status_code==409
    assert browser.post('/api/account/reports/ready/unlock',json={'expected_cost':0},headers=HEADERS).status_code==409
    quote=browser.get('/api/account/reports/ready/quote').json()
    assert quote['cost']==2 and quote['balance']==12
    assert browser.post('/api/account/reports/ready/unlock',json={'expected_cost':2},headers=HEADERS).status_code==200
    assert browser.get('/api/account').json()['balance']==10


def test_checkout_requires_manual_terms_and_rejects_suspended_mode(client,monkeypatch):
    browser=login(client)
    assert browser.post('/api/account/checkout',json={'plan_key':'individual'},headers=HEADERS).status_code==400
    client[2]['mode']='payments_suspended'
    assert browser.post('/api/account/checkout',json={'plan_key':'individual','accept_manual_renewal':True},headers=HEADERS).status_code==409


def test_checkout_sends_exact_amount_and_portal_callback(client,monkeypatch):
    browser=login(client)
    monkeypatch.setattr(portal,'_require_secret_key',lambda *a:'test-key')
    sent=[]
    def gateway(method,path,payload,market):
        sent.append(payload)
        return {'status':True,'data':{'authorization_url':'https://checkout.paystack.com/test'}}
    monkeypatch.setattr(portal,'_paystack_request',gateway)
    response=browser.post('/api/account/checkout',json={'plan_key':'researcher','months':12,'accept_manual_renewal':True,'amount':1},headers=HEADERS)
    assert response.status_code==200 and sent[0]['amount']==99000
    assert sent[0]['callback_url'].endswith('/payment/subscription/callback')
    assert 'plan' not in sent[0]  # One prepaid charge, not a recurring mandate.


def test_webhook_signature_is_required_and_mismatches_never_activate(db,monkeypatch):
    import hashlib,hmac,json
    owner,_=customer(db); saved=s.new_order(db,owner['id'],plan_key='individual',now=START)
    raw=json.dumps({'event':'charge.success','data':{'reference':saved['reference']}}).encode()
    monkeypatch.setattr(portal,'_require_secret_key',lambda *a:'secret')
    monkeypatch.setattr(portal,'_paystack_request',lambda *a,**k:{'status':True,'data':verified(saved)})
    monkeypatch.setattr(s,'now_utc',lambda:START)
    assert portal.portal_webhook(db,raw,'invalid')['status_code']==400
    assert s.snapshot(db,owner['id'],START)['balance']==0
    signature=hmac.new(b'secret',raw,hashlib.sha512).hexdigest()
    for _ in range(2): assert portal.portal_webhook(db,raw,signature)['status_code']==200
    assert s.snapshot(db,owner['id'],START)['balance']==12


def test_other_account_cannot_unlock_owned_report(db):
    first,_=customer(db); second,_=customer(db,'other@example.test')
    s.link_report(db,first['id'],'private-job')
    subscribe(db,second)
    with pytest.raises(s.PortalError): s.unlock_review(db,second['id'],'private-job','article','fp',expected_cost=2,now=START)
    assert s.snapshot(db,second['id'],START)['balance']==12


def test_revision_can_use_short_lived_signature_after_original_content_deletion(client,db):
    browser=login(client)
    owner=s.session_account(db,browser.cookies[portal.COOKIE],START);subscribe(db,owner)
    assert browser.post('/api/account/reports/ready/unlock',json={'expected_cost':2},headers=HEADERS).status_code==200
    client[3]['revised']={'status':'completed','result':{'main_text':client[3]['ready']['result']['main_text']+' A revision adds this sentence.'}}
    s.link_report(db,owner['id'],'revised','ready',START)
    client[3]['ready']={'status':'expired','result':{'result_deleted':True}}
    quote=browser.get('/api/account/reports/revised/quote').json()
    assert quote['cost']==0 and quote['recheck_of']=='ready'
    assert browser.post('/api/account/reports/revised/unlock',json={'expected_cost':0},headers=HEADERS).status_code==200
    assert browser.get('/api/account').json()['balance']==10


def test_recheck_rejects_a_different_manuscript_with_no_deduction(client,db):
    browser=login(client)
    owner=s.session_account(db,browser.cookies[portal.COOKIE],START);subscribe(db,owner)
    browser.post('/api/account/reports/ready/unlock',json={'expected_cost':2},headers=HEADERS)
    client[3]['different']={'status':'completed','result':{'main_text':' '.join(f'other{i}' for i in range(100))}}
    response=browser.post('/api/account/reports/different/unlock',json={'expected_cost':0,'recheck_of':'ready'},headers=HEADERS)
    assert response.status_code==409
    assert browser.get('/api/account').json()['balance']==10


def test_signature_and_expired_auth_records_are_cleared_after_recheck_window(db):
    owner,_=customer(db);subscribe(db,owner)
    signature=portal.manuscript_signature({'main_text':' '.join(f'word{i}' for i in range(100))},owner['id'])
    s.unlock_review(db,owner['id'],'job','article','fp',expected_cost=2,signature=signature,now=START)
    assert 'word0' not in s.grant(db,'job')['signature']
    assert portal.signatures_match(s.grant(db,'job')['signature'],signature)
    s.forget_expired_report_fingerprint(db,'job')
    s.cleanup_portal_auth(db,START+timedelta(days=14))
    assert s.grant(db,'job')['signature']=='{}' and s.grant(db,'job')['fingerprint']==''


def test_annual_plan_still_shows_after_more_than_100_topups(db):
    owner,_=customer(db);subscribe(db,owner,months=12)
    for i in range(101):
        saved=s.new_order(db,owner['id'],topup=2,now=START+timedelta(seconds=i+1))
        s.activate_order(db,saved['reference'],verified(saved),START+timedelta(seconds=i+1))
    state=s.snapshot(db,owner['id'],START+timedelta(hours=1))
    assert len(state['payments'])==100 and state['subscription']['plan_key']=='individual'
    assert state['balance']==214


def test_duplicate_after_temporary_deletion_offers_included_recheck(client,db):
    browser=login(client)
    owner=s.session_account(db,browser.cookies[portal.COOKIE],START);subscribe(db,owner)
    browser.post('/api/account/reports/ready/unlock',json={'expected_cost':2},headers=HEADERS)
    text=client[3]['ready']['result']['main_text']
    client[3]['ready']['result']={'result_deleted':True}
    client[3]['same-upload']={'status':'completed','result':{'main_text':text}}
    response=browser.get('/api/account/reports/same-upload/quote')
    assert response.status_code==200
    quote=response.json()
    assert quote['cost']==0 and quote['recheck_of']=='ready' and 'duplicate_of' not in quote
    response=browser.post('/api/account/reports/same-upload/unlock',json={'expected_cost':0,'recheck_of':quote['recheck_of']},headers=HEADERS)
    assert response.status_code==200
    assert browser.get('/api/account').json()['balance']==10


def test_one_off_purchases_are_visible_for_verified_email_without_credit_charge(client,db,monkeypatch):
    browser=login(client)
    owner=s.session_account(db,browser.cookies[portal.COOKIE],START)
    s.link_report(db,owner['id'],'ready',now=START)
    monkeypatch.setattr(portal,'one_off_reports',lambda *args:[{'job_id':'ready','created_at':s.stamp(START),'credits':0}])
    monkeypatch.setattr(portal,'one_off_payments',lambda *args:[{'reference':'old-receipt','kind':'one_off','currency':'USD','amount_subunit':299,'status':'paid','created_at':s.stamp(START)}])
    monkeypatch.setattr(portal,'report_metadata',lambda *args:{'ready':{'status':'completed','deleted':False,'file_name':'Paper.docx'}})
    report=browser.get('/api/account/reports').json()['reports'][0]
    assert report['unlocked'] and report['download_url']
    state=browser.get('/api/account').json()
    assert state['balance']==0 and state['payments'][0]['currency']=='USD'
