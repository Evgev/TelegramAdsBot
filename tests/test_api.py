import json
import unittest

import httpx

from app.ads_api import AdsAPI, AdsError


class APITests(unittest.IsolatedAsyncioTestCase):
    async def make_api(self, handler):
        api = AdsAPI('secret-test-token')
        await api.client.aclose()
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        self.addAsyncCleanup(api.close)
        return api

    async def test_pagination(self):
        offsets = []
        def handler(request):
            offset = json.loads(request.content)['offset']
            offsets.append(offset)
            return httpx.Response(200, json={'ok':True,'result':{'total_count':3,'ads':[{'ad_id':offset+1}]}})
        api = await self.make_api(handler)
        self.assertEqual(len(await api.all_ads()), 3)
        self.assertEqual(offsets, [0,1,2])

    async def test_no_redirect_and_sanitized_errors(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={'location':'https://evil.test'}, json={'ok':False,'error':'secret-test-token BAD'})
        api = await self.make_api(handler)
        with self.assertRaises(AdsError) as exc:
            await api.call('getCurrentAccount')
        self.assertNotIn('secret-test-token', str(exc.exception))
        self.assertEqual(calls, ['https://promoteapi.telegram.org/getCurrentAccount'])

    async def test_idempotency_key_required_and_sent(self):
        headers = []
        def handler(request):
            headers.append(request.headers.get('Idempotency-Key'))
            return httpx.Response(200, json={'ok':True,'result':{'ad_id':1}})
        api = await self.make_api(handler)
        with self.assertRaises(AdsError):
            await api.call('createAd', {})
        await api.call('createAd', {}, idempotency='stable-key')
        self.assertEqual(headers, ['stable-key'])

    async def test_transient_http_error(self):
        api = await self.make_api(lambda _: httpx.Response(503))
        with self.assertRaises(AdsError) as exc:
            await api.call('getCurrentAccount')
        self.assertTrue(exc.exception.retryable)
