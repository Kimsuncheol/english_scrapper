import json
import httpx
from fastapi.testclient import TestClient
from app.main import create_app
from app.kakao_api import COOKIE
from urllib.parse import urlsplit, parse_qs


def configured(monkeypatch):
    monkeypatch.setenv('KAKAO_REST_API_KEY', 'test-app')
    monkeypatch.setenv('KAKAO_REDIRECT_URI', 'https://api.example.com/api/v1/kakao/callback')
    return TestClient(create_app(), base_url='https://api.example.com')


def test_login_and_invalid_state(monkeypatch):
    client = configured(monkeypatch)
    result = client.get('/api/v1/kakao/login', follow_redirects=False)
    assert result.status_code == 302
    assert parse_qs(urlsplit(result.headers['location']).query)['scope'] == ['talk_message']
    assert 'HttpOnly' in result.headers['set-cookie']
    assert 'Secure' in result.headers['set-cookie']
    assert client.get('/api/v1/kakao/callback?state=wrong&code=test').status_code == 400


def test_callback_never_leaks_refresh_token_and_consumes_state(monkeypatch):
    client = configured(monkeypatch)
    client.get('/api/v1/kakao/login', follow_redirects=False)
    state = client.cookies[COOKIE]
    async def post(self, url, **kwargs):
        return httpx.Response(200, request=httpx.Request('POST', url), json={
            'access_token':'access-test', 'refresh_token':'private-refresh', 'expires_in':3600, 'scope':'talk_message'})
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = client.get('/api/v1/kakao/callback?state=' + state + '&code=test')
    assert result.status_code == 200
    assert 'access-test' in result.text
    assert 'private-refresh' not in result.text
    assert 'no-store' in result.headers['cache-control']
    assert 'access-test' not in result.headers.get('set-cookie','')
    assert client.get('/api/v1/kakao/callback?state=' + state + '&code=test').status_code == 400


def test_provider_rejection_is_sanitized(monkeypatch):
    client = configured(monkeypatch)
    client.get('/api/v1/kakao/login', follow_redirects=False)
    state = client.cookies[COOKIE]
    async def post(self, url, **kwargs):
        raise RuntimeError('secret-provider-details')
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = client.get('/api/v1/kakao/callback?state=' + state + '&code=test')
    assert 'secret-provider-details' not in result.text
    assert 'access_token' not in result.text
