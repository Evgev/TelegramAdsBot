from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .ads_api import AdsError
from .controller import decide, money
from .targets import PLACEMENTS, ad_targets, key, parse_targets, payload_target, resolve, source_tag, validate_template


def parse_bots(text, limit=50):
    result, seen = [], set()
    for item in re.split(r'[\s,;]+', text.strip()):
        if not item:
            continue
        item = re.sub(r'^https?://(?:www\.)?t\.me/', '', item).strip('/').lstrip('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', item):
            raise ValueError('Некорректный username бота: ' + item[:50])
        if item.lower() not in seen:
            seen.add(item.lower())
            result.append(item)
    if not result:
        raise ValueError('Добавьте хотя бы одного бота')
    if len(result) > limit:
        raise ValueError(f'За одну пачку можно добавить не более {limit} ботов')
    return result


def make_link(url, source):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.netloc != 't.me' or len(parts.path.strip('/').split('/')) != 1:
        raise ValueError('Для отдельной метки нужна ссылка https://t.me/имя_бота')
    query = dict(parse_qsl(parts.query))
    query['start'] = source
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def sign(value, secret):
    body = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode()).decode().rstrip('=')
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return body + '.' + signature


def unsign(token, secret):
    try:
        body, signature = token.rsplit('.', 1)
        expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        result = json.loads(base64.urlsafe_b64decode(body + '=' * (-len(body) % 4)))
        if result['exp'] < time.time():
            raise ValueError()
        return result
    except (ValueError, KeyError, TypeError):
        raise ValueError('Предпросмотр устарел. Проверьте настройки ещё раз.') from None


