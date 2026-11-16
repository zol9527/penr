"""知识库 API：CRUD / POST search / upload / config；promote 保留。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.knowledge.promote import KnowledgePromoter
from penr.knowledge.service import KnowledgeService

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class KnowledgeDocBody(BaseModel):
    title: str = ""
    content: str = ""
    source: str = "manual"
    scope: str = "task"
    task_id: int | None = None
    category: str = ""


class KnowledgeSearchBody(BaseModel):
    query: str = Field(min_length=1)
    task_id: int = 0
    top_k: int = 5


class KnowledgeConfigBody(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    dim: int | None = None


def _doc_dto(row: dict[str, Any], *, with_content: bool = False) -> dict[str, Any]:
    out = {
        "id": int(row["id"]),
        "scope": row.get("scope") or "",
        "task_id": row.get("task_id"),
        "category": row.get("category") or "",
        "title": row.get("title") or "",
        "source": row.get("source") or "",
        "file_name": row.get("file_name") or "",
        "bytes": int(row.get("bytes") or 0),
        "indexed": bool(row.get("indexed")),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }
    if with_content:
        out["content"] = row.get("content") or ""
    return out


@router.get("")
async def list_docs(
    scope: str = "",
    task: int = 0,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    # 对齐 web：直接返回数组
    docs = await KnowledgeService(get_db()).list_docs(
        scope=scope or None,
        task_id=task or None,
    )
    return [_doc_dto(d) for d in docs]


@router.post("")
async def create_doc(
    body: KnowledgeDocBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if body.scope == "task" and body.task_id is None:
        raise HTTPException(400, "task scope requires task_id")
    svc = KnowledgeService(get_db())
    doc_id = await svc.ingest(
        body.title or "untitled",
        body.content,
        source=body.source,
        scope=body.scope,
        task_id=body.task_id,
        category=body.category,
    )
    row = await get_db().fetchrow("SELECT * FROM knowledge_docs WHERE id = $1", doc_id)
    assert row is not None
    return _doc_dto(dict(row), with_content=True)


@router.get("/categories")
async def categories(
    task: int = 0,
    user: dict[str, Any] = Depends(require_user),
) -> list[str]:
    if task:
        rows = await get_db().fetch(
            """
            SELECT DISTINCT category FROM knowledge_docs
            WHERE category <> '' AND task_id = $1
            ORDER BY category
            """,
            task,
        )
    else:
        rows = await get_db().fetch(
            """
            SELECT DISTINCT category FROM knowledge_docs
            WHERE category <> ''
            ORDER BY category
            """
        )
    return [str(r["category"]) for r in rows]


@router.post("/search")
async def search_post(
    body: KnowledgeSearchBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    svc = KnowledgeService(get_db())
    task_id = body.task_id or None
    result = await svc.search(
        body.query,
        task_id=task_id,
        layers=["task", "longterm"] if task_id else ["longterm"],
        limit=body.top_k,
    )
    hits = []
    for h in result.get("hits") or []:
        hits.append(
            {
                "doc_id": h.get("id"),
                "title": h.get("title") or "",
                "category": h.get("category") or "",
                "scope": h.get("scope") or h.get("layer") or "",
                "content": h.get("snippet") or h.get("content") or "",
                "score": float(h.get("score") or 0),
                "layer": h.get("layer"),
                "uri": h.get("uri"),
            }
        )
    # 检索日志
    await get_db().execute(
        """
        INSERT INTO knowledge_retrieval_logs
            (task_id, agent_key, query, scope, mode, top_k, hits, doc_ids)
        VALUES ($1, 'api', $2, $3, 'keyword', $4, $5, $6)
        """,
        task_id,
        body.query[:500],
        "task,longterm",
        body.top_k,
        len(hits),
        [int(h["doc_id"]) for h in hits if h.get("doc_id")],
    )
    return {"mode": "keyword+openviking", "hits": hits}


@router.get("/logs")
async def logs(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    rows = await get_db().fetch(
        "SELECT * FROM knowledge_retrieval_logs ORDER BY id DESC LIMIT 100"
    )
    return [dict(r) for r in rows]


@router.get("/config")
async def get_config(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT * FROM knowledge_config WHERE id = 1")
    if not row:
        await get_db().execute("INSERT INTO knowledge_config (id) VALUES (1) ON CONFLICT DO NOTHING")
        row = await get_db().fetchrow("SELECT * FROM knowledge_config WHERE id = 1")
    assert row is not None
    g = await get_db().fetchval(
        "SELECT COUNT(*) FROM knowledge_docs WHERE scope = 'global'"
    )
    t = await get_db().fetchval("SELECT COUNT(*) FROM knowledge_docs WHERE scope = 'task'")
    return {
        "enabled": bool(row["enabled"]),
        "base_url": row["base_url"] or "",
        "model": row["model"] or "",
        "dim": int(row["dim"] or 0),
        "has_key": bool(row["api_key"]),
        "vector_available": False,
        "semantic_ready": bool(row["enabled"] and row["api_key"]),
        "docs_global": int(g or 0),
        "docs_task": int(t or 0),
    }


@router.put("/config")
async def put_config(
    body: KnowledgeConfigBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    db = get_db()
    await db.execute("INSERT INTO knowledge_config (id) VALUES (1) ON CONFLICT DO NOTHING")
    cur = await db.fetchrow("SELECT * FROM knowledge_config WHERE id = 1")
    assert cur is not None
    enabled = body.enabled if body.enabled is not None else cur["enabled"]
    base_url = body.base_url if body.base_url is not None else cur["base_url"]
    model = body.model if body.model is not None else cur["model"]
    api_key = body.api_key if body.api_key is not None else cur["api_key"]
    dim = body.dim if body.dim is not None else cur["dim"]
    await db.execute(
        """
        UPDATE knowledge_config
        SET enabled=$1, base_url=$2, model=$3, api_key=$4, dim=$5, updated_at=now()
        WHERE id=1
        """,
        enabled,
        base_url,
        model,
        api_key,
        dim,
    )
    return {"ok": True}


@router.post("/config/test")
async def test_embedder(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # 最小实现：回报配置 dim；真正 embedding 调用留给后续
    row = await get_db().fetchrow("SELECT dim, enabled, api_key FROM knowledge_config WHERE id = 1")
    if not row or not row["api_key"]:
        raise HTTPException(400, "embedder not configured")
    return {"ok": True, "dim": int(row["dim"] or 0)}


@router.post("/reindex")
async def reindex(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    # 无 pgvector 时：标记 indexed=false 的文档计数返回（长期层靠 OV promote）
    n = await get_db().fetchval("SELECT COUNT(*) FROM knowledge_docs")
    await get_db().execute("UPDATE knowledge_docs SET indexed = false")
    return {"ok": True, "indexed": int(n or 0)}


@router.post("/upload")
async def upload_doc(
    file: UploadFile = File(...),
    scope: str = Form("task"),
    task_id: int = Form(0),
    category: str = Form(""),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    title = Path(file.filename or "upload.txt").name
    if scope == "task" and not task_id:
        raise HTTPException(400, "task scope requires task_id")
    svc = KnowledgeService(get_db())
    doc_id = await svc.ingest(
        title,
        text,
        source="upload",
        scope=scope,
        task_id=task_id or None,
        category=category,
    )
    await get_db().execute(
        "UPDATE knowledge_docs SET file_name = $2, bytes = $3 WHERE id = $1",
        doc_id,
        title,
        len(raw),
    )
    row = await get_db().fetchrow("SELECT * FROM knowledge_docs WHERE id = $1", doc_id)
    assert row is not None
    return _doc_dto(dict(row), with_content=False)


@router.get("/{doc_id}")
async def get_doc(doc_id: int, user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT * FROM knowledge_docs WHERE id = $1", doc_id)
    if not row:
        raise HTTPException(404, "not found")
    return _doc_dto(dict(row), with_content=True)


@router.put("/{doc_id}")
async def update_doc(
    doc_id: int,
    body: KnowledgeDocBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT id FROM knowledge_docs WHERE id = $1", doc_id)
    if not row:
        raise HTTPException(404, "not found")
    await get_db().execute(
        """
        UPDATE knowledge_docs
        SET title=$2, content=$3, category=$4, scope=$5, task_id=$6,
            bytes=$7, indexed=false, updated_at=now()
        WHERE id=$1
        """,
        doc_id,
        body.title,
        body.content,
        body.category,
        body.scope,
        body.task_id,
        len(body.content.encode("utf-8")),
    )
    row2 = await get_db().fetchrow("SELECT * FROM knowledge_docs WHERE id = $1", doc_id)
    assert row2 is not None
    return _doc_dto(dict(row2), with_content=True)


@router.delete("/{doc_id}")
async def delete_doc(
    doc_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    await get_db().execute("DELETE FROM knowledge_docs WHERE id = $1", doc_id)
    return {"ok": True}


# promote 挂在 tasks 下，由 app 或本模块额外 router 提供
promote_router = APIRouter(tags=["knowledge"])


@promote_router.post("/api/tasks/{task_id}/knowledge/promote")
async def knowledge_promote(
    task_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return await KnowledgePromoter(get_db()).promote_task(task_id)


@promote_router.get("/api/knowledge/longterm/health")
async def longterm_health(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return await KnowledgeService(get_db()).longterm_health()
