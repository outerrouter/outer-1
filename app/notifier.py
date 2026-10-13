"""Telegram bot: pushes alerts to you and lets you give tasks from Telegram too (two-way)."""
from __future__ import annotations

import asyncio
import html
import json
import logging
from pathlib import Path

from .util import fmt_price

log = logging.getLogger("notifier")

VERDICT_BN = {"buy": "✅ কিনতে পারেন", "wait": "⏳ অপেক্ষা করুন", "avoid": "🚫 এড়িয়ে চলুন"}
PLATFORM_BN = {"amazon": "Amazon", "flipkart": "Flipkart", "meesho": "Meesho"}
HELP = ("কী করতে পারেন:\n/tasks - চলমান task\n/stop 3 - task #3 বন্ধ\n/status - সারাংশ\n"
        "অথবা সরাসরি লিখুন, যেমন: \"boat earbuds ৯০০ টাকার নিচে চাই\"")


class Notifier:
    def __init__(self, cfg, db, http, bus):
        self.cfg, self.db, self.http, self.bus = cfg, db, http, bus
        self.token = cfg.secrets["bot_token"]
        self.pipeline = self.tasks = self.chat = None      # set by main
        self.media_dir = Path(cfg.data_dir) / "media"

    @property
    def enabled(self) -> bool:
        return bool(self.token) and self.chat_id is not None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    @property
    def chat_id(self):
        return self.cfg.secrets["owner_chat_id"] or self.db.kv_get("tg_chat_id")

    # ---------------------------------------------------------------- low level
    async def _call(self, method: str, **payload):
        try:
            resp = await self.http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=35)
            data = resp.json()
        except Exception as exc:                             # never log the exception text: it may contain the token URL
            log.warning("telegram %s error: %s", method, type(exc).__name__)
            return None
        if not data.get("ok"):
            log.warning("telegram %s failed: %s", method, data.get("description"))
            return None
        return data.get("result")

    async def send_text(self, text: str, buttons=None, chat_id=None) -> bool:
        if not self.configured or not (chat_id or self.chat_id):
            return False
        payload = {"chat_id": chat_id or self.chat_id, "text": text[:4000], "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return await self._call("sendMessage", **payload) is not None

    # ---------------------------------------------------------------- deal alerts
    @staticmethod
    def format_deal(deal: dict, verdict: dict | None, task: dict | None) -> str:
        e = html.escape
        lines = [f"🔔 <b>{e(deal['title'])}</b>"]
        price = "🆓 ফ্রি" if deal["status"] == "free" else fmt_price(deal["price"])
        extra = f" (MRP {fmt_price(deal['mrp'])}, {deal['discount']:.0f}% ছাড়)" if deal.get("mrp") and deal.get("discount") else ""
        lines.append(f"💰 {price}{extra}")
        if deal.get("coupon"):
            lines.append(f"🎟 coupon: <code>{e(deal['coupon'])}</code>")
        lines.append(f"🏷 {PLATFORM_BN.get(deal['platform'], deal['platform'])} • স্কোর {deal['score']:.0f}/100 • {deal['sources']}টি channel-এ")
        if verdict:
            lines.append(f"🤖 {VERDICT_BN.get(verdict['verdict'], verdict['verdict'])} ({verdict['confidence']}%)"
                         + (" — শুধু rules দিয়ে, AI যাচাই হয়নি" if verdict.get("source") == "rules" else ""))
            if verdict.get("summary"):
                lines.append(e(verdict["summary"]))
            if verdict.get("risks"):
                lines.append("⚠️ " + e("; ".join(verdict["risks"][:2])))
        if deal.get("link_state") != "verified":
            lines.append("ℹ️ দাম channel-এর দাবি, দোকানের সাইটে একবার দেখে নিন।")
        if task:
            lines.append(f"📌 আপনার task #{task['id']}: {e(task['text'])}")
        return "\n".join(lines)

    async def send_deal(self, deal: dict, verdict: dict | None = None, task: dict | None = None) -> bool:
        if not self.enabled:
            return False
        text = self.format_deal(deal, verdict, task)
        buttons = [[{"text": "🛒 খুলুন", "url": deal["url"]}],
                   [{"text": "🔎 আবার যাচাই", "callback_data": f"chk:{deal['id']}"}]]
        if task:
            buttons[1].append({"text": "⏹ task বন্ধ", "callback_data": f"stop:{task['id']}"})
        image = deal.get("image")
        if image and image.startswith("https://"):
            ok = await self._call("sendPhoto", chat_id=self.chat_id, photo=image, caption=text[:1000], parse_mode="HTML",
                                  reply_markup={"inline_keyboard": buttons})
            if ok is not None:
                return True
        elif image and not image.startswith(("http", "/")):
            path = self.media_dir / Path(image).name
            if path.is_file():
                try:
                    resp = await self.http.post(
                        f"https://api.telegram.org/bot{self.token}/sendPhoto",
                        data={"chat_id": str(self.chat_id), "caption": text[:1000], "parse_mode": "HTML",
                              "reply_markup": json.dumps({"inline_keyboard": buttons})},
                        files={"photo": (path.name, path.read_bytes(), "image/jpeg")}, timeout=60)
                    if resp.json().get("ok"):
                        return True
                except Exception as exc:
                    log.warning("telegram photo upload error: %s", type(exc).__name__)
        return await self.send_text(text, buttons)

    # ---------------------------------------------------------------- incoming (long polling)
    async def poll(self) -> None:
        if not self.configured:
            log.info("Telegram bot token not set: alerts and Telegram chat are off")
            return
        offset = int(self.db.kv_get("tg_offset", 0) or 0)
        while True:
            try:
                resp = await self.http.post(f"https://api.telegram.org/bot{self.token}/getUpdates",
                                            json={"offset": offset, "timeout": 25,
                                                  "allowed_updates": ["message", "callback_query"]}, timeout=40)
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(10)
                    continue
                for upd in data.get("result", []):
                    offset = upd["update_id"] + 1
                    self.db.kv_set("tg_offset", offset)
                    try:
                        await self._handle(upd)
                    except Exception as exc:
                        log.warning("update handling failed: %s", type(exc).__name__)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("getUpdates error: %s", type(exc).__name__)
                await asyncio.sleep(5)

    async def _handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
            if str(chat_id) != str(self.chat_id):
                return
            await self._call("answerCallbackQuery", callback_query_id=cq["id"])
            kind, _, ref = (cq.get("data") or "").partition(":")
            if kind == "stop" and ref.isdigit():
                done = self.tasks.cancel(int(ref))
                await self.send_text("⏹ task বন্ধ করা হয়েছে।" if done else "task আগেই শেষ বা বন্ধ।")
            elif kind == "chk" and ref.isdigit():
                row = self.db.one("SELECT key FROM deals WHERE id=?", (int(ref),))
                verdict = await self.pipeline.verify_deal(row["key"], kind="chat") if row else None
                if verdict:
                    await self.send_text(f"{VERDICT_BN.get(verdict['verdict'])} ({verdict['confidence']}%)\n{html.escape(verdict['summary'])}")
            return

        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        chat_id = (msg.get("chat") or {}).get("id")
        if not text or chat_id is None:
            return
        if self.chat_id is None:                               # not paired yet: only the secret pairing code works
            parts = text.split()
            code = self.cfg.secrets["pair_code"]
            if parts[0] == "/start" and code and len(parts) > 1 and parts[1] == code:
                self.db.kv_set("tg_chat_id", chat_id)
                await self.send_text("✅ জোড়া লেগেছে! এখন থেকে alert এখানে আসবে।\n\n" + HELP, chat_id=chat_id)
            return
        if str(chat_id) != str(self.chat_id):
            return
        if text.startswith("/start") or text.startswith("/help"):
            await self.send_text(HELP)
        elif text.startswith("/tasks"):
            rows = self.tasks.list(include_closed=False)
            await self.send_text("\n".join(f"#{t['id']} {html.escape(t['text'])} — {t['status']}" for t in rows) or "কোনো চলমান task নেই।")
        elif text.startswith("/stop"):
            arg = text.split()[1:] or [""]
            ok = arg[0].isdigit() and self.tasks.cancel(int(arg[0]))
            await self.send_text("⏹ বন্ধ করা হয়েছে।" if ok else "ব্যবহার: /stop 3")
        elif text.startswith("/status"):
            s = self.pipeline.stats()
            await self.send_text(f"চালু deal: {s['live']}, ফ্রি: {s['free']}, আজকের post: {s['posts_today']}, block: {s['blocked_today']}")
        else:
            out = await self.chat.handle(text, source="telegram")
            await self.send_text(html.escape(out["reply"]))
