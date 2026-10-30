"""Authenticated HTTP console for a trusted network; school credentials never leave it."""

import argparse
import hmac
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from getpass import getpass
from typing import Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .accounts import AccountInput, Accounts, public_account
from .admin import Administrators, COOKIE_NAME, SESSION_SECONDS, hash_password
from .app import DEFAULT_CATEGORIES
from .cli import configuration_from, data_root_from
from .manager import Manager

ASSETS = Path(__file__).parent / 'web_assets'
CAMPUSES = ['九龙湖校区', '四牌楼校区', '丁家桥校区', '无锡校区', '苏州校区']
MAX_BODY_BYTES = 65536

# Every response, including middleware early exits, carries the same browser hardening. Plain HTTP
# has no TLS to rely on for these, so they are always set; HSTS is deliberately absent.
SECURITY_HEADERS = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin',
    'Content-Security-Policy': (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'"),
}

SAFE_ERRORS = {
    'invalid_port', 'invalid_data_directory', 'unknown_configuration_key', 'manager_already_running',
    'account_not_found', 'account_exists', 'account_already_running', 'account_stopping',
    'account_identity_immutable', 'account_deleting', 'reservation_protected',
    'mail_configuration_invalid', 'mail_disabled', 'challenge_expired', 'invalid_answer',
    'password_required', 'stop_timeout', 'service_stopping', 'state_in_use',
    'configuration_conflict', 'admin_configuration_invalid', 'invalid_admin_password',
    'login_rejected', 'login_rate_limited',
}

MAIL_FAILURE_REASONS = {
    'dns_failed', 'tls_failed', 'timeout', 'authentication_failed', 'sender_refused',
    'recipient_refused', 'message_rejected', 'connection_failed', 'smtp_failed',
    'network_failed', 'unknown',
}


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    challenge: str = Field(min_length=32, max_length=32)
    answer: str = Field(max_length=16)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=1024)


def rejected(detail: str, status_code: int) -> JSONResponse:
    return JSONResponse({'detail': detail}, status_code=status_code, headers=SECURITY_HEADERS)


