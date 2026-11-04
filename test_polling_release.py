"""Pricing projection and optional status payload regressions."""
import ast
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from entitlements import build_plan_selection_payload
from payment_router import resolve_payment_market
from verification_coverage import reconcile_verification_meta


def load(names, **context):
    ns={'Dict':Dict, 'Any':Any, 'Request':Request, 'HTTPException':HTTPException,
        'JSONResponse':JSONResponse,'jsonable_encoder':jsonable_encoder,
        'run_in_threadpool':run_in_threadpool,'DATABASE_URL':'fixture','RealDictCursor':object,
        'now':lambda:'2026-11-04T13:09:59Z', 'redis_conn':None,
        'reconcile_verification_meta':reconcile_verification_meta}
    ns.update(context)
    nodes=[node for node in ast.parse(Path('main.py').read_text()).body
           if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list=[]
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'polling_routes','exec'),ns)
    return ns


def pricing(row):
    cursor=Mock()
    cursor.fetchone.return_value=row
    conn=Mock(cursor=Mock(return_value=cursor))
    fallback=Mock(return_value={'word_count':500,'reference_count':1,'citation_count':2,'file_name':'Legacy.docx'})
    ns=load({'_commercial_plan_counts'},psycopg2=SimpleNamespace(connect=Mock(return_value=conn)),
            _commercial_job_counts=fallback)
    return ns['_commercial_plan_counts'],fallback,conn,cursor


def row(**updates):
    data={'status':'completed','summary':{'word_count':12000,'reference_entries_found':40,'in_text_citations_found':60},
          'raw_references':30,'references':10,'raw_citations':50,'citations':5,'file_name':'Paper.docx',
          'preflight_words':0,'result_words':0,'content_deleted':False}
    data.update(updates)
    return data


def test_pricing_loads_only_metadata_and_keeps_authoritative_summary_counts():
    get,fallback,conn,cursor=pricing(row())
    assert get('job')=={'word_count':12000,'reference_count':40,'citation_count':60,'file_name':'Paper.docx'}
    fallback.assert_not_called()
    query=cursor.execute.call_args.args[0]
    assert 'SELECT status, file_name' in query and 'result->\'summary\'' in query
    assert 'SELECT *' not in query and 'main_text' not in query
    assert conn.close.called and cursor.close.called


@pytest.mark.parametrize('summary,preflight,result_words,expected',[
    ({},'9000',10000,9000), ({},None,'7000',7000),
    ({'word_count':3000},'9000',10000,3000)])
def test_pricing_word_count_precedence_matches_checkout(summary,preflight,result_words,expected):
    get,fallback,_,_=pricing(row(summary=summary,preflight_words=preflight,result_words=result_words))
    counts=get('job')
    assert counts['word_count']==expected and counts['reference_count']==30 and counts['citation_count']==50
    fallback.assert_not_called()


def test_legacy_count_fallback_keeps_original_text_count_rules():
    get,fallback,_,_=pricing(row(summary={},preflight_words=None,result_words=None))
    assert get('job')['file_name']=='Legacy.docx'
    fallback.assert_called_once_with('job')


@pytest.mark.parametrize('data,status',[(None,404),(row(status='running'),409),(row(content_deleted=True),410)])
def test_projected_quote_preserves_missing_processing_and_deleted_checks(data,status):
    get,fallback,conn,_=pricing(data)
    with pytest.raises(HTTPException) as error:
        get('job')
    assert error.value.status_code==status and conn.close.called
    fallback.assert_not_called()


def test_count_projection_failure_falls_back_and_closes_connection():
    get,fallback,conn,cursor=pricing(row())
    cursor.execute.side_effect=RuntimeError('simulated database failure')
    assert get('job')['word_count']==500 and conn.close.called
    fallback.assert_called_once()


def test_quote_route_returns_prices_without_exposing_document_counts():
    projected=Mock(return_value={'reference_count':40,'citation_count':60,'word_count':12000})
    ns=load({'recommend_commercial_plan'}, _commercial_plan_counts=projected,
            resolve_payment_market=resolve_payment_market,build_plan_selection_payload=build_plan_selection_payload)
    result=asyncio.run(ns['recommend_commercial_plan']('job',billing_country='GH'))
    assert result['market']['currency']=='GHS' and result['ok']
    assert all(key not in result for key in ['reference_count','citation_count','word_count'])
    assert any(plan['selected_amount']==10 for plan in result['tiers'])


def status_context(completed=False,redis=None):
    refs=[f'Author{i}, J. (2024). Research methods.' for i in range(3)]
    result={'main_text':'PRIVATE MANUSCRIPT','references_raw':refs,
        'summary':{'reference_entries_found':3},
        'verification':{'state':'completed' if completed else 'running','rq_job_id':'verify-job','progress':3 if completed else 1,'total':3},
        'online_verification':{'rows':[{'reference':ref,'status':'verified'} for ref in (refs if completed else refs[:1])]},
        'final_tables_ready':completed}
    prepare=Mock(side_effect=lambda job,result,**kw:result)
    present=Mock(side_effect=lambda result,*a:result)
    build=Mock(return_value={'items':[]})
    update=Mock()
    ns=load({'online_status'},load_job_record_fresh=lambda job:{'result':copy.deepcopy(result)},
        _demo_prepare_full_review_result=prepare,_prepare_student_result=present,
        build_correction_plan=build,update_verification_status=update,redis_conn=redis)
    return ns,prepare,present,build,update


def test_small_status_payload_preserves_progress_without_manuscript_or_presentation():
    ns,prepare,present,build,update=status_context(completed=True)
    response=ns['online_status'](Mock(),'job',include_result=False)
    body=json.loads(response.body)
    assert response.status_code==200 and body['online']['progress']==3 and body['online']['total']==3
    assert body['online']['state']=='completed' and 'result' not in body
    assert b'PRIVATE MANUSCRIPT' not in response.body
    for mock in (prepare,present,build,update):
        mock.assert_not_called()


def test_older_status_clients_still_receive_full_result_by_default():
    ns,prepare,present,build,_=status_context(completed=True)
    body=json.loads(ns['online_status'](Mock(),'job').body)
    assert body['result']['main_text']=='PRIVATE MANUSCRIPT'
    prepare.assert_called_once()
    present.assert_called_once()
    build.assert_called_once()


def test_small_status_request_checks_queue_without_rewriting_entire_result(monkeypatch):
    from rq.job import Job
    monkeypatch.setattr(Job,'fetch',Mock(return_value=SimpleNamespace(get_status=lambda **k:'started')))
    ns,_,_,_,update=status_context(redis=Mock())
    body=json.loads(ns['online_status'](Mock(),'job',include_result=False).body)
    assert body['online']['rq_status']=='started' and body['online']['state']=='running'
    assert body['online']['progress']==1 and body['online']['total']==3
    update.assert_not_called()
