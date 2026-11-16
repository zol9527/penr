"""P3 触发器调度：定时 / finding / goal / timeout 事件驱动对话。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from penr.crews.main_chat import run_main_chat
from penr.db.engine import Database
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore, TaskStore

logger = logging.getLogger(__name__)


class TriggerScheduler:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="trigger-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.wait([self._task], timeout=3)

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduler tick error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=15.0)
            except TimeoutError:
                pass

    async def _tick(self) -> None:
        triggers = await self.db.fetch(
            "SELECT * FROM agent_triggers WHERE enabled = true"
        )
        now = datetime.now(timezone.utc)
        for tr in triggers:
            if int(tr["interval_sec"] or 0) > 0:
                last = tr["last_fire"]
                due = True
                if last is not None:
                    delta = (now - last.replace(tzinfo=timezone.utc)).total_seconds()
                    due = delta >= int(tr["interval_sec"])
                if due:
                    await self._fire(tr, tr["interval_message"] or "interval trigger")

    async def _fire(self, trigger: Any, message: str) -> None:
        # 对所有 running 任务发消息（简化：生产可按 agent_key 过滤）
        tasks = await TaskStore(self.db).list(limit=20)
        for t in tasks:
            if t.status != "running":
                continue
            exp = ExplorationStore(self.db, t.exploration_id)
            await run_main_chat(
                message=message or f"trigger:{trigger['agent_key']}",
                task_id=t.id,
                exploration=exp,
                assets=AssetStore(self.db),
                findings=FindingsStore(self.db),
                goal=t.goal,
                mode="main",
            )
        await self.db.execute(
            "UPDATE agent_triggers SET last_fire = now(), updated_at = now() WHERE id = $1",
            int(trigger["id"]),
        )

    async def on_finding(self, task_id: int, summary: str) -> None:
        triggers = await self.db.fetch(
            "SELECT * FROM agent_triggers WHERE enabled = true AND on_finding = true"
        )
        for tr in triggers:
            msg = tr["finding_message"] or f"new finding: {summary}"
            task = await TaskStore(self.db).get(task_id)
            if not task:
                continue
            await run_main_chat(
                message=msg,
                task_id=task_id,
                exploration=ExplorationStore(self.db, task.exploration_id),
                assets=AssetStore(self.db),
                findings=FindingsStore(self.db),
                goal=task.goal,
                mode="main",
            )
