"""对话页 API：conversations 持久化 + 异步消息轮次。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.crews.main_chat import run_main_chat
from penr.db.engine import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# 单会话单 in-flight turn（对齐 Go chatBusy）
_busy: dict[int, asyncio.Task[Any]] = {}
_cancel: dict[int, asyncio.Event] = {}


class CreateConversationBody(BaseModel):
    agent_key: str = "chat"
    title: str = ""
    llm_profile_id: int | None = None


class RenameBody(BaseModel):
    title: str


class ProfileBody(BaseModel):
    llm_profile_id: int | None = None


class SendMessageBody(BaseModel):
    message: str = Field(min_length=1)


def _conv_dto(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "agent_key": row["agent_key"],
        "title": row["title"] or "",
        "llm_profile_id": row["llm_profile_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _activity_dto(row: Any) -> dict[str, Any]:
    return {
        "seq": int(row["id"]),
        "worker": row["worker"] or "",
        "ts": row["created_at"],
        "kind": row["kind"] or "",
        "tool": row["tool"] or "",
        "tool_use_id": row["tool_use_id"] or "",
        "is_error": bool(row["is_error"]),
        "summary": row["summary"] or "",
        "detail": (row["detail"] or "")[:500] if row["detail"] else None,
        "input_tokens": row["input_tokens"],
        "output_tokens": row["output_tokens"],
        "cache_read_tokens": row["cache_read_tokens"],
        "cache_write_tokens": row["cache_write_tokens"],
    }


@router.get("")
async def list_conversations(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await get_db().fetch(
        "SELECT * FROM conversations ORDER BY updated_at DESC, id DESC LIMIT 200"
    )
    return {"conversations": [_conv_dto(r) for r in rows]}


@router.post("")
async def create_conversation(
    body: CreateConversationBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        INSERT INTO conversations (agent_key, title, llm_profile_id)
        VALUES ($1, $2, $3)
        RETURNING *
        """,
        body.agent_key or "chat",
        body.title or "",
        body.llm_profile_id,
    )
    assert row is not None
    return _conv_dto(row)


@router.patch("/{conv_id}")
async def rename_conversation(
    conv_id: int,
    body: RenameBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    n = await get_db().execute(
        "UPDATE conversations SET title = $2 WHERE id = $1",
        conv_id,
        body.title,
    )
    if n == "UPDATE 0":
        raise HTTPException(404, "conversation not found")
    return {"ok": True}


@router.patch("/{conv_id}/profile")
async def update_profile(
    conv_id: int,
    body: ProfileBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    n = await get_db().execute(
        "UPDATE conversations SET llm_profile_id = $2 WHERE id = $1",
        conv_id,
        body.llm_profile_id,
    )
    if n == "UPDATE 0":
        raise HTTPException(404, "conversation not found")
    return {"ok": True}


@router.delete("/{conv_id}")
async def delete_conversation(
    conv_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, int]:
    task = _busy.pop(conv_id, None)
    if task and not task.done():
        ev = _cancel.setdefault(conv_id, asyncio.Event())
        ev.set()
        task.cancel()
    await get_db().execute("DELETE FROM conversations WHERE id = $1", conv_id)
    return {"deleted": conv_id}


@router.get("/{conv_id}/messages")
async def list_messages(
    conv_id: int,
    since: int = 0,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT id FROM conversations WHERE id = $1", conv_id)
    if not row:
        raise HTTPException(404, "conversation not found")
    rows = await get_db().fetch(
        """
        SELECT * FROM conversation_activities
        WHERE conversation_id = $1 AND id > $2
        ORDER BY id ASC
        LIMIT 500
        """,
        conv_id,
        since,
    )
    items = [_activity_dto(r) for r in rows]
    cursor = items[-1]["seq"] if items else since
    running = conv_id in _busy and not _busy[conv_id].done()
    return {"items": items, "cursor": cursor, "running": running}


@router.get("/{conv_id}/messages/{seq}")
async def message_detail(
    conv_id: int,
    seq: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    row = await get_db().fetchrow(
        """
        SELECT detail FROM conversation_activities
        WHERE conversation_id = $1 AND id = $2
        """,
        conv_id,
        seq,
    )
    if not row:
        raise HTTPException(404, "message not found")
    return {"detail": row["detail"] or ""}


@router.post("/{conv_id}/messages", status_code=202)
async def send_message(
    conv_id: int,
    body: SendMessageBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    db = get_db()
    row = await db.fetchrow("SELECT * FROM conversations WHERE id = $1", conv_id)
    if not row:
        raise HTTPException(404, "conversation not found")
    if conv_id in _busy and not _busy[conv_id].done():
        raise HTTPException(409, "conversation busy")

    await db.execute(
        """
        INSERT INTO conversation_activities
            (conversation_id, worker, kind, summary, detail)
        VALUES ($1, 'user', 'message', $2, $3)
        """,
        conv_id,
        body.message[:500],
        body.message,
    )
    # 若无标题，用首条消息截断
    if not (row["title"] or "").strip():
        await db.execute(
            "UPDATE conversations SET title = $2 WHERE id = $1",
            conv_id,
            body.message[:60],
        )

    cancel = asyncio.Event()
    _cancel[conv_id] = cancel

    async def _run() -> None:
        try:
            if cancel.is_set():
                return
            result = await run_main_chat(
                message=body.message,
                mode="chat",
            )
            reply = str(result.get("reply") or "")
            await db.execute(
                """
                INSERT INTO conversation_activities
                    (conversation_id, worker, kind, summary, detail)
                VALUES ($1, $2, 'message', $3, $4)
                """,
                conv_id,
                row["agent_key"] or "chat",
                reply[:500],
                reply,
            )
            await db.execute(
                "UPDATE conversations SET updated_at = now() WHERE id = $1",
                conv_id,
            )
        except asyncio.CancelledError:
            await db.execute(
                """
                INSERT INTO conversation_activities
                    (conversation_id, worker, kind, summary, is_error)
                VALUES ($1, 'system', 'status', 'stopped', false)
                """,
                conv_id,
            )
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("conversation turn failed: %s", exc)
            await db.execute(
                """
                INSERT INTO conversation_activities
                    (conversation_id, worker, kind, summary, detail, is_error)
                VALUES ($1, 'system', 'error', $2, $3, true)
                """,
                conv_id,
                f"error: {exc}"[:500],
                str(exc)[:4000],
            )
        finally:
            _busy.pop(conv_id, None)
            _cancel.pop(conv_id, None)

    _busy[conv_id] = asyncio.create_task(_run())
    return {"status": "accepted"}


@router.post("/{conv_id}/stop")
async def stop_conversation(
    conv_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    task = _busy.get(conv_id)
    if not task or task.done():
        return {"status": "idle"}
    ev = _cancel.setdefault(conv_id, asyncio.Event())
    ev.set()
    task.cancel()
    return {"status": "stopping"}
