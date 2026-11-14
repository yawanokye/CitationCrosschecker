"""Paid recovery uses real transactions and never creates another checkout."""
import asyncio
import ast
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import Mock,AsyncMock

import pytest
from fastapi import FastAPI,HTTPException,UploadFile
from fastapi.testclient import TestClient
import subscriptions as s
import paid_analysis_recovery as r

NOW=datetime(2030,1,15,tzinfo=timezone.utc)
HEADERS={'X-CI-Portal':'1','Origin':'http://testserver'}


@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setattr(s,'now_utc',lambda:NOW)
    db='sqlite:///'+str(tmp_path/'recovery.sqlite')
    s.init_portal_tables(db)
    with s.transaction(db) as cur:
        cur.execute('''CREATE TABLE purchases(id INTEGER PRIMARY KEY,user_email TEXT,status TEXT,
            payment_provider TEXT,provider_reference TEXT,preview_job_id TEXT,stripe_session_id TEXT,
            market TEXT,document_tier TEXT,preview_file_name TEXT,expires_at TEXT,created_at TEXT,
            analyses_used INTEGER)''')
        cur.execute('''CREATE TABLE purchase_runs(purchase_id INTEGER,job_id TEXT UNIQUE,reference_count INTEGER,citation_count INTEGER)''')
        cur.execute('''CREATE TABLE jobs(job_id TEXT PRIMARY KEY,status TEXT,file_name TEXT,result TEXT)''')
    return db


def owner(db,email='author@example.test'):
    messages=[]
    s.request_code(db,email,lambda *args:messages.append(args))
    token,account=s.verify_code(db,*messages[-1])
    return account,token


def put_job(db,job='failed-job',status='failed',result=None):
    with s.transaction(db) as cur:
        cur.execute('INSERT INTO jobs(job_id,status,file_name,result) VALUES(%s,%s,%s,%s) ON CONFLICT(job_id) DO UPDATE SET status=excluded.status,result=excluded.result',
                    (job,status,'Paper.docx',json.dumps(result or {})))


def paid(db,account,status='paid',provider='paystack'):
    with s.transaction(db) as cur:
        cur.execute('''INSERT INTO purchases VALUES(41,%s,%s,%s,'receipt-ref','failed-job','cs_saved','ghana','article','Paper.docx',%s,%s,1)''',
                    (account['email'],status,provider,s.stamp(NOW+timedelta(days=14)),s.stamp(NOW)))
    put_job(db)


def load(db,job):
    with s.transaction(db) as cur:return r._job(cur,job)


def prepare(db,account,identifier='receipt-ref'):
    return r.recovery_plan(db,account,identifier,lambda job:load(db,job))


def completed_result(**overrides):
    return {'summary':{'reference_entries_found':20,'in_text_citations_found':30,'word_count':4000},**overrides}


def test_paid_failure_prepares_same_payment_replacement_without_spending_run(db):
    account,_=owner(db);paid(db,account)
    plan=prepare(db,account)
    assert plan['action']=='retry' and plan['source_job_id']=='failed-job'
    r.reserve_replacement(db,account,'failed-job','replacement','Paper.docx',4000)
    put_job(db,'replacement','completed',completed_result())
    r.recovery_queued(db,'failed-job','replacement')
    for _ in range(2):assert r.finalise_replacement(db,'replacement',completed_result())['status']=='recovered'
    with s.transaction(db) as cur:
        assert cur.execute('SELECT analyses_used FROM purchases').one()['analyses_used']==1
        assert cur.execute('SELECT COUNT(*) AS n FROM purchase_runs').one()['n']==1
        assert cur.execute('SELECT purchase_id FROM purchase_runs').one()['purchase_id']==41
    assert prepare(db,account)['job_id']=='replacement'


def test_pending_paystack_payment_is_verified_before_recovery(db,monkeypatch):
    account,_=owner(db);paid(db,account,'pending')
    def verify(**kwargs):
        assert kwargs['reference']=='receipt-ref'
        with s.transaction(db) as cur:cur.execute("UPDATE purchases SET status='paid' WHERE id=41")
        return {'activated':True}
    call=Mock(side_effect=verify);monkeypatch.setattr(r,'verify_and_activate_purchase',call)
    assert prepare(db,account)['action']=='retry'
    call.assert_called_once()


def test_pending_stripe_payment_uses_saved_session(db,monkeypatch):
    account,_=owner(db);paid(db,account,'pending','stripe')
    def verify(**kwargs):
        assert kwargs['session_id']=='cs_saved' and kwargs['fallback_job_id']=='failed-job'
        with s.transaction(db) as cur:cur.execute("UPDATE purchases SET status='paid' WHERE id=41")
    call=Mock(side_effect=verify);monkeypatch.setattr(r,'verify_and_activate_stripe_session',call)
    assert prepare(db,account)['action']=='retry'
    call.assert_called_once()


