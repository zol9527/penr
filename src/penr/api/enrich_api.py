"""Finding enrich + evidence：按 exploration node_id 路由。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from penr.api.auth import require_user
from penr.config import get_settings
from penr.db.engine import get_db
from penr.enrich.service import EnrichService

router = APIRouter(prefix="/api/findings", tags=["enrich"])


class FindingPatch(BaseModel):
    name: str | None = None
    target: str | None = None
    severity: str | None = None
    status: str | None = None
    locale: str | None = None
    module: str | None = None
    description: list[Any] | None = None
    test_process: list[Any] | None = None
    risk_analysis: list[Any] | None = None
    remediation: list[Any] | None = None


class EvidencePatch(BaseModel):
    caption: str | None = None
    step_index: int | None = None
    sort_order: int | None = None


def _evidence_dir(node_id: int) -> Path:
    d = Path(get_settings().data_dir) / "report_assets" / "evidence" / str(node_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_filename(name: str) -> str:
    base = Path(name or "evidence.bin").name
    base = re.sub(r"[^\w.\-]+", "_", base) or "evidence.bin"
    return base


@router.get("/{node_id}")
async def finding_detail(
    node_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    svc = EnrichService(get_db())
    try:
        return await svc.detail(node_id)
    except LookupError:
        raise HTTPException(404, "not found") from None


@router.post("/{node_id}/enrich", status_code=202)
async def start_enrich(
    node_id: int,
    locale: str = Query("zh-CN"),
    company: str = Query(""),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    svc = EnrichService(get_db())
    if not await svc.get_node(node_id):
        raise HTTPException(404, "node not found")
    return await svc.start_enrich(node_id, locale=locale, company=company)


@router.get("/{node_id}/enrich/status")
async def enrich_status(
    node_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    return await EnrichService(get_db()).status(node_id)


@router.put("/{node_id}")
async def update_finding(
    node_id: int,
    body: FindingPatch,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    svc = EnrichService(get_db())
    if not await svc.get_node(node_id):
        raise HTTPException(404, "node not found")
    enrichment = await svc.upsert_manual(node_id, body.model_dump(exclude_none=True))
    enriching = False
    # 确认即富化：status→confirmed 且正文为空时后台触发
    if body.status == "confirmed":
        desc = enrichment.get("description") or []
        if not desc:
            await svc.start_enrich(node_id, locale=body.locale or "zh-CN")
            enriching = True
    return {"enrichment": enrichment, "enriching": enriching}


@router.get("/{node_id}/evidence")
async def list_evidence(
    node_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    rows = await get_db().fetch(
        """
        SELECT * FROM finding_evidence
        WHERE node_id = $1
        ORDER BY sort_order ASC, id ASC
        """,
        node_id,
    )
    items = []
    for r in rows:
        ev = dict(r)
        items.append(
            {
                "evidence": ev,
                "url": f"/api/findings/{node_id}/evidence/{ev['filename']}",
            }
        )
    return {"items": items}


@router.post("/{node_id}/evidence")
async def upload_evidence(
    node_id: int,
    file: UploadFile = File(...),
    caption: str = Form(""),
    step_index: int = Form(0),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    data = await file.read()
    fname = _safe_filename(file.filename or f"{uuid.uuid4().hex}.bin")
    dest = _evidence_dir(node_id) / fname
    # 避免覆盖
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        fname = f"{stem}_{uuid.uuid4().hex[:6]}{suf}"
        dest = _evidence_dir(node_id) / fname
    dest.write_bytes(data)
    # sort_order = 当前最大 + 1
    max_sort = await get_db().fetchval(
        "SELECT COALESCE(MAX(sort_order), -1) FROM finding_evidence WHERE node_id = $1",
        node_id,
    )
    row = await get_db().fetchrow(
        """
        INSERT INTO finding_evidence (node_id, filename, caption, step_index, sort_order)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        node_id,
        fname,
        caption or "",
        int(step_index),
        int(max_sort) + 1,
    )
    assert row is not None
    return {
        "evidence": dict(row),
        "url": f"/api/findings/{node_id}/evidence/{fname}",
    }


@router.get("/{node_id}/evidence/{file_name}")
async def get_evidence_file(
    node_id: int,
    file_name: str,
    user: dict[str, Any] = Depends(require_user),
):
    if "/" in file_name or ".." in file_name:
        raise HTTPException(400, "invalid name")
    path = _evidence_dir(node_id) / file_name
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path)


@router.put("/{node_id}/evidence/{eid}")
async def update_evidence(
    node_id: int,
    eid: int,
    body: EvidencePatch,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    row = await get_db().fetchrow(
        "SELECT * FROM finding_evidence WHERE id = $1 AND node_id = $2",
        eid,
        node_id,
    )
    if not row:
        raise HTTPException(404, "not found")
    caption = body.caption if body.caption is not None else row["caption"]
    step_index = body.step_index if body.step_index is not None else row["step_index"]
    sort_order = body.sort_order if body.sort_order is not None else row["sort_order"]
    await get_db().execute(
        """
        UPDATE finding_evidence
        SET caption=$3, step_index=$4, sort_order=$5
        WHERE id=$1 AND node_id=$2
        """,
        eid,
        node_id,
        caption,
        step_index,
        sort_order,
    )
    return {"ok": True}


@router.delete("/{node_id}/evidence/{eid}")
async def delete_evidence(
    node_id: int,
    eid: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    row = await get_db().fetchrow(
        "SELECT * FROM finding_evidence WHERE id = $1 AND node_id = $2",
        eid,
        node_id,
    )
    if row:
        path = _evidence_dir(node_id) / row["filename"]
        path.unlink(missing_ok=True)
        await get_db().execute(
            "DELETE FROM finding_evidence WHERE id = $1 AND node_id = $2",
            eid,
            node_id,
        )
    return {"ok": True}
