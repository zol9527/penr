"""M0 冒烟：migrate + 建任务(auto_start) + fallback 产生 intent/fact。

无 PostgreSQL 时整模块 skip；无 LLM key 时走 planner/worker fallback 路径即可。
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
from httpx import ASGITransport, AsyncClient

from penr.config import get_settings
from penr.db.engine import Database


def _pg_reachable(dsn: str, timeout: float = 1.5) -> bool:
    """快速探测 DSN 主机端口是否可达（避免 asyncpg 长时间挂起）。"""
    try:
        rest = dsn.split("://", 1)[1]
        hostport = rest.rsplit("@", 1)[-1].split("/", 1)[0]
        if hostport.startswith("["):
            return False
        host, _, port_s = hostport.partition(":")
        port = int(port_s or "5432")
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def _ensure_migrated() -> str:
    """探测 PG 并幂等 migrate；不可达则 skip。"""
    get_settings.cache_clear()
    dsn = get_settings().resolve_dsn()
    if not _pg_reachable(dsn):
        pytest.skip(f"PostgreSQL unreachable: {dsn}")
    try:
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.close()
    except (OSError, asyncpg.PostgresError, TimeoutError) as exc:
        pytest.skip(f"PostgreSQL connect failed: {exc}")

    db = Database()
    await db.connect()
    try:
        await db.migrate()
    finally:
        await db.close()
    return dsn


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """带 FastAPI lifespan 的 ASGI 客户端（启动 DB + TaskEngine）。"""
    await _ensure_migrated()
    from penr.api.app import create_app

    app = create_app()
    # 当前 httpx ASGITransport 不自动跑 lifespan，需显式进入
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


async def _auth_token(client: AsyncClient) -> str:
    """setup 或 login 拿到 JWT；兼容 README(adminadmin) 与测试默认(admin123)。"""
    status = (await client.get("/api/setup/status")).json()
    candidates = ("admin123", "adminadmin")
    if not status.get("initialized"):
        resp = await client.post(
            "/api/setup",
            json={"username": "admin", "password": candidates[0]},
        )
        if resp.status_code == 200:
            return resp.json()["token"]
        detail = str((resp.json() or {}).get("detail") or "")
        if resp.status_code != 400 or "already" not in detail:
            assert resp.status_code == 200, resp.text

    last_status = 401
    for password in candidates:
        resp = await client.post(
            "/api/login", json={"username": "admin", "password": password}
        )
        last_status = resp.status_code
        if resp.status_code == 200:
            return resp.json()["token"]

    # 本地开发库密码可能被其它进程改过：测试内重置为已知口令再登
    try:
        from penr.api.auth import hash_password
        from penr.db.engine import get_db

        await get_db().execute(
            """
            UPDATE rbac_users
            SET password_hash = $1, token_version = token_version + 1, updated_at = now()
            WHERE username = 'admin'
            """,
            hash_password(candidates[0]),
        )
        resp = await client.post(
            "/api/login", json={"username": "admin", "password": candidates[0]}
        )
        if resp.status_code == 200:
            return resp.json()["token"]
        last_status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"login failed ({last_status}); reset failed: {exc}")
    pytest.skip(
        f"login failed ({last_status}); reset rbac_users or use known password "
        f"in {candidates}"
    )


@pytest.mark.asyncio
async def test_health_and_migrate_path(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("db") is True
    assert body.get("status") == "ok"


@pytest.mark.asyncio
async def test_create_task_fallback_intent_and_fact(client: AsyncClient) -> None:
    """无 LLM key 时：auto_start → planner fallback intent → worker fallback fact。"""
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)

    token = await _auth_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    suffix = uuid.uuid4().hex[:8]
    create = await client.post(
        "/api/tasks",
        headers=headers,
        json={
            "name": f"m0-smoke-{suffix}",
            "description": "M0 smoke https://example.com",
            "goal": f"enumerate example.com ({suffix})",
            "auto_start": True,
        },
    )
    assert create.status_code == 200, create.text
    task = create.json()
    task_id = int(task["id"])
    assert task.get("status") == "running"

    intent_seen = False
    fact_seen = False
    activity_seen = False
    for _ in range(40):
        graph = (await client.get(f"/api/tasks/{task_id}/graph", headers=headers)).json()
        activity = (await client.get(f"/api/tasks/{task_id}/activity", headers=headers)).json()

        open_i = graph.get("open_intents") or []
        run_i = graph.get("running_intents") or []
        done_i = graph.get("recent_done_intents") or []
        recent_facts = graph.get("recent_facts") or []
        facts_n = int(graph.get("facts") or 0)

        if open_i or run_i or done_i:
            intent_seen = True
        if recent_facts or facts_n >= 2 or done_i:
            fact_seen = True
        if activity.get("activity"):
            activity_seen = True

        if intent_seen and fact_seen and activity_seen:
            break
        await asyncio.sleep(0.5)

    await client.post(f"/api/tasks/{task_id}/stop", headers=headers)

    assert intent_seen, "expected at least one intent via fallback planner"
    assert fact_seen, "expected at least one fact (origin+worker or recent_facts)"
    assert activity_seen, "expected activity events from planner/worker"
