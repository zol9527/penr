"""报告 API：异步生成 + settings/templates/logo + 清单下载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.config import get_settings
from penr.db.engine import get_db
from penr.db.stores import ExplorationStore, FindingsStore, TaskStore
from penr.report.build import build_markdown_report
from penr.report.jobs import get_report_job, start_report_job

router = APIRouter(tags=["report"])


class ReportSettingsBody(BaseModel):
    company_name: str | None = None
    company_name_en: str | None = None
    logo_path: str | None = None
    personnel: list[dict[str, Any]] | None = None
    cvss_version: str | None = None


class TemplateBody(BaseModel):
    name: str = ""
    company_name: str = ""
    company_name_en: str = ""
    logo_path: str = ""
    personnel: list[dict[str, Any]] = Field(default_factory=list)
    cvss_version: str = "3.1"
    locale: str = "zh-CN"


def _assets_dir() -> Path:
    d = Path(get_settings().data_dir) / "report_assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _settings_dto(row: Any | None) -> dict[str, Any]:
    if not row:
        return {
            "company_name": "",
            "company_name_en": "",
            "logo_path": "",
            "personnel": [],
            "cvss_version": "3.1",
            "updated_at": None,
        }
    personnel = row["personnel_json"]
    if isinstance(personnel, str):
        try:
            personnel = json.loads(personnel)
        except json.JSONDecodeError:
            personnel = []
    return {
        "company_name": row["company_name"] or "",
        "company_name_en": row["company_name_en"] or "",
        "logo_path": row["logo_path"] or "",
        "personnel": personnel or [],
        "cvss_version": row["cvss_version"] or "3.1",
        "updated_at": row["updated_at"],
    }


def _template_dto(row: Any) -> dict[str, Any]:
    personnel = row["personnel_json"]
    if isinstance(personnel, str):
        try:
            personnel = json.loads(personnel)
        except json.JSONDecodeError:
            personnel = []
    return {
        "id": int(row["id"]),
        "name": row["name"] or "",
        "company_name": row["company_name"] or "",
        "company_name_en": row["company_name_en"] or "",
        "logo_path": row["logo_path"] or "",
        "personnel": personnel or [],
        "cvss_version": row["cvss_version"] or "3.1",
        "locale": row["locale"] or "zh-CN",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.get("/api/report")
async def report_preview(
    task: int | None = Query(None),
    user: dict[str, Any] = Depends(require_user),
):
    if task is None:
        raise HTTPException(400, "task required")
    t = await TaskStore(get_db()).get(task)
    if not t:
        raise HTTPException(404, "task not found")
    findings = await FindingsStore(get_db()).list_for_task(task)
    overview = await ExplorationStore(get_db(), t.exploration_id).graph_overview()
    md = build_markdown_report(
        title=t.name or f"Task #{t.id}",
        goal=t.goal,
        findings=findings,
        overview=overview,
    )
    return PlainTextResponse(md)


@router.post("/api/report/generate", status_code=202)
async def report_generate(
    task: int | None = Query(None),
    company: str = "",
    client: str = "",
    title: str = "",
    format: str = "docx",
    template_id: int | None = None,
    locale: str = "zh-CN",
    finding_ids: str = "",
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if task is None:
        raise HTTPException(400, "task required")
    ids = [int(x) for x in finding_ids.split(",") if x.strip().isdigit()]
    job = await start_report_job(
        get_db(),
        task_id=task,
        format=format,
        title=title or client,
        company=company,
        finding_ids=ids or None,
        template_id=template_id,
        locale=locale,
    )
    # 已在 generating 时 Go 返回 200；简化统一 202/快照
    return job


@router.get("/api/report/status")
async def report_status(
    task: int | None = Query(None),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if task is None:
        raise HTTPException(400, "task required")
    return get_report_job(str(task))


@router.get("/api/report/download")
async def report_download(
    task: int | None = Query(None),
    user: dict[str, Any] = Depends(require_user),
):
    if task is None:
        raise HTTPException(400, "task required")
    job = get_report_job(str(task))
    if job.get("status") != "done" or not job.get("path"):
        raise HTTPException(404, "report not ready")
    path = Path(job["path"])
    if not path.is_file():
        raise HTTPException(404, "file missing")
    return FileResponse(path, filename=job.get("filename") or path.name)


@router.get("/api/report-settings")
async def get_report_settings(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT * FROM report_settings WHERE id = 1")
    if not row:
        await get_db().execute("INSERT INTO report_settings (id) VALUES (1) ON CONFLICT DO NOTHING")
        row = await get_db().fetchrow("SELECT * FROM report_settings WHERE id = 1")
    return _settings_dto(row)


@router.put("/api/report-settings")
async def put_report_settings(
    body: ReportSettingsBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    await db.execute("INSERT INTO report_settings (id) VALUES (1) ON CONFLICT DO NOTHING")
    cur = await db.fetchrow("SELECT * FROM report_settings WHERE id = 1")
    assert cur is not None
    company_name = body.company_name if body.company_name is not None else cur["company_name"]
    company_name_en = (
        body.company_name_en if body.company_name_en is not None else cur["company_name_en"]
    )
    logo_path = body.logo_path if body.logo_path is not None else cur["logo_path"]
    if body.personnel is not None:
        personnel = json.dumps(body.personnel, ensure_ascii=False)
    else:
        personnel = (
            cur["personnel_json"]
            if isinstance(cur["personnel_json"], str)
            else json.dumps(cur["personnel_json"] or [])
        )
    cvss = body.cvss_version if body.cvss_version is not None else cur["cvss_version"]
    await db.execute(
        """
        UPDATE report_settings
        SET company_name=$1, company_name_en=$2, logo_path=$3,
            personnel_json=$4::jsonb, cvss_version=$5, updated_at=now()
        WHERE id=1
        """,
        company_name,
        company_name_en,
        logo_path,
        personnel if isinstance(personnel, str) else json.dumps(personnel),
        cvss,
    )
    row = await db.fetchrow("SELECT * FROM report_settings WHERE id = 1")
    return _settings_dto(row)


@router.post("/api/report-assets/logo")
async def upload_logo(
    file: UploadFile = File(...),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    data = await file.read()
    ext = Path(file.filename or "logo.png").suffix or ".png"
    rel = f"logo{ext}"
    path = _assets_dir() / rel
    path.write_bytes(data)
    await get_db().execute(
        """
        INSERT INTO report_settings (id, logo_path) VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET logo_path = EXCLUDED.logo_path, updated_at = now()
        """,
        rel,
    )
    return {"logo_path": rel, "url": f"/api/report-assets/{rel}"}


@router.get("/api/report-assets/{file_name}")
async def get_asset(file_name: str, user: dict[str, Any] = Depends(require_user)):
    if "/" in file_name or ".." in file_name:
        raise HTTPException(400, "invalid name")
    path = _assets_dir() / file_name
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path)


@router.get("/api/reports")
async def list_reports(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    rows = await get_db().fetch(
        """
        SELECT id, task_id, task_desc, filename, size, company, created_at
        FROM reports ORDER BY created_at DESC LIMIT 200
        """
    )
    return [dict(r) for r in rows]


@router.get("/api/reports/{report_id}/download")
async def download_report_record(
    report_id: int,
    user: dict[str, Any] = Depends(require_user),
):
    row = await get_db().fetchrow("SELECT * FROM reports WHERE id = $1", report_id)
    if not row:
        raise HTTPException(404, "not found")
    path = Path(row["path"])
    if not path.is_file():
        raise HTTPException(404, "file missing")
    return FileResponse(path, filename=row["filename"])


@router.delete("/api/reports/{report_id}")
async def delete_report(
    report_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    row = await get_db().fetchrow("SELECT * FROM reports WHERE id = $1", report_id)
    if row:
        path = Path(row["path"] or "")
        if path.is_file():
            path.unlink(missing_ok=True)
        await get_db().execute("DELETE FROM reports WHERE id = $1", report_id)
    return {"ok": True}


@router.get("/api/report-templates")
async def list_templates(user: dict[str, Any] = Depends(require_user)) -> list[dict[str, Any]]:
    rows = await get_db().fetch("SELECT * FROM report_templates ORDER BY id DESC")
    return [_template_dto(r) for r in rows]


@router.post("/api/report-templates")
async def create_template(
    body: TemplateBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        INSERT INTO report_templates
            (name, company_name, company_name_en, logo_path, personnel_json, cvss_version, locale)
        VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7)
        RETURNING *
        """,
        body.name,
        body.company_name,
        body.company_name_en,
        body.logo_path,
        json.dumps(body.personnel, ensure_ascii=False),
        body.cvss_version,
        body.locale,
    )
    assert row is not None
    return _template_dto(row)


@router.put("/api/report-templates/{template_id}")
async def update_template(
    template_id: int,
    body: TemplateBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow(
        """
        UPDATE report_templates SET
            name=$2, company_name=$3, company_name_en=$4, logo_path=$5,
            personnel_json=$6::jsonb, cvss_version=$7, locale=$8, updated_at=now()
        WHERE id=$1
        RETURNING *
        """,
        template_id,
        body.name,
        body.company_name,
        body.company_name_en,
        body.logo_path,
        json.dumps(body.personnel, ensure_ascii=False),
        body.cvss_version,
        body.locale,
    )
    if not row:
        raise HTTPException(404, "not found")
    return _template_dto(row)


@router.delete("/api/report-templates/{template_id}")
async def delete_template(
    template_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    await get_db().execute("DELETE FROM report_templates WHERE id = $1", template_id)
    return {"ok": True}
