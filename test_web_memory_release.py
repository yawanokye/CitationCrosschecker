"""Memory regressions with controlled database, ASGI and document fixtures."""
import ast
import asyncio
import gc
import io
import json
import threading
import tracemalloc
import weakref
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, Mock

import pytest
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from web_memory import BoundedJobStore, WebMemoryGuard, PREFLIGHT_BYTES, estimate_upload_load


def document_xml(texts):
    paragraphs = ''.join('<w:p><w:r><w:t>'+text+'</w:t></w:r></w:p>' for text in texts)
    return ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            +paragraphs+'</w:body></w:document>').encode()


def docx(xml, media=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('word/document.xml', xml)
        if media:
            archive.writestr('word/media/image1.png', media)
    return buffer.getvalue()


def test_preflight_counts_document_and_table_text_without_loading_media(monkeypatch):
    xml = document_xml(['sustainable procurement', 'climate policy'])
    raw = docx(xml, b'not-a-real-image' * 100000)
    original = zipfile.ZipFile.open
    def open_part(self, name, *args, **kwargs):
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        assert not filename.startswith('word/media/'), 'Web preflight must not unpack manuscript images'
        return original(self, name, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'open', open_part)
    info = estimate_upload_load(raw, 'Paper.docx')
    assert info['word_count'] == 4 and info['queue_name'] == 'document_processing'


def test_preflight_counts_split_word_runs_as_one_word():
    raw = docx(document_xml(['sustainable']).replace(b'sustainable', b'sus</w:t></w:r><w:r><w:t>tainable'))
    assert estimate_upload_load(raw, 'Paper.docx')['word_count'] == 1


def test_large_document_goes_to_worker_without_pdf_parsing():
    reader = Mock(side_effect=AssertionError('must not parse large PDF in web process'))
    info = estimate_upload_load(b'x'*(PREFLIGHT_BYTES+1), 'Thesis.pdf', pdf_reader=reader)
    assert info['large_file'] and info['queue_name'] == 'large_document_processing'
    reader.assert_not_called()


def test_large_compressed_document_xml_is_not_expanded_by_preflight():
    info = estimate_upload_load(docx(b'x'*(PREFLIGHT_BYTES+1)), 'Thesis.docx')
    assert info['large_file'] and info['word_count'] is None


@pytest.mark.parametrize('prefix', [b'', b' ' * 65532])
def test_doctype_anywhere_in_document_defers_parsing(prefix):
    xml = prefix + b'<!DOCTYPE foo [<!ENTITY example "expansion">]>' + document_xml(['test'])
    assert estimate_upload_load(docx(xml), 'Paper.docx')['large_file']


@pytest.mark.parametrize('raw,filename', [(b'invalid','Paper.docx'), (b'invalid','Paper.pdf')])
def test_failed_preflight_still_routes_for_worker_review(raw, filename):
    info = estimate_upload_load(raw, filename, pdf_reader=Mock(side_effect=ValueError('invalid')))
    assert info['large_file']


def test_normal_pdf_and_word_thresholds_preserve_queue_routing():
    assert estimate_upload_load(b'%PDF', 'paper.pdf', pdf_reader=lambda _: SimpleNamespace(pages=[None]*2))['page_count'] == 2
    info = estimate_upload_load(docx(document_xml(['one two three'])), 'paper.docx', normal_docx_words=2)
    assert info['large_file'] and info['word_count'] == 3


def test_job_store_evicts_old_entries_and_expires_without_deleting_persistent_data():
    clock = [0]
    store = BoundedJobStore(max_entries=2, max_bytes=10000, ttl=10, clock=lambda: clock[0])
    store['a']={'result':'a'}
    store['b']={'result':'b'}
    assert store['a']['result'] == 'a'
    store['c']={'result':'c'}
    assert set(store) == {'a','c'}
    clock[0] = 11
    assert len(store) == 0


def test_cache_budget_accounts_for_mutated_records():
    store = BoundedJobStore(max_bytes=2048)
    record = {'result':'small'}
    store['job'] = record
    record['result'] = 'x'*10000
    assert store.get('job') is None
    store['large'] = {'data':b'x'*10000}
    assert len(store) == 0


def test_cache_stress_does_not_retain_all_200_results():
    store = BoundedJobStore(max_entries=256, max_bytes=1024*1024)
    tracemalloc.start()
    try:
        for index in range(200):
            store[str(index)] = {'result':bytes([index%255])*128000}
        _, peak = tracemalloc.get_traced_memory()
        assert len(store) <= 8
        assert peak < 4*1024*1024
    finally:
        tracemalloc.stop()


def load_functions(names, namespace):
    defaults = {'Dict':Dict, 'Any':Any, 'Optional':Optional, 'datetime':datetime,
                'timedelta':timedelta, 'json':json, '_lock':threading.Lock(), '_store':{},
                'DATABASE_URL':'fixture', 'redis_conn':None, 'RealDictCursor':object}
    defaults.update(namespace)
    nodes = [node for node in ast.parse(Path('main.py').read_text()).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in nodes:
        node.decorator_list=[]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'memory_routes', 'exec'), defaults)
    return defaults