def test_unverified_and_other_email_receipts_never_prepare_recovery(db,monkeypatch):
    account,_=owner(db);paid(db,account,'pending')
    call=Mock(return_value={'activated':False});monkeypatch.setattr(r,'verify_and_activate_purchase',call)
    with pytest.raises(s.PortalError) as result:prepare(db,account)
    assert result.value.status==409
    other,_=owner(db,'other@example.test')
    with pytest.raises(s.PortalError) as result:prepare(db,other)
    assert result.value.status==404
    with s.transaction(db) as cur:assert cur.execute('SELECT COUNT(*) AS n FROM ci_analysis_recovery').one()['n']==0


@pytest.mark.parametrize('state',['failed','error','incomplete','running'])
def test_incomplete_verification_reuses_original_paid_job(db,state):
    account,_=owner(db);paid(db,account)
    put_job(db,status='completed',result={'references_raw':['A reference'],'verification':{'state':state}})
    assert prepare(db,account)['action']=='verification'


def test_in_progress_and_complete_jobs_are_reopened_without_new_jobs(db):
    account,_=owner(db);paid(db,account)
    for state in ['queued','processing','completed']:
        put_job(db,status=state,result={'verification':{'state':'completed'}})
        assert prepare(db,account)['action']=='open'
    with s.transaction(db) as cur:assert cur.execute('SELECT COUNT(*) AS n FROM ci_analysis_recovery').one()['n']==0


def test_expired_paid_receipt_requires_support_without_new_checkout(db):
    account,_=owner(db);paid(db,account)
    with s.transaction(db) as cur:cur.execute('UPDATE purchases SET expires_at=%s',(s.stamp(NOW-timedelta(days=1)),))
    with pytest.raises(s.PortalError,match='do not pay again'):prepare(db,account)


def test_parallel_replacement_uploads_only_reserve_once(db):
    account,_=owner(db);paid(db,account);prepare(db,account)
    def reserve(i):
        try:r.reserve_replacement(db,account,'failed-job',f'retry-{i}','Paper.docx',4000);return True
        except s.PortalError:return False
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(reserve,range(8)))
    assert sum(results)==1


def test_wrong_document_oversize_and_revoked_purchase_cannot_use_recovery(db):
    account,_=owner(db);paid(db,account);prepare(db,account)
    for name,words in [('Other document.docx',4000),('Paper.docx',16000)]:
        with pytest.raises(s.PortalError):r.reserve_replacement(db,account,'failed-job','replacement',name,words)
    with s.transaction(db) as cur:cur.execute("UPDATE purchases SET status='revoked'")
    with pytest.raises(s.PortalError):r.reserve_replacement(db,account,'failed-job','replacement','Paper.docx',4000)


def test_incomplete_or_oversize_replacement_is_not_unlocked(db):
    account,_=owner(db);paid(db,account);prepare(db,account)
    r.reserve_replacement(db,account,'failed-job','replacement','Paper.docx',4000)
    put_job(db,'replacement','processing')
    assert r.finalise_replacement(db,'replacement',completed_result())['status']=='pending'
    put_job(db,'replacement','completed')
    result={'summary':{'reference_entries_found':101,'in_text_citations_found':30,'word_count':4000}}
    assert r.finalise_replacement(db,'replacement',result)['status']=='review_required'
    with s.transaction(db) as cur:assert cur.execute('SELECT COUNT(*) AS n FROM purchase_runs').one()['n']==0


def test_failed_recovery_can_be_retried_without_new_purchase(db):
    account,_=owner(db);paid(db,account);prepare(db,account)
    r.reserve_replacement(db,account,'failed-job','first','Paper.docx',4000)
    r.recovery_queued(db,'failed-job','first');put_job(db,'first','failed')
    assert prepare(db,account)['action']=='retry'
    r.reserve_replacement(db,account,'failed-job','second','Paper.docx',4000)
    assert s.snapshot(db,account['id'])['balance']==0
    with s.transaction(db) as cur:
        assert cur.execute('SELECT attempts FROM ci_analysis_recovery').one()['attempts']==2
        assert cur.execute('SELECT COUNT(*) AS n FROM purchases').one()['n']==1


def test_queue_failure_releases_recovery_reservation(db):
    account,_=owner(db);paid(db,account);prepare(db,account)
    r.reserve_replacement(db,account,'failed-job','first','Paper.docx',4000)
    r.release_failed_upload(db,'failed-job','first')
    r.reserve_replacement(db,account,'failed-job','second','Paper.docx',4000)


