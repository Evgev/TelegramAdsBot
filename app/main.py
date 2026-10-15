from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .ads_api import AdsAPI, AdsError
from .config import Settings, verify_password
from .db import DB
from .engine import Engine, sign, unsign


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Login(StrictModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=500)


class Template(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=160)
    promote_url: str = Field(min_length=1, max_length=256)
    impression_frequency: int = Field(default=1, ge=1, le=4)
    source_ad_id: int | None = None

    @model_validator(mode='after')
    def check_url(self):
        parts = urlsplit(self.promote_url)
        if parts.scheme != 'https' or parts.netloc != 't.me' or not parts.path.strip('/'):
            raise ValueError('Укажите HTTPS-ссылку на Telegram-бота')
        return self


class Policy(StrictModel):
    target_views: int = Field(ge=1, le=10000000)
    duration_hours: float = Field(default=24, ge=1, le=720)
    min_cpm: float = Field(default=0.1, ge=0.01, le=10000, multiple_of=0.01)
    max_cpm: float = Field(default=5, ge=0.01, le=10000, multiple_of=0.01)
    spend_limit: float = Field(default=2, ge=0.01, le=100000, multiple_of=0.01)
    step_percent: float = Field(default=10, ge=1, le=25)
    tolerance_percent: float = Field(default=20, ge=5, le=50)
    cooldown_minutes: int = Field(default=120, ge=60, le=1440)
    settle_minutes: int = Field(default=60, ge=30, le=240)
    window_minutes: int = Field(default=60, ge=30, le=720)

    @model_validator(mode='after')
    def check_range(self):
        if self.min_cpm > self.max_cpm:
            raise ValueError('Минимальная CPM должна быть не больше максимальной')
        if self.cooldown_minutes < self.settle_minutes + self.window_minutes:
            raise ValueError('Пауза между изменениями должна покрывать задержку и окно наблюдения')
        return self


class BatchSpec(StrictModel):
    name: str = Field(min_length=1, max_length=60)
    template_id: str = Field(min_length=1, max_length=100)
    bots: str = Field(min_length=1, max_length=10000)
    budget: float = Field(ge=0.01, le=100000, multiple_of=0.01)
    cpm: float = Field(ge=0.01, le=10000, multiple_of=0.01)
    tracking: Literal['keep', 'unique'] = 'keep'
    policy: Policy | None = None

    @model_validator(mode='after')
    def check_policy(self):
        if self.policy and not self.policy.min_cpm <= self.cpm <= self.policy.max_cpm:
            raise ValueError('Начальная CPM должна входить в диапазон автоподбора')
        return self


class ConfirmBatch(StrictModel):
    token: str = Field(min_length=20, max_length=100000)


class Policies(StrictModel):
    ad_ids: list[int] = Field(min_length=1, max_length=100)
    enabled: bool
    policy: Policy | None = None


class AdActions(StrictModel):
    ad_ids: list[int] = Field(min_length=1, max_length=50)
    action: Literal['pause', 'resume', 'cpm']
    cpm: float | None = Field(default=None, ge=0.01, le=10000, multiple_of=0.01)


class Automation(StrictModel):
    paused: bool


def click_through_rate(ad):
    views, clicks = ad.get('views'), ad.get('clicks')
    if views is None or views <= 0 or clicks is None or clicks < 0:
        return None
    return clicks / views * 100


