"""Traffic + finding↔traffic 证据链 API（从 app.py 薄挂载）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.config import get_settings
from penr.db.engine import get_db
from penr.db.stores import FindingsStore
from penr.traffic.index import (
    get_exchange,
    get_exchange_bodies,
    list_exchanges,
    meta_by_ids,
    search_body,
)

router = APIRouter(tags=["traffic"])


class LinkTrafficBody(BaseModel):
    exchange_ids: list[str] = Field(default_factory=list)


@router.get("/api/traffic")
async def traffic_list(
    host: str = "",
    method: str = "",
    q: str = "",
    task: str = "",
    page: int = 0,
    size: int = 100,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """分页流量列表（host/task/method/q 过滤）。"""
    return await list_exchanges(
        host=host or None,
        method=method or None,
        q=q or None,
        task_id=task or None,
        page=page,
        size=size,
    )


@router.get("/api/traffic/exchange")
async def traffic_exchange(
    id: str = Query(..., description="exchange id"),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    """单条报文原文 {req, resp}。"""
    body = await get_exchange_bodies(id)
    if not body:
        raise HTTPException(404, "not found")
    return body


@router.get("/api/traffic/meta")
async def traffic_meta(
    id: str = Query(..., description="exchange id"),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    metas = await meta_by_ids([id])
    if not metas:
        raise HTTPException(404, "not found")
    return metas[0]


@router.get("/api/traffic/search")
async def traffic_search(
    q: str = Query(...),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return {"exchanges": await search_body(q)}


@router.get("/api/traffic/{exchange_id}")
async def traffic_get(
    exchange_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_exchange(exchange_id)
    if not row:
        raise HTTPException(404, "not found")
    return row


@router.post("/api/findings/{finding_id}/traffic")
async def link_finding_traffic(
    finding_id: int,
    body: LinkTrafficBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    store = FindingsStore(get_db())
    node_id = await store.resolve_node_id(finding_id)
    if node_id is None:
        raise HTTPException(404, "finding not found")
    linked = await store.link_traffic(node_id, body.exchange_ids, source="manual")
    return {"ok": True, "linked": linked, "node_id": node_id}


@router.get("/api/findings/{finding_id}/traffic")
async def list_finding_traffic(
    finding_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    store = FindingsStore(get_db())
    node_id = await store.resolve_node_id(finding_id)
    if node_id is None:
        raise HTTPException(404, "finding not found")
    ids = await store.list_traffic_ids(node_id)
    metas = await meta_by_ids(ids)
    return {"exchanges": metas, "node_id": node_id}


@router.delete("/api/findings/{finding_id}/traffic/{exchange_id}")
async def unlink_finding_traffic(
    finding_id: int,
    exchange_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    store = FindingsStore(get_db())
    node_id = await store.resolve_node_id(finding_id)
    if node_id is None:
        raise HTTPException(404, "finding not found")
    await store.unlink_traffic(node_id, exchange_id)
    return {"ok": True}


@router.get("/api/traffic/status")
async def traffic_status(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    settings = get_settings()
    return {
        "enabled": True,
        "proxy": settings.traffic_proxy_addr,
    }
