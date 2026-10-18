import asyncio
import re

import httpx


class AdsError(Exception):
    def __init__(self, code, retryable=False):
        self.code = str(code)[:200]
        self.retryable = retryable
        super().__init__(self.code)


class AdsAPI:
    base_url = 'https://promoteapi.telegram.org'
    allowed = {'getCurrentAccount', 'getAdsList', 'getAdsById', 'getTargetBot', 'getTargetChannel', 'getAdStats', 'createAd', 'editAd'}

    def __init__(self, token):
        self.token = token
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False)
        self.lock = asyncio.Lock()

    async def close(self):
        await self.client.aclose()

    async def call(self, method, params=None, idempotency=None):
        if method not in self.allowed:
            raise AdsError('METHOD_NOT_ALLOWED')
        if method == 'createAd' and not idempotency:
            raise AdsError('IDEMPOTENCY_KEY_REQUIRED')
        headers = {'Authorization': 'Bearer ' + self.token}
        if idempotency:
            headers['Idempotency-Key'] = idempotency
        try:
            async with self.lock:
                response = await self.client.post(self.base_url + '/' + method, json=params or {}, headers=headers)
            if response.status_code >= 400:
                raise AdsError('HTTP_' + str(response.status_code), response.status_code in {429, 500, 502, 503, 504})
            payload = response.json()
        except httpx.HTTPError:
            # Delivery of a write may be uncertain. Caller keeps the same idempotency key.
            raise AdsError('NETWORK_ERROR', True) from None
        except ValueError:
            raise AdsError('INVALID_API_RESPONSE', True) from None
        if not isinstance(payload, dict) or not payload.get('ok'):
            code = str(payload.get('error', 'UNKNOWN_API_ERROR')) if isinstance(payload, dict) else 'INVALID_API_RESPONSE'
            code = code.replace(self.token, '[redacted]')
            raise AdsError(code, bool(re.match(r'FLOOD_WAIT|RATE_LIMIT|INTERNAL|TIMEOUT', code)))
        return payload.get('result')

    async def all_ads(self):
        rows, offset, seen = [], 0, set()
        while len(rows) < 10000:
            page = await self.call('getAdsList', {'offset': offset, 'limit': 100, 'return_target': True})
            batch = page.get('ads', [])
            rows.extend(batch)
            if not batch or len(rows) >= page.get('total_count', len(rows)):
                return rows
            next_offset = page.get('next_offset', len(rows))
            if str(next_offset) in seen:
                raise AdsError('PAGINATION_STALLED')
            seen.add(str(next_offset))
            offset = next_offset
        raise AdsError('TOO_MANY_ADS')
