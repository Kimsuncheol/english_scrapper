"""Moatok's OAuth bridge. Credentials remain on the server, tokens in opener memory."""
import json
import os
import secrets
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter(prefix='/api/v1/kakao', tags=['Moatok OAuth'])
COOKIE = '__Host-moatok-kakao-state'


def config():
    rest = os.getenv('KAKAO_REST_API_KEY', '')
    redirect = os.getenv('KAKAO_REDIRECT_URI', '')
    origin = os.getenv('KAKAO_FRONTEND_ORIGIN', 'https://moatalk-e6fe1.web.app')
    valid = bool(rest and redirect and urlsplit(redirect).scheme == 'https' and
                 urlsplit(origin).scheme == 'https' and urlsplit(origin).path in ('', '/'))
    return rest, redirect, origin, valid


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
            if not token.get('access_token') or 'talk_message' not in token.get('scope', '').split() or not isinstance(token.get('expires_in'), int):
                raise ValueError('Missing message consent')
            result = {'type': 'moatok-kakao-login', 'access_token': token['access_token'], 'expires_in': token['expires_in']}
        except Exception:
            # Never log provider responses, authorization codes, or tokens.
            result['error'] = '카카오 연결에 실패했습니다. 메시지 동의 및 앱 설정을 확인하세요.'
    nonce = secrets.token_urlsafe(24)
    payload = json.dumps(result, ensure_ascii=True).replace('<', '\\u003c')
    target = json.dumps(origin.rstrip('/')).replace('<', '\\u003c')
    response = HTMLResponse(f'<!doctype html><meta charset="utf-8"><p>카카오 연결 처리가 끝났습니다. 이 창을 닫아 주세요.</p><script nonce="{nonce}">if(window.opener){{window.opener.postMessage({payload},{target});window.close();}}</script>',
        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                 'Content-Security-Policy': f"default-src 'none'; script-src 'nonce-{nonce}'"})
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax', path='/')
    return response