def fake_connection(row):
    cursor = MagicMock()
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor, SimpleNamespace(connect=lambda *a, **kw:conn)


def test_persistent_job_reads_do_not_populate_the_process_cache():
    fresh = Mock(return_value={'result':{'new':'result'}})
    store = {'job':{'result':{'old':'stale'}}}
    ns=load_functions({'load_job_record','get_job'}, {'_store':store, 'load_job_record_fresh':fresh})
    assert ns['load_job_record']('job')['result'] == {'new':'result'}
    assert ns['get_job']('evicted')['result'] == {'new':'result'}
    assert set(store) == {'job'}


def test_fresh_job_read_does_not_serialize_another_redis_copy():
    conn, cursor, database = fake_connection({'status':'completed','result':{'summary':{}},'error':None})
    redis = Mock()
    ns=load_functions({'load_job_record_fresh'}, {'psycopg2':database, 'redis_conn':redis,
        'reconcile_verification_meta':lambda result:{'state':'completed'}})
    assert ns['load_job_record_fresh']('job')['status'] == 'completed'
    redis.setex.assert_not_called()
    assert conn.close.called


def test_cleanup_fetches_only_ids_then_releases_each_complete_result():
    class Payload(dict):
        pass
    alive=[]
    peak=[0]
    conn,cursor,database=fake_connection(None)
    cursor.fetchall.return_value=[{'job_id':str(index)} for index in range(30)]
    def one_result():
        payload=Payload(main_text='private text'*1000)
        alive.append(weakref.ref(payload))
        peak[0]=max(peak[0],sum(ref() is not None for ref in alive))
        return {'result':payload}
    cursor.fetchone.side_effect=one_result
    redis=Mock()
    ns=load_functions({'cleanup_expired_manuscript_content'}, {'psycopg2':database,
        'CONTENT_TTL_SECONDS':86400, 'redis_conn':redis,
        '_snapshot_feature_usage':lambda result:None,
        'purge_result_content':lambda result, **kw:{'result_deleted':True},
        'redis_content_keys':lambda job:[f'result:{job}']})
    result=ns['cleanup_expired_manuscript_content']()
    assert result == {'checked':30,'deleted':30}
    assert peak[0] == 1
    assert all(ref() is None for ref in alive)
    first_query=cursor.execute.call_args_list[0].args[0]
    assert 'SELECT job_id FROM' in first_query and 'SELECT job_id, result' not in first_query
    assert cursor.fetchone.call_count == 30 and redis.delete.call_count == 30
    assert conn.commit.call_count == 31


def test_polling_does_not_write_the_manuscript_back_to_storage():
    _,_,database=fake_connection({'status':'completed','result':{'main_text':'private manuscript'},'error':None})
    prep=Mock(side_effect=lambda job, result, **kw:result)
    ns=load_functions({'get_result'}, {'Request':object,'psycopg2':database,
        '_demo_prepare_full_review_result':prep, '_prepare_student_result':lambda result, *a:result})
    result=asyncio.run(ns['get_result'](object(),'job',fresh=1))
    assert result['status'] == 'completed'
    assert prep.call_args.kwargs['persist'] is False


