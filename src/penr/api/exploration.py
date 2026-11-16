"""M2：exploration 缺页（frontier/intents/findings/tokens）+ /api/stats。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.db.stores import (
    AssetStore,
    ExplorationStore,
    FindingsStore,
    _ts,
    resolve_task_by_param,
)
from penr.llmpool import LLMPool
from penr.orchestration.task_engine import get_engine

router = APIRouter(tags=["exploration"])


def _finding_dto(row: dict[str, Any], *, from_node: bool = False) -> dict[str, Any]:
    if from_node:
        payload = row.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        evidence = payload.get("evidence", "")
        if isinstance(evidence, (dict, list)):
            evidence = json.dumps(evidence, ensure_ascii=False)
        return {
            "id": str(row["id"]),
            "node_id": str(row["id"]),
            "vulnclass": payload.get("vulnclass") or "",
            "severity": payload.get("severity") or "",
            "summary": payload.get("summary") or "",
            "evidence": evidence or "",
            "task_id": str(row["task_id"]) if row.get("task_id") is not None else None,
            "task_description": row.get("task_description") or "",
            "ts": row.get("ts") or _ts(row.get("created_at")),
        }
    return {
        "id": str(row["id"]),
        "node_id": str(row["node_id"]) if row.get("node_id") is not None else None,
        "vulnclass": row.get("vulnclass") or "",
        "severity": row.get("severity") or "",
        "summary": row.get("summary") or "",
        "evidence": row.get("evidence") or "",
        "task_id": str(row["task_id"]) if row.get("task_id") is not None else None,
        "task_description": row.get("task_description") or "",
        "ts": _ts(row.get("created_at")),
    }


@router.get("/api/exploration/frontier")
async def frontier(
    task: str | None = None,
    limit: int = 100,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        return []
    return await ExplorationStore(get_db(), t.exploration_id).frontier(limit)


@router.get("/api/exploration/intents")
async def intents(
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        return []
    return await ExplorationStore(get_db(), t.exploration_id).list_by_kind("intent", 300)


@router.get("/api/exploration/findings")
async def findings(
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    db = get_db()
    if not task:
        rows = await FindingsStore(db).list_all(500)
        return [_finding_dto(r) for r in rows]
    t = await resolve_task_by_param(db, task)
    if not t:
        return []
    nodes = await ExplorationStore(db, t.exploration_id).list_by_kind("finding", 200)
    out = []
    for n in nodes:
        # list_by_kind 已是 DTO；补 payload 解析需要再读 — 用原始查询更稳
        out.append(
            {
                "id": n["id"],
                "node_id": n["id"],
                "vulnclass": "",
                "severity": "",
                "summary": "",
                "evidence": "",
                "task_id": str(t.id),
                "task_description": t.description,
                "ts": n.get("ts") or "",
            }
        )
        payload_s = n.get("payload") or ""
        if payload_s:
            try:
                p = json.loads(payload_s) if isinstance(payload_s, str) else payload_s
            except Exception:  # noqa: BLE001
                p = {}
            if isinstance(p, dict):
                out[-1]["vulnclass"] = p.get("vulnclass") or ""
                out[-1]["severity"] = p.get("severity") or ""
                out[-1]["summary"] = p.get("summary") or ""
                ev = p.get("evidence", "")
                out[-1]["evidence"] = (
                    json.dumps(ev, ensure_ascii=False) if isinstance(ev, (dict, list)) else (ev or "")
                )
    return out


@router.get("/api/exploration/tokens")
async def token_stats(
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    t = await resolve_task_by_param(get_db(), task)
    if not t:
        return {"workers": [], "total": {
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }}
    workers, total = await ExplorationStore(get_db(), t.exploration_id).token_stats_by_worker()
    return {"workers": workers, "total": total}


@router.get("/api/stats")
async def stats(
    task: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    assets = AssetStore(db)
    asset_counts = await assets.counts_by_type()
    asset_total = sum(asset_counts.values())
    llm_on = bool(await LLMPool(db).pick_active())
    out: dict[str, Any] = {
        "engine_mode": "idle",
        "llm_configured": llm_on,
        "assets": asset_total,
        "asset_counts": asset_counts,
        "traffic": 0,
        "traffic_enabled": False,
        "roe_enabled": False,
        "findings_confirmed": 0,
    }
    t = await resolve_task_by_param(db, task)
    if task and task not in ("", "active") and t is None:
        raise HTTPException(404, detail="task not found")
    if not t:
        return out

    exp = ExplorationStore(db, t.exploration_id)
    counts = await exp.exploration_counts()
    out["exploration"] = counts
    out["findings_confirmed"] = counts.get("findings", 0)

    engine = get_engine()
    rt = getattr(engine, "_tasks", {}).get(t.id)
    paused = bool(t.paused) or (rt.paused.is_set() if rt else False)
    running = rt is not None and rt.loop_task and not rt.loop_task.done() and not paused
    in_flight = counts.get("intents_running", 0)
    last_act = await db.fetchval(
        "SELECT EXTRACT(EPOCH FROM MAX(created_at))::bigint FROM activity WHERE exploration_id = $1",
        t.exploration_id,
    )
    last_activity = int(last_act or 0)
    import time

    stalled = bool(running and in_flight == 0 and last_activity > 0 and time.time() - last_activity > 60)
    if paused:
        out["engine_mode"] = "paused"
    elif stalled:
        out["engine_mode"] = "stalled"
    elif running:
        out["engine_mode"] = "exploring"
    else:
        out["engine_mode"] = "idle"

    out["active_task"] = {
        "id": str(t.id),
        "description": t.description,
        "goal": t.goal,
        "running": running,
        "paused": paused,
        "in_flight": in_flight,
        "last_activity": last_activity,
        "stalled": stalled,
        "goals_total": counts.get("goals_total", 0),
        "goals_met": counts.get("goals_met", 0),
    }
    return out
