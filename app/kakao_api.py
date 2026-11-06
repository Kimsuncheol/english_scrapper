"""Moatok's OAuth bridge. Credentials remain on the server, tokens in opener memory."""
import base64
import hashlib
import time
import json
import os
import secrets
from urllib.parse import urlencode, urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter(prefix='/api/v1/kakao', tags=['Moatok OAuth'])
COOKIE = '__Host-moatok-kakao-state'
SESSION = '__Host-moatok-kakao-session'


def config():
    rest = os.getenv('KAKAO_REST_API_KEY', '')
    redirect = os.getenv('KAKAO_REDIRECT_URI', '')
    origin = os.getenv('KAKAO_FRONTEND_ORIGIN', 'https://moatalk-e6fe1.web.app')
    valid = bool(rest and redirect and urlsplit(redirect).scheme == 'https' and
                 urlsplit(origin).scheme == 'https' and urlsplit(origin).path in ('', '/'))
    return rest, redirect, origin, valid


def cipher():
    secret = os.getenv('KAKAO_SESSION_SECRET') or os.getenv('KAKAO_CLIENT_SECRET')
    if not secret:
        raise ValueError('Session encryption is not configured')
    return AESGCM(hashlib.sha256(('moatok-session-v1:' + secret).encode()).digest())


def seal(data):
    nonce = secrets.token_bytes(12)
    raw = nonce + cipher().encrypt(nonce, json.dumps(data).encode(), config()[0].encode())
    return base64.urlsafe_b64encode(raw).decode()


def unseal(raw):
    data = base64.urlsafe_b64decode(raw)
    return json.loads(cipher().decrypt(data[:12], data[12:], config()[0].encode()))