def create_app(settings=None, api=None):
    settings = settings or Settings.from_env()
    settings.validate()
    db = DB(settings.data_dir / 'papin-ads.sqlite3')
    ads_api = api or AdsAPI(settings.ads_token)
    engine = Engine(settings, db, ads_api)
    attempts = {}
    static = Path(__file__).parent / 'static'
    # Deliver the first-party UI in the document: filters or stale asset caches
    # must not leave the login page unstyled or without its event handlers.
    script = (static / 'app.js').read_text().replace('</script', r'<\/script')
    style = (static / 'style.css').read_text().replace('</style', r'<\/style')
    html = (static / 'index.html').read_text()
    html = html.replace('<link rel="stylesheet" href="/static/style.css">', '<style>' + style + '</style>')
    html = html.replace('<script src="/static/app.js" defer></script>', '')
    html = html.replace('</body>', '<script>' + script + '</script></body>')
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(engine.run()) if settings.run_worker else None
        yield
        engine.stopping = True
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await ads_api.close()

    app = FastAPI(title='Papin Ads', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.engine, app.state.db = engine, db

    def session(request):
        try:
            value = unsign(request.cookies.get('papin_session', ''), settings.session_secret)
            if value.get('kind') != 'session' or value.get('user') != settings.admin_username:
                return None
            return value
        except ValueError:
            return None

    @app.middleware('http')
    async def protect(request, call_next):
        path = request.url.path
        if path.startswith('/api/'):
            if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                if request.headers.get('origin') != settings.public_url:
                    return JSONResponse({'detail': 'Origin не разрешён'}, status_code=403)
                if request.headers.get('content-length') and int(request.headers['content-length']) > 150000:
                    return JSONResponse({'detail': 'Слишком большой запрос'}, status_code=413)
            if path != '/api/login':
                value = session(request)
                if not value:
                    return JSONResponse({'detail': 'Войдите в систему'}, status_code=401)
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), value['csrf']):
                        return JSONResponse({'detail': 'Обновите страницу и повторите действие'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = f"default-src 'self'; script-src 'self' 'sha256-{script_hash}'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if path.startswith('/api/') or path == '/':
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.exception_handler(AdsError)
    async def api_error(request, exc):
        return JSONResponse({'detail': 'Telegram Ads: ' + exc.code}, status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=400)

    @app.get('/healthz')
    async def health():
        db.query('SELECT 1')
        return {'status': 'ok'}

    @app.post('/api/login')
    async def login(body: Login, request: Request):
        ip = request.client.host if request.client else 'unknown'
        now = time.time()
        recent = [ts for ts in attempts.get(ip, []) if now - ts < 600]
        attempts[ip] = recent
        if len(recent) >= 10:
            raise HTTPException(429, 'Слишком много попыток. Повторите через 10 минут.')
        if not (hmac.compare_digest(body.username.encode(), settings.admin_username.encode()) and verify_password(body.password, settings.password_hash)):
            recent.append(now)
            raise HTTPException(401, 'Неверный логин или пароль')
        attempts.pop(ip, None)
        csrf = secrets.token_urlsafe(24)
        value = {'kind': 'session', 'user': settings.admin_username, 'csrf': csrf, 'exp': int(now + 7 * 86400)}
        response = JSONResponse({'csrf': csrf, 'username': settings.admin_username})
        response.set_cookie('papin_session', sign(value, settings.session_secret), max_age=7 * 86400,
                            httponly=True, secure=settings.secure_cookie, samesite='strict', path='/')
        return response

    @app.get('/api/session')
    async def get_session(request: Request):
        value = session(request)
        return {'username': settings.admin_username, 'csrf': value['csrf'], 'public_url': settings.public_url}

    @app.post('/api/logout')
    async def logout():
        response = JSONResponse({'ok': True})
        response.delete_cookie('papin_session', path='/')
        return response

    @app.get('/api/overview')
    async def overview():
        ads = db.ads()
        account = db.get('account', {})
        def total(key):
            values = [a.get(key) for a in ads]
            return sum(values) if all(v is not None for v in values) else None
        return {'account': {k: v for k, v in account.items() if k != 'account_id'},
                'metrics': {'views': total('views'), 'clicks': total('clicks'), 'actions': total('actions'),
                            'spent': account.get('spent_budget'), 'active': sum(a.get('status') == 'active' for a in ads), 'ads': len(ads)},
                'last_sync': db.get('last_sync'), 'sync_error': db.get('sync_error'),
                'automation_paused': db.get('automation_paused', False),
                'enabled_policies': sum(p.get('enabled', False) for p in db.policies().values()),
                'limits': {'max_batch_size': settings.max_batch_size, 'max_batch_budget': settings.max_batch_budget}}

    @app.post('/api/sync')
    async def sync():
        if engine.sync_lock.locked():
            return {'status': 'already_running'}
        return await engine.sync(history=True)

    @app.get('/api/ads')
    async def list_ads():
        policies = db.policies()
        return {'ads': [dict(ad, ctr=click_through_rate(ad), policy=policies.get(ad['ad_id'])) for ad in db.ads()]}

    @app.post('/api/ads/actions')
    async def ad_actions(body: AdActions):
        if body.action == 'cpm' and body.cpm is None:
            raise ValueError('Укажите CPM')
        result = []
        async with engine.action_lock:
            for ad_id in dict.fromkeys(body.ad_ids):
                if not db.ad(ad_id):
                    result.append({'ad_id': ad_id, 'error': 'Объявление не найдено'})
                    continue
                policy = db.policies().get(ad_id)
                if policy:
                    policy['enabled'] = False
                    policy['last_reason'] = 'Ручное управление'
                    db.save_policy(ad_id, policy)
                params = {'ad_id': ad_id, 'return_target': True}
                params.update({'cpm': body.cpm} if body.action == 'cpm' else {'is_paused': body.action == 'pause'})
                try:
                    ad = await ads_api.call('editAd', params)
                    db.save_ad(ad)
                    db.event('manual', {'pause': 'Пауза объявления', 'resume': 'Возобновление объявления', 'cpm': 'CPM изменена вручную'}[body.action], ad_id, params)
                    result.append({'ad_id': ad_id, 'ok': True})
                except AdsError as exc:
                    db.event('error', 'Ручное действие: ' + exc.code, ad_id)
                    result.append({'ad_id': ad_id, 'error': exc.code})
        return {'results': result}

    @app.get('/api/templates')
    async def templates():
        return {'templates': [dict(json.loads(row['body']), id=row['id']) for row in db.query('SELECT * FROM templates ORDER BY created DESC')]}

    @app.post('/api/templates')
    async def new_template(body: Template):
        template_id = engine.save_template(body.model_dump())
        db.event('template', 'Сохранён шаблон: ' + body.name)
        return {'id': template_id}

    @app.put('/api/templates/{template_id}')
    async def edit_template(template_id: str, body: Template):
        if not db.query('SELECT id FROM templates WHERE id=?', (template_id,)):
            raise HTTPException(404, 'Шаблон не найден')
        engine.save_template(body.model_dump(), template_id)
        db.event('template', 'Изменён шаблон: ' + body.name)
        return {'id': template_id}

    @app.post('/api/templates/from-ad/{ad_id}')
    async def template_from_ad(ad_id: int):
        ad = db.ad(ad_id)
        if not ad or not ad.get('text'):
            raise ValueError('У объявления нет текста для шаблона')
        return {'id': engine.save_template({'name': ad['title'], 'text': ad['text'], 'promote_url': ad['promote_url'],
                                           'impression_frequency': ad.get('impression_frequency', 1), 'source_ad_id': ad_id})}

    @app.post('/api/batches/preview')
    async def preview_batch(body: BatchSpec):
        return await engine.preview(body.model_dump())

    @app.post('/api/batches')
    async def create_batch(body: ConfirmBatch):
        return {'id': engine.create_batch(body.token)}

    @app.get('/api/batches')
    async def batches():
        result = []
        for row in db.query('SELECT * FROM batches ORDER BY created DESC LIMIT 100'):
            items = db.query('SELECT id,username,status,ad_id,error,attempts FROM batch_items WHERE batch_id=? ORDER BY created,rowid', (row['id'],))
            value = json.loads(row['spec'])
            result.append({'id': row['id'], 'name': row['name'], 'status': row['status'], 'created': row['created'],
                           'total': value['total'], 'cpm': value['spec']['cpm'], 'budget': value['spec']['budget'],
                           'items': items, 'done': sum(i['status'] == 'done' for i in items), 'count': len(items)})
        return {'batches': result}

    @app.post('/api/batches/{batch_id}/cancel')
    async def cancel_batch(batch_id: str):
        async with engine.work_lock:
            db.execute("UPDATE batches SET status='cancelled' WHERE id=?", (batch_id,))
            db.execute("UPDATE batch_items SET status='cancelled' WHERE batch_id=? AND status IN ('queued','attention')", (batch_id,))
            db.event('batch', 'Отменены оставшиеся задания пачки', details={'batch_id': batch_id})
        return {'ok': True}

    @app.post('/api/batches/{batch_id}/retry')
    async def retry_batch(batch_id: str):
        async with engine.work_lock:
            db.execute("UPDATE batch_items SET status='queued',next_attempt=0 WHERE batch_id=? AND status='attention'", (batch_id,))
            db.execute("UPDATE batches SET status='queued' WHERE id=? AND status='attention'", (batch_id,))
        return {'ok': True}

    @app.post('/api/policies')
    async def policies(body: Policies):
        async with engine.action_lock:
            ids = list(dict.fromkeys(body.ad_ids))
            if body.enabled:
                if not body.policy:
                    raise ValueError('Укажите параметры стратегии')
                current = await ads_api.call('getAdsById', {'ad_ids': ids, 'return_target': True})
                if {a['ad_id'] for a in current} != set(ids):
                    raise ValueError('Часть объявлений не найдена')
                for ad in current:
                    if ad.get('currency') != 'TON' or not body.policy.min_cpm <= ad['cpm'] <= body.policy.max_cpm:
                        raise ValueError(f"CPM объявления {ad['ad_id']} должна входить в диапазон стратегии")
                for ad in current:
                    db.save_ad(ad)
                    engine.enable_policy(ad, body.policy.model_dump())
            else:
                existing = db.policies()
                for ad_id in ids:
                    if ad_id in existing:
                        value = dict(existing[ad_id], enabled=False, last_reason='Автоподбор выключен пользователем')
                        db.save_policy(ad_id, value)
                        db.event('policy', 'Автоподбор выключен', ad_id)
        return {'ok': True}

    @app.post('/api/automation')
    async def automation(body: Automation):
        async with engine.action_lock:
            db.put('automation_paused', body.paused)
            db.event('policy', 'Все стратегии приостановлены' if body.paused else 'Стратегии возобновлены')
        return {'ok': True}

    @app.get('/api/events')
    async def events():
        return {'events': [dict(row, details=json.loads(row['details'])) for row in db.query('SELECT * FROM events ORDER BY id DESC LIMIT 200')]}

    @app.get('/api/reports')
    async def reports(days: int = 14):
        days = max(1, min(days, 90))
        start = (int(time.time()) // 86400 - days + 1) * 86400
        result = {}
        for row in db.query('SELECT ts,raw FROM daily_stats WHERE ts>=? ORDER BY ts', (start,)):
            value = json.loads(row['raw'])
            bucket = result.setdefault(row['ts'], {'ts': row['ts'], 'views': 0, 'clicks': 0, 'actions': 0, 'spent': 0})
            for dest, source in [('views', 'views'), ('clicks', 'clicks'), ('actions', 'actions'), ('spent', 'spent_budget')]:
                v = value.get(source, value.get('joins') if source == 'actions' else None)
                if v is None or bucket[dest] is None:
                    bucket[dest] = None
                else:
                    bucket[dest] += v
        return {'days': list(result.values()), 'timezone': 'UTC', 'currency': 'TON', 'last_sync': db.get('last_history_sync')}

    @app.get('/api/export.csv')
    async def export():
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['ID', 'Название', 'Статус', 'Размещение', 'CPM TON', 'Расход TON', 'Остаток TON', 'Показы', 'Клики', 'CTR %', 'Действия', 'Ссылка'])
        def cell(value):
            if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
                return "'" + value
            return value
        for ad in db.ads():
            row = dict(ad, ctr=click_through_rate(ad))
            writer.writerow([cell(row.get(k)) for k in ['ad_id', 'title', 'status', 'placement', 'cpm', 'spent_budget', 'remaining_budget', 'views', 'clicks', 'ctr', 'actions', 'promote_url']])
        return Response('\ufeff' + output.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename=papin-ads.csv'})

    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    async def index():
        return HTMLResponse(html)

    return app
