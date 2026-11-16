"""报告异步作业（内存态，对齐 Go reportJobs）。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from penr.config import get_settings
from penr.db.engine import Database
from penr.db.stores import ExplorationStore, FindingsStore, TaskStore
from penr.report.build import build_docx_report, build_markdown_report

logger = logging.getLogger(__name__)

# task_id(str) → job snapshot
_report_jobs: dict[str, dict[str, Any]] = {}
_report_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_report_job(task_key: str) -> dict[str, Any]:
    return dict(
        _report_jobs.get(
            task_key,
            {"status": "idle", "filename": None, "error": None, "started_at": None},
        )
    )


async def start_report_job(
    db: Database,
    *,
    task_id: int,
    format: str = "docx",
    title: str = "",
    company: str = "",
    finding_ids: list[int] | None = None,
    template_id: int | None = None,
    locale: str = "zh-CN",
) -> dict[str, Any]:
    key = str(task_id)
    async with _report_lock:
        cur = _report_jobs.get(key)
        if cur and cur.get("status") == "generating":
            return dict(cur)
        job = {
            "status": "generating",
            "filename": None,
            "error": None,
            "started_at": _now_iso(),
            "path": None,
            "report_id": None,
        }
        _report_jobs[key] = job

    asyncio.create_task(
        _run_report(
            db,
            task_id=task_id,
            format=format,
            title=title,
            company=company,
            finding_ids=finding_ids,
            template_id=template_id,
            locale=locale,
        )
    )
    return dict(job)


async def _run_report(
    db: Database,
    *,
    task_id: int,
    format: str,
    title: str,
    company: str,
    finding_ids: list[int] | None,
    template_id: int | None,
    locale: str,
) -> None:
    key = str(task_id)
    settings = get_settings()
    try:
        task = await TaskStore(db).get(task_id)
        if not task:
            raise ValueError("task not found")

        # settings / template 覆盖公司名
        company_name = company
        if template_id:
            tpl = await db.fetchrow("SELECT * FROM report_templates WHERE id = $1", template_id)
            if tpl and not company_name:
                company_name = tpl["company_name"] or ""
        if not company_name:
            rs = await db.fetchrow("SELECT company_name FROM report_settings WHERE id = 1")
            if rs:
                company_name = rs["company_name"] or ""

        findings = await FindingsStore(db).list_for_task(task_id)
        if finding_ids:
            id_set = set(finding_ids)
            # 按 node_id 过滤（web 传的是 exploration node id）
            findings = [f for f in findings if int(f.get("node_id") or 0) in id_set]
        overview = await ExplorationStore(db, task.exploration_id).graph_overview()
        report_title = title or task.name or f"Task #{task.id}"
        out_dir = Path(settings.data_dir) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        fmt = (format or "docx").lower()
        if fmt in ("md", "markdown"):
            path = out_dir / f"task-{task_id}.md"
            md = build_markdown_report(
                title=report_title,
                goal=task.goal,
                findings=findings,
                overview=overview,
                language=locale or "zh-CN",
            )
            path.write_text(md, encoding="utf-8")
        else:
            path = out_dir / f"task-{task_id}.docx"
            build_docx_report(
                title=report_title,
                goal=task.goal,
                findings=findings,
                overview=overview,
                output_path=path,
            )
        size = path.stat().st_size
        row = await db.fetchrow(
            """
            INSERT INTO reports (task_id, task_desc, filename, path, size, company)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            task_id,
            task.description,
            path.name,
            str(path),
            size,
            company_name or "",
        )
        async with _report_lock:
            _report_jobs[key] = {
                "status": "done",
                "filename": path.name,
                "error": None,
                "started_at": _report_jobs.get(key, {}).get("started_at"),
                "path": str(path),
                "report_id": int(row["id"]) if row else None,
            }
    except Exception as exc:  # noqa: BLE001
        logger.exception("report job failed: %s", exc)
        async with _report_lock:
            _report_jobs[key] = {
                "status": "failed",
                "filename": None,
                "error": str(exc)[:500],
                "started_at": _report_jobs.get(key, {}).get("started_at"),
                "path": None,
                "report_id": None,
            }
