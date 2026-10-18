"""Read-only Telegram discovery, typed CSV research and first-party performance."""
import asyncio
import csv
import hashlib
import io
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from .targets import KINDS, ad_targets, key, parse_targets, usernames


class TelegramDirectory:
    def __init__(self, settings):
        self.settings = settings
        self.client = None
        self.lock = asyncio.Lock()
        self.retry_after = 0

    def configured(self):
        return bool(self.settings.telegram_api_id and self.settings.telegram_api_hash and
                    Path(str(self.settings.telegram_session) if str(self.settings.telegram_session).endswith('.session') else str(self.settings.telegram_session) + '.session').is_file())

    async def close(self):
        if self.client:
            await self.client.disconnect()

    async def fetch(self, kind, query, mode):
        if not self.configured():
            raise ValueError('Telegram не подключён. Доступны ручной список, история и импорт CSV. Подключение описано в документации.')
        if time.time() < self.retry_after:
            raise ValueError(f'Telegram ограничил запросы. Повторите через {int(self.retry_after-time.time())+1} с.')
        from telethon import TelegramClient, errors, functions, types
        async with self.lock:
            try:
                if not self.client:
                    self.client = TelegramClient(str(self.settings.telegram_session), self.settings.telegram_api_id,
                                                 self.settings.telegram_api_hash, flood_sleep_threshold=0,
                                                 request_retries=0, connection_retries=1, timeout=10)
                if not self.client.is_connected():
                    await self.client.connect()
                if not await self.client.is_user_authorized() or await self.client.is_bot():
                    raise ValueError('Нужна авторизованная сессия пользовательского Telegram-аккаунта')
                if mode == 'search':
                    result = await self.client(functions.contacts.SearchRequest(
                        q=query, limit=30, bots=kind == 'bots', broadcasts=kind == 'channels'))
                else:
                    entity = await self.client.get_entity('@' + usernames(query, 1)[0])
                    if kind == 'bots':
                        if not getattr(entity, 'bot', False):
                            raise ValueError('Исходная площадка должна быть ботом')
                        result = await self.client(functions.bots.GetBotRecommendationsRequest(entity))
                    else:
                        if not isinstance(entity, types.Channel) or not getattr(entity, 'broadcast', False):
                            raise ValueError('Исходная площадка должна быть публичным каналом')
                        result = await self.client(functions.channels.GetChannelRecommendationsRequest(entity))
                entities = getattr(result, 'users' if kind == 'bots' else 'chats', [])
                rows = []
                for entity in entities:
                    if kind == 'bots' and not getattr(entity, 'bot', False):
                        continue
                    if kind == 'channels' and not getattr(entity, 'broadcast', False):
                        continue
                    username = getattr(entity, 'username', None)
                    if not username or getattr(entity, 'deleted', False):
                        continue
                    rows.append({'username': username, 'telegram_id': entity.id,
                                 'title': getattr(entity, 'title', None) or getattr(entity, 'first_name', username),
                                 'audience': getattr(entity, 'bot_active_users' if kind == 'bots' else 'participants_count', None),
                                 'audience_metric': 'mau' if kind == 'bots' else 'subscribers'})
                return {'items': rows[:100], 'total': getattr(result, 'count', len(rows))}
            except errors.FloodWaitError as exc:
                self.retry_after = time.time() + exc.seconds
                raise ValueError(f'Telegram ограничил запросы на {exc.seconds} с. Сохранённые результаты доступны.') from None
            except ValueError:
                raise
            except (errors.RPCError, OSError, asyncio.TimeoutError):
                raise ValueError('Не удалось получить данные Telegram. Проверьте подключение и сессию; ручной список доступен.') from None


def csv_rows(text):
    text = text.lstrip('\ufeff')
    if len(text.encode()) > 120000:
        raise ValueError('CSV должен быть не больше 120 КБ')
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=',;\t')
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    if not headers or len(headers) > 40 or len(headers) != len(set(headers)) or any(not h or len(h) > 120 for h in headers):
        raise ValueError('CSV должен содержать уникальные названия колонок в первой строке')
    rows = []
    try:
        for row in reader:
            if len(rows) >= 500:
                raise ValueError('Не более 500 строк в одном CSV')
            if None in row:
                raise ValueError('Количество полей CSV не совпадает с заголовком')
            rows.append(row)
    except csv.Error:
        raise ValueError('Не удалось прочитать CSV') from None
    return headers, rows


