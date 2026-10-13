import asyncio
import copy
import json
import unittest

from app.ai.router import AIUnavailable
from app.config import DEFAULTS, Box
from app.db import Database
from app.events import Bus
from app.pipeline import Pipeline
from app.tasks import TaskEngine
from tests.test_links import FakeClient


class FakeAI:
    def __init__(self, replies=None, down=False):
        self.replies, self.down, self.calls = list(replies or []), down, []
        self.enabled = True

    async def ask(self, kind, system, user, **kw):
        self.calls.append((kind, user))
        if self.down or not self.replies:
            raise AIUnavailable("down")
        reply = self.replies.pop(0)
        return reply(user) if callable(reply) else reply


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    async def send_deal(self, deal, verdict=None, task=None):
        self.sent.append((deal["title"], verdict["verdict"], task["id"] if task else None))
        return True


VERDICT_BUY = json.dumps({"verdict": "buy", "confidence": 82, "scam_likelihood": "low", "matches_request": True,
                          "summary": "ঠিক আছে", "reasons": ["ok"], "risks": [], "fit": ""})
VERDICT_AVOID = json.dumps({"verdict": "avoid", "confidence": 90, "scam_likelihood": "high", "matches_request": True,
                            "summary": "সন্দেহ", "reasons": ["bad"], "risks": ["scam"], "fit": ""})


def build(ai=None, client=None):
    cfg = Box(copy.deepcopy(DEFAULTS))
    cfg["pipeline"]["probe_links"] = False
    db, bus = Database(":memory:"), Bus()
    pl = Pipeline(cfg, db, bus, ai or FakeAI(down=True), client or FakeClient({}))
    pl.tasks = TaskEngine(db, bus)
    pl.tasks.pipeline = pl
    pl.notifier = FakeNotifier()
    pl.tasks.notifier = pl.notifier
    return pl


def post(text, channel="Chan A", msg_id=1, **kw):
    return {"channel": channel, "channel_id": channel, "msg_id": msg_id, "text": text, **kw}


EARBUDS = "🔥 Boat Airdopes 141 Earbuds @ ₹999 (MRP ₹4490)\nhttps://www.amazon.in/dp/B0AAAAAAA1?tag=zzz"


