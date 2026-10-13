"""SQLite storage. The layers: raw_posts -> deals -> price_history / deal_sources -> tasks / alerts."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_posts(              -- layer 1: exactly what was received
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT, channel_id TEXT, msg_id INTEGER, ts REAL, text TEXT,
  state TEXT DEFAULT 'new',                        -- new|skipped|queued_ai|pending_ai|parsed
  UNIQUE(channel_id, msg_id));
CREATE TABLE IF NOT EXISTS deals(                  -- layer 2+3+5: parsed, verified, scored
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  key TEXT UNIQUE, platform TEXT, pid TEXT, url TEXT, title TEXT, image TEXT,
  price REAL, mrp REAL, discount REAL, coupon TEXT,
  status TEXT,                                     -- live|free|stale|dead
  link_state TEXT DEFAULT 'claim',                 -- claim|verified
  flags TEXT DEFAULT '[]', sources INTEGER DEFAULT 1, score REAL DEFAULT 0,
  verdict TEXT, verdict_json TEXT,
  first_seen REAL, last_seen REAL, ended_at REAL, end_reason TEXT,
  notified INTEGER DEFAULT 0, probed INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_deals_status ON deals(status, score);
CREATE TABLE IF NOT EXISTS deal_sources(
  deal_key TEXT, channel TEXT, ts REAL, post_id INTEGER, UNIQUE(deal_key, channel));
CREATE TABLE IF NOT EXISTS price_history(          -- layer 4: tracking
  id INTEGER PRIMARY KEY AUTOINCREMENT, deal_key TEXT, ts REAL, price REAL, source TEXT);
CREATE INDEX IF NOT EXISTS idx_hist ON price_history(deal_key, ts);
CREATE TABLE IF NOT EXISTS channels(
  name TEXT PRIMARY KEY, posts INTEGER DEFAULT 0, good INTEGER DEFAULT 0,
  bad INTEGER DEFAULT 0, trust REAL DEFAULT 0.5);
CREATE TABLE IF NOT EXISTS blocked(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, channel TEXT, reason TEXT, url TEXT);
CREATE TABLE IF NOT EXISTS tasks(                  -- layer 6: what the user asked for
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, keywords TEXT DEFAULT '[]',
  product_key TEXT, target_price REAL, platform TEXT,
  status TEXT DEFAULT 'waiting',                   -- waiting|checking|done|cancelled
  created REAL, last_checked REAL, result TEXT, deal_key TEXT);
CREATE TABLE IF NOT EXISTS alerts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, deal_key TEXT, task_id INTEGER, ts REAL, ok INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS ai_usage(
  day TEXT, kind TEXT, provider TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(day, kind, provider));
CREATE TABLE IF NOT EXISTS ai_cache(key TEXT PRIMARY KEY, value TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, text TEXT, ts REAL);
"""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    def run(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def all(self, sql: str, params=()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def one(self, sql: str, params=()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # --- small key/value store -------------------------------------------------
    def kv_get(self, key: str, default=None):
        row = self.one("SELECT v FROM kv WHERE k=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except ValueError:
            return default

    def kv_set(self, key: str, value) -> None:
        self.run("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (key, json.dumps(value, ensure_ascii=False)))

    # --- cache -----------------------------------------------------------------
    def cache_get(self, key: str):
        row = self.one("SELECT value, expires FROM ai_cache WHERE key=?", (key,))
        if row and row["expires"] > time.time():
            return row["value"]
        return None

    def cache_set(self, key: str, value: str, hours: float) -> None:
        self.run("INSERT INTO ai_cache(key,value,expires) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value, expires=excluded.expires",
                 (key, value, time.time() + hours * 3600))

    def count_usage(self, kind: str, provider: str) -> None:
        self.run("INSERT INTO ai_usage(day,kind,provider,count) VALUES(?,?,?,1) "
                 "ON CONFLICT(day,kind,provider) DO UPDATE SET count=count+1", (today(), kind, provider))
