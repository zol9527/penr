"""FastAPI 应用：健康检查、鉴权、任务引擎、SSE、扩展 API。"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from penr import __version__
from penr.api.auth import (
    authenticate,
    bootstrap_admin,
    create_token,
    require_user,
    seed_rbac_catalog,
)
from penr.api.companies import router as companies_router
from penr.api.compat import router as compat_router
from penr.api.conversations import router as conversations_router
from penr.api.enrich_api import router as enrich_router
from penr.api.exploration import router as exploration_router
from penr.api.intercept_api import router as intercept_router
from penr.api.knowledge_api import promote_router as knowledge_promote_router
from penr.api.knowledge_api import router as knowledge_router
from penr.api.llm_api import router as llm_router
from penr.api.mcp_api import router as mcp_router
from penr.api.rbac import RBACEnforceMiddleware, router as rbac_router
from penr.api.report_api import router as report_router
from penr.api.settings_api import router as settings_router
from penr.api.skills_api import router as skills_router
from penr.api.traffic_routes import router as traffic_router
from penr.config import get_settings
from penr.crews.main_chat import run_main_chat
from penr.db.engine import Database, get_db, set_db
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore, TaskStore
from penr.guard.guard import resolve_pending
from penr.orchestration.broadcast import get_broadcaster
from penr.orchestration.task_engine import TaskEngine, get_engine
from penr.traffic.index import init_traffic_db

logger = logging.getLogger(__name__)


class SetupBody(BaseModel):
    username: str = "admin"
    password: str = Field(min_length=6)


class LoginBody(BaseModel):
    username: str
    password: str


class TaskCreateBody(BaseModel):
    description: str
    goal: str
    name: str = ""
    company_id: int | None = None
    llm_profile_id: int | None = None
    timeout_seconds: int = 0
    auto_start: bool = True


class SteerBody(BaseModel):
    intent_id: int
    message: str


class KillBody(BaseModel):
    intent_id: int


class ChatBody(BaseModel):
    message: str
    task_id: int | None = None
    mode: str = "main"


class LLMProfileBody(BaseModel):
    id: int | None = None
    name: str
    format: str = "openai"
    base_url: str | None = None
    model: str
    api_key: str | None = None
    api_key_hint: str = ""
    is_default: bool = False
    priority: int = 0
    pool_exclude: bool = False


class InterceptResolveBody(BaseModel):
    allow: bool


class KnowledgeIngestBody(BaseModel):
    title: str
    content: str
    source: str = "manual"
    # 默认 task：任务临时知识；长期库只通过 promote → 本仓 OpenViking
    scope: str = "task"
    task_id: int | None = None
    category: str = ""


class TriggerBody(BaseModel):
    agent_key: str
    enabled: bool = True
    interval_sec: int = 0
    on_finding: bool = False
    on_goal_met: bool = False
    on_task_timeout: bool = False
    interval_message: str = ""
    finding_message: str = ""
    goal_message: str = ""
    task_timeout_message: str = ""


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings)
        await db.connect()
        try:
            await db.migrate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("schema migrate warning: %s", exc)
        set_db(db)
        try:
            await seed_rbac_catalog()
        except Exception as exc:  # noqa: BLE001
            logger.warning("rbac seed warning: %s", exc)
        await init_traffic_db()
        engine = TaskEngine(db)
        from penr.orchestration.scheduler import TriggerScheduler

        scheduler = TriggerScheduler(db)
        scheduler.start()
        app.state.db = db
        app.state.engine = engine
        app.state.scheduler = scheduler
        # 覆盖全局单例
        import penr.orchestration.task_engine as te

        te._engine = engine
        yield
        await scheduler.stop()
        await engine_shutdown(engine)
        await db.close()

    app = FastAPI(title="AntiFox Agents", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 管理面权限强制（rbac/settings/llm/intercept）：缺权限默认 403
    app.add_middleware(RBACEnforceMiddleware)

    # M1–M8：独立模块路由一次挂载
    for _r in (
        compat_router,
        exploration_router,
        companies_router,
        traffic_router,
        rbac_router,
        settings_router,
        llm_router,
        intercept_router,
        conversations_router,
        mcp_router,
        skills_router,
        report_router,
        enrich_router,
        knowledge_router,
        knowledge_promote_router,
    ):
        app.include_router(_r)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        ok = False
        try:
            ok = await get_db().ping()
        except Exception:  # noqa: BLE001
            ok = False
        return {"status": "ok" if ok else "degraded", "version": __version__, "db": ok}

    @app.get("/api/health")
    async def api_health() -> dict[str, Any]:
        return await health()

    # ---- auth ----
    @app.get("/api/setup/status")
    async def setup_status() -> dict[str, Any]:
        n = await get_db().fetchval("SELECT COUNT(*) FROM rbac_users")
        return {"initialized": int(n or 0) > 0}

    @app.post("/api/setup")
    async def setup(body: SetupBody) -> dict[str, Any]:
        user = await bootstrap_admin(body.username, body.password)
        token = create_token(user)
        return {"token": token, "user": {"id": user["id"], "username": user["username"]}}

    @app.post("/api/login")
    async def login(body: LoginBody) -> dict[str, Any]:
        user = await authenticate(body.username, body.password)
        await get_db().execute(
            """
            INSERT INTO audit_logs (actor, category, action, result, message)
            VALUES ($1, 'auth', 'login', 'success', 'login ok')
            """,
            user["username"],
        )
        return {"token": create_token(user), "user": {"id": user["id"], "username": user["username"]}}

    @app.get("/api/me")
    async def me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        return {"id": user["id"], "username": user["username"], "display_name": user.get("display_name")}

    # ---- tasks ----
    @app.get("/api/tasks")
    async def list_tasks(
        user: dict[str, Any] = Depends(require_user),
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        items = await TaskStore(get_db()).list(limit=limit, offset=offset)
        engine = get_engine()
        active = ""
        for t in items:
            rt = getattr(engine, "_tasks", {}).get(t.id)
            if rt and rt.loop_task and not rt.loop_task.done() and not t.paused:
                active = str(t.id)
                break
        if not active and items:
            active = str(items[0].id)
        return {
            "tasks": [
                {
                    "id": str(t.id),
                    "name": t.name,
                    "description": t.description,
                    "goal": t.goal,
                    "exploration_id": t.exploration_id,
                    "status": t.status,
                    "paused": t.paused,
                    "created_at": t.created_at.isoformat() if t.created_at else "",
                    "llm_profile_id": t.llm_profile_id,
                    "timeout_seconds": getattr(t, "timeout_seconds", 0) or 0,
                }
                for t in items
            ],
            "active": active,
        }

    @app.post("/api/tasks")
    async def create_task(
        body: TaskCreateBody,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        store = TaskStore(get_db())
        task = await store.create(
            description=body.description,
            goal=body.goal,
            name=body.name,
            company_id=body.company_id,
            llm_profile_id=body.llm_profile_id,
            timeout_seconds=body.timeout_seconds,
        )
        (settings.data_dir / "work" / str(task.id)).mkdir(parents=True, exist_ok=True)
        if body.auto_start:
            await get_engine().start(task.id)
        return {
            "id": task.id,
            "name": task.name,
            "goal": task.goal,
            "exploration_id": task.exploration_id,
            "timeout_seconds": task.timeout_seconds,
            "status": "running" if body.auto_start else task.status,
        }

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        task = await TaskStore(get_db()).get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        exp = ExplorationStore(get_db(), task.exploration_id)
        overview = await exp.graph_overview()
        return {
            "id": task.id,
            "name": task.name,
            "description": task.description,
            "goal": task.goal,
            "exploration_id": task.exploration_id,
            "status": task.status,
            "paused": task.paused,
            "timeout_seconds": task.timeout_seconds,
            "deadline_at": task.deadline_at,
            "overview": overview,
        }

    @app.post("/api/tasks/{task_id}/start")
    async def start_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
        await get_engine().start(task_id)
        return {"status": "running"}

    @app.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
        await get_engine().pause(task_id)
        return {"status": "paused"}

    @app.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
        await get_engine().resume(task_id)
        return {"status": "running"}

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
        await get_engine().stop(task_id)
        return {"status": "done"}

    @app.post("/api/tasks/{task_id}/rerun")
    async def rerun_task(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, str]:
        await get_engine().rerun(task_id)
        return {"status": "running"}

    @app.post("/api/tasks/{task_id}/steer")
    async def steer_task(
        task_id: int,
        body: SteerBody,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        try:
            get_engine().steer_work(body.intent_id, body.message)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await get_engine().notify(task_id)
        return {"ok": True, "intent_id": body.intent_id}

    @app.post("/api/tasks/{task_id}/kill")
    async def kill_task_work(
        task_id: int,
        body: KillBody,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        try:
            get_engine().kill_work(body.intent_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        await get_engine().notify(task_id)
        return {"ok": True, "intent_id": body.intent_id}

    @app.get("/api/tasks/{task_id}/graph")
    async def task_graph(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        task = await TaskStore(get_db()).get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        return await ExplorationStore(get_db(), task.exploration_id).graph_overview()

    @app.get("/api/tasks/{task_id}/activity")
    async def task_activity(
        task_id: int,
        since: int = 0,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        task = await TaskStore(get_db()).get(task_id)
        if not task:
            raise HTTPException(404, "task not found")
        items = await ExplorationStore(get_db(), task.exploration_id).list_activity_since(since)
        return {"activity": items}

    @app.get("/api/tasks/{task_id}/events")
    async def task_events(task_id: int, request: Request, user: dict[str, Any] = Depends(require_user)):
        bc = get_broadcaster()
        q = await bc.subscribe(str(task_id))

        async def gen():
            try:
                yield {"event": "connected", "data": json.dumps({"task_id": task_id})}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        evt = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield {"event": "activity", "data": json.dumps(evt, default=str)}
                    except TimeoutError:
                        yield {"event": "ping", "data": "{}"}
            finally:
                await bc.unsubscribe(str(task_id), q)

        return EventSourceResponse(gen())

    # ---- findings / assets ----
    @app.get("/api/tasks/{task_id}/findings")
    async def task_findings(task_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        return {"findings": await FindingsStore(get_db()).list_for_task(task_id)}

    # GET /api/findings/{node_id} 由 enrich_api 按 exploration node_id 提供

    @app.get("/api/assets")
    async def list_assets(
        q: str = "",
        type: str = "",
        task_id: int | None = None,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        rows = await AssetStore(get_db()).list_assets(
            q=q or None, asset_type=type or None, task_id=task_id
        )
        return {"assets": rows}

    # ---- chat / main ----
    @app.post("/api/chat")
    async def chat(body: ChatBody, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        exploration = None
        assets = AssetStore(get_db())
        findings = FindingsStore(get_db())
        goal = ""
        if body.task_id:
            task = await TaskStore(get_db()).get(body.task_id)
            if not task:
                raise HTTPException(404, "task not found")
            exploration = ExplorationStore(get_db(), task.exploration_id)
            goal = task.goal
            # 图变更后唤醒引擎
            result = await run_main_chat(
                message=body.message,
                task_id=body.task_id,
                exploration=exploration,
                assets=assets,
                findings=findings,
                goal=goal,
                mode=body.mode,
            )
            await get_engine().notify(body.task_id)
            return result
        return await run_main_chat(message=body.message, mode="chat")

    # ---- HITL resolve 兼容别名（主路径在 intercept_api：/decide）----
    @app.post("/api/intercept/pending/{pending_id}/resolve")
    async def intercept_resolve(
        pending_id: int,
        body: InterceptResolveBody,
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        ok = await resolve_pending(pending_id, allow=body.allow)
        if not ok:
            raise HTTPException(404, "pending not found or already decided")
        return {"ok": True, "allow": body.allow}

    # ---- LLM profiles：见 llm_api 路由模块 ----

    # knowledge / report-async / mcp / skills → 见上方 include_router（M6/M7）
    # 保留同步报告快捷入口（兼容旧调用）；异步路径见 /api/report/generate
    @app.post("/api/tasks/{task_id}/report")
    async def make_report(
        task_id: int,
        format: str = "md",
        user: dict[str, Any] = Depends(require_user),
    ) -> dict[str, Any]:
        from penr.report.jobs import start_report_job

        job = await start_report_job(get_db(), task_id=task_id, format=format)
        return job

    # ---- triggers (P3) ----
    @app.get("/api/triggers")
    async def list_triggers(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        rows = await get_db().fetch("SELECT * FROM agent_triggers ORDER BY id")
        return {"triggers": [dict(r) for r in rows]}

    @app.post("/api/triggers")
    async def create_trigger(body: TriggerBody, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        row = await get_db().fetchrow(
            """
            INSERT INTO agent_triggers
                (agent_key, enabled, interval_sec, on_finding, on_goal_met, on_task_timeout,
                 interval_message, finding_message, goal_message, task_timeout_message)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id
            """,
            body.agent_key,
            body.enabled,
            body.interval_sec,
            body.on_finding,
            body.on_goal_met,
            body.on_task_timeout,
            body.interval_message,
            body.finding_message,
            body.goal_message,
            body.task_timeout_message,
        )
        return {"id": int(row["id"])}

    # skills 列表由 skills_api 提供（/api/skills）

    # ---- static web (optional) ----
    web_dist = settings.web_dist or (settings.data_dir / "webui")
    if web_dist.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dist), html=True), name="webui")

    return app


async def engine_shutdown(engine: TaskEngine) -> None:
    for tid in list(engine._tasks.keys()):
        try:
            await engine.stop(tid)
        except Exception:  # noqa: BLE001
            pass