def create_app(accounts: Accounts, shared: Mapping[str, str], *, manager: Manager | None = None) -> FastAPI:
    administrators = Administrators(shared)
    supervisor = manager or Manager(accounts, shared)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        administrators.clear()
        try:
            await run_in_threadpool(supervisor.open)
        except BaseException as exc:
            # uvicorn exits the process (SystemExit) on a lifespan startup failure; keep the reason
            # so main() can report a stable event instead of losing it in the server log.
            application.state.startup_error = str(exc) if isinstance(exc, ValueError) else ''
            raise
        try:
            yield
        finally:
            administrators.clear()
            await run_in_threadpool(supervisor.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = supervisor
    app.state.startup_error = ''
    app.state.administrators = administrators

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # FastAPI's default error includes the rejected input, including passwords/SMS codes.
        return JSONResponse({'detail': 'invalid_request'}, status_code=422)

    @app.exception_handler(ValueError)
    async def business_error(request: Request, exc: ValueError):
        reason = str(exc)
        safe = SAFE_ERRORS | {'mail_' + item for item in MAIL_FAILURE_REASONS}
        status = {'account_not_found': 404, 'login_rejected': 401, 'login_rate_limited': 429}.get(reason, 409)
        return JSONResponse({'detail': reason if reason in safe else 'configuration_or_state_invalid'}, status_code=status)

    @app.middleware('http')
    async def protect(request: Request, call_next):
        api = request.url.path.startswith('/api/')
        mutating = request.method not in ('GET', 'HEAD')
        if mutating and api:
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/'):
                return rejected('origin_rejected', 403)
            media_type = request.headers.get('content-type', '').split(';')[0].strip().lower()
            if media_type != 'application/json':
                return rejected('json_required', 415)
            length = request.headers.get('content-length')
            if length is None:
                return rejected('content_length_required', 411)
            if not length.isdigit() or int(length) > MAX_BODY_BYTES:
                return rejected('request_too_large', 413)
        if api and request.url.path != '/api/login':
            session = administrators.session(request.cookies.get(COOKIE_NAME, ''))
            if session is None:
                return rejected('authentication_required', 401)
            if mutating and not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), session['csrf'].encode()):
                return rejected('csrf_rejected', 403)
            request.state.admin = session
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.post('/api/login')
    def login(request: Request, data: LoginInput):
        token, session = administrators.login(data.username, data.password,
                                              request.client.host if request.client else 'unknown')
        administrators.logout(request.cookies.get(COOKIE_NAME, ''))
        response = JSONResponse({'username': session['username'], 'csrf': session['csrf']})
        response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True, samesite='strict')
        return response

    @app.get('/api/session')
    def session(request: Request):
        return {key: request.state.admin[key] for key in ('username', 'csrf')}

    @app.post('/api/logout')
    def logout(request: Request):
        administrators.logout(request.cookies.get(COOKIE_NAME, ''))
        response = JSONResponse({'ok': True})
        response.delete_cookie(COOKIE_NAME)
        return response

    @app.get('/api/meta')
    def meta():
        return {'categories': list(DEFAULT_CATEGORIES), 'campuses': list(CAMPUSES)}

    @app.get('/api/accounts')
    def account_list():
        return [supervisor.snapshot(account) for account in accounts.list()]

    @app.post('/api/accounts', status_code=201)
    def create_account(data: AccountInput):
        return public_account(accounts.create(data))

    @app.put('/api/accounts/{identifier}')
    def edit_account(identifier: str, data: AccountInput):
        return public_account(supervisor.update(identifier, data))

    @app.delete('/api/accounts/{identifier}')
    def delete_account(identifier: str):
        supervisor.delete(identifier)
        return {'ok': True}

    @app.post('/api/accounts/{identifier}/start')
    def start_account(identifier: str):
        supervisor.start(identifier)
        return {'ok': True}

    @app.post('/api/accounts/{identifier}/stop')
    def stop_account(identifier: str):
        supervisor.stop(identifier)
        return {'ok': True}

    @app.post('/api/accounts/{identifier}/answer')
    def answer_challenge(identifier: str, data: AnswerInput):
        supervisor.answer(identifier, data.challenge, data.answer)
        return {'ok': True}

    @app.post('/api/accounts/{identifier}/test-mail')
    def test_mail(identifier: str):
        supervisor.test_mail(identifier)
        return {'ok': True, 'status': 'smtp_accepted'}

    @app.get('/')
    def index():
        return FileResponse(ASSETS / 'index.html', media_type='text/html')

    app.mount('/assets', StaticFiles(directory=ASSETS), name='assets')
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='SEU multi-account HTTP administration console')
    parser.add_argument('--config', type=Path, help='dotenv file (default: .env)')
    parser.add_argument('--hash-password', action='store_true', help='generate an administrator password hash locally')
    args = parser.parse_args(argv)
    try:
        if args.hash_password:
            password = getpass('管理员密码：')
            if password != getpass('再次输入密码：'):
                raise ValueError('invalid_admin_password')
            print(hash_password(password))
            return 0
        _, shared = configuration_from(args.config, os.environ)
        source = args.config or Path('.env')
        root = data_root_from(source, shared)
        try:
            port = int(shared.get('SEU_WEB_PORT') or '8080')
        except ValueError:
            raise ValueError('invalid_port') from None
        if not 1 <= port <= 65535:
            raise ValueError('invalid_port')
        host = (shared.get('SEU_WEB_HOST') or '').strip() or '0.0.0.0'
        Administrators(shared)
        app = create_app(Accounts(root), shared)
        if host in ('0.0.0.0', '::'):
            print(f'管理台监听 {host}:{port}；本机浏览器请打开 http://127.0.0.1:{port}'
                  '（0.0.0.0 只是监听所有网卡的绑定地址，不能直接作为网址访问）', flush=True)
        else:
            print(f'管理台监听 {host}:{port}；浏览器请打开 http://{host}:{port}', flush=True)
        import uvicorn
        try:
            uvicorn.run(app, host=host, port=port, workers=1, proxy_headers=False,
                        access_log=False, server_header=False, timeout_graceful_shutdown=15)
        except SystemExit:
            raise ValueError(str(getattr(app.state, 'startup_error', '') or 'startup_failed')) from None
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        reason = str(exc) if isinstance(exc, ValueError) and str(exc) in SAFE_ERRORS else 'configuration_or_state_invalid'
        print('event=web_configuration_or_state_invalid reason=' + reason)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
