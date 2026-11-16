"""MCP 管理 API：CRUD / tools / health / stats / refresh。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.tools.mcp import (
    health_check_mcp,
    mcp_stats_summary,
    refresh_mcp_cache,
)

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MCPServerBody(BaseModel):
    id: int = 0
    name: str = Field(min_length=1)
    transport: str = "http"
    command: str | None = None
    args: list[Any] = Field(default_factory=list)
    env: dict[str, Any] = Field(default_factory=dict)
    headers: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None
    description: str = ""
    timeout_seconds: int = 30
    enabled: bool = True


def _server_dto(row: Any, tools: list[str] | None = None) -> dict[str, Any]:
    args = row["args"]
    env = row["env"]
    headers = row["headers"]
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = []
    if isinstance(env, str):
        try:
            env = json.loads(env)
        except json.JSONDecodeError:
            env = {}
    if isinstance(headers, str):
        try:
            headers = json.loads(headers)
        except json.JSONDecodeError:
            headers = {}
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "transport": row["transport"],
        "command": row["command"] or "",
        "args": args or [],
        "env": env or {},
        "headers": headers or {},
        "url": row["url"] or "",
        "description": row["description"] or "",
        "timeout_seconds": int(row["timeout_seconds"] or 30),
        "enabled": bool(row["enabled"]),
        "status": row["status"] or "",
        "status_at": row["status_at"],
        "status_error": row["status_error"] or "",
        "tools": tools or [],
    }


@router.get("")
async def list_mcp(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    db = get_db()
    rows = await db.fetch("SELECT * FROM mcp_servers ORDER BY id")
    out = []
    for r in rows:
        tools = await db.fetch(
            "SELECT tool_name FROM mcp_tools_cache WHERE server_id = $1 ORDER BY tool_name",
            int(r["id"]),
        )
        out.append(_server_dto(r, [t["tool_name"] for t in tools]))
    return {"servers": out}


@router.post("")
async def save_mcp(
    body: MCPServerBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, int]:
    if body.transport not in ("http", "stdio"):
        raise HTTPException(400, "transport must be http|stdio")
    db = get_db()
    if body.id:
        await db.execute(
            """
            UPDATE mcp_servers SET
                name=$2, transport=$3, command=$4, args=$5::jsonb, env=$6::jsonb,
                headers=$7::jsonb, url=$8, description=$9, timeout_seconds=$10, enabled=$11
            WHERE id=$1
            """,
            body.id,
            body.name,
            body.transport,
            body.command,
            json.dumps(body.args),
            json.dumps(body.env),
            json.dumps(body.headers),
            body.url,
            body.description,
            body.timeout_seconds,
            body.enabled,
        )
        sid = body.id
    else:
        row = await db.fetchrow(
            """
            INSERT INTO mcp_servers
                (name, transport, command, args, env, headers, url, description, timeout_seconds, enabled)
            VALUES ($1,$2,$3,$4::jsonb,$5::jsonb,$6::jsonb,$7,$8,$9,$10)
            RETURNING id
            """,
            body.name,
            body.transport,
            body.command,
            json.dumps(body.args),
            json.dumps(body.env),
            json.dumps(body.headers),
            body.url,
            body.description,
            body.timeout_seconds,
            body.enabled,
        )
        assert row is not None
        sid = int(row["id"])
    # 新建/更新后尝试发现工具（失败不阻断保存）
    try:
        await refresh_mcp_cache(db, sid)
    except Exception:  # noqa: BLE001
        pass
    return {"id": sid}


@router.delete("/{server_id}")
async def delete_mcp(
    server_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, int]:
    await get_db().execute("DELETE FROM mcp_servers WHERE id = $1", server_id)
    return {"deleted": server_id}


@router.get("/stats")
async def mcp_stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return await mcp_stats_summary(get_db())


@router.get("/{server_id}/tools")
async def mcp_tools(
    server_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    rows = await get_db().fetch(
        """
        SELECT tool_name AS name, description
        FROM mcp_tools_cache WHERE server_id = $1 ORDER BY tool_name
        """,
        server_id,
    )
    return {"tools": [dict(r) for r in rows]}


@router.post("/{server_id}/health")
async def mcp_health(
    server_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        return await health_check_mcp(get_db(), server_id)
    except ValueError:
        raise HTTPException(404, "mcp server not found") from None


@router.post("/{server_id}/refresh")
async def mcp_refresh(
    server_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    try:
        tools = await refresh_mcp_cache(get_db(), server_id)
    except ValueError:
        raise HTTPException(404, "mcp server not found") from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)[:300]) from None
    return {
        "tools": [
            {"name": t.get("name"), "description": t.get("description") or ""} for t in tools
        ]
    }
