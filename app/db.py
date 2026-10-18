import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS discovery_cache (key TEXT PRIMARY KEY, body TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS research (id TEXT PRIMARY KEY, kind TEXT NOT NULL, target_key TEXT NOT NULL, body TEXT NOT NULL, updated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_research_target ON research(kind,target_key);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ads (id INTEGER PRIMARY KEY, raw TEXT NOT NULL, updated INTEGER NOT NULL, present INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS templates (id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL, spec TEXT NOT NULL, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS batch_items (
 id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES batches(id), username TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL, ad_id INTEGER, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL,
 UNIQUE(batch_id, username)
);
CREATE TABLE IF NOT EXISTS snapshots (
 ad_id INTEGER NOT NULL, ts INTEGER NOT NULL, views INTEGER NOT NULL, clicks INTEGER, actions INTEGER,
 spent REAL NOT NULL, cpm REAL NOT NULL, status TEXT NOT NULL, PRIMARY KEY(ad_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots(ts);
CREATE TABLE IF NOT EXISTS daily_stats (ad_id INTEGER NOT NULL, ts INTEGER NOT NULL, raw TEXT NOT NULL, PRIMARY KEY(ad_id, ts));
CREATE TABLE IF NOT EXISTS policies (ad_id INTEGER PRIMARY KEY, body TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, ad_id INTEGER, details TEXT NOT NULL DEFAULT '{}');
'''


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql, args=()):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as conn:
            cursor = conn.execute(sql, args)
            return cursor.lastrowid

    def get(self, key, default=None):
        rows = self.query('SELECT value FROM meta WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default

    def put(self, key, value):
        self.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False)))

    def event(self, kind, message, ad_id=None, details=None):
        self.execute('INSERT INTO events(ts,kind,message,ad_id,details) VALUES (?,?,?,?,?)',
                     (int(time.time()), kind, message, ad_id, json.dumps(details or {}, ensure_ascii=False)))

    def save_ad(self, ad, now=None):
        now = now or int(time.time())
        with self.connect() as conn:
            conn.execute('INSERT INTO ads(id,raw,updated,present) VALUES (?,?,?,1) ON CONFLICT(id) DO UPDATE SET raw=excluded.raw,updated=excluded.updated,present=1',
                         (ad['ad_id'], json.dumps(ad, ensure_ascii=False), now))
            conn.execute('INSERT OR REPLACE INTO snapshots(ad_id,ts,views,clicks,actions,spent,cpm,status) VALUES (?,?,?,?,?,?,?,?)',
                         (ad['ad_id'], now // 300 * 300, ad.get('views', 0), ad.get('clicks'), ad.get('actions', ad.get('joins')),
                          ad.get('spent_budget', 0), ad.get('cpm', 0), ad.get('status', 'unknown')))

    def ads(self):
        return [dict(json.loads(row['raw']), synced_at=row['updated']) for row in self.query('SELECT raw,updated FROM ads WHERE present=1 ORDER BY id DESC')]

    def ad(self, ad_id):
        rows = self.query('SELECT raw FROM ads WHERE id=? AND present=1', (ad_id,))
        return json.loads(rows[0]['raw']) if rows else None

    def policies(self):
        return {row['ad_id']: dict(json.loads(row['body']), enabled=bool(row['enabled'])) for row in self.query('SELECT * FROM policies')}

    def save_policy(self, ad_id, body):
        self.execute('INSERT INTO policies(ad_id,body,enabled,updated) VALUES (?,?,?,?) ON CONFLICT(ad_id) DO UPDATE SET body=excluded.body,enabled=excluded.enabled,updated=excluded.updated',
                     (ad_id, json.dumps(body), int(body.get('enabled', False)), int(time.time())))
