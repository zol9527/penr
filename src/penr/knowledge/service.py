"""双层知识库。

1. **task 层**：PostgreSQL `knowledge_docs.scope='task'`，只活在单次任务内。
2. **long-term 层**：本仓内置 OpenViking（compose 服务），跨任务复用。

检索顺序：当前 task 文档 → 本仓 OpenViking 长期库。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from penr.db.engine import Database
from penr.knowledge.openviking import OpenVikingClient, OVHit

KnowledgeScope = Literal["task", "longterm"]


def chunk_text(text: str, *, size: int = 800, overlap: int = 100) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    i = 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += max(1, size - overlap)
    return chunks


class KnowledgeService:
    def __init__(self, db: Database, ov: OpenVikingClient | None = None) -> None:
        self.db = db
        self.ov = ov or OpenVikingClient()

    # ---- task 层（PG）----

    async def ingest_task(
        self,
        *,
        task_id: int,
        title: str,
        content: str,
        category: str = "",
        source: str = "manual",
    ) -> int:
        """写入任务临时知识（必须带 task_id）。"""
        return await self._ingest_pg(
            title=title,
            content=content,
            scope="task",
            task_id=task_id,
            category=category,
            source=source,
        )

    async def list_task_docs(self, task_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, scope, task_id, category, title, source, bytes, indexed, created_at
            FROM knowledge_docs
            WHERE scope = 'task' AND task_id = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            task_id,
            limit,
        )
        return [dict(r) for r in rows]

    async def get_task_doc_contents(self, task_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, category, title, content, source, created_at
            FROM knowledge_docs
            WHERE scope = 'task' AND task_id = $1
            ORDER BY id ASC
            """,
            task_id,
        )
        return [dict(r) for r in rows]

    async def search_task(self, query: str, *, task_id: int, limit: int = 8) -> list[dict[str, Any]]:
        return await self._search_pg(query, scope="task", task_id=task_id, limit=limit)

    # ---- long-term 层（本仓 OpenViking）----

    async def search_longterm(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        hits = await self.ov.find(query, limit=limit)
        return [_hit_to_dict(h, layer="longterm") for h in hits]

    async def longterm_health(self) -> dict[str, Any]:
        return await self.ov.health()

    # ---- 统一检索 ----

    async def search(
        self,
        query: str,
        *,
        task_id: int | None = None,
        layers: list[KnowledgeScope] | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """默认同时查 task + longterm；可按 layers 过滤。"""
        want = set(layers or ["task", "longterm"])
        task_hits: list[dict[str, Any]] = []
        longterm_hits: list[dict[str, Any]] = []
        if "task" in want and task_id is not None:
            task_hits = await self.search_task(query, task_id=task_id, limit=limit)
        if "longterm" in want:
            longterm_hits = await self.search_longterm(query, limit=limit)
        return {
            "query": query,
            "task": task_hits,
            "longterm": longterm_hits,
            "hits": task_hits + longterm_hits,
        }

    async def list_docs(
        self,
        *,
        scope: str | None = None,
        task_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["TRUE"]
        args: list[Any] = []
        if scope:
            args.append(scope)
            clauses.append(f"scope = ${len(args)}")
        if task_id is not None:
            args.append(task_id)
            clauses.append(f"task_id = ${len(args)}")
        args.extend([limit, offset])
        where = " AND ".join(clauses)
        rows = await self.db.fetch(
            f"""
            SELECT id, scope, task_id, category, title, source, bytes, indexed, created_at
            FROM knowledge_docs
            WHERE {where}
            ORDER BY id DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,
            *args,
        )
        return [dict(r) for r in rows]

    # 兼容旧 ingest：默认进 task（要求 task_id）；global 仅作兼容别名不再作为长期层
    async def ingest(
        self,
        title: str,
        content: str,
        *,
        source: str = "manual",
        scope: str = "task",
        task_id: int | None = None,
        category: str = "",
    ) -> int:
        if scope == "task":
            if task_id is None:
                raise ValueError("task scope requires task_id")
            return await self.ingest_task(
                task_id=task_id,
                title=title,
                content=content,
                category=category,
                source=source,
            )
        # 遗留 global：仍写入 PG，但不视为长期库（长期库只走 OpenViking）
        return await self._ingest_pg(
            title=title,
            content=content,
            scope="global",
            task_id=task_id,
            category=category,
            source=source,
        )

    async def _ingest_pg(
        self,
        *,
        title: str,
        content: str,
        scope: str,
        task_id: int | None,
        category: str,
        source: str,
    ) -> int:
        row = await self.db.fetchrow(
            """
            INSERT INTO knowledge_docs
                (scope, task_id, category, title, content, source, bytes, indexed)
            VALUES ($1, $2, $3, $4, $5, $6, $7, false)
            RETURNING id
            """,
            scope,
            task_id,
            category,
            title,
            content,
            source,
            len(content.encode("utf-8")),
        )
        assert row is not None
        return int(row["id"])

    async def _search_pg(
        self,
        query: str,
        *,
        scope: str,
        task_id: int | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        tokens = [t for t in re.split(r"\s+", query.strip()) if t]
        if not tokens:
            return []
        like = "%" + tokens[0] + "%"
        args: list[Any] = [like, scope]
        clauses = ["(title ILIKE $1 OR content ILIKE $1)", "scope = $2"]
        if task_id is not None:
            args.append(task_id)
            clauses.append(f"task_id = ${len(args)}")
        args.append(limit)
        where = " AND ".join(clauses)
        rows = await self.db.fetch(
            f"""
            SELECT id, scope, task_id, category, title, content, source, created_at
            FROM knowledge_docs
            WHERE {where}
            ORDER BY id DESC
            LIMIT ${len(args)}
            """,
            *args,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["layer"] = "task" if d.get("scope") == "task" else d.get("scope")
            content = d.get("content") or ""
            for ch in chunk_text(content, size=600, overlap=50):
                if tokens[0].lower() in ch.lower():
                    d["snippet"] = ch
                    break
            else:
                d["snippet"] = content[:600]
            out.append(d)
        return out


def _hit_to_dict(h: OVHit, *, layer: str) -> dict[str, Any]:
    return {
        "layer": layer,
        "uri": h.uri,
        "title": h.uri.rsplit("/", 1)[-1] if h.uri else "",
        "snippet": h.content or h.abstract,
        "abstract": h.abstract,
        "score": h.score,
        "context_type": h.context_type,
    }
