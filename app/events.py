"""Tiny in-process event bus; feeds the live status panel through Server-Sent Events."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class Bus:
    def __init__(self, keep: int = 80):
        self.subs: set[asyncio.Queue] = set()
        self.recent: deque = deque(maxlen=keep)

    def publish(self, type_: str, data: dict | None = None) -> None:
        event = {"type": type_, "data": data or {}, "ts": time.time()}
        self.recent.append(event)
        for q in list(self.subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self.subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)
