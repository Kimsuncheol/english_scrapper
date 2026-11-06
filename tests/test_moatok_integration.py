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
