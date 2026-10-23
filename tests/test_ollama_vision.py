"""Ollama integration uses mocked HTTP only: no billable inference in tests."""

import asyncio
from io import BytesIO
import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest

from app.main import app
from app.services import ocr, scrape
from app.services.article import FetchedArticle
from app.services.errors import ArticleFetchError, OCRError
from app.services.images import DownloadedImage
from app.services.ollama_vision import (
    FIELD_NAMES, OLLAMA_CHAT_URL, OllamaConfig, OllamaVisionService,
    normalize_transcription, prepare_image,
)


def transcription():
    data = {'lines': ["Let's take turns.", '번갈아 하자.', '차례를 나누자고 하는 말입니다.',
        "A: Shall I read first?", "B: Let's take turns.", 'A: 내가 먼저 읽을까?',
        'B: 번갈아 하자.', 'take turns: 번갈아 하다'],
        'fields': dict(zip(FIELD_NAMES, [[0], [1], [2], [3,4], [5,6], [7], None])),
        'uncertain_lines': []}
    data['fields'] = {name: [data['lines'][i] for i in indices] if indices else None
        for name, indices in data['fields'].items()}
    return data


def png(size=(720,1800)):
    stream=BytesIO()
    Image.new('RGB',size,'white').save(stream,format='PNG')
    return stream.getvalue()


def response(data=None, **updates):
    value={'message':{'role':'assistant','content':json.dumps(data if data is not None else transcription(),ensure_ascii=False)},
        'done':True,'done_reason':'stop','model':'gemma4:31b'}
    value.update(updates)
    return value


def service(handler, **kwargs):
    return OllamaVisionService(OllamaConfig(api_key='test-private-key', **kwargs), transport=httpx.MockTransport(handler))


def test_normalizes_bilingual_fields_without_inventing_coordinates_or_confidence():
    result=normalize_transcription(json.dumps(transcription()), 'gemma4:31b')
    assert result.ocr.engine=='ollama/gemma4:31b'
    assert result.ocr.raw_text=="\n".join(transcription()['lines'])
    assert result.ocr.regions==[] and result.ocr.mean_confidence is None
    assert result.lesson.fields.expression.text=="Let's take turns."
    assert result.lesson.fields.english_dialogue.text=="A: Shall I read first?\nB: Let's take turns."
    assert result.lesson.fields.expression.line_indices==[0]
    assert result.lesson.fields.expression.region_indices==[]
    assert result.lesson.fields.expression.bounding_box is None
    assert result.lesson.fields.expression.confidence is None
    assert result.lesson.method=='ollama_vision_v1'
    assert result.lesson.status=='structured'


def test_missing_fields_and_unreadable_text_are_not_invented():
    data=transcription()
    data['fields']['korean_explanation']=None
    data['lines'][2]='[unreadable]'
    data['uncertain_lines']=['[unreadable]']
    result=normalize_transcription(json.dumps(data), 'gemma4:31b')
    assert result.lesson.fields.korean_explanation is None
    assert result.ocr.status=='low_confidence'
    assert result.lesson.status=='partial'
    assert '[unreadable]' in result.ocr.raw_text


def test_empty_image_output_is_clean_and_optional_fields_stay_null():
    data={'lines':[],'fields':dict.fromkeys(FIELD_NAMES),'uncertain_lines':[]}
    result=normalize_transcription(json.dumps(data), 'gemma4:31b')
    assert result.ocr.status=='empty' and result.lesson.status=='empty'
    assert result.ocr.raw_text=='' and result.ocr.regions==[]


@pytest.mark.parametrize('indices', [[-1],[999],[True],[0,0],[1,0]])
def test_invalid_field_references_rejected(indices):
    data=transcription()
    data['fields']['expression']=indices
    with pytest.raises(OCRError) as error:
        normalize_transcription(json.dumps(data), 'gemma4:31b')
    assert error.value.code=='ollama_invalid_response'


def test_rewritten_field_is_unassigned_without_losing_raw_transcription():
    data=transcription()
    data['fields']['expression']=['A sentence that is absent from the image transcript.']
    result=normalize_transcription(json.dumps(data),'gemma4:31b')
    assert result.lesson.fields.expression is None
    assert result.ocr.lines==data['lines']
    assert result.lesson.status=='partial'
    assert any('left unassigned' in note for note in result.ocr.warnings)


@pytest.mark.parametrize('content', ['not json','{}','[]','I think the answer is {}','{"lines":null}'])
def test_malformed_output_rejected(content):
    with pytest.raises(OCRError):
        normalize_transcription(content,'gemma4:31b')


def test_single_json_fence_supported():
    result=normalize_transcription('```json\n'+json.dumps(transcription())+'\n```','gemma4:31b')
    assert result.lesson.fields.expression.text=="Let's take turns."


