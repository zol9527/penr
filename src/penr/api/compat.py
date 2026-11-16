"""M1 API 兼容层：auth 别名、task control、exploration graph/activity、SSE ?token=。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from penr.api.auth import (
    authenticate,
    bootstrap_admin,
    create_token,
    hash_password,
    require_user,
    verify_password,
)
from penr.db.engine import get_db
from penr.db.stores import ExplorationStore, TaskStore, resolve_task_by_param
from penr.orchestration.broadcast import get_broadcaster
from penr.orchestration.task_engine import get_engine

router = APIRouter(tags=["compat"])


class AuthInitBody(BaseModel):
    password: str = Field(min_length=6)
    username: str = "admin"


class AuthLoginBody(BaseModel):
    username: str
    password: str


class ChangePasswordBody(BaseModel):
    old_password: str
    new_password: str = Field(min_length=6)


class ControlBody(BaseModel):
    action: str  # pause | resume | rerun


# ---- auth aliases（web 调 /api/auth/*）----


@router.get("/api/auth/status")
async def auth_status() -> dict[str, Any]:
    n = await get_db().fetchval("SELECT COUNT(*) FROM rbac_users")
    return {"initialized": int(n or 0) > 0}


@router.post("/api/auth/init")
async def auth_init(body: AuthInitBody) -> dict[str, Any]:
    user = await bootstrap_admin(body.username, body.password)
    return {"token": create_token(user)}


@router.post("/api/auth/login")
async def auth_login(body: AuthLoginBody) -> dict[str, Any]:
    user = await authenticate(body.username, body.password)
    await get_db().execute(
        """
        INSERT INTO audit_logs (actor, category, action, result, message)
        VALUES ($1, 'auth', 'login', 'success', 'login ok')
        """,
        user["username"],
    )
    return {"token": create_token(user)}


@router.post("/api/auth/change-password")
async def auth_change_password(
    body: ChangePasswordBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if not verify_password(body.old_password, user["password_hash"]):
        raise HTTPException(401, detail="当前密码错误")
    db = get_db()
    await db.execute(
        """
        UPDATE rbac_users
        SET password_hash = $2, token_version = token_version + 1, updated_at = now()
        WHERE id = $1
        """,
        user["id"],
        hash_password(body.new_password),
    )
    row = await db.fetchrow("SELECT * FROM rbac_users WHERE id = $1", user["id"])
    assert row is not None
    return {"ok": True, "token": create_token(dict(row))}


# ---- task control ----


@router.post("/api/tasks/{task_id}/control")
async def control_task(
    task_id: int,
    body: ControlBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    task = await TaskStore(get_db()).get(task_id)
    if not task:
        raise HTTPException(404, detail="task not found")
    engine = get_engine()
    action = (body.action or "").strip().lower()
    if action == "pause":
        await engine.pause(task_id)
        return {"id": str(task_id), "paused": True}
    if action == "resume":
        # 确保环活着再 resume
        try:
            await engine.start(task_id)
        except Exception:  # noqa: BLE001
            await engine.resume(task_id)
        else:
            # start 对已运行任务会走 resume；若新建了环也已是 running
            await engine.resume(task_id)
        return {"id": str(task_id), "paused": False}
    if action == "rerun":
        await engine.rerun(task_id)
        return {"id": str(task_id), "paused": False, "status": "running"}
    raise HTTPException(400, detail="action must be pause|resume|rerun")


# ---- exploration graph / activity（?task=）----


@router.get("/api/exploration/graph")
async def exploration_graph(
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        return {"nodes": [], "edges": []}
    return await ExplorationStore(get_db(), t.exploration_id).graph_nodes_edges()


@router.get("/api/exploration/activity")
async def exploration_activity(
    task: str | None = None,
    intent: str | None = None,
    since: int = 0,
    limit: int = 300,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        return {"items": [], "cursor": 0}
    intent_id: int | None = None
    if intent:
        try:
            intent_id = int(intent)
        except ValueError:
            intent_id = None
    items, cursor = await ExplorationStore(get_db(), t.exploration_id).activity_list(
        since=since,
        limit=limit,
        intent_id=intent_id,
        include_detail=False,
    )
    return {"items": items, "cursor": cursor}


@router.get("/api/exploration/activity/stream")
async def exploration_activity_stream(
    request: Request,
    task: str | None = None,
    intent: str | None = None,
    since: int = 0,
    user: dict[str, Any] = Depends(require_user),
):
    """SSE：先回放 DB activity，再订阅 broadcaster；鉴权支持 ?token=。"""
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        raise HTTPException(404, detail="task not found")
    intent_id: int | None = None
    if intent:
        try:
            intent_id = int(intent)
        except ValueError:
            intent_id = None

    bc = get_broadcaster()
    q = await bc.subscribe(str(t.id))
    exp = ExplorationStore(get_db(), t.exploration_id)

    async def gen():
        nonlocal since
        try:
            items, cursor = await exp.activity_list(
                since=since, limit=2000, intent_id=intent_id, include_detail=True
            )
            for item in items:
                yield {"event": "message", "data": json.dumps(item, default=str)}
            if cursor > since:
                since = cursor
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=20.0)
                except TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                # 引擎 status 事件或 ActivityDTO
                if "seq" in evt:
                    seq = int(evt["seq"])
                    if seq <= since:
                        continue
                    if intent_id is not None and str(evt.get("intent_id") or "") != str(intent_id):
                        continue
                    since = seq
                    yield {"event": "message", "data": json.dumps(evt, default=str)}
                else:
                    # 非 activity 的引擎事件：包装成可忽略的旁路帧
                    yield {"event": "engine", "data": json.dumps(evt, default=str)}
        finally:
            await bc.unsubscribe(str(t.id), q)

    return EventSourceResponse(gen())


@router.get("/api/exploration/activity/{seq}")
async def exploration_activity_detail(
    seq: int,
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        raise HTTPException(404, detail="task not found")
    row = await get_db().fetchrow(
        """
        SELECT detail FROM activity
        WHERE id = $1 AND exploration_id = $2
        """,
        seq,
        t.exploration_id,
    )
    if not row:
        raise HTTPException(404, detail="activity not found")
    return {"detail": row["detail"] or ""}
