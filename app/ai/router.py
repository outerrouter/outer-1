"""AI request router: rate limit, daily budget, priority queue, cache, key fallback.

Design goals (from the requirements): ~10-14 requests/minute and ~2000/day must never be exceeded,
chat must never wait behind background work, and the app keeps working (rule-only) when AI is out.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from ..db import today

PRIORITY = {"chat": 0, "alert": 1, "deep": 2, "parse": 3}


class AIUnavailable(Exception):
    """No AI answer could be produced (budget used up, all providers cooling down, no key...)."""


class RateLimited(Exception):
    def __init__(self, retry_after: float = 20.0):
        super().__init__("rate limited")
        self.retry_after = retry_after


class AuthError(Exception):
    pass


class ProviderError(Exception):
    pass


class Slot:
    """One provider plus its sliding one-minute window and cooldown state."""

    def __init__(self, impl, rpm: int, min_gap: float = 1.0):
        self.impl, self.name, self.rpm, self.min_gap = impl, impl.name, rpm, min_gap
        self.calls: deque = deque()
        self.cool_until = 0.0
        self.fails = 0

    def wait_time(self, now: float) -> float:
        while self.calls and now - self.calls[0] >= 60:
            self.calls.popleft()
        wait = max(0.0, self.cool_until - now)
        if len(self.calls) >= self.rpm:
            wait = max(wait, 60 - (now - self.calls[0]) + 0.05)
        if self.calls and now - self.calls[-1] < self.min_gap:   # never fire faster than 1 request/second
            wait = max(wait, self.min_gap - (now - self.calls[-1]))
        return wait


@dataclass(order=False)
class Job:
    kind: str
    system: str
    user: str
    json_mode: bool
    max_tokens: int
    fut: asyncio.Future = field(repr=False, default=None)


class AIRouter:
    def __init__(self, db, slots: list, cfg):
        self.db, self.slots, self.cfg = db, slots, cfg
        self.q: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0
        self._task: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.slots)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    # ------------------------------------------------------------------ public API
    async def ask(self, kind: str, system: str, user: str, *, json_mode: bool = False,
                  max_tokens: int = 700, cache_key: str | None = None) -> str:
        if cache_key:
            hit = self.db.cache_get(cache_key)
            if hit is not None:
                return hit
        if not self.slots:
            raise AIUnavailable("no AI key configured")
        fut = asyncio.get_running_loop().create_future()
        self._seq += 1
        await self.q.put((PRIORITY.get(kind, 3), self._seq, Job(kind, system, user, json_mode, max_tokens, fut)))
        text = await fut
        if cache_key:
            self.db.cache_set(cache_key, text, float(self.cfg.ai.cache_hours))
        return text

    def usage(self) -> dict:
        rows = self.db.all("SELECT kind, SUM(count) AS c FROM ai_usage WHERE day=? AND provider!='tavily' GROUP BY kind",
                           (today(),))
        by_kind = {r["kind"]: int(r["c"]) for r in rows}
        now = time.monotonic()
        return {
            "day": today(), "total": sum(by_kind.values()), "limit": int(self.cfg.ai.daily_total),
            "by_kind": by_kind, "shares": dict(self.cfg.ai.shares), "rpm": int(self.cfg.ai.rpm),
            "providers": [{"name": s.name, "cooling": s.cool_until > now,
                           "last_minute": len([c for c in s.calls if time.monotonic() - c < 60])} for s in self.slots],
        }

    # ------------------------------------------------------------------ budget
    def budget_ok(self, kind: str) -> bool:
        used = self.usage()
        total, limit = used["total"], used["limit"]
        if total >= limit:
            return False
        if kind in ("parse", "deep") and total >= 0.9 * limit:      # keep the last 10% for chat + alerts
            return False
        shares = used["shares"]
        if used["by_kind"].get(kind, 0) < shares.get(kind, 0):
            return True
        if kind in ("chat", "alert"):                                 # may dip into the shared reserve
            borrowed = sum(max(0, used["by_kind"].get(k, 0) - shares.get(k, 0)) for k in used["by_kind"])
            return borrowed < shares.get("reserve", 0)
        return False

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        while True:
            _, _, job = await self.q.get()
            try:
                if not job.fut.done():
                    await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:                                   # the worker must never die
                if not job.fut.done():
                    job.fut.set_exception(AIUnavailable(str(exc)))
            finally:
                self.q.task_done()

    def _pick(self, now: float):
        best, best_wait = None, None
        for slot in self.slots:
            wait = slot.wait_time(now)
            if wait <= 0:
                return slot, 0.0
            if best_wait is None or wait < best_wait:
                best, best_wait = slot, wait
        return None, best_wait

    async def _run(self, job: Job) -> None:
        if not self.budget_ok(job.kind):
            job.fut.set_exception(AIUnavailable(f"daily budget reached for '{job.kind}'"))
            return
        last_error: Exception | None = None
        for _ in range(len(self.slots) * 3 + 3):
            now = time.monotonic()
            slot, wait = self._pick(now)
            if slot is None:
                if job.kind in ("deep", "parse") and wait > 30:         # background work gives up quickly
                    break
                await asyncio.sleep(min(wait, 30))
                continue
            slot.calls.append(time.monotonic())
            try:
                text = await slot.impl.generate(job.system, job.user, job.json_mode, job.max_tokens)
            except RateLimited as exc:
                slot.fails += 1
                slot.cool_until = time.monotonic() + min(exc.retry_after * (2 ** (slot.fails - 1)), 3600)
                last_error = exc
                continue
            except AuthError as exc:
                slot.cool_until = time.monotonic() + 6 * 3600
                last_error = exc
                continue
            except Exception as exc:                                    # network / 5xx
                slot.fails += 1
                slot.cool_until = time.monotonic() + min(10 * slot.fails, 300)
                last_error = exc
                continue
            slot.fails = 0
            self.db.count_usage(job.kind, slot.name)
            job.fut.set_result(text)
            return
        job.fut.set_exception(AIUnavailable(f"all providers busy or failing ({type(last_error).__name__ if last_error else 'cooldown'})"))
