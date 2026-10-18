"""Placement-specific parsing and payloads shared by discovery and batch creation."""
import hashlib
import re
from urllib.parse import urlsplit

KINDS = ('bots', 'channels', 'search')
PLACEMENTS = {'bots': 'bot_banner', 'channels': 'channel_post', 'search': 'search_result'}


def usernames(text, limit=50):
    result = {}
    for value in re.split(r'[\s,;]+', text.strip()):
        if not value:
            continue
        value = re.sub(r'^https?://(?:www\.)?t\.me/', '', value).strip('/').lstrip('@')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
            raise ValueError('Некорректный публичный username: ' + value[:50])
        result.setdefault(value.lower(), value)
    if not result:
        raise ValueError('Добавьте хотя бы одну площадку')
    if len(result) > limit:
        raise ValueError(f'За одну пачку можно добавить не более {limit} площадок')
    return list(result.values())


def keywords(text, limit=50):
    result = {}
    for value in re.split(r'[\n;]+', text):
        value = ' '.join(value.split()).strip()
        if not value:
            continue
        if len(value) > 32 or any(ord(c) < 32 for c in value):
            raise ValueError('Поисковая фраза должна содержать от 1 до 32 символов')
        result.setdefault(value.casefold(), value)
    if not result:
        raise ValueError('Добавьте хотя бы одну поисковую фразу')
    if len(result) > limit:
        raise ValueError(f'За одну пачку можно добавить не более {limit} фраз')
    return list(result.values())


def parse_targets(kind, text, limit=50):
    if kind not in KINDS:
        raise ValueError('Неизвестный тип размещения')
    return keywords(text, limit) if kind == 'search' else usernames(text, limit)


def key(kind, value):
    return kind + ':' + ' '.join(value.split()).lstrip('@').casefold()


def ad_targets(ad):
    target = ad.get('target') or {}
    for kind in ('bots', 'channels'):
        for item in target.get(kind, []):
            if item.get('username'):
                yield kind, item['username']
    for value in target.get('search_queries', []):
        yield 'search', value


async def resolve(api, kind, value):
    if kind == 'search':
        return {'username': value, 'title': value, 'key': key(kind, value),
                'validation': 'local', 'note': 'Фразу и доступность формата окончательно проверяет Telegram Ads при создании'}
    field = 'bot_id' if kind == 'bots' else 'channel_id'
    target = await api.call('getTargetBot' if kind == 'bots' else 'getTargetChannel', {field: '@' + value})
    if not isinstance(target, dict) or not target.get(field) or not target.get('username'):
        raise ValueError('Площадка не разрешена рекламным API: @' + value)
    return {field: target[field], 'username': target['username'], 'title': target.get('title', value),
            'key': key(kind, target['username']), 'validation': 'telegram_ads'}


def validate_template(template, kind, tracking):
    if kind not in template.get('placements', list(KINDS)):
        raise ValueError('Шаблон не разрешён для выбранного типа размещения')
    if kind != 'search' and not template.get('text', '').strip():
        raise ValueError('Для размещения в ботах и каналах нужен текст объявления')
    if kind == 'search':
        url = urlsplit(template['promote_url'])
        if tracking != 'keep' or url.query or url.fragment or not re.fullmatch(r'/[A-Za-z][A-Za-z0-9_]{3,31}/?', url.path):
            raise ValueError('Для поиска нужна прямая ссылка https://t.me/username без start, query, поста или invite; отдельные метки недоступны')


def payload_target(kind, target):
    if kind == 'search':
        return {'type': 'search', 'search_queries': [target['username']]}
    field = 'bot_id' if kind == 'bots' else 'channel_id'
    return {'type': kind, field + 's': [target[field]]}


def source_tag(batch_id, target):
    return 'pa_' + batch_id[:8] + '_' + hashlib.sha256(target.encode()).hexdigest()[:16]
