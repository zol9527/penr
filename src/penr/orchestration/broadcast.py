"""任务活动广播：供 SSE 订阅。"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class Broadcaster:
    def __init__(self) -> None:
        self._subs: dict[str, list[asyncio.Queue[dict[str, Any]]]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def subscribe(self, task_key: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subs[task_key].append(q)
        return q

    async def unsubscribe(self, task_key: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            subs = self._subs.get(task_key) or []
            if q in subs:
                subs.remove(q)

    async def publish(self, task_key: str, event: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subs.get(task_key) or [])
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


_bc = Broadcaster()


def get_broadcaster() -> Broadcaster:
    return _bc