def popup(result):
    nonce = secrets.token_urlsafe(24)
    payload = json.dumps({'type': 'moatok-kakao-login', **result}, ensure_ascii=True).replace('<', '\\u003c')
    target = json.dumps(config()[2].rstrip('/')).replace('<', '\\u003c')
    return HTMLResponse(f'<!doctype html><meta charset="utf-8"><p>카카오 연결 처리가 끝났습니다. 이 창을 닫아 주세요.</p><script nonce="{nonce}">if(window.opener){{window.opener.postMessage({payload},{target});window.close();}}</script>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                 'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'"})


def set_session(response, data):
    value = seal(data)
    if len(value) > 3800:
        raise ValueError('Session exceeds cookie limit')
    response.set_cookie(SESSION, value, max_age=max(0, int(data['refresh_until'] - time.time())),
                        secure=True, httponly=True, samesite='lax', path='/')


@router.get('/session')
async def restore_session(request: Request):
    try:
        data = unseal(request.cookies.get(SESSION, ''))
        if data['refresh_until'] <= time.time():
            raise ValueError('Expired')
    except Exception:
        response = RedirectResponse('/api/v1/kakao/login', status_code=302)
        response.delete_cookie(SESSION, secure=True, httponly=True, samesite='lax', path='/')
        response.headers['Cache-Control'] = 'no-store'
        return response
    if data['access_until'] <= time.time() + 60:
        try:
            form = {'grant_type':'refresh_token', 'client_id':config()[0], 'refresh_token':data['refresh_token']}
            if os.getenv('KAKAO_CLIENT_SECRET'):
                form['client_secret'] = os.environ['KAKAO_CLIENT_SECRET']
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                upstream = await client.post('https://kauth.kakao.com/oauth/token', data=form)
                if upstream.status_code == 400 or upstream.status_code == 401:
                    response = RedirectResponse('/api/v1/kakao/login', status_code=302)
                    response.delete_cookie(SESSION, secure=True, httponly=True, samesite='lax', path='/')
                    response.headers['Cache-Control'] = 'no-store'
                    return response
                upstream.raise_for_status()
                token = upstream.json()
            if not token.get('access_token') or not isinstance(token.get('expires_in'), int):
                raise ValueError('Invalid token response')
            data['access_token'] = token['access_token']
            data['access_until'] = time.time() + token['expires_in']
            if token.get('refresh_token'):
                data['refresh_token'] = token['refresh_token']
                data['refresh_until'] = time.time() + token['refresh_token_expires_in']
        except Exception:
            # A temporary provider failure does not discard a valid refresh session.
            return popup({'error':'카카오 연결을 갱신하지 못했습니다. 잠시 후 다시 시도하세요.'})
    response = popup({'access_token':data['access_token'], 'expires_in':int(data['access_until']-time.time())})
    set_session(response, data)
    return response


@router.get('/disconnect')
async def disconnect_page():
    # POST via same-origin script; no state-changing GET or cross-site form allowed.
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(f"<!doctype html><script nonce='{nonce}'>fetch('/api/v1/kakao/disconnect',{{method:'POST',headers:{{'X-Moatok-Disconnect':'1'}}}}).then(()=>{{if(opener)opener.postMessage({{type:'moatok-kakao-login',disconnected:true}},{json.dumps(config()[2])});window.close();}})</script>",
        headers={'Cache-Control':'no-store','Content-Security-Policy':f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'"})


@router.post('/disconnect')
async def disconnect(request: Request):
    if request.headers.get('x-moatok-disconnect') != '1' or request.headers.get('origin') != str(request.base_url).rstrip('/'):
        return JSONResponse({'error':'forbidden'}, status_code=403)
    # Revoke this browser's Kakao session when possible, then delete its cookie.
    try:
        data = unseal(request.cookies.get(SESSION, ''))
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post('https://kapi.kakao.com/v1/user/logout', headers={'Authorization':'Bearer ' + data['access_token']})
    except Exception:
        pass
    response = JSONResponse({'disconnected':True}, headers={'Cache-Control':'no-store'})
    response.delete_cookie(SESSION, secure=True, httponly=True, samesite='lax', path='/')
    return response


@router.get('/status')
async def status():
    return JSONResponse({'configured': config()[3]}, headers={'Cache-Control': 'no-store'})


@router.get('/login')
async def login():
    rest, uri, origin, valid = config()
    if not valid:
        return JSONResponse({'error': 'kakao_not_configured'}, status_code=503)
    state = secrets.token_urlsafe(32)
    response = RedirectResponse('https://kauth.kakao.com/oauth/authorize?' + urlencode({
        'client_id': rest, 'redirect_uri': uri, 'response_type': 'code',
        'scope': 'talk_message', 'state': state}), status_code=302)
    response.set_cookie(COOKIE, state, max_age=600, secure=True, httponly=True, samesite='lax', path='/')
    response.headers['Cache-Control'] = 'no-store'
    return response


@router.get('/callback')
async def callback(request: Request):
    rest, uri, origin, valid = config()
    state = request.cookies.get(COOKIE, '')
    supplied = request.query_params.get('state', '')
    if not valid:
        return JSONResponse({'error': 'kakao_not_configured'}, status_code=503)
    if not state or not secrets.compare_digest(state.encode(), supplied.encode()):
        return JSONResponse({'error': 'invalid_oauth_state'}, status_code=400)
    result = {'type': 'moatok-kakao-login', 'error': '카카오 연결이 취소되었거나 실패했습니다.'}
    code = request.query_params.get('code')
    if code and not request.query_params.get('error'):
        data = {'grant_type': 'authorization_code', 'client_id': rest, 'redirect_uri': uri, 'code': code}
        if os.getenv('KAKAO_CLIENT_SECRET'):
            data['client_secret'] = os.environ['KAKAO_CLIENT_SECRET']
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.post('https://kauth.kakao.com/oauth/token', data=data)
                response.raise_for_status()
                token = response.json()
            if (not token.get('access_token') or 'talk_message' not in token.get('scope', '').split()
                    or not isinstance(token.get('expires_in'), int) or token['expires_in'] <= 0
                    or not token.get('refresh_token') or not isinstance(token.get('refresh_token_expires_in'), int)
                    or token['refresh_token_expires_in'] <= 0):
                raise ValueError('Missing message consent')
            result = {'type': 'moatok-kakao-login', 'access_token': token['access_token'], 'expires_in': token['expires_in']}
        except Exception:
            # Never log provider responses, authorization codes, or tokens.
            result['error'] = '카카오 연결에 실패했습니다. 메시지 동의 및 앱 설정을 확인하세요.'
    response = popup(result)
    if result.get('access_token') and token.get('refresh_token'):
        try:
            set_session(response, {'access_token':token['access_token'], 'access_until':time.time()+token['expires_in'],
                'refresh_token':token['refresh_token'], 'refresh_until':time.time()+token['refresh_token_expires_in']})
        except Exception:
            return popup({'error':'로그인 유지 세션을 저장하지 못했습니다. 서버 설정을 확인하세요.'})
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax', path='/')
    return response