class PipelineTests(unittest.TestCase):
    def test_rule_parsed_post_creates_scored_deal_without_ai(self):
        async def go():
            ai = FakeAI(down=True)
            pl = build(ai)
            await pl.ingest(post(EARBUDS))
            return pl, ai
        pl, ai = asyncio.run(go())
        deal = pl.get_deal("amazon:B0AAAAAAA1")
        self.assertEqual((deal["price"], deal["mrp"], deal["status"]), (999.0, 4490.0, "live"))
        self.assertGreater(deal["score"], 0)
        self.assertNotIn("tag=", deal["url"])
        self.assertEqual(ai.calls, [])                       # zero AI requests for a normal post
        self.assertEqual(len(pl.db.all("SELECT * FROM price_history")), 1)

    def test_same_product_in_two_channels_is_one_deal(self):
        async def go():
            pl = build()
            await pl.ingest(post(EARBUDS, "Chan A", 1))
            first = pl.get_deal("amazon:B0AAAAAAA1")["score"]
            await pl.ingest(post(EARBUDS.replace("Boat Airdopes 141", "BOAT Airdopes"), "Chan B", 7))
            return pl, first
        pl, first = asyncio.run(go())
        self.assertEqual(len(pl.db.all("SELECT * FROM deals")), 1)
        deal = pl.get_deal("amazon:B0AAAAAAA1")
        self.assertEqual(deal["sources"], 2)
        self.assertGreater(deal["score"], first)
        self.assertEqual(len(pl.db.all("SELECT * FROM price_history")), 1)   # unchanged price is not logged twice

    def test_duplicate_message_ignored(self):
        async def go():
            pl = build()
            await pl.ingest(post(EARBUDS, msg_id=5))
            await pl.ingest(post(EARBUDS, msg_id=5))
            return pl
        pl = asyncio.run(go())
        self.assertEqual(pl.db.one("SELECT COUNT(*) AS n FROM raw_posts")["n"], 1)

    def test_lookalike_link_is_blocked_and_hurts_channel_trust(self):
        async def go():
            pl = build()
            await pl.ingest(post("iPhone 15 @ ₹9,999 only!!\nhttps://amazon-offers.xyz/win"))
            return pl
        pl = asyncio.run(go())
        self.assertEqual(pl.db.one("SELECT COUNT(*) AS n FROM deals")["n"], 0)
        self.assertEqual(pl.db.one("SELECT reason FROM blocked")["reason"], "lookalike")
        self.assertLess(pl.db.one("SELECT trust FROM channels")["trust"], 0.5)

    def test_ambiguous_post_uses_batched_ai_and_ai_cannot_invent_links(self):
        text = "Samsung M14 5G loot today\nhttps://www.amazon.in/dp/B0BBBBBBB2\nhttps://www.flipkart.com/x/p/itmabc?pid=MOBXYZ"

        def reply(user):
            ids = [p["id"] for p in json.loads(user)]
            return json.dumps({"posts": [{"id": ids[0], "items": [
                {"url": "https://www.amazon.in/dp/B0BBBBBBB2", "title": "Samsung Galaxy M14 5G", "price": 9999, "mrp": 17999,
                 "coupon": None, "is_free": False},
                {"url": "https://www.amazon.in/dp/B0INVENTED9", "title": "Made up", "price": 1, "mrp": None,
                 "coupon": None, "is_free": False}]}]})

        async def go():
            ai = FakeAI([reply])
            pl = build(ai)
            await pl.ingest(post(text))
            entries = []
            while not pl.parse_q.empty():
                entries.append(pl.parse_q.get_nowait())
            await pl._ai_parse(entries)
            return pl, ai
        pl, ai = asyncio.run(go())
        titles = [d["title"] for d in pl.db.all("SELECT title FROM deals")]
        self.assertEqual(titles, ["Samsung Galaxy M14 5G"])
        self.assertEqual(len(ai.calls), 1)

    def test_ai_down_keeps_partial_deal_then_retry_fills_it(self):
        text = "Realme Buds Air loot\nhttps://www.amazon.in/dp/B0CCCCCCC3"

        def reply(user):
            pid = json.loads(user)[0]["id"]
            return json.dumps({"posts": [{"id": pid, "items": [
                {"url": None, "title": "Realme Buds Air", "price": 1299, "mrp": 2999, "coupon": None, "is_free": False}]}]})

        async def go():
            ai = FakeAI(down=True)
            pl = build(ai)
            await pl.ingest(post(text))
            await pl._ai_parse([pl.parse_q.get_nowait()])                  # AI outage: falls back to rules
            before = pl.get_deal("amazon:B0CCCCCCC3")
            ai.down, ai.replies = False, [reply]
            await pl.retry_pending()
            await pl._ai_parse([pl.parse_q.get_nowait()])
            return pl, before
        pl, before = asyncio.run(go())
        self.assertIsNone(before["price"])
        self.assertEqual(pl.get_deal("amazon:B0CCCCCCC3")["price"], 1299.0)

    def test_end_notice_marks_deal_dead_even_via_reply(self):
        async def go():
            pl = build()
            await pl.ingest(post(EARBUDS, msg_id=10))
            await pl.ingest(post("Deal ended ❌", msg_id=11, reply_to=10))
            return pl
        pl = asyncio.run(go())
        deal = pl.get_deal("amazon:B0AAAAAAA1")
        self.assertEqual((deal["status"], deal["end_reason"]), ("dead", "ended_notice"))

    def test_stale_aging(self):
        async def go():
            pl = build()
            await pl.ingest(post(EARBUDS))
            pl.db.run("UPDATE deals SET last_seen=last_seen-100000")
            return pl, pl.age_deals()
        pl, n = asyncio.run(go())
        self.assertEqual(n, 1)
        self.assertEqual(pl.get_deal("amazon:B0AAAAAAA1")["status"], "stale")

    def test_task_matches_new_deal_and_notifies_once(self):
        async def go():
            ai = FakeAI([VERDICT_BUY])
            pl = build(ai)
            task = pl.tasks.create("boat earbuds under 1500", ["boat", "earbuds"], 1500, "any")
            await pl.ingest(post(EARBUDS))
            await pl.ingest(post(EARBUDS, "Chan B", 3))               # second sighting must not alert again
            return pl, task
        pl, task = asyncio.run(go())
        self.assertEqual(pl.tasks.get(task["id"])["status"], "done")
        self.assertEqual(len(pl.notifier.sent), 1)
        self.assertEqual(pl.notifier.sent[0][2], task["id"])

    def test_task_rejects_when_ai_says_avoid_and_keeps_waiting(self):
        async def go():
            pl = build(FakeAI([VERDICT_AVOID]))
            task = pl.tasks.create("boat earbuds", ["boat", "earbuds"], 1500, "any")
            await pl.ingest(post(EARBUDS))
            return pl, task
        pl, task = asyncio.run(go())
        self.assertEqual(pl.tasks.get(task["id"])["status"], "waiting")
        self.assertEqual(pl.notifier.sent, [])

    def test_new_task_is_satisfied_by_existing_deal(self):
        async def go():
            pl = build(FakeAI([VERDICT_BUY]))
            await pl.ingest(post(EARBUDS))
            task = pl.tasks.create("airdopes", ["airdopes"], 1200, "amazon")
            await pl.tasks.scan_existing(task["id"])
            return pl, task
        pl, task = asyncio.run(go())
        self.assertEqual(pl.tasks.get(task["id"])["status"], "done")

    def test_price_too_high_does_not_match(self):
        async def go():
            pl = build(FakeAI([VERDICT_BUY]))
            task = pl.tasks.create("boat", ["boat"], 500, "any")
            await pl.ingest(post(EARBUDS))
            return pl, task
        pl, task = asyncio.run(go())
        self.assertEqual(pl.tasks.get(task["id"])["status"], "waiting")

    def test_scam_flag_overrides_ai_buy(self):
        async def go():
            pl = build(FakeAI([VERDICT_BUY]))
            await pl.ingest(post("Free iPhone 15 Pro! Send OTP to claim https://www.amazon.in/dp/B0DDDDDDD4 @ ₹0"))
            return pl, await pl.verify_deal("amazon:B0DDDDDDD4", kind="deep")
        pl, verdict = asyncio.run(go())
        self.assertEqual(verdict["verdict"], "avoid")

    def test_free_deal_alert_needs_corroboration(self):
        async def go():
            pl = build(FakeAI([VERDICT_BUY]))
            text = "Free Redmi Pad worth ₹14999 FREE\nhttps://www.flipkart.com/redmi/p/itmzzz?pid=TABFREE0001"
            await pl.ingest(post(text, "Chan A", 1))
            sent_after_one = len(pl.notifier.sent)
            for i, ch in enumerate(["Chan B", "Chan C", "Chan D", "Chan E"]):
                await pl.ingest(post(text, ch, 10 + i))
            return pl, sent_after_one
        pl, after_one = asyncio.run(go())
        self.assertEqual(after_one, 0)
        self.assertEqual(len(pl.notifier.sent), 1)
        self.assertEqual(pl.get_deal("flipkart:TABFREE0001")["status"], "free")


if __name__ == "__main__":
    unittest.main()
