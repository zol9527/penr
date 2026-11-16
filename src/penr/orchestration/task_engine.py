"""LangGraph 任务引擎：debounce → plan → workers → writeback 环。

对齐 Go Engine 的 steer/kill/deadline/rerun 控制面。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypedDict

from penr.config import get_settings
from penr.crews.planner import run_planner
from penr.crews.worker import run_worker
from penr.db.engine import Database, get_db
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore, TaskStore
from penr.orchestration.broadcast import get_broadcaster

logger = logging.getLogger(__name__)

# 任务超时收尾：等待在跑 work 的优雅上限
_SETTLE_DRAIN_GRACE = 90.0
_DEADLINE_POLL = 2.0
_DEADLINE_MAX_SLEEP = 30.0


class EngineState(TypedDict, total=False):
    task_id: int
    exploration_id: int
    goal: str
    done_intents: list[int]
    met: bool
    reason: str
    round: int


@dataclass
class _TaskRuntime:
    task_id: int
    exploration_id: int
    goal: str
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    paused: asyncio.Event = field(default_factory=asyncio.Event)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    done_intents: list[int] = field(default_factory=list)
    loop_task: asyncio.Task[None] | None = None
    last_activity: float = field(default_factory=time.time)
    round: int = 0
    settling: bool = False
    deadline_coord: asyncio.Task[None] | None = None


class TaskEngine:
    """等价 Go server.Engine.Run 的事件驱动环。

    LangGraph 用于单轮 plan→execute 状态机；外层 asyncio 负责 debounce/并发 worker。
    per-intent：steer_box / work_cancel 对齐 Go SteerWork / KillWork。
    """

    def __init__(self, db: Database | None = None) -> None:
        self.db = db or get_db()
        self.settings = get_settings()
        self._tasks: dict[int, _TaskRuntime] = {}
        self._lock = asyncio.Lock()
        self.bc = get_broadcaster()
        self._graph = None
        # 按 intent_id 索引（与 Go 一致；节点 id 全局唯一）
        self._work_cancel: dict[int, asyncio.Event] = {}
        self._work_task: dict[int, int] = {}  # intent_id -> task_id（收尾时只杀本任务）
        self._steer_box: dict[int, list[str]] = {}
        self._deadline: dict[int, float] = {}  # task_id -> unix deadline
        self._coord_started: set[int] = set()
        self._work_mu = asyncio.Lock()
        self._steer_mu = asyncio.Lock()

    def _build_graph(self):
        """构建单轮 LangGraph：plan → fanout workers 标记。"""
        if self._graph is not None:
            return self._graph
        try:
            from langgraph.graph import END, StateGraph
        except ImportError:
            return None

        graph = StateGraph(EngineState)

        async def plan_node(state: EngineState) -> EngineState:
            return {**state, "round": int(state.get("round") or 0) + 1}

        async def decide_node(state: EngineState) -> EngineState:
            return state

        graph.add_node("plan", plan_node)
        graph.add_node("decide", decide_node)
        graph.set_entry_point("plan")
        graph.add_edge("plan", "decide")
        graph.add_edge("decide", END)
        self._graph = graph.compile()
        return self._graph

    async def notify(self, task_id: int, done_intent: int | None = None) -> None:
        rt = self._tasks.get(task_id)
        if not rt:
            return
        if done_intent is not None:
            rt.done_intents.append(done_intent)
        rt.wake.set()

    # ---- per-intent steer / kill（对齐 Go）----

    def register_work(self, intent_id: int, task_id: int | None = None) -> asyncio.Event:
        """记录正在跑的 work 的取消事件；返回供 worker 轮询的 Event。"""
        evt = asyncio.Event()
        iid = int(intent_id)
        self._work_cancel[iid] = evt
        if task_id is not None:
            self._work_task[iid] = int(task_id)
        return evt

    def unregister_work(self, intent_id: int) -> None:
        iid = int(intent_id)
        self._work_cancel.pop(iid, None)
        self._work_task.pop(iid, None)
        self._steer_box.pop(iid, None)

    def is_work_running(self, intent_id: int) -> bool:
        return int(intent_id) in self._work_cancel

    def is_work_killed(self, intent_id: int) -> bool:
        evt = self._work_cancel.get(int(intent_id))
        return bool(evt and evt.is_set())

    def steer_work(self, intent_id: int, msg: str) -> None:
        """向运行中的意图投递纠偏消息（FIFO）；无运行中 work 则报错。"""
        text = (msg or "").strip()
        if not text:
            raise ValueError("纠偏消息不能为空")
        iid = int(intent_id)
        if iid not in self._work_cancel:
            raise ValueError(f"意图 {iid} 当前没有运行中的 work（可能已结束或未被领取）")
        self._steer_box.setdefault(iid, []).append(text)

    def drain_steer(self, intent_id: int) -> str | None:
        """弹出该意图最早一条纠偏（FIFO）；无则返回 None。"""
        iid = int(intent_id)
        q = self._steer_box.get(iid)
        if not q:
            return None
        msg = q.pop(0)
        if not q:
            self._steer_box.pop(iid, None)
        return msg

    def kill_work(self, intent_id: int) -> None:
        """取消指定意图的在跑 work。"""
        iid = int(intent_id)
        evt = self._work_cancel.get(iid)
        if evt is None:
            raise ValueError(f"意图 {iid} 当前没有运行中的 work（可能已结束或未被领取）")
        evt.set()

    # ---- 生命周期 ----

    async def start(self, task_id: int) -> None:
        tasks = TaskStore(self.db)
        task = await tasks.get(task_id)
        if not task:
            raise ValueError(f"task {task_id} not found")
        async with self._lock:
            if task_id in self._tasks and self._tasks[task_id].loop_task and not self._tasks[task_id].loop_task.done():
                await self.resume(task_id)
                return
            rt = _TaskRuntime(
                task_id=task_id,
                exploration_id=task.exploration_id,
                goal=task.goal,
            )
            self._tasks[task_id] = rt
            deadline = await tasks.mark_first_run(task_id)
            if deadline is not None:
                self._deadline[task_id] = deadline
            rt.loop_task = asyncio.create_task(self._run_loop(rt), name=f"engine-{task_id}")
            rt.wake.set()
            await self._ensure_deadline_coordinator(rt, task.timeout_seconds)

    async def pause(self, task_id: int) -> None:
        rt = self._tasks.get(task_id)
        if rt:
            rt.paused.set()
        await TaskStore(self.db).set_status(task_id, "paused", paused=True)
        await self.bc.publish(str(task_id), {"kind": "status", "status": "paused"})

    async def resume(self, task_id: int) -> None:
        rt = self._tasks.get(task_id)
        if rt:
            rt.paused.clear()
            rt.wake.set()
        await TaskStore(self.db).set_status(task_id, "running", paused=False)
        await self.bc.publish(str(task_id), {"kind": "status", "status": "running"})

    async def stop(self, task_id: int, *, status: str = "done") -> None:
        rt = self._tasks.get(task_id)
        if rt:
            rt.stop.set()
            rt.wake.set()
            if rt.deadline_coord and not rt.deadline_coord.done():
                rt.deadline_coord.cancel()
            if rt.loop_task:
                try:
                    await asyncio.wait_for(rt.loop_task, timeout=5)
                except TimeoutError:
                    rt.loop_task.cancel()
        self._coord_started.discard(task_id)
        self._deadline.pop(task_id, None)
        await TaskStore(self.db).set_status(task_id, status, paused=False)
        if status == "done":
            await self._promote_knowledge(task_id)

    async def rerun(self, task_id: int) -> None:
        """继续运行：清 deadline、重启环、再臂超时协调器（保留探索图）。"""
        tasks = TaskStore(self.db)
        task = await tasks.get(task_id)
        if not task:
            raise ValueError(f"task {task_id} not found")
        await tasks.clear_deadline(task_id)
        self._deadline.pop(task_id, None)
        self._coord_started.discard(task_id)

        rt = self._tasks.get(task_id)
        if rt and rt.loop_task and not rt.loop_task.done():
            rt.settling = False
            rt.stop.clear()
            rt.paused.clear()
            if rt.deadline_coord and not rt.deadline_coord.done():
                rt.deadline_coord.cancel()
                rt.deadline_coord = None
            await tasks.set_status(task_id, "running", paused=False)
            deadline = await tasks.mark_first_run(task_id)
            if deadline is not None:
                self._deadline[task_id] = deadline
            await self._ensure_deadline_coordinator(rt, task.timeout_seconds)
            rt.wake.set()
            await self.bc.publish(str(task_id), {"kind": "status", "status": "running", "rerun": True})
            return

        # 环已结束：重新 start
        self._tasks.pop(task_id, None)
        await tasks.set_status(task_id, "running", paused=False)
        await self.start(task_id)
        await self.bc.publish(str(task_id), {"kind": "status", "status": "running", "rerun": True})

    async def _promote_knowledge(
        self,
        task_id: int,
        publish: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        """任务结束后：task 层知识点 → 本仓 OpenViking 长期库。"""
        try:
            from penr.knowledge.promote import KnowledgePromoter

            result = await KnowledgePromoter(self.db).promote_task(task_id)
            if publish:
                await publish({"kind": "knowledge_promoted", **result})
            else:
                await self.bc.publish(str(task_id), {"kind": "knowledge_promoted", **result})
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge promote failed task=%s: %s", task_id, exc)

    # ---- deadline coordinator ----

    async def _ensure_deadline_coordinator(self, rt: _TaskRuntime, timeout_seconds: int) -> None:
        if timeout_seconds <= 0:
            return
        if rt.task_id in self._coord_started:
            return
        self._coord_started.add(rt.task_id)
        rt.deadline_coord = asyncio.create_task(
            self._deadline_coordinator(rt),
            name=f"deadline-{rt.task_id}",
        )

    async def _deadline_coordinator(self, rt: _TaskRuntime) -> None:
        """绝对墙钟超时：到点进入收尾 → timeout/done。"""
        task_id = rt.task_id
        try:
            while not rt.stop.is_set():
                status_row = await TaskStore(self.db).get(task_id)
                if not status_row or status_row.status in {"done", "failed", "timeout"}:
                    return
                dl = self._deadline.get(task_id)
                if dl is None and status_row.deadline_at is not None:
                    dl = status_row.deadline_at.timestamp()
                    self._deadline[task_id] = dl
                if dl is None:
                    await asyncio.sleep(_DEADLINE_POLL)
                    continue
                remaining = dl - time.time()
                if remaining > 0:
                    await asyncio.sleep(min(remaining, _DEADLINE_MAX_SLEEP))
                    continue
                await self._settle_task(rt)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("deadline coordinator failed task=%s: %s", task_id, exc)
        finally:
            self._coord_started.discard(task_id)

    async def _settle_task(self, rt: _TaskRuntime) -> None:
        if rt.settling:
            return
        rt.settling = True
        task_id = rt.task_id
        logger.info("[deadline] task %s 到达超时上限,进入收尾", task_id)
        await self.bc.publish(str(task_id), {"kind": "deadline", "status": "settling"})

        # 等本任务在跑 work 退出；超时则强制 kill（勿波及其他任务）
        def _mine() -> list[asyncio.Event]:
            return [
                evt
                for iid, evt in self._work_cancel.items()
                if self._work_task.get(iid) == task_id
            ]

        hard_stop = time.time() + _SETTLE_DRAIN_GRACE
        while any(not e.is_set() for e in _mine()) and time.time() < hard_stop:
            if rt.stop.is_set():
                return
            await asyncio.sleep(0.5)
        for evt in _mine():
            evt.set()

        # 终局：直接落 timeout（完整终局 planner 留给后续增强）
        await TaskStore(self.db).set_status(task_id, "timeout", paused=False)
        await self.bc.publish(str(task_id), {"kind": "status", "status": "timeout"})
        rt.stop.set()
        rt.wake.set()

    async def _run_loop(self, rt: _TaskRuntime) -> None:
        debounce = self.settings.planner_debounce_ms / 1000.0
        workers_n = max(1, self.settings.worker_concurrency)
        exploration = ExplorationStore(self.db, rt.exploration_id)
        assets = AssetStore(self.db)
        findings = FindingsStore(self.db)
        graph = self._build_graph()

        async def publish(evt: dict[str, Any]) -> None:
            rt.last_activity = time.time()
            await self.bc.publish(str(rt.task_id), evt)

        try:
            while not rt.stop.is_set():
                try:
                    await asyncio.wait_for(rt.wake.wait(), timeout=30.0)
                except TimeoutError:
                    continue
                rt.wake.clear()
                if rt.stop.is_set():
                    break
                while rt.paused.is_set() and not rt.stop.is_set():
                    await asyncio.sleep(0.2)
                if rt.stop.is_set():
                    break
                if rt.settling:
                    # 收尾中不再派新意图
                    await asyncio.sleep(0.5)
                    continue

                await asyncio.sleep(debounce)
                if rt.wake.is_set():
                    rt.wake.clear()
                    await asyncio.sleep(debounce)

                done = list(rt.done_intents)
                rt.done_intents.clear()
                rt.round += 1

                if graph is not None:
                    try:
                        await graph.ainvoke(
                            {
                                "task_id": rt.task_id,
                                "exploration_id": rt.exploration_id,
                                "goal": rt.goal,
                                "done_intents": done,
                                "round": rt.round,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("langgraph invoke skipped: %s", exc)

                await publish({"kind": "planner_start", "round": rt.round})
                plan_result = await run_planner(
                    task_id=rt.task_id,
                    goal=rt.goal,
                    exploration=exploration,
                    assets=assets,
                    findings=findings,
                    done_intents=done,
                    work_dir=str(self.settings.data_dir / "work" / str(rt.task_id)),
                    proxy_addr=f"http://{self.settings.traffic_proxy_addr}",
                    notify=publish,
                )
                await publish({"kind": "planner_done", "round": rt.round, **plan_result})

                if plan_result.get("met"):
                    await TaskStore(self.db).set_status(rt.task_id, "done")
                    await publish({"kind": "task_done", "reason": plan_result.get("reason")})
                    await self._promote_knowledge(rt.task_id, publish)
                    break

                async def one_worker(name: str) -> None:
                    if rt.paused.is_set() or rt.stop.is_set() or rt.settling:
                        return
                    result = await run_worker(
                        task_id=rt.task_id,
                        exploration=exploration,
                        assets=assets,
                        findings=findings,
                        worker_name=name,
                        work_dir=str(self.settings.data_dir / "work" / str(rt.task_id)),
                        proxy_addr=f"http://{self.settings.traffic_proxy_addr}",
                        notify=publish,
                    )
                    if result:
                        await publish({"kind": "worker_done", **result})
                        if not result.get("killed"):
                            await self.notify(rt.task_id, done_intent=int(result["intent_id"]))

                await asyncio.gather(*[one_worker(f"work#{i + 1}") for i in range(workers_n)])

                overview = await exploration.graph_overview()
                if overview.get("open_intents"):
                    rt.wake.set()
                elif not overview.get("running_intents"):
                    await publish({"kind": "idle", "round": rt.round})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("engine loop failed task=%s: %s", rt.task_id, exc)
            await TaskStore(self.db).set_status(rt.task_id, "failed")
            await publish({"kind": "error", "error": str(exc)})


_engine: TaskEngine | None = None


def get_engine() -> TaskEngine:
    global _engine
    if _engine is None:
        _engine = TaskEngine()
    return _engine
