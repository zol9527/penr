"""PreToolUse 安全审计 + intercept 规则（allow/deny/ask）。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from penr.db.engine import get_db


@dataclass
class ToolDecision:
    action: str  # allow | deny | ask
    message: str = ""
    pending_id: int | None = None
    rule_id: int | None = None


# 内置危险模式（规则表为空时的兜底）
_DEFAULT_DENY = [
    (r"rm\s+-rf\s+/", "destructive rm -rf /"),
    (r"mkfs\.", "filesystem format"),
    (r":\(\)\s*\{\s*:\|:\s*&\s*\}\s*;", "fork bomb"),
]


async def check_tool(
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    task_id: str = "",
    agent_name: str = "",
    conversation_id: int | None = None,
) -> ToolDecision:
    db = get_db()
    try:
        rules = await db.fetch(
            """
            SELECT id, name, match_target, match_type, pattern, action, message,
                   timeout_enabled, timeout_seconds, timeout_action
            FROM intercept_rules
            WHERE enabled = true
            ORDER BY priority DESC, id ASC
            """
        )
    except Exception:  # noqa: BLE001 — DB 未就绪时走内置规则
        rules = []

    blob = json.dumps(tool_input, ensure_ascii=False)
    command = str(tool_input.get("command") or "")

    for rule in rules:
        target = command if rule["match_target"] == "tool_input" else tool_name
        if rule["match_target"] == "tool_input" and "command" not in tool_input:
            target = blob
        matched = False
        if rule["match_type"] == "string":
            matched = rule["pattern"] in target
        else:
            try:
                matched = re.search(rule["pattern"], target) is not None
            except re.error:
                matched = False
        if not matched:
            continue
        action = rule["action"]
        if action == "ask":
            pending_id = await _create_pending(
                rule_id=int(rule["id"]),
                task_id=task_id,
                agent_name=agent_name,
                tool_name=tool_name,
                tool_input=tool_input,
                conversation_id=conversation_id,
            )
            return ToolDecision(
                action="ask",
                message=rule["message"] or f"HITL required by rule {rule['name']}",
                pending_id=pending_id,
                rule_id=int(rule["id"]),
            )
        return ToolDecision(
            action=action,
            message=rule["message"] or f"matched rule {rule['name']}",
            rule_id=int(rule["id"]),
        )

    # 内置兜底
    for pattern, msg in _DEFAULT_DENY:
        if re.search(pattern, command):
            return ToolDecision(action="deny", message=msg)

    return ToolDecision(action="allow")


async def _create_pending(
    *,
    rule_id: int | None,
    task_id: str,
    agent_name: str,
    tool_name: str,
    tool_input: dict[str, Any],
    conversation_id: int | None,
) -> int | None:
    db = get_db()
    try:
        row = await db.fetchrow(
            """
            INSERT INTO intercept_pending
                (rule_id, conversation_id, task_id, agent_name, tool_name, tool_input, status)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, 'pending')
            RETURNING id
            """,
            rule_id,
            conversation_id,
            task_id,
            agent_name,
            tool_name,
            json.dumps(tool_input, ensure_ascii=False),
        )
        return int(row["id"]) if row else None
    except Exception:  # noqa: BLE001
        return None


async def resolve_pending(pending_id: int, *, allow: bool) -> bool:
    db = get_db()
    status = "allowed" if allow else "denied"
    result = await db.execute(
        """
        UPDATE intercept_pending
        SET status = $2, decided_at = now()
        WHERE id = $1 AND status = 'pending'
        """,
        pending_id,
        status,
    )
    return result.endswith("1")


async def list_pending(task_id: str | None = None) -> list[dict[str, Any]]:
    db = get_db()
    if task_id:
        rows = await db.fetch(
            """
            SELECT id, rule_id, task_id, agent_name, tool_name, tool_input, status, created_at
            FROM intercept_pending
            WHERE status = 'pending' AND task_id = $1
            ORDER BY created_at DESC
            """,
            task_id,
        )
    else:
        rows = await db.fetch(
            """
            SELECT id, rule_id, task_id, agent_name, tool_name, tool_input, status, created_at
            FROM intercept_pending
            WHERE status = 'pending'
            ORDER BY created_at DESC
            """
        )
    return [dict(r) for r in rows]
