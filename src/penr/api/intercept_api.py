"""M5：intercept rules CRUD + /decide 别名 + pending/history/tool-config。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.db.stores import _ts, get_setting, set_setting
from penr.guard.guard import list_pending, resolve_pending

router = APIRouter(tags=["intercept"])


class RuleBody(BaseModel):
    name: str
    enabled: bool = True
    priority: int = 0
    match_target: str = "tool_name"
    match_type: str = "string"
    pattern: str
    action: str = "ask"
    message: str = ""
    timeout_enabled: bool = True
    timeout_seconds: int = 60
    timeout_action: str = "deny"


class ToggleBody(BaseModel):
    enabled: bool


class DecideBody(BaseModel):
    decision: str  # allowed | denied


class ToolConfigBody(BaseModel):
    enabled_tools: list[str] = Field(default_factory=list)


def _rule_dto(row: Any) -> dict[str, Any]:
    d = dict(row)
    d["created_at"] = _ts(d.get("created_at"))
    d["updated_at"] = _ts(d.get("updated_at"))
    return d


@router.get("/api/intercept/rules")
async def list_rules(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await get_db().fetch(
        "SELECT * FROM intercept_rules ORDER BY priority DESC, id ASC"
    )
    return {"rules": [_rule_dto(r) for r in rows]}


@router.post("/api/intercept/rules")
async def create_rule(
    body: RuleBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        INSERT INTO intercept_rules
            (name, enabled, priority, match_target, match_type, pattern, action,
             message, timeout_enabled, timeout_seconds, timeout_action)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        RETURNING *
        """,
        body.name,
        body.enabled,
        body.priority,
        body.match_target,
        body.match_type,
        body.pattern,
        body.action,
        body.message,
        body.timeout_enabled,
        body.timeout_seconds,
        body.timeout_action,
    )
    assert row is not None
    return _rule_dto(row)


@router.put("/api/intercept/rules/{rule_id}")
async def update_rule(
    rule_id: int,
    body: RuleBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        UPDATE intercept_rules SET
            name=$2, enabled=$3, priority=$4, match_target=$5, match_type=$6,
            pattern=$7, action=$8, message=$9, timeout_enabled=$10,
            timeout_seconds=$11, timeout_action=$12, updated_at=now()
        WHERE id=$1
        RETURNING *
        """,
        rule_id,
        body.name,
        body.enabled,
        body.priority,
        body.match_target,
        body.match_type,
        body.pattern,
        body.action,
        body.message,
        body.timeout_enabled,
        body.timeout_seconds,
        body.timeout_action,
    )
    if not row:
        raise HTTPException(404, detail="rule not found")
    return _rule_dto(row)


@router.delete("/api/intercept/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    r = await get_db().execute("DELETE FROM intercept_rules WHERE id = $1", rule_id)
    try:
        n = int(r.split()[-1])
    except Exception:  # noqa: BLE001
        n = 0
    return {"deleted": n}


@router.post("/api/intercept/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: int,
    body: ToggleBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        UPDATE intercept_rules SET enabled = $2, updated_at = now()
        WHERE id = $1 RETURNING enabled
        """,
        rule_id,
        body.enabled,
    )
    if not row:
        raise HTTPException(404, detail="rule not found")
    return {"ok": True, "enabled": bool(row["enabled"])}


@router.get("/api/intercept/pending")
async def intercept_pending_list(
    task_id: str | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return {"pending": await list_pending(task_id)}


@router.get("/api/intercept/pending/{pending_id}")
async def intercept_get_one(
    pending_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        "SELECT * FROM intercept_pending WHERE id = $1", pending_id
    )
    if not row:
        raise HTTPException(404, detail="not found")
    d = dict(row)
    d["created_at"] = _ts(d.get("created_at"))
    d["decided_at"] = _ts(d.get("decided_at")) if d.get("decided_at") else None
    return d


@router.post("/api/intercept/pending/{pending_id}/decide")
async def intercept_decide(
    pending_id: int,
    body: DecideBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    decision = (body.decision or "").lower()
    if decision not in ("allowed", "denied"):
        raise HTTPException(400, detail="decision must be allowed|denied")
    ok = await resolve_pending(pending_id, allow=(decision == "allowed"))
    if not ok:
        raise HTTPException(404, detail="pending not found or already decided")
    return {"ok": True}


@router.get("/api/intercept/history")
async def intercept_history(
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    rows = await get_db().fetch(
        """
        SELECT * FROM intercept_pending
        WHERE status <> 'pending'
        ORDER BY COALESCE(decided_at, created_at) DESC
        LIMIT 200
        """
    )
    items = []
    for r in rows:
        d = dict(r)
        d["created_at"] = _ts(d.get("created_at"))
        d["decided_at"] = _ts(d.get("decided_at")) if d.get("decided_at") else None
        items.append(d)
    return {"items": items}


@router.get("/api/intercept/task/{task_id}")
async def intercept_task_items(
    task_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    rows = await get_db().fetch(
        """
        SELECT * FROM intercept_pending WHERE task_id = $1
        ORDER BY created_at DESC LIMIT 200
        """,
        task_id,
    )
    items = []
    for r in rows:
        d = dict(r)
        d["created_at"] = _ts(d.get("created_at"))
        d["decided_at"] = _ts(d.get("decided_at")) if d.get("decided_at") else None
        items.append(d)
    return {"items": items}


@router.get("/api/intercept/tool-config")
async def get_tool_config(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    raw = await get_setting(get_db(), "intercept_enabled_tools", '["bash"]')
    try:
        tools = json.loads(raw)
        if not isinstance(tools, list):
            tools = ["bash"]
    except Exception:  # noqa: BLE001
        tools = ["bash"]
    return {"enabled_tools": tools}


@router.put("/api/intercept/tool-config")
async def set_tool_config(
    body: ToolConfigBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    await set_setting(
        get_db(),
        "intercept_enabled_tools",
        json.dumps(body.enabled_tools, ensure_ascii=False),
    )
    return {"ok": True, "enabled_tools": body.enabled_tools}