class Discovery:
    def __init__(self, settings, db, directory=None):
        self.settings, self.db = settings, db
        self.directory = directory or TelegramDirectory(settings)
        self.lock = asyncio.Lock()

    def status(self):
        return {'telegram_configured': self.directory.configured(), 'tgmaps_mode': 'csv',
                'research_count': self.db.query('SELECT count(*) n FROM research')[0]['n'],
                'cache_seconds': self.settings.discovery_cache_seconds,
                'telegram_help': 'python -m app.telegram_login'}

    def import_csv(self, body):
        headers, rows = csv_rows(body['csv'])
        columns = body['columns']
        if not columns.get('target') or any(value and value not in headers for value in columns.values()):
            raise ValueError('Укажите существующую колонку с username или поисковой фразой')
        kind, relation = body['kind'], body['relation']
        if kind not in KINDS or relation not in ('destination', 'placement', 'keyword'):
            raise ValueError('Проверьте тип площадки и связь рекламных данных')
        if (kind == 'search') != (relation == 'keyword'):
            raise ValueError('Для поисковых фраз используйте связь «Ключевые слова»')
        parsed, now = [], int(time.time())
        for n, row in enumerate(rows, 2):
            def cell(name, limit=2000):
                return (row.get(columns.get(name)) or '').strip()[:limit]
            if not cell('target'):
                continue
            values = parse_targets(kind, cell('target'), 1)
            url = cell('destination_url', 2048)
            if url and (urlsplit(url).scheme != 'https' or not urlsplit(url).netloc or urlsplit(url).username):
                raise ValueError(f'Строка {n}: ссылка назначения должна быть HTTPS')
            item = {'username': values[0], 'title': cell('title', 200) or values[0], 'kind': kind,
                    'relation': relation, 'creative_text': cell('creative_text'), 'destination_url': url,
                    'observed_at': cell('observed_at', 100), 'detected_in': cell('detected_in', 200),
                    'source': body.get('source', 'TGMaps CSV'), 'imported_at': now}
            identity = hashlib.sha256(json.dumps(item | {'imported_at': 0}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            parsed.append((identity, kind, key(kind, values[0]), json.dumps(item, ensure_ascii=False), now))
        if not parsed:
            raise ValueError('В CSV нет целей для импорта')
        with self.db.connect() as conn:
            before = conn.total_changes
            conn.executemany('INSERT OR IGNORE INTO research(id,kind,target_key,body,updated) VALUES (?,?,?,?,?)', parsed)
            added = conn.total_changes - before
            if conn.execute('SELECT count(*) FROM research').fetchone()[0] > 10000:
                raise ValueError('Достигнут лимит 10000 исследовательских записей; обратитесь к администратору')
        self.db.event('research', 'Импорт исследовательских данных', details={'rows': len(parsed), 'added': added, 'kind': kind, 'relation': relation})
        return {'rows': len(parsed), 'added': added, 'duplicates': len(parsed)-added}

    def history(self):
        result = {}
        for ad in self.db.ads():
            targets = list(dict.fromkeys(ad_targets(ad)))
            for kind, target_value in targets:
                row = result.setdefault(key(kind, target_value), {'username': target_value, 'kind': kind, 'ad_ids': [], 'active': 0,
                                                          'views': 0, 'clicks': 0, 'spent': 0, 'grouped': False})
                row['grouped'] |= len(targets) > 1
                row['ad_ids'].append(ad['ad_id'])
                row['active'] += ad.get('status') == 'active'
                for dest, src in [('views', 'views'), ('clicks', 'clicks'), ('spent', 'spent_budget')]:
                    value = ad.get(src)
                    row[dest] = None if value is None or row[dest] is None else row[dest] + value
        for row in result.values():
            row['ctr'] = row['clicks']/row['views']*100 if row['views'] and row['clicks'] is not None else None
            row['cpc'] = row['spent']/row['clicks'] if row['clicks'] and row['spent'] is not None else None
        return result

    async def find(self, kind, query, mode='similar', refresh=False):
        if mode != 'research' and not query.strip():
            raise ValueError('Введите исходную площадку или фразу')
        history = self.history()
        kind_history = [row for row in history.values() if row['kind'] == kind]
        research = [json.loads(r['body']) for r in self.db.query('SELECT body FROM research WHERE kind=? ORDER BY updated DESC', (kind,))]
        warnings, collected, timestamps, total = [], {}, [], 0
        telegram_keys = {}
        def add(item, source, updated=None):
            k = key(kind, item['username'])
            if item.get('telegram_id') is not None:
                k = telegram_keys.setdefault(item['telegram_id'], k)
            row = collected.setdefault(k, {'key': k, 'kind': kind, 'username': item['username'],
                                          'title': item.get('title', item['username']), 'audience': None,
                                          'sources': [], 'research': []})
            if source not in row['sources']:
                row['sources'].append(source)
            if source.startswith('Telegram'):
                row.update({name: item.get(name) for name in ('title', 'audience', 'audience_metric', 'telegram_id')})
                row['audience_updated'] = updated
            return row
        if mode == 'research':
            for item in research:
                if not query or query.casefold() in (item['username']+' '+item['title']).casefold():
                    add(item, item['source'])
        elif kind == 'search':
            seeds = parse_targets(kind, query, 20)
            for seed in seeds:
                add({'username': seed}, 'Введено вручную')
                for suffix in (' бот', ' telegram'):
                    if len(seed + suffix) <= 32:
                        add({'username': seed + suffix}, 'Гипотеза: вариант фразы')
            for item in kind_history + research:
                if any(seed.casefold() in item['username'].casefold() for seed in seeds):
                    add(item, 'Ваша история' if 'ad_ids' in item else item['source'])
        elif mode in ('similar', 'search'):
            seeds = usernames(query, 5) if mode == 'similar' else [query.strip().lstrip('@')]
            if mode == 'search' and len(seeds[0]) < 3:
                raise ValueError('Для поиска введите не менее трёх символов')
            async with self.lock:
                for seed in seeds:
                    cache_key = hashlib.sha256(f'{kind}:{mode}:{seed.casefold()}'.encode()).hexdigest()
                    cached = self.db.query('SELECT body,updated FROM discovery_cache WHERE key=?', (cache_key,))
                    cached = cached[0] if cached else None
                    value = json.loads(cached['body']) if cached else None
                    updated = cached['updated'] if cached else None
                    if refresh or not cached or time.time()-updated > self.settings.discovery_cache_seconds:
                        try:
                            value = await asyncio.wait_for(self.directory.fetch(kind, seed, mode), timeout=35)
                            updated = int(time.time())
                            self.db.execute('INSERT INTO discovery_cache(key,body,updated) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body,updated=excluded.updated',
                                            (cache_key, json.dumps(value, ensure_ascii=False), updated))
                            self.db.execute('DELETE FROM discovery_cache WHERE key IN (SELECT key FROM discovery_cache ORDER BY updated DESC LIMIT -1 OFFSET 1000)')
                        except (ValueError, asyncio.TimeoutError) as exc:
                            warnings.append(str(exc) if isinstance(exc, ValueError) else 'Telegram не ответил вовремя')
                            if cached:
                                warnings.append('Показан ранее сохранённый список; время данных указано в таблице')
                    if value:
                        total += value.get('total', len(value['items']))
                        timestamps.append(updated)
                        for item in value['items']:
                            add(item, 'Telegram · ' + seed, updated)
                        if value.get('total', 0) > len(value['items']):
                            warnings.append('Telegram вернул сокращённый список. Подписка Premium может расширить рекомендации.')
        else:
            for value in parse_targets(kind, query, 100):
                add({'username': value}, 'Введено вручную')
        # CSV and first-party history remain useful when Telegram is not configured.
        if not collected and kind != 'search':
            q = query.strip().lstrip('@').casefold()
            for item in kind_history + research:
                if q in (item['username'] + ' ' + item.get('title', '')).casefold():
                    add(item, 'Ваша история' if 'ad_ids' in item else item['source'])
        by_key = {}
        for item in research:
            by_key.setdefault(key(kind, item['username']), []).append(item)
        for k, row in collected.items():
            row['history'] = history.get(k)
            row['research'] = by_key.get(k, [])[:30]
            row['research_count'] = len(by_key.get(k, []))
            row['availability'] = 'unchecked'
        return {'items': list(collected.values())[:500], 'warnings': list(dict.fromkeys(warnings)),
                'telegram_total': total, 'updated': min(timestamps) if timestamps else None,
                'history_note': 'История показана на уровне объявления. При нескольких целях его показатели относятся ко всему объявлению.'}
