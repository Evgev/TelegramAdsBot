import asyncio
import base64
import copy
import hashlib
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ads_api import AdsError
from app.config import Settings, hash_password
from app.controller import decide
from app.engine import Engine, parse_bots
from app.main import create_app


AD = dict(ad_id=7, title='Example', text='VPN', promote_url='https://t.me/papin_vpn_bot?start=Ejx',
          placement='bot_banner', currency='TON', status='active', cpm=5, remaining_budget=2,
          spent_budget=0, views=0, clicks=0, actions=0, impression_frequency=1,
          target={'bots': [{'username': 'Example_bot'}]})
PARAMS = dict(target_views=1000, duration_hours=24, min_cpm=1, max_cpm=10, spend_limit=2,
              step_percent=10, tolerance_percent=20, cooldown_minutes=120, settle_minutes=60, window_minutes=60)


class FakeAPI:
    def __init__(self):
        self.ads = [copy.deepcopy(AD)]
        self.calls = []
        self.fail_create_after_delivery = False
        self.fail_list = False

    async def close(self):
        pass

    async def all_ads(self):
        if self.fail_list:
            raise AdsError('NETWORK_ERROR', True)
        return copy.deepcopy(self.ads)

    async def call(self, method, params=None, idempotency=None):
        params = params or {}
        self.calls.append((method, copy.deepcopy(params), idempotency))
        if method == 'getCurrentAccount':
            return dict(title='Test account', currency='TON', remaining_budget=20, spent_budget=1, ads_budget=2)
        if method == 'getTargetBot':
            return dict(bot_id=123, username=params['bot_id'].lstrip('@'), title='Example bot')
        if method == 'getAdsById':
            return [copy.deepcopy(a) for a in self.ads if a['ad_id'] in params['ad_ids']]
        if method == 'getAdStats':
            return []
        if method == 'createAd':
            ad = dict(AD, **{k: v for k, v in params.items() if k in AD})
            ad['ad_id'] = max(a['ad_id'] for a in self.ads) + 1
            self.ads.append(ad)
            if self.fail_create_after_delivery:
                self.fail_create_after_delivery = False
                raise AdsError('NETWORK_ERROR', True)
            return copy.deepcopy(ad)
        if method == 'editAd':
            ad = next(a for a in self.ads if a['ad_id'] == params['ad_id'])
            if 'cpm' in params:
                ad['cpm'] = params['cpm']
            if 'is_paused' in params:
                ad['status'] = 'stopped' if params['is_paused'] else 'active'
            return copy.deepcopy(ad)
        raise AssertionError(method)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 200000
        self.ad = copy.deepcopy(AD)
        self.policy = dict(PARAMS, enabled=True, started_at=self.now-14400, deadline=self.now+36000,
                           baseline_views=0, baseline_spent=0, last_change=self.now-14400, last_cpm=5)
        self.samples = [dict(ts=self.now-3600, views=0, cpm=5, status='active'),
                        dict(ts=self.now, views=0, cpm=5, status='active')]

    def decision(self, **kwargs):
        return decide(self.ad, self.policy, self.samples, self.now, kwargs.get('last_sync', self.now))

    def test_increases_for_zero_delivery_after_mature_window(self):
        self.assertEqual(self.decision().cpm, 5.5)

    def test_decreases_when_ahead_of_target(self):
        self.samples[-1]['views'] = self.ad['views'] = 500
        self.assertEqual(self.decision().cpm, 4.5)

    def test_bid_cap_and_floor(self):
        self.policy['max_cpm'] = 5
        self.assertEqual(self.decision().action, 'hold')
        self.samples[-1]['views'] = self.ad['views'] = 500
        self.policy['min_cpm'] = 5
        self.assertEqual(self.decision().action, 'hold')

    def test_stale_or_inactive_never_changes(self):
        self.assertEqual(self.decision(last_sync=self.now-901).action, 'hold')
        for status in ['in_review', 'stopped', 'declined']:
            self.ad['status'] = status
            self.assertEqual(self.decision().action, 'hold')

    def test_insufficient_samples_and_cooldown(self):
        self.samples[0]['ts'] = self.now-1200
        self.assertEqual(self.decision().action, 'hold')
        self.policy['last_change'] = self.now-600
        self.assertEqual(self.decision().action, 'hold')

    def test_external_change_disables_even_during_cooldown(self):
        self.policy['last_change'] = self.now-600
        self.ad['cpm'] = 4
        self.assertEqual(self.decision().action, 'external')

    def test_external_change_takes_priority_over_automatic_pause(self):
        self.ad.update(cpm=4, views=1000)
        self.assertEqual(self.decision().action, 'external')

    def test_each_goal_condition_pauses(self):
        self.ad['views'] = 1000
        self.assertEqual(self.decision().action, 'pause')
        self.ad['views'] = 0
        self.ad['spent_budget'] = 2
        self.assertEqual(self.decision().action, 'pause')
        self.ad['spent_budget'] = 0
        self.policy['deadline'] = self.now
        self.assertEqual(self.decision().action, 'pause')

    def test_counter_reset_holds(self):
        self.samples[0]['views'] = 100
        self.assertEqual(self.decision().action, 'hold')


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        settings = Settings(Path(self.temp.name), 'test-token', 'admin', hash_password('test-password'),
                            's'*48, 'http://testserver', secure_cookie=False, run_worker=False)
        self.app = create_app(settings, self.api)
        self.db = self.app.state.db
        self.engine = self.app.state.engine
        self.client = TestClient(self.app)
        self.origin = {'origin': 'http://testserver'}
        response = self.client.post('/api/login', json={'username': 'admin', 'password': 'test-password'}, headers=self.origin)
        self.headers = dict(self.origin, **{'x-csrf-token': response.json()['csrf']})
        asyncio.run(self.engine.sync())
        self.template = self.db.query('SELECT id FROM templates')[0]['id']
        self.spec = dict(name='VPN batch', template_id=self.template, bots='@Example_bot, https://t.me/Another_bot', budget=2, cpm=5)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def preview(self):
        response = self.client.post('/api/batches/preview', json=self.spec, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def batch(self):
        return self.engine.create_batch(self.preview()['token'])

    def test_auth_csrf_origin(self):
        self.assertEqual(TestClient(self.app).get('/api/ads').status_code, 401)
        self.assertEqual(self.client.post('/api/sync', headers=self.origin).status_code, 403)
        self.assertEqual(self.client.post('/api/sync', headers=dict(self.headers, origin='https://evil.test')).status_code, 403)
        response = self.client.get('/api/overview')
        self.assertNotIn('test-token', response.text)

    def test_unicode_bad_username_is_unauthorized(self):
        response = self.client.post('/api/login', json={'username': 'админ', 'password': 'bad'}, headers=self.origin)
        self.assertEqual(response.status_code, 401)

    def test_document_contains_ui_without_external_style_or_script_requests(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('rel="stylesheet"', response.text)
        self.assertNotIn('<script src=', response.text)
        self.assertNotIn('@import', response.text)
        self.assertIn('.login-screen{', response.text)
        self.assertIn('id="workspace" class="workspace hidden" hidden', response.text)
        script = re.search(r'<script>(.*?)</script>', response.text, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        script_policy = response.headers['Content-Security-Policy'].split('script-src ', 1)[1].split(';', 1)[0]
        self.assertIn("'sha256-"+digest+"'", script_policy)
        self.assertNotIn("'unsafe-inline'", script_policy)
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertGreater(response.text.index('<script>'), response.text.index('id="modal-form"'))

    def test_preview_has_no_side_effects(self):
        preview = self.preview()
        self.assertEqual(preview['total'], 4)
        self.assertEqual(self.db.query('SELECT * FROM batches'), [])
        self.assertFalse(any(c[0] == 'createAd' for c in self.api.calls))

    def test_same_confirmation_is_idempotent(self):
        preview = self.preview()
        first = self.engine.create_batch(preview['token'])
        self.assertEqual(first, self.engine.create_batch(preview['token']))
        self.assertEqual(len(self.db.query('SELECT * FROM batch_items')), 2)

    def test_tampered_confirmation_is_rejected(self):
        token = self.preview()['token']
        response = self.client.post('/api/batches', json={'token': token+'a'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.query('SELECT * FROM batches'), [])

    def test_budget_and_policy_validation(self):
        for key, value in [('budget', -1), ('budget', 0), ('budget', 'NaN'), ('cpm', 0), ('cpm', 1.001)]:
            spec = dict(self.spec, **{key: value})
            self.assertEqual(self.client.post('/api/batches/preview', json=spec, headers=self.headers).status_code, 422)
        self.spec['budget'] = 15
        self.assertEqual(self.client.post('/api/batches/preview', json=self.spec, headers=self.headers).status_code, 400)
        self.spec['policy'] = dict(PARAMS, max_cpm=1)
        self.assertEqual(self.client.post('/api/batches/preview', json=self.spec, headers=self.headers).status_code, 422)

    def test_resume_after_uncertain_delivery_does_not_duplicate(self):
        self.batch()
        self.api.fail_create_after_delivery = True
        asyncio.run(self.engine.process_item())
        self.db.execute('UPDATE batch_items SET next_attempt=0')
        restarted = Engine(self.engine.settings, self.db, self.api)
        asyncio.run(restarted.process_item())
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'createAd']), 1)
        asyncio.run(restarted.process_item())
        self.assertEqual(len(self.api.ads), 3)
        self.assertEqual(self.db.query('SELECT status FROM batches')[0]['status'], 'done')

    def test_cancel_does_not_delete_created_ads(self):
        batch = self.batch()
        asyncio.run(self.engine.process_item())
        self.client.post('/api/batches/'+batch+'/cancel', headers=self.headers)
        asyncio.run(self.engine.process_item())
        self.assertEqual(len(self.api.ads), 2)
        self.assertFalse(any(c[0] == 'editAd' for c in self.api.calls))

    def test_expired_uncertain_write_needs_attention(self):
        self.batch()
        self.db.execute('UPDATE batch_items SET attempts=1,created=?', (int(time.time())-25*3600,))
        asyncio.run(self.engine.process_item())
        self.assertEqual(self.db.query("SELECT error FROM batch_items WHERE status='attention'")[0]['error'], 'IDEMPOTENCY_WINDOW_EXPIRED')
        self.assertFalse(any(c[0] == 'createAd' for c in self.api.calls))

    def test_failed_sync_preserves_ads(self):
        self.api.fail_list = True
        with self.assertRaises(AdsError):
            asyncio.run(self.engine.sync())
        self.assertEqual(len(self.db.ads()), 1)

    def test_policy_waits_for_moderation(self):
        ad = dict(AD, status='in_review')
        self.db.save_ad(ad)
        self.engine.enable_policy(ad, PARAMS)
        with patch('app.engine.time.time', return_value=time.time()+3*86400):
            self.db.save_ad(AD)
            asyncio.run(self.engine.regulate())
        policy = self.db.policies()[7]
        self.assertTrue(policy['enabled'])
        self.assertFalse(policy['waiting_for_active'])
        self.assertAlmostEqual(policy['deadline']-policy['started_at'], 86400)
        self.assertFalse(any(c[0] == 'editAd' for c in self.api.calls))

    def test_global_pause_blocks_goal_pause(self):
        self.engine.enable_policy(AD, PARAMS)
        self.db.save_ad(dict(AD, views=2000))
        self.db.put('automation_paused', True)
        asyncio.run(self.engine.regulate())
        self.assertFalse(any(c[0] == 'editAd' for c in self.api.calls))

    def test_manual_action_disables_policy(self):
        self.engine.enable_policy(AD, PARAMS)
        result = self.client.post('/api/ads/actions', json={'ad_ids': [7], 'action': 'cpm', 'cpm': 4}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertFalse(self.db.policies()[7]['enabled'])

    def test_tracking_and_dedup(self):
        self.assertEqual(parse_bots('@Example_bot, example_BOT\nhttps://t.me/Another_bot'), ['Example_bot', 'Another_bot'])
        self.spec['tracking'] = 'unique'
        self.batch()
        payload = json.loads(self.db.query('SELECT payload FROM batch_items LIMIT 1')[0]['payload'])
        self.assertIn('?start=pa_', payload['promote_url'])
        self.assertLessEqual(len(payload['promote_url'].split('start=')[1]), 64)

    def test_csv_formula_injection(self):
        self.db.save_ad(dict(AD, title='=HYPERLINK("evil")'))
        self.assertIn("'=HYPERLINK", self.client.get('/api/export.csv').text)


if __name__ == '__main__':
    unittest.main()