class Engine:
    def __init__(self, settings, db, api):
        self.settings, self.db, self.api = settings, db, api
        self.sync_lock, self.work_lock, self.action_lock = asyncio.Lock(), asyncio.Lock(), asyncio.Lock()
        self.stopping = False

    async def sync(self, history=False):
        async with self.sync_lock:
            account = await self.api.call('getCurrentAccount')
            ads = await self.api.all_ads()
            now = int(time.time())
            # Only mark absent after a complete successful listing.
            with self.db.connect() as conn:
                conn.execute('UPDATE ads SET present=0')
                for ad in ads:
                    conn.execute('INSERT INTO ads(id,raw,updated,present) VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,updated=excluded.updated,present=1',
                                 (ad['ad_id'], json.dumps(ad, ensure_ascii=False), now))
            for ad in ads:
                self.db.save_ad(ad, now)
            self.db.put('account', account)
            self.db.put('last_sync', now)
            self.db.put('sync_error', None)
            if not self.db.get('templates_seeded'):
                for ad in ads:
                    if ad.get('placement') == 'bot_banner' and ad.get('text'):
                        self.save_template({'name': ad['title'], 'text': ad['text'], 'promote_url': ad['promote_url'],
                                            'impression_frequency': ad.get('impression_frequency', 1), 'source_ad_id': ad['ad_id']})
                self.db.put('templates_seeded', True)
            if history or now - self.db.get('last_history_sync', 0) >= 3600:
                await self.history(ads, now)
            return {'count': len(ads), 'synced_at': now}

    async def history(self, ads, now):
        start = (now // 86400 - 13) * 86400
        for ad in ads:
            try:
                rows = await self.api.call('getAdStats', {'ad_id': ad['ad_id'], 'from_time': start, 'to_time': (now // 86400 + 1) * 86400, 'interval': 86400})
                with self.db.connect() as conn:
                    for row in rows or []:
                        ts = row.get('from_time')
                        if ts is not None:
                            conn.execute('INSERT OR REPLACE INTO daily_stats(ad_id,ts,raw) VALUES (?,?,?)', (ad['ad_id'], ts, json.dumps(row)))
            except AdsError as exc:
                self.db.event('error', 'Не удалось получить историю: ' + exc.code, ad['ad_id'])
            await asyncio.sleep(0.15)
        self.db.put('last_history_sync', now)

    def save_template(self, body, template_id=None):
        template_id = template_id or uuid.uuid4().hex
        self.db.execute('INSERT INTO templates(id,name,body,created) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body',
                        (template_id, body['name'], json.dumps(body, ensure_ascii=False), int(time.time())))
        return template_id

    async def preview(self, spec):
        kind = spec.get('kind', 'bots')
        names = parse_targets(kind, spec.get('targets') or spec.get('bots', ''), self.settings.max_batch_size)
        rows = self.db.query('SELECT body FROM templates WHERE id=?', (spec['template_id'],))
        if not rows:
            raise ValueError('Шаблон не найден')
        template = json.loads(rows[0]['body'])
        validate_template(template, kind, spec.get('tracking', 'keep'))
        targets, rejected, seen = [], [], set()
        for name in names:
            try:
                target = await resolve(self.api, kind, name)
                identity = target.get('bot_id', target.get('channel_id', target['key']))
                if identity not in seen:
                    seen.add(identity)
                    targets.append(target)
            except AdsError as exc:
                if exc.retryable:
                    raise
                rejected.append({'username': name, 'error': exc.code})
            except ValueError as exc:
                rejected.append({'username': name, 'error': str(exc)})
        if not targets:
            raise ValueError('Нет разрешённых целей: ' + '; '.join(t['username'] + ': ' + t['error'] for t in rejected))
        total = money(Decimal(str(spec['budget'])) * len(targets))
        if total > self.settings.max_batch_budget:
            raise ValueError(f'Лимит одной пачки — {self.settings.max_batch_budget:g} TON')
        account = await self.api.call('getCurrentAccount')
        if account.get('currency') != 'TON':
            raise ValueError('Этот прототип настроен для TON-кабинета')
        if total > account['remaining_budget']:
            raise ValueError('Недостаточно свободного баланса: нужно ' + str(total) + ' TON')
        if spec.get('tracking') == 'unique':
            make_link(template['promote_url'], 'check')
        value = {'spec': spec, 'template': template, 'targets': targets, 'total': total, 'exp': int(time.time()) + 900,
                 'batch_id': uuid.uuid4().hex}
        existing = {key(k, v) for ad in self.db.ads() for k, v in ad_targets(ad)}
        warnings = []
        overlap = [t['username'] for t in targets if key(kind, t['username']) in existing]
        if overlap:
            warnings.append('Уже есть объявления с таргетом: ' + ', '.join(('' if kind == 'search' else '@') + x for x in overlap))
        if kind == 'search':
            warnings.append('Поиск: одно объявление на фразу; текст шаблона не передаётся, ссылка без start. Платформа окончательно проверяет фразу и доступность формата при создании.')
        if rejected:
            warnings.append('Недоступные площадки исключены. Проверьте список перед подтверждением.')
        if spec.get('tracking') == 'unique':
            warnings.append('Метки будут сохранены здесь. Регистрация новых меток в Minishop выполняется отдельно.')
        if spec.get('policy'):
            policy = spec['policy']
            max_affordable = money(spec['budget'] * 1000 / policy['target_views'])
            if policy['max_cpm'] > max_affordable:
                warnings.append(f'Для цели по показам бюджет допускает средний CPM до {max_affordable:g} TON; при более высокой ставке бюджет может закончиться раньше цели.')
        return {'token': sign(value, self.settings.session_secret), 'batch_id': value['batch_id'], 'targets': targets,
                'total': total, 'currency': account['currency'], 'available': account['remaining_budget'],
                'template': template, 'spec': spec, 'warnings': warnings, 'rejected': rejected}

    def create_batch(self, token):
        value = unsign(token, self.settings.session_secret)
        spec, template, targets, batch_id = value['spec'], value['template'], value['targets'], value['batch_id']
        now = int(time.time())
        with self.db.connect() as conn:
            if conn.execute('SELECT id FROM batches WHERE id=?', (batch_id,)).fetchone():
                return batch_id
            conn.execute('INSERT INTO batches(id,name,status,spec,created) VALUES (?,?,?,?,?)',
                         (batch_id, spec['name'], 'queued', json.dumps(value, ensure_ascii=False), now))
            for target in targets:
                item_id = uuid.uuid4().hex
                kind = spec.get('kind', 'bots')
                source = source_tag(batch_id, target['username'])
                title = ('PA ' + batch_id[:8] + '-' + item_id[:12] + ' · ' + spec['name'] + ' · ' + target['username']).encode()[:128].decode(errors='ignore')
                payload = {'title': title, 'text': '' if kind == 'search' else template['text'],
                           'promote_url': make_link(template['promote_url'], source) if spec.get('tracking') == 'unique' else template['promote_url'],
                           'cpm': spec['cpm'], 'placement': PLACEMENTS[kind], 'target': payload_target(kind, target),
                           'impression_frequency': template.get('impression_frequency', 1), 'initial_budget': spec['budget'], 'return_target': True}
                conn.execute('INSERT INTO batch_items(id,batch_id,username,status,payload,created) VALUES (?,?,?,?,?,?)',
                             (item_id, batch_id, target['username'], 'queued', json.dumps(payload, ensure_ascii=False), now))
        self.db.event('batch', 'Создана пачка: ' + spec['name'], details={'batch_id': batch_id, 'count': len(targets), 'total': value['total']})
        return batch_id

    async def process_item(self):
        async with self.work_lock:
            rows = self.db.query("SELECT i.* FROM batch_items i JOIN batches b ON b.id=i.batch_id WHERE i.status IN ('queued','processing') AND i.next_attempt<=? AND b.status NOT IN ('cancelled','paused') ORDER BY i.created,i.rowid LIMIT 1", (int(time.time()),))
            if not rows:
                return
            item = rows[0]
            payload = json.loads(item['payload'])
            self.db.execute("UPDATE batch_items SET status='processing',attempts=attempts+1 WHERE id=?", (item['id'],))
            self.db.execute("UPDATE batches SET status='running' WHERE id=?", (item['batch_id'],))
            try:
                if item['attempts']:
                    # Reconcile uncertain writes before retry, including after a restart.
                    matching = [a for a in await self.api.all_ads() if a['title'] == payload['title'] and a['promote_url'] == payload['promote_url']]
                    if len(matching) > 1:
                        raise AdsError('AMBIGUOUS_EXISTING_AD')
                    if not matching and time.time() - item['created'] > 23 * 3600:
                        raise AdsError('IDEMPOTENCY_WINDOW_EXPIRED')
                    ad = matching[0] if matching else await self.api.call('createAd', payload, idempotency='papin-' + item['id'])
                else:
                    ad = await self.api.call('createAd', payload, idempotency='papin-' + item['id'])
                self.db.save_ad(ad)
                self.db.execute("UPDATE batch_items SET status='done',ad_id=?,error=NULL WHERE id=?", (ad['ad_id'], item['id']))
                self.db.event('created', 'Создано объявление для ' + item['username'], ad['ad_id'], {'batch_id': item['batch_id']})
                batch = self.db.query('SELECT spec FROM batches WHERE id=?', (item['batch_id'],))[0]
                policy = json.loads(batch['spec'])['spec'].get('policy')
                if policy:
                    try:
                        self.enable_policy(ad, policy)
                    except ValueError:
                        self.db.event('error', 'Объявление создано; параметры автоподбора требуют проверки', ad['ad_id'])
            except AdsError as exc:
                attempts = item['attempts'] + 1
                retry = exc.retryable and attempts < 5
                self.db.execute('UPDATE batch_items SET status=?,error=?,next_attempt=? WHERE id=?',
                                ('queued' if retry else 'attention', exc.code, int(time.time()) + min(900, 30 * 2 ** attempts), item['id']))
                self.db.event('error', 'Создание ' + item['username'] + ': ' + exc.code, details={'batch_id': item['batch_id']})
            self.refresh_batch(item['batch_id'])

    def refresh_batch(self, batch_id):
        counts = {r['status']: r['n'] for r in self.db.query('SELECT status,count(*) n FROM batch_items WHERE batch_id=? GROUP BY status', (batch_id,))}
        status = 'running' if counts.get('queued') or counts.get('processing') else ('attention' if counts.get('attention') else 'done')
        self.db.execute('UPDATE batches SET status=? WHERE id=?', (status, batch_id))

    def enable_policy(self, ad, params):
        now = int(time.time())
        if not params['min_cpm'] <= ad['cpm'] <= params['max_cpm']:
            raise ValueError('Текущая CPM должна находиться в диапазоне стратегии')
        body = dict(params, enabled=True, started_at=now, deadline=now + int(params['duration_hours'] * 3600),
                    baseline_views=ad.get('views', 0), baseline_spent=ad.get('spent_budget', 0),
                    waiting_for_active=ad.get('status') != 'active',
                    last_change=now, last_cpm=ad['cpm'], last_reason='Прогрев: ожидаем статистику при текущей ставке')
        self.db.save_policy(ad['ad_id'], body)
        self.db.event('policy', 'Включён подбор CPM', ad['ad_id'], params)

    async def regulate(self):
        if self.db.get('automation_paused', False):
            return
        async with self.action_lock:
            if self.db.get('automation_paused', False):
                return
            for ad_id, policy in self.db.policies().items():
                if not policy.get('enabled'):
                    continue
                ad = self.db.ad(ad_id)
                if not ad:
                    continue
                now = int(time.time())
                if policy.get('waiting_for_active') and ad.get('status') == 'active':
                    policy.update(waiting_for_active=False, started_at=now,
                                  deadline=now + int(policy['duration_hours'] * 3600),
                                  baseline_views=ad.get('views', 0), baseline_spent=ad.get('spent_budget', 0),
                                  last_change=now)
                snapshots = self.db.query('SELECT * FROM snapshots WHERE ad_id=? AND ts>=? ORDER BY ts', (ad_id, now - 86400))
                decision = decide(ad, policy, snapshots, now, self.db.get('last_sync', 0))
                policy['last_reason'] = decision.reason
                policy['observed_rate'] = decision.rate
                policy['target_rate'] = decision.target_rate
                if decision.action == 'external':
                    policy['enabled'] = False
                    self.db.event('policy', decision.reason, ad_id)
                if decision.action in {'bid', 'pause'}:
                    try:
                        current = (await self.api.call('getAdsById', {'ad_ids': [ad_id], 'return_target': True}))[0]
                        if current['status'] != 'active' or abs(current['cpm'] - ad['cpm']) > 0.009:
                            self.db.save_ad(current)
                            policy['last_reason'] = 'Состояние объявления изменилось; ожидаем синхронизацию'
                            if abs(current['cpm'] - ad['cpm']) > 0.009:
                                policy['enabled'] = False
                        else:
                            params = {'ad_id': ad_id, 'return_target': True}
                            params.update({'cpm': decision.cpm} if decision.action == 'bid' else {'is_paused': True})
                            result = await self.api.call('editAd', params)
                            self.db.save_ad(result)
                            policy.update(last_change=now, last_cpm=result['cpm'])
                            if decision.action == 'pause':
                                policy['enabled'] = False
                            self.db.event('bid' if decision.action == 'bid' else 'pause', decision.reason, ad_id,
                                          {'before_cpm': ad['cpm'], 'after_cpm': result['cpm'], 'rate': decision.rate, 'target_rate': decision.target_rate})
                    except (AdsError, IndexError) as exc:
                        policy['enabled'] = False
                        policy['last_reason'] = 'Требуется проверка после ошибки API'
                        self.db.event('error', 'Автоподбор приостановлен: ' + str(exc), ad_id)
                self.db.save_policy(ad_id, policy)

    async def run(self):
        last_sync = 0
        while not self.stopping:
            try:
                if time.time() - last_sync >= self.settings.sync_interval:
                    try:
                        await self.sync()
                    except AdsError as exc:
                        self.db.put('sync_error', exc.code)
                        self.db.event('error', 'Ошибка синхронизации: ' + exc.code)
                    last_sync = time.time()
                await self.process_item()
                await self.regulate()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Store only a sanitized class name for unexpected failures.
                self.db.event('error', 'Ошибка фоновой задачи: ' + type(exc).__name__)
            await asyncio.sleep(5)
