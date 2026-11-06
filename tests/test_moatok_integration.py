from fastapi.testclient import TestClient
from app.main import create_app


def test_moatok_preflight_and_unapproved_origin():
    client = TestClient(create_app())
    response = client.options('/api/v1/scrape', headers={
        'Origin': 'https://moatalk-e6fe1.web.app',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'https://moatalk-e6fe1.web.app'
    denied = client.options('/api/v1/scrape', headers={
        'Origin': 'https://unapproved.example', 'Access-Control-Request-Method': 'POST'})
    assert denied.status_code == 400


def test_production_series_route_exists():
    paths = create_app().openapi()['paths']
    assert '/api/v1/series/check' in paths


def test_public_fetch_resolves_section_without_storage(monkeypatch):
    from app.schemas import ScrapeResponse
    from app.services.errors import ArticleFetchError
    calls = []
    async def discover():
        return ['https://www.chosun.com/national/nie/english/2026/11/06/example/']
    async def scrape(url):
        calls.append(str(url))
        raise ArticleFetchError('test_reached_extraction', 'Reached extraction without storage', 422)
    monkeypatch.setattr('app.services.discovery.discover_articles', discover)
    monkeypatch.setattr('app.services.scrape.scrape_article', scrape)
    client = TestClient(create_app())
    response = client.post('/api/v1/lessons/fetch', json={'article_url': 'https://www.chosun.com/national/nie/english/'})
    assert response.json()['error']['code'] == 'test_reached_extraction'
    assert calls == ['https://www.chosun.com/national/nie/english/2026/11/06/example/']
    assert client.post('/api/v1/lessons/fetch', json={'article_url': 'https://localhost/'}).status_code == 422
