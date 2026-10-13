"""Watch tasks: 'tell me when X is cheaper than Y'. Matching runs on every new/updated deal."""
from __future__ import annotations

import asyncio
import json
import time

from .events import Bus


class TaskEngine:
    def __init__(self, db, bus: Bus):
        self.db, self.bus = db, bus
        self.pipeline = None     # set by main
        self.notifier = None     # set by main
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------------- CRUD
    def create(self, text: str, keywords=None, target_price=None, platform=None, product_key=None) -> dict:
        kws = [str(k).lower().strip()[:30] for k in (keywords or []) if str(k).strip()][:6]
        cur = self.db.run(
            "INSERT INTO tasks(text,keywords,product_key,target_price,platform,status,created) VALUES(?,?,?,?,?,'waiting',?)",
            (text[:200], json.dumps(kws, ensure_ascii=False), product_key, target_price, platform, time.time()))
        task = self.get(cur.lastrowid)
        self.bus.publish("task", {"id": task["id"], "status": "waiting", "text": task["text"]})
        return task

    def get(self, task_id: int) -> dict | None:
        row = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["keywords"] = json.loads(row["keywords"] or "[]")
        return row

    def list(self, include_closed: bool = True) -> list[dict]:
        sql = "SELECT * FROM tasks" + ("" if include_closed else " WHERE status IN ('waiting','checking')")
        rows = self.db.all(sql + " ORDER BY id DESC LIMIT 100")
        for r in rows:
            r["keywords"] = json.loads(r["keywords"] or "[]")
            r["result"] = json.loads(r["result"]) if r.get("result") else None
        return rows

    def cancel(self, task_id: int) -> bool:
        cur = self.db.run("UPDATE tasks SET status='cancelled' WHERE id=? AND status IN ('waiting','checking')", (task_id,))
        if cur.rowcount:
            self.bus.publish("task", {"id": task_id, "status": "cancelled"})
        return bool(cur.rowcount)

    def _set(self, task_id: int, status: str, deal_key=None, result=None) -> None:
        self.db.run("UPDATE tasks SET status=?, last_checked=?, deal_key=COALESCE(?,deal_key), result=COALESCE(?,result) WHERE id=?",
                    (status, time.time(), deal_key, json.dumps(result, ensure_ascii=False) if result else None, task_id))
        self.bus.publish("task", {"id": task_id, "status": status})

    # ---------------------------------------------------------------- matching
    @staticmethod
    def matches(task: dict, deal: dict) -> bool:
        if deal["status"] not in ("live", "free"):
            return False
        if task["platform"] and task["platform"] != "any" and deal["platform"] != task["platform"]:
            return False
        if task["target_price"] is not None and (deal["price"] is None or deal["price"] > task["target_price"]):
            return False
        if task["product_key"]:
            return deal["key"] == task["product_key"]
        kws = json.loads(task["keywords"]) if isinstance(task["keywords"], str) else task["keywords"]
        title = (deal["title"] or "").lower()
        return bool(kws) and all(k in title for k in kws)

    async def on_deal(self, deal: dict | None) -> None:
        if not deal:
            return
        async with self._lock:
            for row in self.db.all("SELECT * FROM tasks WHERE status='waiting'"):
                if self.matches(row, deal):
                    await self._fulfil(row, deal)

    async def scan_existing(self, task_id: int) -> None:
        """A new task may already be satisfied by a deal we have seen."""
        task = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not task or not self.pipeline:
            return
        kws = json.loads(task["keywords"] or "[]")
        if task["product_key"]:
            candidates = [d for d in [self.pipeline.get_deal(task["product_key"])] if d]
        else:
            candidates = self.pipeline.search(kws, task["platform"], limit=20)
        async with self._lock:
            for deal in candidates:
                fresh = self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,))
                if fresh and fresh["status"] == "waiting" and self.matches(fresh, deal):
                    await self._fulfil(fresh, deal)

    async def _fulfil(self, task: dict, deal: dict) -> None:
        if self.db.one("SELECT 1 AS x FROM alerts WHERE task_id=? AND deal_key=?", (task["id"], deal["key"])):
            return
        self._set(task["id"], "checking")
        verdict = await self.pipeline.verify_deal(deal["key"], kind="alert", request=task["text"])
        ok = bool(verdict) and verdict["verdict"] != "avoid" and verdict.get("matches_request") is not False
        self.db.run("INSERT INTO alerts(deal_key,task_id,ts,ok,note) VALUES(?,?,?,?,?)",
                    (deal["key"], task["id"], time.time(), 1 if ok else 0, (verdict or {}).get("summary", "")[:200]))
        if not ok:
            self._set(task["id"], "waiting", result={"rejected": deal["title"][:80],
                                                      "reasons": (verdict or {}).get("reasons", [])})
            return
        sent = False
        if self.notifier and self.notifier.enabled:
            sent = await self.notifier.send_deal(self.pipeline.get_deal(deal["key"]), verdict, task=task)
        self._set(task["id"], "done", deal_key=deal["key"],
                  result={"verdict": verdict["verdict"], "confidence": verdict["confidence"], "telegram_sent": sent,
                          "price": deal["price"], "title": deal["title"][:80]})
        self.bus.publish("alert", {"task": task["id"], "title": deal["title"][:80], "price": deal["price"], "sent": sent})
