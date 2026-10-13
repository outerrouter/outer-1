"""Chat engine. One AI call returns the reply plus at most one action; rules keep it useful without AI."""
from __future__ import annotations

import json
import logging
import re
import time

from .ai import prompts
from .ai.router import AIUnavailable
from .util import deal_out, fmt_price, safe_json, to_float, tokenize

log = logging.getLogger("chat")
PLATFORMS = ("amazon", "flipkart", "meesho")
WATCH_RE = re.compile(r"(চাই|alert|track|watch|নজর|জানাও|notify|কমলে|under|below|নিচে|less than|ফ্রি হলে|লাগবে)", re.I)
BN_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")


class ChatEngine:
    def __init__(self, cfg, db, ai, pipeline, tasks):
        self.cfg, self.db, self.ai, self.pipeline, self.tasks = cfg, db, ai, pipeline, tasks

    def _log(self, role: str, text: str) -> None:
        self.db.run("INSERT INTO chat(role,text,ts) VALUES(?,?,?)", (role, text[:2000], time.time()))

    def history(self, limit: int = 30) -> list[dict]:
        rows = self.db.all("SELECT role,text,ts FROM chat ORDER BY id DESC LIMIT ?", (limit,))
        return list(reversed(rows))

    async def handle(self, text: str, source: str = "web") -> dict:
        text = (text or "").strip()[:500]
        if not text:
            return {"reply": "কিছু লিখুন।", "cards": [], "chart": None, "task": None}
        past = [{"role": r["role"], "text": r["text"][:200]} for r in self.history(6)]
        self._log("user", text)
        try:
            out = await self._with_ai(text, past)
        except (AIUnavailable, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.info("chat without AI (%s)", exc)
            out = await self._rules(text)
        self._log("assistant", out["reply"])
        return out

    # ------------------------------------------------------------------ AI path
    async def _with_ai(self, text: str, past: list) -> dict:
        deals = self.db.all("SELECT id,title,platform,price,mrp,discount,status,score FROM deals "
                            "WHERE status IN ('live','free') ORDER BY score DESC, last_seen DESC LIMIT 8")
        tasks = [{"id": t["id"], "text": t["text"], "status": t["status"], "target_price": t["target_price"]}
                 for t in self.tasks.list(include_closed=False)[:5]]
        ctx = {"profile": self.db.kv_get("profile", dict(self.cfg.profile)), "deals": deals, "tasks": tasks, "recent_chat": past}
        user = json.dumps({"context": ctx, "buyer_message": text}, ensure_ascii=False)[:6000]
        data = safe_json(await self.ai.ask("chat", prompts.CHAT, user, json_mode=True, max_tokens=600))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return await self._act(text, str(data.get("reply") or "")[:800], data.get("action"))

    @staticmethod
    def _clean_kws(raw) -> list[str]:
        out = []
        for k in raw if isinstance(raw, list) else []:
            k = re.sub(r"[^\w\u0980-\u09FF\- ]", "", str(k).lower()).strip()[:30]
            if k and k not in out:
                out.append(k)
        return out[:6]

    async def _act(self, text: str, reply: str, action) -> dict:
        out = {"reply": reply or "ঠিক আছে।", "cards": [], "chart": None, "task": None}
        if not isinstance(action, dict):
            return out
        kind = action.get("type")
        rows: list[dict] = []
        if kind == "create_task":
            kws = self._clean_kws(action.get("keywords")) or tokenize(text)[:4]
            if not kws:
                out["reply"] = "কোন পণ্য চান, একটু খুলে লিখুন।"
                return out
            target = to_float(action.get("target_price"))
            platform = action.get("platform") if action.get("platform") in PLATFORMS else "any"
            task = self.tasks.create(text, kws, target, platform)
            await self.tasks.scan_existing(task["id"])
            task = self.tasks.get(task["id"])
            out["task"] = task
            out["reply"] += f"\n\n📌 Task #{task['id']} চালু: {', '.join(kws)}" + (f" — {fmt_price(target)}-এর নিচে" if target else "")
            if task["status"] == "done":
                out["reply"] += "\n✅ মিলে যাওয়া deal আগেই ছিল, Telegram-এ জানানো হয়েছে।"
            rows = self.pipeline.search(kws, platform, 4)
        elif kind == "search":
            kws = self._clean_kws(action.get("keywords")) or tokenize(text)
            rows = self.pipeline.search(kws, None, 6)
            if not rows:
                rows = self.db.all("SELECT * FROM deals WHERE status IN ('live','free') ORDER BY score DESC LIMIT 4")
                out["reply"] = "ঠিক মিল পাইনি, তবে এখন সেরা কয়েকটি এগুলো। চাইলে দাম বলুন, আমি task বানিয়ে নজর রাখব।"
        elif kind == "deep_check":
            try:
                deal_id = int(action.get("deal_id"))
            except (TypeError, ValueError):
                return out
            row = self.db.one("SELECT * FROM deals WHERE id=?", (deal_id,))
            if row:
                verdict = await self.pipeline.verify_deal(row["key"], kind="chat")
                if verdict:
                    out["reply"] += "\n\n🔎 " + verdict["summary"]
                rows = [self.pipeline.get_deal(row["key"])]
        out["cards"] = [deal_out(r) for r in rows if r]
        if len(rows) == 1 and rows[0]:
            hist = self.db.all("SELECT ts, price FROM price_history WHERE deal_key=? ORDER BY ts LIMIT 40", (rows[0]["key"],))
            if len(hist) >= 2:
                out["chart"] = {"title": rows[0]["title"][:60], "points": hist}
        return out

    # ------------------------------------------------------------------ rules path (no AI needed)
    async def _rules(self, text: str) -> dict:
        norm = text.translate(BN_DIGITS)
        nums = [n for n in (to_float(x) for x in re.findall(r"\d[\d,]*", norm)) if n and n >= 50]
        target = max(nums) if nums else None
        kws = tokenize(norm)[:5]
        ascii_kws = [k for k in kws if k.isascii()]
        kws = ascii_kws or kws          # shop titles are English; without AI we cannot translate Bengali product names
        if WATCH_RE.search(text) and kws:
            task = self.tasks.create(text, kws, target, "any")
            await self.tasks.scan_existing(task["id"])
            task = self.tasks.get(task["id"])
            rows = self.pipeline.search(kws, None, 4)
            reply = (f"📌 Task #{task['id']} চালু করলাম: {', '.join(kws)}"
                     + (f" — {fmt_price(target)}-এর নিচে পেলেই জানাব।" if target else " — পেলেই জানাব।")
                     + "\n(AI এখন বন্ধ, তাই সরল নিয়মে বুঝেছি। ভুল হলে task বাতিল করে আবার লিখুন।)")
            return {"reply": reply, "cards": [deal_out(r) for r in rows], "chart": None, "task": task}
        rows = self.pipeline.search(kws, None, 6) if kws else []
        if rows:
            reply = f"{len(rows)}টি deal পেলাম।"
        else:
            rows = self.db.all("SELECT * FROM deals WHERE status IN ('live','free') ORDER BY score DESC LIMIT 4")
            reply = "ঠিক মিল পাইনি, এখনকার সেরা কয়েকটি দেখাচ্ছি।" if rows else "এখনো কোনো deal জমা হয়নি।"
        return {"reply": reply, "cards": [deal_out(r) for r in rows], "chart": None, "task": None}
