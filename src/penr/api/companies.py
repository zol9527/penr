"""M2：companies 最小 CRUD。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.db.stores import CompanyStore

router = APIRouter(tags=["companies"])


class CompanyCreateBody(BaseModel):
    name: str
    logo: str = ""
    scope: list[str] = Field(default_factory=list)


class ScopeBody(BaseModel):
    scope: list[str] = Field(default_factory=list)
    reason: str = ""
    reset: bool = False


class DeleteBody(BaseModel):
    delete_assets: bool = False


@router.get("/api/companies")
async def list_companies(
    task: int = 0,
    user: dict[str, Any] = Depends(require_user),
) -> list[dict[str, Any]]:
    return await CompanyStore(get_db()).list(task_id=task or None)


@router.post("/api/companies", status_code=201)
async def create_company(
    body: CompanyCreateBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    if not body.name.strip():
        raise HTTPException(400, detail="name required")
    store = CompanyStore(get_db())
    cid, created = await store.upsert(body.name.strip(), body.logo)
    out: dict[str, Any] = {"id": cid, "created": created}
    if body.scope:
        scope_res = await store.add_scope(cid, body.scope, reason="api")
        out["scope_added"] = scope_res.get("added", 0)
        out["scope_skipped"] = scope_res.get("skipped", 0)
        out["scope_invalid"] = scope_res.get("invalid", 0)
        if scope_res.get("errors"):
            out["scope_errors"] = scope_res["errors"]
    return out


@router.get("/api/companies/{company_id}")
async def get_company(
    company_id: int,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    store = CompanyStore(get_db())
    c = await store.get(company_id)
    if not c:
        raise HTTPException(404, detail="company not found")
    return {"company": c, "scope": await store.get_scope(company_id)}


@router.post("/api/companies/{company_id}/scope")
async def add_company_scope(
    company_id: int,
    body: ScopeBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    store = CompanyStore(get_db())
    if not await store.get(company_id):
        raise HTTPException(404, detail="company not found")
    return await store.add_scope(
        company_id, body.scope, reason=body.reason or "api", reset=body.reset
    )


@router.delete("/api/companies/{company_id}")
async def delete_company(
    company_id: int,
    body: DeleteBody | None = None,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    delete_assets = bool(body and body.delete_assets)
    deleted, assets_deleted = await CompanyStore(get_db()).delete(
        company_id, delete_assets=delete_assets
    )
    return {"deleted": deleted, "assets_deleted": assets_deleted}