async def call_guard(guard,path,*,method='GET',headers=None,chunks=None):
    messages=[]
    parts=iter(chunks or [b''])
    async def receive():
        try:
            chunk=next(parts)
        except StopIteration:
            return {'type':'http.request','body':b'','more_body':False}
        return {'type':'http.request','body':chunk,'more_body':True}
    async def send(message):
        messages.append(message)
    await guard({'type':'http','path':path,'method':method,'headers':headers or []},receive,send)
    return messages


def test_heavy_capacity_is_held_through_response_delivery_and_health_bypasses_it():
    async def scenario():
        headers_sent=asyncio.Event()
        finish_body=asyncio.Event()
        async def app(scope,receive,send):
            await send({'type':'http.response.start','status':200,'headers':[]})
            if scope['path']=='/result/slow':
                headers_sent.set()
                await finish_body.wait()
            await send({'type':'http.response.body','body':b'ok'})
        guard=WebMemoryGuard(app,max_active=1)
        first=asyncio.create_task(call_guard(guard,'/result/slow'))
        await headers_sent.wait()
        assert (await call_guard(guard,'/result/other'))[0]['status'] == 429
        assert (await call_guard(guard,'/health'))[0]['status'] == 200
        assert (await call_guard(guard,'/api/webhooks/stripe'))[0]['status'] == 200
        finish_body.set()
        assert (await first)[0]['status'] == 200
        assert guard.active == 0
        assert (await call_guard(guard,'/result/other'))[0]['status'] == 200
    asyncio.run(scenario())


def test_heavy_slot_is_released_after_cancellation():
    async def scenario():
        started=asyncio.Event()
        async def app(scope,receive,send):
            started.set()
            await asyncio.Event().wait()
        guard=WebMemoryGuard(app,max_active=1)
        pending=asyncio.create_task(call_guard(guard,'/result/slow'))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert guard.active == 0
    asyncio.run(scenario())


def test_large_declared_upload_is_rejected_before_body_or_parser():
    invoked=Mock()
    async def app(scope,receive,send):
        invoked()
    guard=WebMemoryGuard(app,max_upload_bytes=32)
    response=asyncio.run(call_guard(guard,'/verify',method='POST',headers=[(b'content-length',b'2000000')]))
    assert response[0]['status']==413 and guard.active==0
    invoked.assert_not_called()


def test_chunked_upload_limit_remains_413_through_real_fastapi_multipart_parser():
    app=FastAPI()
    app.add_middleware(WebMemoryGuard,max_upload_bytes=32)
    reached=[]
    @app.post('/verify')
    async def route(file:UploadFile=File(...)):
        reached.append(True)
        return {'ok':True}
    data=b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="large.docx"\r\nContent-Type: application/octet-stream\r\n\r\n'+b'x'*1200000+b'\r\n--boundary--\r\n'
    client=TestClient(app)
    response=client.post('/verify',content=iter([data[:600000],data[600000:]]),headers={'content-type':'multipart/form-data; boundary=boundary'})
    assert response.status_code==413
    assert reached==[]


def test_production_upload_reads_only_limit_plus_one_byte_and_closes_file():
    class Upload:
        filename='paper.docx'
        limit_read=None
        closed=False
        async def read(self, size):
            self.limit_read=size
            return b'x'*size
        async def close(self):
            self.closed=True
    upload=Upload()
    ns=load_functions({'verify'}, {'UploadFile':UploadFile,'File':lambda *a:None,
        'Form':lambda *a:None,'Request':object,'JSONResponse':JSONResponse,
        'MAX_UPLOAD_BYTES':32,'is_server_busy':lambda:False})
    result=asyncio.run(ns['verify'](file=upload,style='apa',enable_autofix='false',
        enable_online_verification='true',enable_academic_voice='false',request=None))
    assert result.status_code==413 and upload.limit_read==33 and upload.closed
