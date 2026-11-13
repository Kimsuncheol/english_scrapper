from unittest.mock import AsyncMock
from fastapi.testclient import TestClient
from app.main import app
from app.schemas import ScrapeResponse
from app.services import scrape


def result():
    return ScrapeResponse(
        article={'url':'https://www.chosun.com/lesson/client/'},
        lesson_image={'url':'https://images.chosun.com/lesson.png'},
        ocr={'raw_text':'Try another way\n다른 방법을 시도해 봐.'},
        lesson={'status':'partial','fields':{
            'expression':{'text':'Try another way','region_indices':[]},
            'korean_meaning':{'text':'다른 방법을 시도해 봐.','region_indices':[]}}},
        processing={'status':'partial','completed_stages':[],
                    'ocr_status':'ok','lesson_status':'partial','elapsed_ms':1})


def test_public_client_receives_ordered_sections_and_raw_text(monkeypatch):
    value = result()
    monkeypatch.setattr(scrape, 'scrape_article', AsyncMock(return_value=value))
    response = TestClient(app).post('/api/v1/lessons/fetch', json={
        'article_url':'https://www.chosun.com/lesson/client/'})
    assert response.status_code == 200
    body = response.json()
    assert [s['key'] for s in body['content']['sections']] == ['expression','korean_meaning']
    assert body['content']['sections'][1]['text'] == '다른 방법을 시도해 봐.'
    assert body['content']['raw_text'] == body['ocr']['raw_text']
    assert body['content']['source_image_url'] == body['lesson_image']['url']
    assert body['content']['needs_review'] is True
    assert body['lesson']['fields']['expression']['text'] == 'Try another way'


def test_legacy_or_stale_saved_projection_uses_authoritative_fields():
    data = result().model_dump(mode='json')
    data['content']['sections'][0]['text'] = 'stale'
    loaded = ScrapeResponse.model_validate(data)
    assert loaded.content.sections[0].text == 'Try another way'
    data.pop('content')
    assert ScrapeResponse.model_validate(data).content == loaded.content
    loaded.lesson.fields.expression = None
    loaded.lesson.fields.korean_meaning = None
    assert loaded.content.sections == []
    assert loaded.content.raw_text
