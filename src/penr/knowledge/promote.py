"""任务完成后：从 task 层提炼知识点 → 写入本仓 OpenViking 长期库。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from penr.config import get_settings
from penr.db.engine import Database
from penr.db.stores import ExplorationStore, FindingsStore, TaskStore
from penr.knowledge.openviking import OpenVikingClient
from penr.knowledge.service import KnowledgeService

logger = logging.getLogger(__name__)


class KnowledgePromoter:
    def __init__(self, db: Database, ov: OpenVikingClient | None = None) -> None:
        self.db = db
        self.ov = ov or OpenVikingClient()
        self.ks = KnowledgeService(db, self.ov)

    async def promote_task(self, task_id: int) -> dict[str, Any]:
        """主入口：任务 done 后调用。"""
        settings = get_settings()
        if not settings.openviking_auto_promote:
            return {"ok": False, "skipped": True, "reason": "auto_promote disabled"}

        task = await TaskStore(self.db).get(task_id)
        if not task:
            return {"ok": False, "error": "task not found"}

        # 1) 收集素材
        task_docs = await self.ks.get_task_doc_contents(task_id)
        exp = ExplorationStore(self.db, task.exploration_id)
        overview = await exp.graph_overview()
        findings = await FindingsStore(self.db).list_for_task(task_id)
        points = self._extract_points(task=task, docs=task_docs, overview=overview, findings=findings)

        if not points:
            return {"ok": True, "promoted": 0, "reason": "nothing to promote"}

        # 2) 写一份任务蒸馏包
        bundle = self._render_bundle(task=task, overview=overview, findings=findings, points=points)
        write_res = await self.ov.write_knowledge_doc(
            relative_path=f"tasks/task-{task_id}/distill.md",
            content=bundle,
            mode="create",
            wait=False,
        )

        # 3) 逐条知识点 → memory + 独立文档
        memory_results: list[dict[str, Any]] = []
        for i, pt in enumerate(points, 1):
            body = (
                f"# {pt['title']}\n\n"
                f"- source_task: {task_id}\n"
                f"- category: {pt.get('category') or 'tradecraft'}\n"
                f"- promoted_at: {datetime.now(timezone.utc).isoformat()}\n\n"
                f"{pt['content']}\n"
            )
            doc = await self.ov.write_knowledge_doc(
                relative_path=f"tasks/task-{task_id}/points/{i:02d}-{_slug(pt['title'])}.md",
                content=body,
                mode="create",
            )
            mem = await self.ov.add_memory(
                f"[agents task#{task_id}] {pt['title']}: {pt['content'][:1200]}"
            )
            memory_results.append({"doc": doc, "memory": mem})

        # 4) 标记 task 文档已晋升（不删，便于审计）
        await self.db.execute(
            """
            UPDATE knowledge_docs
            SET indexed = true,
                category = CASE
                    WHEN category = '' OR category IS NULL THEN 'promoted'
                    WHEN category LIKE '%promoted%' THEN category
                    ELSE category || ',promoted'
                END,
                updated_at = now()
            WHERE scope = 'task' AND task_id = $1
            """,
            task_id,
        )

        logger.info("promoted task=%s points=%s to project OpenViking", task_id, len(points))
        return {
            "ok": True,
            "task_id": task_id,
            "promoted": len(points),
            "bundle": write_res,
            "points": memory_results,
        }

    def _extract_points(
        self,
        *,
        task: Any,
        docs: list[dict[str, Any]],
        overview: dict[str, Any],
        findings: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        points: list[dict[str, str]] = []

        for d in docs:
            title = (d.get("title") or "task note").strip()
            content = (d.get("content") or "").strip()
            if len(content) < 20:
                continue
            points.append(
                {
                    "title": title,
                    "content": content[:4000],
                    "category": d.get("category") or "task_doc",
                }
            )

        for f in overview.get("recent_facts") or []:
            summary = str(f.get("summary") or "").strip()
            if len(summary) < 12:
                continue
            points.append(
                {
                    "title": f"fact:{summary[:60]}",
                    "content": summary,
                    "category": "fact",
                }
            )

        for finding in findings:
            summary = str(finding.get("summary") or "").strip()
            if not summary:
                continue
            repro = finding.get("reproduction") or {}
            evidence = finding.get("evidence") or ""
            content = (
                f"vulnclass={finding.get('vulnclass')}; severity={finding.get('severity')}\n"
                f"summary={summary}\n"
                f"evidence={evidence}\n"
                f"reproduction={json.dumps(repro, ensure_ascii=False, default=str)[:2000]}"
            )
            points.append(
                {
                    "title": f"finding:{summary[:60]}",
                    "content": content,
                    "category": "finding",
                }
            )

        # 去重（按 title）
        seen: set[str] = set()
        uniq: list[dict[str, str]] = []
        for p in points:
            key = p["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        return uniq[:40]

    def _render_bundle(
        self,
        *,
        task: Any,
        overview: dict[str, Any],
        findings: list[dict[str, Any]],
        points: list[dict[str, str]],
    ) -> str:
        lines = [
            f"# Task {task.id} knowledge distill",
            "",
            f"- name: {task.name}",
            f"- goal: {task.goal}",
            f"- promoted_at: {datetime.now(timezone.utc).isoformat()}",
            f"- facts: {overview.get('facts')}",
            f"- findings: {len(findings)}",
            "",
            "## Points",
            "",
        ]
        for i, p in enumerate(points, 1):
            lines.append(f"### {i}. {p['title']}")
            lines.append("")
            lines.append(p["content"])
            lines.append("")
        return "\n".join(lines)


def _slug(text: str) -> str:
    import re

    s = re.sub(r"[^\w\-]+", "-", text.strip().lower(), flags=re.UNICODE)
    s = re.sub(r"-+", "-", s).strip("-")
    return (s or "point")[:48]
