"""The pipeline: raw post -> parse -> link check -> deal -> score -> verify -> tasks / alerts.

Order matters: every cheap, deterministic step runs first; the AI only sees what rules cannot read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from .ai import prompts
from .ai.router import PRIORITY, AIUnavailable
from .db import IST
from .links import DomainThrottle, check_link, host_of, is_shortener, looks_like_brand, platform_of, probe
from .parser import parse_post
from .scoring import SEVERE, deal_score, rule_verdict
from .util import safe_json, to_float

log = logging.getLogger("pipeline")


@dataclass
class Item:
    url: str
    title: str = ""
    price: float | None = None
    mrp: float | None = None
    discount: float | None = None
    coupon: str | None = None
    is_free: bool = False


class Pipeline:
    def __init__(self, cfg, db, bus, ai, http, tavily=None, sources=None):
        self.cfg, self.db, self.bus, self.ai, self.http = cfg, db, bus, ai, http
        self.tavily = tavily
        self.sources = sources or {}
        self.shorteners = set(cfg.shorteners) | set(cfg.get("extra_shorteners") or [])
        self.throttle = DomainThrottle(2.0)
        self.link_cache: dict = {}
        self.parse_q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self.tasks = None          # TaskEngine (set by main)
        self.notifier = None       # Notifier (set by main)
        self._bg: set = set()

    # ------------------------------------------------------------------ helpers
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    def get_deal(self, key: str) -> dict | None:
        return self.db.one("SELECT * FROM deals WHERE key=?", (key,))

    def _set_state(self, raw_id: int, state: str) -> None:
        self.db.run("UPDATE raw_posts SET state=? WHERE id=?", (state, raw_id))

    def _count_post(self, channel: str) -> None:
        self.db.run("INSERT INTO channels(name,posts) VALUES(?,1) ON CONFLICT(name) DO UPDATE SET posts=posts+1", (channel,))

    def _trust(self, channel: str, good: int = 0, bad: int = 0) -> None:
        self.db.run("INSERT OR IGNORE INTO channels(name) VALUES(?)", (channel,))
        self.db.run("UPDATE channels SET good=good+?, bad=bad+? WHERE name=?", (good, bad, channel))
        self.db.run("UPDATE channels SET trust=(good+1.0)/(good+bad+2.0) WHERE name=?", (channel,))

    def _deal_urls(self, urls) -> list[str]:
        out = []
        for url in urls:
            host = host_of(url)
            if platform_of(host) or is_shortener(host, self.shorteners) or looks_like_brand(host):
                out.append(url)
        return out

    @staticmethod
    def _discount(price, mrp, explicit):
        if price is not None and mrp and mrp > price:
            return round((mrp - price) / mrp * 100, 1)
        return explicit

    # ------------------------------------------------------------------ ingest (layer 1 -> 2)
    async def ingest(self, post: dict, edited: bool = False) -> None:
        pc = self.cfg.pipeline
        text = post.get("text") or ""
        extra = [u for u in (post.get("extra_urls") or []) if u not in text]
        if extra:
            text += "\n" + "\n".join(extra)
        text = text[: int(pc.max_text_chars) * 3]
        channel = post.get("channel") or "unknown"
        channel_id = str(post.get("channel_id") or channel)
        msg_id = int(post.get("msg_id") or 0) or int(time.time() * 1000)

        cur = self.db.run("INSERT OR IGNORE INTO raw_posts(channel,channel_id,msg_id,ts,text,state) VALUES(?,?,?,?,?,'new')",
                          (channel, channel_id, msg_id, post.get("ts") or time.time(), text))
        if cur.rowcount == 0:
            if not edited:
                return
            self.db.run("UPDATE raw_posts SET text=? WHERE channel_id=? AND msg_id=?", (text, channel_id, msg_id))
        raw_id = self.db.one("SELECT id FROM raw_posts WHERE channel_id=? AND msg_id=?", (channel_id, msg_id))["id"]
        self._count_post(channel)
        parsed = parse_post(text)
        self.bus.publish("post", {"channel": channel, "text": text[:140]})

        if parsed.ended:
            await self._handle_end(post, parsed, channel_id, channel)
            self._set_state(raw_id, "parsed")
            return
        deal_urls = self._deal_urls(parsed.urls)
        if not deal_urls:
            self._set_state(raw_id, "skipped")
            return
        entry = {"raw_id": raw_id, "post": post, "parsed": parsed, "deal_urls": deal_urls,
                 "channel": channel, "text": text}
        if len(deal_urls) == 1 and parsed.title and parsed.price is not None:
            self._set_state(raw_id, "parsed")
            await self._process_item(entry, Item(deal_urls[0], parsed.title, parsed.price, parsed.mrp,
                                                 parsed.discount, parsed.coupon, parsed.is_free))
        else:
            await self._enqueue_ai(entry)

    async def _enqueue_ai(self, entry: dict) -> None:
        if not self.ai.enabled:
            self._set_state(entry["raw_id"], "pending_ai")
            await self._partial(entry)
            return
        try:
            self.parse_q.put_nowait(entry)
            self._set_state(entry["raw_id"], "queued_ai")
        except asyncio.QueueFull:
            self._set_state(entry["raw_id"], "pending_ai")
            await self._partial(entry)

    async def _partial(self, entry: dict) -> None:
        """No AI answer yet: keep what rules found (maybe without price). The AI fills it in later."""
        p = entry["parsed"]
        for url in entry["deal_urls"][:3]:
            await self._process_item(entry, Item(url, p.title, p.price, p.mrp, p.discount, p.coupon, p.is_free))

    # ------------------------------------------------------------------ batched AI parse
    async def batcher(self) -> None:
        size, wait = int(self.cfg.pipeline.batch_size), float(self.cfg.pipeline.batch_wait_sec)
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self.parse_q.get()]
            deadline = loop.time() + wait
            while len(batch) < size:
                left = deadline - loop.time()
                if left <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.parse_q.get(), left))
                except asyncio.TimeoutError:
                    break
            try:
                await self._ai_parse(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("batch parse failed")

    async def _ai_parse(self, batch: list[dict]) -> None:
        limit = int(self.cfg.pipeline.max_text_chars)
        payload = [{"id": e["raw_id"], "text": e["text"][:limit]} for e in batch]
        results = None
        try:
            reply = await self.ai.ask("parse", prompts.BATCH_PARSE, json.dumps(payload, ensure_ascii=False),
                                      json_mode=True, max_tokens=1500)
            data = safe_json(reply)
            results = {int(p["id"]): (p.get("items") or []) for p in data.get("posts", [])
                       if isinstance(p, dict) and "id" in p}
        except (AIUnavailable, ValueError, KeyError, TypeError) as exc:
            log.info("AI parse unavailable (%s) - using rules only for now", exc)
        for e in batch:
            items = results.get(e["raw_id"]) if results is not None else None
            if items is None:
                self._set_state(e["raw_id"], "pending_ai")
                await self._partial(e)
                continue
            self._set_state(e["raw_id"], "parsed")
            for raw in items[:5]:
                item = self._validate_item(raw, e)
                if item:
                    await self._process_item(e, item)

    def _validate_item(self, raw, entry) -> Item | None:
        """The AI may only choose among links that really are in the post; numbers are sanity-checked."""
        if not isinstance(raw, dict):
            return None
        urls, url = entry["deal_urls"], raw.get("url")
        if isinstance(url, str) and url.rstrip(".,;)") in urls:
            chosen = url.rstrip(".,;)")
        elif len(urls) == 1:
            chosen = urls[0]
        else:
            return None
        title = re.sub(r"\s+", " ", str(raw.get("title") or entry["parsed"].title or "")).strip()[:140]
        if not title:
            return None
        price, mrp = to_float(raw.get("price")), to_float(raw.get("mrp"))
        if price is not None and price > 10_000_000:
            price = None
        if mrp is not None and (mrp > 10_000_000 or (price is not None and mrp < price)):
            mrp = None
        is_free = bool(raw.get("is_free"))
        if is_free:
            price = 0.0
        coupon = re.sub(r"[^A-Za-z0-9]", "", str(raw.get("coupon") or ""))[:20] or None
        return Item(chosen, title, price, mrp, self._discount(price, mrp, None), coupon, is_free)

    async def retry_pending(self) -> None:
        """Posts that could not be AI-parsed earlier (budget / outage) are retried when AI is back."""
        if not self.ai.enabled:
            return
        rows = self.db.all("SELECT * FROM raw_posts WHERE state='pending_ai' AND ts>? ORDER BY ts LIMIT ?",
                           (time.time() - 86400, int(self.cfg.pipeline.batch_size) * 2))
        for r in rows:
            parsed = parse_post(r["text"])
            urls = self._deal_urls(parsed.urls)
            if not urls:
                self._set_state(r["id"], "skipped")
                continue
            entry = {"raw_id": r["id"], "post": {}, "parsed": parsed, "deal_urls": urls,
                     "channel": r["channel"], "text": r["text"]}
            try:
                self.parse_q.put_nowait(entry)
                self._set_state(r["id"], "queued_ai")
            except asyncio.QueueFull:
                break

    # ------------------------------------------------------------------ end notices (layer 4: "offer removed")
    async def _handle_end(self, post: dict, parsed, channel_id: str, channel: str) -> None:
        keys = []
        for url in self._deal_urls(parsed.urls):
            info = await self._link(url)
            if info.state == "ok":
                keys.append(info.key)
        reply_to = post.get("reply_to")
        if not keys and reply_to:
            row = self.db.one("SELECT id FROM raw_posts WHERE channel_id=? AND msg_id=?", (channel_id, int(reply_to)))
            if row:
                keys = [r["deal_key"] for r in self.db.all("SELECT deal_key FROM deal_sources WHERE post_id=?", (row["id"],))]
        for key in set(keys):
            self.mark_dead(key, "ended_notice")

    def mark_dead(self, key: str, reason: str) -> None:
        deal = self.get_deal(key)
        if not deal or deal["status"] == "dead":
            return
        self.db.run("UPDATE deals SET status='dead', ended_at=?, end_reason=? WHERE key=?", (time.time(), reason, key))
        if reason == "link_dead":
            first = self.db.one("SELECT channel FROM deal_sources WHERE deal_key=? ORDER BY ts LIMIT 1", (key,))
            if first:
                self._trust(first["channel"], bad=1)
        self.bus.publish("deal_status", {"id": deal["id"], "title": deal["title"][:80], "status": "dead", "reason": reason})

    # ------------------------------------------------------------------ link check + upsert (layer 2 -> 3 -> 5)
    async def _link(self, url: str):
        now = time.time()
        hit = self.link_cache.get(url)
        if hit and now - hit[0] < 21600:
            return hit[1]
        info = await check_link(url, self.http, self.shorteners)
        if info.state == "ok" or info.reason in ("lookalike", "unknown_domain", "userinfo_trick", "bad_scheme"):
            if len(self.link_cache) > 5000:
                self.link_cache.clear()
            self.link_cache[url] = (now, info)
        return info

    def _blocked(self, channel: str, info) -> None:
        self.db.run("INSERT INTO blocked(ts,channel,reason,url) VALUES(?,?,?,?)",
                    (time.time(), channel, info.reason, info.original[:200]))
        if info.reason in ("lookalike", "userinfo_trick", "bad_scheme"):
            self._trust(channel, bad=1)
        self.bus.publish("blocked", {"channel": channel, "reason": info.reason})

    async def _process_item(self, entry: dict, item: Item):
        info = await self._link(item.url)
        if info.state != "ok":
            self._blocked(entry["channel"], info)
            return None
        deal = self._upsert(entry, item, info, list(entry["parsed"].flags))
        await self._after_upsert(deal, entry)
        return deal

    def _upsert(self, entry: dict, item: Item, info, flags: list) -> dict:
        now, channel, key = time.time(), entry["channel"], info.key
        row = self.get_deal(key)
        status = "free" if (item.is_free or item.price == 0) else "live"
        if row is None:
            discount = self._discount(item.price, item.mrp, item.discount)
            self.db.run(
                "INSERT INTO deals(key,platform,pid,url,title,price,mrp,discount,coupon,status,flags,first_seen,last_seen) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, info.platform, info.pid, info.canonical, item.title or f"{info.platform} product", item.price,
                 item.mrp, discount, item.coupon, status, json.dumps(sorted(set(flags))), now, now))
        else:
            price = item.price if item.price is not None else row["price"]
            mrp = max([v for v in (row["mrp"], item.mrp) if v] or [0]) or None
            discount = self._discount(price, mrp, item.discount or row["discount"])
            merged = sorted(set(flags) | set(json.loads(row["flags"] or "[]")))
            ended_at, end_reason, new_status = row["ended_at"], row["end_reason"], status
            if row["status"] == "dead":
                if row["ended_at"] and now - row["ended_at"] > 7200:     # a fresh post 2h+ after it ended: it is back
                    ended_at, end_reason = None, None
                else:
                    new_status = "dead"
            elif row["status"] == "stale":
                ended_at, end_reason = None, None
            self.db.run("UPDATE deals SET title=?, price=?, mrp=?, discount=?, coupon=COALESCE(?,coupon), status=?, "
                        "flags=?, last_seen=?, ended_at=?, end_reason=? WHERE key=?",
                        (row["title"] or item.title, price, mrp, discount, item.coupon, new_status,
                         json.dumps(merged), now, ended_at, end_reason, key))

        cur = self.db.run("INSERT OR IGNORE INTO deal_sources(deal_key,channel,ts,post_id) VALUES(?,?,?,?)",
                          (key, channel, now, entry["raw_id"]))
        if cur.rowcount:
            n = self.db.one("SELECT COUNT(*) AS n FROM deal_sources WHERE deal_key=?", (key,))["n"]
            self.db.run("UPDATE deals SET sources=? WHERE key=?", (n, key))
            if n == 3:                                        # corroborated: the first channel was right
                first = self.db.one("SELECT channel FROM deal_sources WHERE deal_key=? ORDER BY ts LIMIT 1", (key,))
                if first:
                    self._trust(first["channel"], good=1)

        deal = self.get_deal(key)
        if deal["price"] is not None:
            last = self.db.one("SELECT price FROM price_history WHERE deal_key=? ORDER BY ts DESC LIMIT 1", (key,))
            if last is None or last["price"] != deal["price"]:
                self.db.run("INSERT INTO price_history(deal_key,ts,price,source) VALUES(?,?,?,?)",
                            (key, now, deal["price"], f"claim:{channel}"))
        self.rescore(key)
        return self.get_deal(key)

    def rescore(self, key: str) -> None:
        d = self.get_deal(key)
        if not d:
            return
        trust = (self.db.one("SELECT AVG(c.trust) AS t FROM deal_sources s JOIN channels c ON c.name=s.channel "
                             "WHERE s.deal_key=?", (key,)) or {}).get("t") or 0.5
        h = self.db.one("SELECT MIN(price) AS m, COUNT(*) AS n FROM price_history WHERE deal_key=?", (key,))
        is_low = bool(h and h["n"] >= 2 and d["price"] is not None and d["price"] <= h["m"])
        score = deal_score(price=d["price"], discount=d["discount"], sources=d["sources"], trust=trust,
                           flags=json.loads(d["flags"] or "[]"), verified=d["link_state"] == "verified", is_low=is_low)
        self.db.run("UPDATE deals SET score=? WHERE key=?", (score, key))

    async def _after_upsert(self, deal: dict, entry: dict) -> None:
        self.bus.publish("deal", {"id": deal["id"], "title": deal["title"][:80], "price": deal["price"],
                                  "platform": deal["platform"], "channel": entry["channel"], "status": deal["status"]})
        post = entry["post"] or {}
        if not deal.get("image") and post.get("download_image"):
            self._spawn(self._fetch_image(deal["key"], post["download_image"]))
        if self.cfg.pipeline.probe_links and not deal["probed"] and not post.get("no_probe"):
            self.db.run("UPDATE deals SET probed=1 WHERE key=?", (deal["key"],))
            self._spawn(self._probe_deal(deal["key"], deal["url"]))
        fresh = self.get_deal(deal["key"])
        if self.tasks:
            await self.tasks.on_deal(fresh)
        await self._maybe_free_alert(self.get_deal(deal["key"]))

    async def _fetch_image(self, key: str, download) -> None:
        try:
            name = await download()
        except Exception:
            return
        if name:
            self.db.run("UPDATE deals SET image=? WHERE key=? AND (image IS NULL OR image='')", (name, key))

    async def _probe_deal(self, key: str, url: str) -> None:
        await self.throttle.wait(host_of(url))
        if await probe(url, self.http) == "dead":
            self.mark_dead(key, "link_dead")

    # ------------------------------------------------------------------ alerts for free deals
    def alerts_last_hour(self) -> int:
        return self.db.one("SELECT COUNT(*) AS n FROM alerts WHERE ok=1 AND ts>?", (time.time() - 3600,))["n"]

    async def _maybe_free_alert(self, deal: dict) -> None:
        pc = self.cfg.pipeline
        if not (pc.free_alerts and self.notifier and self.notifier.enabled):
            return
        if deal["notified"] or deal["status"] != "free" or deal["score"] < float(pc.alert_min_score):
            return
        if self.alerts_last_hour() >= int(pc.alerts_per_hour):
            return
        self.db.run("UPDATE deals SET notified=1 WHERE key=?", (deal["key"],))
        verdict = await self.verify_deal(deal["key"], kind="alert")
        if not verdict or verdict["verdict"] == "avoid":
            return
        sent = await self.notifier.send_deal(self.get_deal(deal["key"]), verdict)
        self.db.run("INSERT INTO alerts(deal_key,task_id,ts,ok,note) VALUES(?,?,?,?,?)",
                    (deal["key"], None, time.time(), 1 if sent else 0, "free-deal"))

    # ------------------------------------------------------------------ deep verification (layer 3 + 5)
    @staticmethod
    def _clean_verdict(data: dict) -> dict:
        verdict = str(data.get("verdict", "wait")).lower()
        verdict = verdict if verdict in ("buy", "wait", "avoid") else "wait"
        scam = str(data.get("scam_likelihood", "medium")).lower()
        scam = scam if scam in ("low", "medium", "high") else "medium"
        if scam == "high" and verdict == "buy":
            verdict = "wait"
        confidence = int(max(0, min(100, to_float(data.get("confidence")) or 0)))
        matches = data.get("matches_request")
        return {
            "verdict": verdict, "confidence": confidence, "scam_likelihood": scam,
            "matches_request": matches if isinstance(matches, bool) else None,
            "summary": str(data.get("summary", ""))[:300], "fit": str(data.get("fit", ""))[:200],
            "reasons": [str(x)[:140] for x in (data.get("reasons") or [])[:5]],
            "risks": [str(x)[:140] for x in (data.get("risks") or [])[:5]], "source": "ai",
        }

    async def verify_deal(self, key: str, kind: str = "deep", request: str | None = None) -> dict | None:
        deal = self.get_deal(key)
        if not deal:
            return None
        flags = json.loads(deal["flags"] or "[]")
        hist = self.db.all("SELECT ts, price, source FROM price_history WHERE deal_key=? ORDER BY ts DESC LIMIT 10", (key,))
        chans = self.db.all("SELECT s.channel AS name, c.trust AS trust FROM deal_sources s "
                            "LEFT JOIN channels c ON c.name=s.channel WHERE s.deal_key=? LIMIT 6", (key,))
        web = []
        if self.tavily and self.tavily.enabled and deal["score"] >= 50 and kind in ("deep", "alert", "chat"):
            web = await self.tavily.snippets(deal["title"])
        profile = self.db.kv_get("profile", dict(self.cfg.profile))
        evidence = {
            "buyer_request": request,
            "deal": {"title": deal["title"], "platform": deal["platform"], "price": deal["price"], "mrp": deal["mrp"],
                     "discount_percent": deal["discount"], "coupon": deal["coupon"], "status": deal["status"],
                     "channels_posted": deal["sources"], "score": deal["score"], "link_state": deal["link_state"]},
            "rule_flags": flags,
            "price_history": [{"time": datetime.fromtimestamp(h["ts"], IST).strftime("%d %b %H:%M"),
                               "price": h["price"], "source": h["source"].split(":")[0]} for h in hist],
            "channels": [{"name": c["name"], "trust": round(c["trust"] or 0.5, 2)} for c in chans],
            "web_snippets": web, "buyer_profile": profile,
        }
        user_msg = json.dumps(evidence, ensure_ascii=False)[:7000]
        cache_key = "deep:%s:%s:%s" % (key, deal["price"], hashlib.md5((request or "").encode()).hexdigest()[:6])
        try:
            reply = await self.ai.ask(kind if kind in PRIORITY else "deep", prompts.DEEP_CHECK, user_msg,
                                      json_mode=True, max_tokens=700, cache_key=cache_key)
            verdict = self._clean_verdict(safe_json(reply))
        except (AIUnavailable, ValueError, TypeError, KeyError, AttributeError):
            verdict = rule_verdict(deal, flags)
        if set(flags) & SEVERE and verdict["verdict"] == "buy":      # rules always beat the model on hard scam signals
            verdict["verdict"] = "avoid"
            verdict["reasons"].append("rule override: scam signal")
        self.db.run("UPDATE deals SET verdict=?, verdict_json=? WHERE key=?",
                    (verdict["verdict"], json.dumps(verdict, ensure_ascii=False), key))
        self.bus.publish("verdict", {"id": deal["id"], "title": deal["title"][:80], "verdict": verdict["verdict"],
                                     "confidence": verdict["confidence"], "source": verdict["source"]})
        return verdict

    # ------------------------------------------------------------------ price sources (optional APIs)
    async def refresh_price(self, key: str) -> bool:
        deal = self.get_deal(key)
        source = self.sources.get(deal["platform"]) if deal else None
        if not source:
            return False
        try:
            data = await source.get(deal)
        except Exception as exc:
            log.warning("price source %s failed: %s", deal["platform"], type(exc).__name__)
            return False
        if not data:
            return False
        now = time.time()
        if data.get("available") is False:
            self.mark_dead(key, "api_unavailable")
            return True
        price = data.get("price")
        if price is not None:
            self.db.run("UPDATE deals SET price=?, mrp=COALESCE(?,mrp), link_state='verified', last_seen=? WHERE key=?",
                        (price, data.get("mrp"), now, key))
            self.db.run("INSERT INTO price_history(deal_key,ts,price,source) VALUES(?,?,?,?)", (key, now, price, "api"))
            d = self.get_deal(key)
            self.db.run("UPDATE deals SET discount=? WHERE key=?", (self._discount(d["price"], d["mrp"], d["discount"]), key))
            if data.get("image") and not d["image"]:
                self.db.run("UPDATE deals SET image=? WHERE key=?", (data["image"], key))
            self.rescore(key)
            if self.tasks:
                await self.tasks.on_deal(self.get_deal(key))
        return True

    # ------------------------------------------------------------------ maintenance
    def age_deals(self) -> int:
        cutoff = time.time() - float(self.cfg.pipeline.stale_hours) * 3600
        rows = self.db.all("SELECT id,key,title FROM deals WHERE status IN ('live','free') AND last_seen<?", (cutoff,))
        for r in rows:
            self.db.run("UPDATE deals SET status='stale', ended_at=COALESCE(ended_at,?), end_reason=COALESCE(end_reason,'not_reposted') WHERE id=?",
                        (time.time(), r["id"]))
            self.bus.publish("deal_status", {"id": r["id"], "title": r["title"][:80], "status": "stale", "reason": "not_reposted"})
        return len(rows)

    def cleanup(self) -> None:
        now = time.time()
        self.db.run("DELETE FROM raw_posts WHERE ts<?", (now - float(self.cfg.pipeline.retention_days) * 86400,))
        self.db.run("DELETE FROM ai_cache WHERE expires<?", (now,))
        self.db.run("DELETE FROM blocked WHERE ts<?", (now - 14 * 86400,))
        self.db.run("DELETE FROM chat WHERE ts<?", (now - 30 * 86400,))

    # ------------------------------------------------------------------ queries used by API / chat
    def search(self, keywords: list[str], platform: str | None = None, limit: int = 8, statuses=("live", "free")) -> list[dict]:
        sql = "SELECT * FROM deals WHERE status IN (%s)" % ",".join("?" * len(statuses))
        params: list = list(statuses)
        for kw in keywords[:6]:
            sql += " AND LOWER(title) LIKE ? ESCAPE '\\'"
            params.append("%" + kw.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if platform and platform != "any":
            sql += " AND platform=?"
            params.append(platform)
        sql += " ORDER BY score DESC, last_seen DESC LIMIT ?"
        params.append(limit)
        return self.db.all(sql, params)

    def stats(self) -> dict:
        n = lambda sql, p=(): self.db.one(sql, p)["n"]
        midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {
            "live": n("SELECT COUNT(*) AS n FROM deals WHERE status='live'"),
            "free": n("SELECT COUNT(*) AS n FROM deals WHERE status='free'"),
            "ended": n("SELECT COUNT(*) AS n FROM deals WHERE status IN ('stale','dead')"),
            "posts_today": n("SELECT COUNT(*) AS n FROM raw_posts WHERE ts>?", (midnight,)),
            "blocked_today": n("SELECT COUNT(*) AS n FROM blocked WHERE ts>?", (midnight,)),
            "channels": n("SELECT COUNT(*) AS n FROM channels"),
            "pending_ai": n("SELECT COUNT(*) AS n FROM raw_posts WHERE state IN ('pending_ai','queued_ai')"),
        }
