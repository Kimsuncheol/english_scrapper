import json
import httpx
from fastapi.testclient import TestClient
from app.main import create_app
from app.kakao_api import COOKIE
from urllib.parse import urlsplit, parse_qs


def configured(monkeypatch):
    monkeypatch.setenv('KAKAO_SESSION_SECRET', 'test-only-encryption-secret')
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
            'access_token':'access-test', 'refresh_token':'private-refresh', 'refresh_token_expires_in':5000, 'expires_in':3600, 'scope':'talk_message'})
    monkeypatch.setattr(httpx.AsyncClient, 'post', post)
    result = client.get('/api/v1/kakao/callback?state=' + state + '&code=test')
    assert result.status_code == 200
    assert 'access-test' in result.text
    assert 'private-refresh' not in result.text
    assert 'no-store' in result.headers['cache-control']
    assert 'access-test' not in result.headers.get('set-cookie','')
    assert 'private-refresh' not in result.headers.get('set-cookie','')
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



def test_session_restore_refresh_rotation_and_cookie_security(monkeypatch):
    from app.kakao_api import SESSION, seal, unseal
    import time
    client = configured(monkeypatch)
    client.cookies.set(SESSION, seal({'access_token':'old', 'access_until':0, 'refresh_token':'refresh-old', 'refresh_until':time.time()+5000}))
    calls = []
    async def post(self, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, request=httpx.Request('POST',url), json={'access_token':'new', 'expires_in':3600,'refresh_token':'rotated','refresh_token_expires_in':8000})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    response = client.get('/api/v1/kakao/session')
    assert '"access_token": "new"' in response.text
    assert 'rotated' not in response.text
    assert calls[0]['data']['refresh_token'] == 'refresh-old'
    cookie = response.headers['set-cookie']
    assert all(value in cookie for value in ['Secure','HttpOnly','SameSite=lax'])
    assert 'rotated' not in cookie
    encoded = cookie.split(';')[0].split('=',1)[1].strip('"')
    assert unseal(encoded)['refresh_token'] == 'rotated'
    client.cookies.clear()
    client.cookies.set(SESSION, encoded)
    awaitless = client.get('/api/v1/kakao/session')
    assert awaitless.status_code == 200
    assert len(calls) == 1  # Valid access token restores without refreshing again.


def test_tampered_expired_and_revoked_sessions_require_login(monkeypatch):
    from app.kakao_api import SESSION, seal
    import time
    client = configured(monkeypatch)
    for value in ['tampered', seal({'refresh_until':0})]:
        client.cookies.set(SESSION,value)
        assert client.get('/api/v1/kakao/session',follow_redirects=False).status_code == 302
    client.cookies.set(SESSION,seal({'access_token':'a','access_until':0,'refresh_token':'r','refresh_until':time.time()+5000}))
    async def post(self,url,**kwargs):
        return httpx.Response(400,request=httpx.Request('POST',url),json={'error':'invalid_grant'})
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    response = client.get('/api/v1/kakao/session',follow_redirects=False)
    assert response.headers['location'] == '/api/v1/kakao/login'
    assert 'Max-Age=0' in response.headers['set-cookie']


def test_transient_refresh_failure_preserves_session_and_disconnect_csrf(monkeypatch):
    from app.kakao_api import SESSION, seal
    import time
    client = configured(monkeypatch)
    client.cookies.set(SESSION,seal({'access_token':'a','access_until':0,'refresh_token':'r','refresh_until':time.time()+5000}))
    async def post(self,url,**kwargs):
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(httpx.AsyncClient,'post',post)
    result = client.get('/api/v1/kakao/session')
    assert 'error' in result.text and 'access_token' not in result.text
    assert 'set-cookie' not in result.headers
    assert client.post('/api/v1/kakao/disconnect').status_code == 403
    result = client.post('/api/v1/kakao/disconnect',headers={'Origin':'https://api.example.com','X-Moatok-Disconnect':'1'})
    assert result.json()['disconnected']
    assert 'Max-Age=0' in result.headers['set-cookie']