def test_credit_failure_refunds_once_and_retry_uses_restored_credits(db):
    account,_=owner(db);put_job(db)
    with s.transaction(db) as cur:s._lot(cur,account['id'],'test-paid-topup','topup',12,NOW,None)
    s.unlock_review(db,account['id'],'failed-job','article','fp',expected_cost=2)
    for _ in range(2):assert prepare(db,account,'failed-job')['credit_refund']
    assert s.snapshot(db,account['id'])['balance']==12
    r.reserve_replacement(db,account,'failed-job','replacement','Paper.docx',4000)
    put_job(db,'replacement','completed')
    assert r.finalise_replacement(db,'replacement',completed_result())['status']=='credits_restored'
    with s.transaction(db) as cur:assert cur.execute('SELECT COUNT(*) AS n FROM purchase_runs').one()['n']==0


def test_subscription_receipt_and_failed_free_preview_require_no_new_payment(db):
    account,_=owner(db)
    order=s.new_order(db,account['id'],topup=2)
    s.activate_order(db,order['reference'],{'status':'success','reference':order['reference'],'amount':1000,'currency':'GHS','customer':{'email':account['email']},'metadata':{'source_app':'citeintegrity','portal_order':order['reference']}})
    assert prepare(db,account,order['reference'])['action']=='upload'
    put_job(db);s.link_report(db,account['id'],'failed-job')
    assert prepare(db,account,'failed-job')['action']=='upload'
    assert s.snapshot(db,account['id'])['balance']==2


def test_failed_included_recheck_can_be_retried_for_zero_credits(db):
    account,_=owner(db)
    with s.transaction(db) as cur:s._lot(cur,account['id'],'test-topup','topup',12,NOW,None)
    s.unlock_review(db,account['id'],'original','article','fp',expected_cost=2)
    s.unlock_review(db,account['id'],'failed-job','article','revision-fp',expected_cost=0,recheck_of='original')
    put_job(db)
    plan=prepare(db,account,'failed-job')
    assert plan['recheck_of']=='original'
    r.reserve_replacement(db,account,'failed-job','replacement','Paper.docx',4000)
    put_job(db,'replacement','completed')
    assert r.finalise_replacement(db,'replacement',completed_result())['status']=='recheck_restored'
    assert s.quote_review(db,account['id'],'replacement','article','new-fp','original')['cost']==0
    assert s.snapshot(db,account['id'])['balance']==10


def test_recovery_endpoint_requires_signin_and_uses_verified_email(db):
    account,token=owner(db);paid(db,account)
    async def retry(request,plan):return {'upload_required':True,'upload_url':'/analyse?recover_job=failed-job'}
    async def verification(job):return None
    app=FastAPI();app.include_router(r.create_recovery_router(lambda:db,lambda job:load(db,job),retry,verification))
    client=TestClient(app)
    assert client.post('/api/account/recovery',json={'reference':'receipt-ref'},headers=HEADERS).status_code==401
    client.cookies.set('ci_customer_session',token)
    assert client.post('/api/account/recovery',json={'reference':'receipt-ref'}).status_code==403
    response=client.post('/api/account/recovery',json={'reference':'receipt-ref'},headers=HEADERS)
    assert response.status_code==200 and response.json()['paid'] and response.json()['upload_required']


def test_original_file_retry_routes_through_standard_upload_and_missing_file_falls_back():
    tree=ast.parse(Path('main.py').read_text())
    nodes=[node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_retry_paid_original']
    import io
    from urllib.parse import quote_plus
    redis=Mock();redis.get.return_value=b'original-file'
    verify=AsyncMock(return_value={'job_id':'replacement'})
    ns={'redis_conn':redis,'run_in_threadpool':__import__('starlette.concurrency',fromlist=['run_in_threadpool']).run_in_threadpool,
        'verify':verify,'UploadFile':UploadFile,'io':io,'quote_plus':quote_plus,'HTTPException':HTTPException,'_style_for_job_result':lambda result:'numeric_square'}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'retry_route','exec'),ns)
    plan={'source_job_id':'source','file_name':'Paper.docx','result':{}}
    result=asyncio.run(ns['_retry_paid_original'](object(),plan))
    assert result['results_url']=='/results/replacement'
    assert verify.call_args.kwargs['recover_job']=='source' and verify.call_args.kwargs['style']=='numeric_square'
    redis.get.return_value=None;verify.reset_mock()
    result=asyncio.run(ns['_retry_paid_original'](object(),plan))
    assert result['upload_required'] and 'recover_job=source' in result['upload_url']
    verify.assert_not_called()
