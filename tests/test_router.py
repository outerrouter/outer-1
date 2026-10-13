import asyncio
import unittest

from app.ai.router import AIRouter, AIUnavailable, PRIORITY, RateLimited, Slot
from app.config import Box
from app.db import Database


class FakeProvider:
    def __init__(self, name, script=None):
        self.name, self.script, self.calls = name, list(script or []), 0

    async def generate(self, system, user, json_mode=False, max_tokens=700):
        self.calls += 1
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
        return f"{self.name}:{user}"


def make(providers, rpm=100, daily=20, shares=None):
    cfg = Box({"ai": {"rpm": rpm, "daily_total": daily, "cache_hours": 1,
                      "shares": shares or {"parse": 5, "deep": 5, "chat": 5, "alert": 3, "reserve": 2}}})
    db = Database(":memory:")
    return AIRouter(db, [Slot(p, rpm, min_gap=0.0) for p in providers], cfg), db


class RouterTests(unittest.TestCase):
    def test_cache_avoids_second_request(self):
        async def go():
            p = FakeProvider("a")
            r, _ = make([p])
            r.start()
            one = await r.ask("chat", "s", "hello", cache_key="k")
            two = await r.ask("chat", "s", "hello", cache_key="k")
            await r.stop()
            return one, two, p.calls
        one, two, calls = asyncio.run(go())
        self.assertEqual(one, two)
        self.assertEqual(calls, 1)

    def test_falls_back_to_second_key_on_rate_limit(self):
        async def go():
            a, b = FakeProvider("a", [RateLimited(30)]), FakeProvider("b")
            r, _ = make([a, b])
            r.start()
            out = await r.ask("chat", "s", "x")
            await r.stop()
            return out, a.calls, b.calls
        out, a_calls, b_calls = asyncio.run(go())
        self.assertEqual(out, "b:x")
        self.assertEqual((a_calls, b_calls), (1, 1))

    def test_per_kind_budget_and_reserve(self):
        async def go():
            r, _ = make([FakeProvider("a")], shares={"parse": 2, "deep": 2, "chat": 2, "alert": 2, "reserve": 1})
            r.start()
            for i in range(2):
                await r.ask("parse", "s", f"p{i}")
            with self.assertRaises(AIUnavailable):               # parse has no reserve access
                await r.ask("parse", "s", "p-extra")
            for i in range(2):
                await r.ask("chat", "s", f"c{i}")
            await r.ask("chat", "s", "c-reserve")                # chat may borrow the single reserve slot
            with self.assertRaises(AIUnavailable):
                await r.ask("chat", "s", "c-too-many")
            await r.stop()
        asyncio.run(go())

    def test_daily_limit_and_ninety_percent_rule(self):
        async def go():
            r, _ = make([FakeProvider("a")], daily=10, shares={"parse": 9, "deep": 9, "chat": 9, "alert": 9, "reserve": 0})
            r.start()
            for i in range(9):
                await r.ask("parse", "s", f"p{i}")
            with self.assertRaises(AIUnavailable):               # 90% used: background work stops
                await r.ask("parse", "s", "late")
            await r.ask("chat", "s", "still ok")                  # chat still allowed (10th request)
            with self.assertRaises(AIUnavailable):
                await r.ask("chat", "s", "over")
            await r.stop()
        asyncio.run(go())

    def test_chat_jumps_ahead_of_parse(self):
        async def go():
            order = []

            class Slow(FakeProvider):
                async def generate(self, system, user, json_mode=False, max_tokens=700):
                    order.append(user)
                    await asyncio.sleep(0.01)
                    return user
            r, _ = make([Slow("a")])
            tasks = [asyncio.create_task(r.ask("parse", "s", f"parse{i}")) for i in range(3)]
            await asyncio.sleep(0)                                # let the parse jobs enqueue
            tasks.append(asyncio.create_task(r.ask("chat", "s", "CHAT")))
            await asyncio.sleep(0)
            r.start()
            await asyncio.gather(*tasks)
            await r.stop()
            return order
        order = asyncio.run(go())
        self.assertEqual(order[0], "CHAT")

    def test_no_key_means_unavailable(self):
        async def go():
            r, _ = make([])
            with self.assertRaises(AIUnavailable):
                await r.ask("chat", "s", "x")
        asyncio.run(go())

    def test_priority_table(self):
        self.assertLess(PRIORITY["chat"], PRIORITY["alert"])
        self.assertLess(PRIORITY["alert"], PRIORITY["deep"])
        self.assertLess(PRIORITY["deep"], PRIORITY["parse"])


if __name__ == "__main__":
    unittest.main()