def test_missing_credentials_never_calls_http(monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY', raising=False)
    monkeypatch.delenv('OLLAMA_API_KEY_FILE', raising=False)
    calls=[]
    svc=OllamaVisionService(transport=httpx.MockTransport(lambda req: calls.append(req)))
    assert not svc.initialize()
    with pytest.raises(OCRError) as error:
        asyncio.run(svc.recognize(png()))
    assert error.value.code=='ollama_not_configured'
    assert calls==[]


def test_secret_file_loading_and_config_repr_never_contains_key(monkeypatch,tmp_path):
    path=tmp_path/'private-key'
    path.write_text('test-private-key\n')
    monkeypatch.delenv('OLLAMA_API_KEY',raising=False)
    monkeypatch.setenv('OLLAMA_API_KEY_FILE',str(path))
    config=OllamaConfig.from_env()
    assert config.api_key=='test-private-key'
    assert 'test-private-key' not in repr(config)


def test_sends_base64_only_to_fixed_official_host_and_never_requests_local_models():
    calls=[]
    def handler(request):
        calls.append(request)
        assert str(request.url)==OLLAMA_CHAT_URL
        assert request.headers['authorization']=='Bearer test-private-key'
        payload=json.loads(request.content)
        assert payload['model']=='gemma4:31b' and payload['stream'] is False
        assert payload['think'] is False
        assert 'format' not in payload  # Cloud does not guarantee structured outputs.
        assert payload['messages'][1]['images'][0]==prepare_image(png(),OllamaConfig())
        assert 'test-private-key' not in request.content.decode()
        return httpx.Response(200,json=response())
    result=asyncio.run(service(handler).recognize(png()))
    assert result.ocr.lines==transcription()['lines']
    assert len(calls)==1


@pytest.mark.parametrize('status,code,http_status',[(401,'ollama_access_denied',503),(403,'ollama_access_denied',503),
    (429,'ollama_rate_limited',503),(500,'ollama_unavailable',502),(404,'ollama_unavailable',502),
    (302,'ollama_unavailable',502)])
def test_provider_failures_are_sanitized_and_redirects_not_followed(status,code,http_status):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(status,headers={'location':'https://evil.example/private'},text='private-provider-details test-private-key')
    with pytest.raises(OCRError) as error:
        asyncio.run(service(handler).recognize(png()))
    assert (error.value.code,error.value.status_code)==(code,http_status)
    assert 'private' not in str(error.value)
    assert len(calls)==1


def test_transcription_timeout_is_clear():
    def handler(request):
        raise httpx.ReadTimeout('private details',request=request)
    with pytest.raises(OCRError) as error:
        asyncio.run(service(handler).recognize(png()))
    assert (error.value.code,error.value.status_code)==('ollama_timeout',504)


def test_response_limit_is_enforced():
    with pytest.raises(OCRError) as error:
        asyncio.run(service(lambda request:httpx.Response(200,content=b'x'*101),max_response_bytes=100).recognize(png()))
    assert error.value.code=='ollama_invalid_response'


@pytest.mark.parametrize('envelope',[response(done=False),response(done_reason='length'),[],{'message':[]},
    response(message={'role':'assistant','content':''}),response(message={'role':'assistant','content':'Refused'})])
def test_incomplete_and_invalid_envelopes_do_not_save_fake_text(envelope):
    with pytest.raises(OCRError) as error:
        asyncio.run(service(lambda request:httpx.Response(200,json=envelope)).recognize(png()))
    assert error.value.code=='ollama_invalid_response'


@pytest.mark.parametrize('data,config,code',[(b'invalid',OllamaConfig(),'ocr_invalid_image'),
    (b'x'*11,OllamaConfig(max_image_bytes=10),'ocr_image_too_large'),
    (png(),OllamaConfig(max_pixels=10),'ocr_image_too_large')])
def test_image_validation_before_api(data,config,code):
    with pytest.raises(OCRError) as error:
        prepare_image(data,config)
    assert error.value.code==code


def test_two_simultaneous_calls_do_not_mix_requests():
    async def run():
        entered,release=asyncio.Event(),asyncio.Event()
        async def handler(request):
            entered.set()
            await release.wait()
            return httpx.Response(200,json=response())
        svc=service(handler)
        pending=asyncio.create_task(svc.recognize(png()))
        await asyncio.wait_for(entered.wait(),2)
        try:
            with pytest.raises(OCRError) as error:
                await svc.recognize(png())
            assert error.value.code=='ocr_busy'
        finally:
            release.set()
            await pending
        assert (await svc.recognize(png())).ocr.lines
    asyncio.run(run())


def test_endpoint_saves_ai_fields_and_duplicate_skips_billable_request(monkeypatch,persisted_store):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json=response())
    svc=service(handler)
    monkeypatch.setattr(ocr,'ocr_service',svc)
    html='<script>Fusion.globalContent={"content_elements":[{"type":"image","url":"https://images.chosun.com/lesson.png","width":720,"height":1800}]};</script>'
    monkeypatch.setattr(scrape,'fetch_article',AsyncMock(return_value=FetchedArticle(
        url='https://www.chosun.com/lesson/ollama/',html=html,title='English lesson')))
    monkeypatch.setattr(scrape,'download_lesson_image',AsyncMock(side_effect=lambda image:DownloadedImage(data=png(),metadata=image)))
    client=TestClient(app)
    for _ in range(2):
        result=client.post('/api/v1/scrape',json={'article_url':'https://www.chosun.com/lesson/ollama/'})
        assert result.status_code==200,result.text
        body=result.json()
        assert body['ocr']['engine']=='ollama/gemma4:31b'
        assert body['lesson']['fields']['expression']['text']=="Let's take turns."
        assert body['lesson']['fields']['english_dialogue']['bounding_box'] is None
        assert body['lesson']['fields']['english_dialogue']['line_indices']==[3,4]
        assert body['lesson_image']['url']=='https://images.chosun.com/lesson.png'
    assert len(calls)==1
    saved=client.get(result.headers['Location'])
    assert saved.status_code==200
    assert saved.json()['payload']['ocr']['lines']==transcription()['lines']


