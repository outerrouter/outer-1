"""Tavily web search, used sparingly (free plan = a limited number of credits per month)."""
from __future__ import annotations

import hashlib
import json
import logging

from .db import today

log = logging.getLogger("tavily")


class Tavily:
    def __init__(self, cfg, db, http):
        self.cfg, self.db, self.http = cfg, db, http
        self.key = cfg.secrets["tavily_key"]

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def _used(self) -> tuple[int, int]:
        day = self.db.one("SELECT COALESCE(SUM(count),0) AS n FROM ai_usage WHERE provider='tavily' AND day=?", (today(),))["n"]
        month = self.db.one("SELECT COALESCE(SUM(count),0) AS n FROM ai_usage WHERE provider='tavily' AND day LIKE ?",
                            (today()[:7] + "%",))["n"]
        return int(day), int(month)

    async def snippets(self, title: str) -> list[dict]:
        """Review / scam-report snippets for a product title. Cached 24h, hard daily + monthly caps."""
        if not self.enabled or not title:
            return []
        cache_key = "web:" + hashlib.md5(title.lower().encode()).hexdigest()[:14]
        cached = self.db.cache_get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except ValueError:
                pass
        day, month = self._used()
        if day >= int(self.cfg.tavily.daily_cap) or month >= int(self.cfg.tavily.monthly_cap):
            return []
        query = f"{title[:100]} review India genuine or fake"
        try:
            resp = await self.http.post("https://api.tavily.com/search",
                                        json={"query": query, "max_results": 5, "search_depth": "basic"},
                                        headers={"Authorization": f"Bearer {self.key}"}, timeout=25)
            if resp.status_code >= 400:
                log.warning("tavily HTTP %s", resp.status_code)
                return []
            results = resp.json().get("results", [])
        except Exception as exc:
            log.warning("tavily error: %s", type(exc).__name__)
            return []
        self.db.count_usage("search", "tavily")
        out = [{"title": str(r.get("title", ""))[:120], "url": str(r.get("url", ""))[:200],
                "text": str(r.get("content", ""))[:300]} for r in results[:5]]
        self.db.cache_set(cache_key, json.dumps(out, ensure_ascii=False), 24)
        return out
