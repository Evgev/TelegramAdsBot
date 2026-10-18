import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.ads_api import AdsError
from app.config import Settings, hash_password
from app.controller import decide
from app.discovery import Discovery, TelegramDirectory, csv_rows
from app.main import create_app
from app.targets import parse_targets, validate_template
from test_app import AD, PARAMS, FakeAPI


class TargetAPI(FakeAPI):
    async def call(self, method, params=None, idempotency=None):
        if method in ('getTargetBot', 'getTargetChannel'):
            params=params or {}
            self.calls.append((method, copy.deepcopy(params), idempotency))
            field='bot_id' if method=='getTargetBot' else 'channel_id'
            username=params[field].lstrip('@')
            if username.lower()=='bad_bot':
                raise AdsError('TARGET_NOT_AVAILABLE')
            if username.lower()=='offline_bot':
                raise AdsError('NETWORK_ERROR', True)
            if username.lower()=='alias_bot':
                username='Example_bot'
            return {field:sum((i+1)*ord(c) for i,c in enumerate(username.lower())), 'username':username,'title':username}
        return await super().call(method,params,idempotency)


class FakeDirectory:
    def __init__(self):
        self.calls=[]
        self.fail=False

    def configured(self): return True
    async def close(self): pass
    async def fetch(self,kind,query,mode):
        self.calls.append((kind,query,mode))
        if self.fail: raise ValueError('Telegram временно недоступен')
        return {'items':[{'username':'Example_bot' if kind=='bots' else 'Example_channel', 'telegram_id':12,
                          'title':'Example', 'audience':None if query=='missing' else 65000,
                          'audience_metric':'mau' if kind=='bots' else 'subscribers'}], 'total':3}


class PlacementTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.api=TargetAPI();self.directory=FakeDirectory()
        self.settings=Settings(Path(self.temp.name),'test-token','admin',hash_password('test-password'),
                               's'*48,'http://testserver',secure_cookie=False,run_worker=False)
        self.app=create_app(self.settings,self.api,self.directory);self.db=self.app.state.db;self.engine=self.app.state.engine
        self.client=TestClient(self.app);r=self.client.post('/api/login',json={'username':'admin','password':'test-password'},headers={'origin':'http://testserver'})
        self.headers={'origin':'http://testserver','x-csrf-token':r.json()['csrf']}
        asyncio.run(self.engine.sync())
        self.template=self.engine.save_template({'name':'General','text':'Test text','promote_url':'https://t.me/example_bot'})

    def tearDown(self): self.client.close();self.temp.cleanup()
    def post(self,path,body): return self.client.post('/api'+path,json=body,headers=self.headers)
    def spec(self,kind='bots',targets='Example_bot'):
        return dict(name='Test',project='VPN',kind=kind,targets=targets,template_id=self.template,budget=2,cpm=5)
    def preview(self,spec):
        r=self.post('/batches/preview',spec);self.assertEqual(r.status_code,200,r.text);return r.json()

    def test_all_three_payloads_and_confirmation(self):
        for kind,value,placement,field in [('bots','Example_bot','bot_banner','bot_ids'),('channels','Example_channel','channel_post','channel_ids'),('search','впн для телефона','search_result','search_queries')]:
            p=self.preview(self.spec(kind,value));self.assertEqual(self.db.query('SELECT count(*) n FROM batches')[0]['n'],len(self.db.query('SELECT id FROM batches')))
            self.assertFalse(any(c[0]=='createAd' for c in self.api.calls))
            bid=self.engine.create_batch(p['token']);self.assertEqual(bid,self.engine.create_batch(p['token']))
            rows=self.db.query('SELECT payload FROM batch_items WHERE batch_id=?',(bid,));self.assertEqual(len(rows),1)
            payload=json.loads(rows[0]['payload']);self.assertEqual(payload['placement'],placement);self.assertIn(field,payload['target'])
            self.assertEqual(payload['text'],'' if kind=='search' else 'Test text')
        self.assertEqual(self.client.get('/api/batches').json()['batches'][0]['project'],'VPN')

    def test_search_phrases_keep_spaces_and_deduplicate(self):
        p=self.preview(self.spec('search','  впн   для телефона\nВПН для телефона; быстрый vpn'))
        self.assertEqual(len(p['targets']),2);self.assertEqual(p['targets'][0]['username'],'впн для телефона')
        self.assertEqual(p['targets'][0]['validation'],'local')
        self.assertFalse(any(c[0].startswith('getTarget') for c in self.api.calls))

    def test_search_rejects_tracking_and_query_and_posts(self):
        for url in ['https://t.me/example_bot?start=hello','https://t.me/channel/4','https://t.me/+invite','https://t.me/example_bot#x']:
            self.template=self.engine.save_template({'name':'Bad','text':'Text','promote_url':url})
            self.assertEqual(self.post('/batches/preview',self.spec('search','быстрый vpn')).status_code,400)
        self.template=self.engine.save_template({'name':'OK','text':'Text','promote_url':'https://t.me/example_bot'})
        self.assertEqual(self.post('/batches/preview',dict(self.spec('search','быстрый vpn'),tracking='unique')).status_code,400)

    def test_partial_rejections_aliases_and_balance_use_valid_targets(self):
        p=self.preview(dict(self.spec(targets='Example_bot alias_bot bad_bot'),budget=15))
        self.assertEqual(len(p['targets']),1);self.assertEqual(p['total'],15);self.assertEqual(p['rejected'][0]['error'],'TARGET_NOT_AVAILABLE')
        self.assertEqual(self.post('/batches/preview',self.spec(targets='offline_bot')).status_code,502)
        self.assertEqual(self.db.query('SELECT * FROM batches'),[])

    def test_template_compatibility_and_empty_search_text(self):
        r=self.post('/templates',{'name':'Search','text':'','promote_url':'https://t.me/example_bot','placements':['search']})
        self.assertEqual(r.status_code,200,r.text);self.template=r.json()['id'];self.preview(self.spec('search','быстрый vpn'))
        self.assertEqual(self.post('/batches/preview',self.spec()).status_code,400)
        self.assertEqual(self.post('/templates',{'name':'Bad','text':'','promote_url':'https://t.me/example_bot','placements':['bots']}).status_code,422)

    def test_keyword_and_username_limits(self):
        self.assertEqual(parse_targets('channels','@durov https://t.me/Durov'),['durov'])
        for kind,text in [('search','x'*33),('channels','https://t.me/channel/3'),('bots','@invalid?x')]:
            with self.assertRaises(ValueError):parse_targets(kind,text)
        self.assertEqual(self.post('/batches/preview',dict(self.spec('channels'),bots='Example_bot')).status_code,422)

    def test_discovery_cache_refresh_and_stale_fallback(self):
        body={'kind':'bots','query':'Example_bot','mode':'similar'}
        first=self.post('/discovery',body).json();self.assertEqual(first['items'][0]['audience'],65000)
        self.assertTrue(first['warnings']);self.assertEqual(first['items'][0]['history']['ad_ids'],[7])
        self.post('/discovery',body);self.assertEqual(len(self.directory.calls),1)
        self.post('/discovery',dict(body,refresh=True));self.assertEqual(len(self.directory.calls),2)
        self.directory.fail=True;r=self.post('/discovery',dict(body,refresh=True)).json()
        self.assertEqual(len(r['items']),1);self.assertIn('сохранённый',' '.join(r['warnings']))

    def test_discovery_missing_audience_and_first_party_metrics(self):
        ad=dict(AD,views=1000,clicks=20,spent_budget=2);self.db.save_ad(ad)
        r=self.post('/discovery',{'kind':'bots','query':'missing','mode':'similar'}).json()['items'][0]
        self.assertIsNone(r['audience']);self.assertEqual(r['history']['ctr'],2);self.assertEqual(r['history']['cpc'],.1)
        self.assertEqual(r['availability'],'unchecked')

    def test_csv_mapping_relations_dedup_and_enrichment(self):
        text='\ufeffБот;Текст;Ссылка\nExample_bot;"VPN; пробный период";https://t.me/example_bot\n'
        columns=self.post('/research/columns',{'csv':text});self.assertEqual(columns.status_code,200)
        body={'csv':text,'kind':'bots','relation':'destination','columns':{'target':'Бот','creative_text':'Текст','destination_url':'Ссылка'}}
        self.assertEqual(self.post('/research/import',body).json()['added'],1)
        self.assertEqual(self.post('/research/import',body).json()['duplicates'],1)
        rows=self.post('/discovery',{'kind':'bots','mode':'research'}).json()['items'];self.assertEqual(len(rows),1)
        r=rows[0]['research'][0];self.assertEqual(r['relation'],'destination');self.assertEqual(r['creative_text'],'VPN; пробный период')
        self.assertIsNone(rows[0]['audience'])
        self.assertEqual(self.post('/research/import',dict(body,kind='search')).status_code,400)

    def test_csv_atomic_invalid_links_headers_and_auth(self):
        body={'csv':'target,url\nExample_bot,https://t.me/example_bot\nAnother_bot,javascript:alert(1)',
              'kind':'bots','relation':'placement','columns':{'target':'target','destination_url':'url'}}
        self.assertEqual(self.post('/research/import',body).status_code,400);self.assertEqual(self.db.query('SELECT * FROM research'),[])
        with self.assertRaises(ValueError):csv_rows('target,target\na,b')
        self.assertEqual(self.client.post('/api/research/import',json=body,headers={'origin':'http://testserver'}).status_code,403)
        self.assertEqual(TestClient(self.app).get('/api/discovery/status').status_code,401)

    def test_keywords_suggestions_mark_hypotheses_no_fake_volume(self):
        rows=self.post('/discovery',{'kind':'search','query':'быстрый vpn'}).json()['items']
        self.assertEqual(len(rows),3);self.assertTrue(all(r['audience'] is None for r in rows));self.assertIn('Гипотеза',rows[1]['sources'][0])
        self.assertFalse(self.directory.calls)

    def test_disconnected_directory_still_allows_manual_discovery(self):
        self.app.state.discovery.directory=TelegramDirectory(self.settings)
        r=self.post('/discovery',{'kind':'channels','query':'@durov','mode':'manual'})
        self.assertEqual(r.status_code,200);self.assertEqual(r.json()['items'][0]['username'],'durov')
        self.assertFalse(self.client.get('/api/discovery/status').json()['telegram_configured'])
        r=self.post('/discovery',{'kind':'bots','query':'Example_bot','mode':'similar'})
        self.assertTrue(r.json()['warnings']);self.assertEqual(len(r.json()['items']),1)

    def test_queue_executes_channels_and_search_and_reconciles_uncertain_delivery(self):
        for kind, target in [('channels','Example_channel'),('search','быстрый vpn')]:
            bid=self.engine.create_batch(self.preview(self.spec(kind,target))['token'])
            self.api.fail_create_after_delivery=True
            asyncio.run(self.engine.process_item())
            self.db.execute('UPDATE batch_items SET next_attempt=0 WHERE batch_id=?',(bid,))
            asyncio.run(self.engine.process_item())
            row=self.db.query('SELECT status,ad_id FROM batch_items WHERE batch_id=?',(bid,))[0]
            self.assertEqual(row['status'],'done')
            self.assertEqual(len([ad for ad in self.api.ads if ad['ad_id']==row['ad_id']]),1)
        self.assertEqual(len([c for c in self.api.calls if c[0]=='createAd']),2)
        summary=self.client.get('/api/batches').json()['projects']
        total=next(p for p in summary if p['project']=='VPN' and p['kind']=='all')
        self.assertEqual(total['count'],2)

    def test_discovery_never_mixes_types_or_allocates_shared_history_metrics(self):
        self.db.save_ad(dict(AD,ad_id=10,target={'type':'search','search_queries':['Example_bot']}))
        rows=self.post('/discovery',{'kind':'channels','query':'Example','mode':'similar'}).json()['items']
        self.assertTrue(all(r['kind']=='channels' for r in rows))
        self.directory.fail=True
        rows=self.post('/discovery',{'kind':'channels','query':'Example','mode':'search'}).json()['items']
        self.assertEqual(rows,[])
        self.db.save_ad(dict(AD,target={'type':'bots','bots':[{'username':'Example_bot'},{'username':'Another_bot'}]}))
        row=self.post('/discovery',{'kind':'bots','query':'Example_bot','mode':'manual'}).json()['items'][0]
        self.assertTrue(row['history']['grouped'])

    def test_cache_deduplicates_telegram_ids_across_seed_aliases(self):
        class Aliases(FakeDirectory):
            async def fetch(self,kind,query,mode):
                return {'items':[{'username':query,'telegram_id':12,'title':query,'audience':123}], 'total':1}
        self.app.state.discovery.directory=Aliases()
        rows=self.post('/discovery',{'kind':'bots','query':'First_bot Second_bot','mode':'similar'}).json()['items']
        self.assertEqual(len(rows),1);self.assertEqual(len(rows[0]['sources']),2)

    def test_long_unicode_names_keep_unique_reconciliation_titles(self):
        bid=self.engine.create_batch(self.preview(dict(self.spec('search','быстрый vpn; впн для телефона'),name='я'*60))['token'])
        payloads=[json.loads(r['payload']) for r in self.db.query('SELECT payload FROM batch_items WHERE batch_id=?',(bid,))]
        self.assertEqual(len({p['title'] for p in payloads}),2)
        self.assertTrue(all(len(p['title'].encode())<=128 for p in payloads))

    def test_search_research_mode_accepts_empty_query_and_preserves_phrase(self):
        body={'csv':'target,text\nвпн для телефона,Example', 'kind':'search','relation':'keyword','columns':{'target':'target','creative_text':'text'}}
        self.assertEqual(self.post('/research/import',body).status_code,200)
        result=self.post('/discovery',{'kind':'search','mode':'research'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['items'][0]['username'],'впн для телефона')
        self.assertEqual(len(result.json()['items']),1)

    def test_project_summary_includes_batches_beyond_recent_list(self):
        bid=self.engine.create_batch(self.preview(self.spec())['token'])
        asyncio.run(self.engine.process_item())
        row=self.db.query('SELECT * FROM batches WHERE id=?',(bid,))[0]
        with self.db.connect() as conn:
            for i in range(101):
                conn.execute('INSERT INTO batches(id,name,status,spec,created) VALUES (?,?,?,?,?)',('extra'+str(i),'Extra','done',row['spec'],row['created']+i+1))
        result=self.client.get('/api/batches').json()
        self.assertEqual(len(result['batches']),100)
        self.assertEqual(next(p for p in result['projects'] if p['kind']=='all')['count'],1)

    def test_template_from_empty_text_search_ad(self):
        self.db.save_ad(dict(AD,ad_id=30,placement='search_result',text='',promote_url='https://t.me/example_bot'))
        response=self.post('/templates/from-ad/30',{})
        self.assertEqual(response.status_code,200,response.text)
        row=json.loads(self.db.query('SELECT body FROM templates WHERE id=?',(response.json()['id'],))[0]['body'])
        self.assertEqual(row['placements'],['search']);self.assertEqual(row['text'],'')

    def test_search_zero_delivery_holds_without_spending(self):
        now=200000;ad=dict(AD,placement='search_result')
        policy=dict(PARAMS,enabled=True,started_at=now-14400,deadline=now+36000,baseline_views=0,baseline_spent=0,last_change=now-14400,last_cpm=5)
        samples=[dict(ts=now-3600,views=0,cpm=5,status='active'),dict(ts=now,views=0,cpm=5,status='active')]
        self.assertEqual(decide(ad,policy,samples,now,now).action,'hold')


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from types import SimpleNamespace
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'telegram';Path(str(self.path)+'.session').touch()
        self.settings=SimpleNamespace(telegram_api_id=123,telegram_api_hash='private-app-hash',telegram_session=self.path)
        self.directory=TelegramDirectory(self.settings)

    async def test_real_request_types_filter_and_public_projection(self):
        from telethon.tl import types,functions
        from unittest.mock import AsyncMock
        bot=types.User(id=1,first_name='Example',username='Example_bot',bot=True,bot_active_users=123,phone='private-number')
        user=types.User(id=2,first_name='Human',username='Human_user')
        class Client:
            is_connected=lambda self:True
            is_user_authorized=AsyncMock(return_value=True)
            is_bot=AsyncMock(return_value=False)
            get_entity=AsyncMock(return_value=bot)
            async def __call__(self,request):
                self.request=request
                return types.users.UsersSlice(count=5,users=[bot,user])
        client=Client();self.directory.client=client
        r=await self.directory.fetch('bots','Example_bot','similar')
        self.assertIsInstance(client.request,functions.bots.GetBotRecommendationsRequest)
        self.assertEqual(len(r['items']),1);self.assertEqual(r['items'][0]['audience'],123)
        self.assertNotIn('phone',r['items'][0]);self.assertEqual(r['total'],5)
        await self.directory.fetch('bots','Example','search')
        self.assertIsInstance(client.request,functions.contacts.SearchRequest)
        self.assertTrue(client.request.bots)

    async def test_flood_wait_blocks_repeat_without_sleep(self):
        from telethon import errors
        from unittest.mock import AsyncMock
        class Client:
            is_connected=lambda self:True
            is_user_authorized=AsyncMock(return_value=True)
            is_bot=AsyncMock(return_value=False)
            calls=0
            async def __call__(self,request):
                self.calls+=1
                raise errors.FloodWaitError(request=request,capture=45)
        client=Client();self.directory.client=client
        with self.assertRaisesRegex(ValueError,'45'):
            await self.directory.fetch('bots','Example','search')
        with self.assertRaisesRegex(ValueError,'Повторите'):
            await self.directory.fetch('bots','Example','search')
        self.assertEqual(client.calls,1)