def test_health_needs_neither_ollama_key_nor_network(monkeypatch):
    monkeypatch.delenv('OLLAMA_API_KEY',raising=False)
    monkeypatch.delenv('OLLAMA_API_KEY_FILE',raising=False)
    calls=[]
    svc=OllamaVisionService(transport=httpx.MockTransport(lambda request:calls.append(request)))
    monkeypatch.setattr(ocr,'ocr_service',svc)
    with TestClient(app) as client:
        assert client.get('/health').json()=={'status':'ok'}
    assert not calls


def test_explicit_refresh_preserves_old_result_and_reuses_normal_duplicates(monkeypatch,persisted_store):
    from app.services.saved import process_and_store
    from test_daily import ONE, result
    old=result()
    changed=old.model_copy(update={'ocr':old.ocr.model_copy(update={'engine':'ollama/gemma4:31b','raw_text':'AI text'})})
    async def run():
        await process_and_store(persisted_store,ONE,scrape=AsyncMock(return_value=old))
        refreshed,_=await process_and_store(persisted_store,ONE,scrape=AsyncMock(return_value=changed),reprocess=True)
        assert refreshed.payload.ocr.engine=='ollama/gemma4:31b'
        assert persisted_store.revisions==[old]
        unused=AsyncMock(side_effect=AssertionError('duplicate must not call AI'))
        saved,_=await process_and_store(persisted_store,ONE,scrape=unused)
        assert saved.payload.ocr.raw_text=='AI text'
        unused.assert_not_awaited()
        broken=AsyncMock(side_effect=OCRError('ollama_invalid_response','Invalid generated text',502))
        with pytest.raises(OCRError):
            await process_and_store(persisted_store,ONE,scrape=broken,reprocess=True)
        assert (await persisted_store.cached(ONE))['payload']['ocr']['raw_text']=='AI text'
    asyncio.run(run())


def test_refresh_route_only_exists_on_explicit_dev_site(monkeypatch,persisted_store):
    from app.main import create_app
    monkeypatch.setenv('ENABLE_TEST_SITE','false')
    assert TestClient(create_app()).post('/_dev/test-site/lessons/unknown/transcribe').status_code==404


def test_empty_refresh_cannot_replace_readable_saved_text(persisted_store):
    from app.services.saved import process_and_store
    from test_daily import ONE,result
    async def run():
        old=result()
        await process_and_store(persisted_store,ONE,scrape=AsyncMock(return_value=old))
        empty=old.model_copy(update={'ocr':old.ocr.model_copy(update={'raw_text':'','status':'empty'})})
        with pytest.raises(ArticleFetchError) as error:
            await process_and_store(persisted_store,ONE,scrape=AsyncMock(return_value=empty),reprocess=True)
        assert error.value.code=='empty_transcription'
        assert (await persisted_store.cached(ONE))['payload']==old.model_dump(mode='json')
    asyncio.run(run())


def test_opt_in_refresh_endpoint_updates_ai_text_and_keeps_revision(monkeypatch,persisted_store):
    from app.dependencies import get_store
    from app.main import create_app
    from app.services.saved import process_and_store
    from test_daily import ONE,result
    monkeypatch.setenv('ENABLE_TEST_SITE','true')
    local=create_app()
    local.dependency_overrides[get_store]=lambda:persisted_store
    old=result()
    saved,_=asyncio.run(process_and_store(persisted_store,ONE,scrape=AsyncMock(return_value=old)))
    changed=old.model_copy(update={'ocr':old.ocr.model_copy(update={'engine':'ollama/gemma4:31b','raw_text':'AI text'})})
    processor=AsyncMock(return_value=changed)
    monkeypatch.setattr(scrape,'scrape_article',processor)
    client=TestClient(local)
    response=client.post(f'/_dev/test-site/lessons/{saved.id}/transcribe')
    assert response.status_code==200,response.text
    assert response.json()['ocr']['engine']=='ollama/gemma4:31b'
    assert persisted_store.revisions==[old]
    assert client.post('/_dev/test-site/lessons/unknown/transcribe').status_code==404
    processor.assert_awaited_once()
