"""Skills 文件系统 API：列表 / 上传 / CRUD / 文件读写。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.config import get_settings
from penr.db.engine import get_db
from penr.skills.loader import (
    _list_rel_files,
    _safe_join,
    delete_skill,
    ensure_skill_root,
    install_skill_zip,
    list_skill_summaries,
    update_skill_meta,
    write_skill_md,
)

router = APIRouter(prefix="/api/skills", tags=["skills"])


class CreateSkillBody(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    license: str = ""
    compatibility: str = ""
    mcps: list[str] = Field(default_factory=list)
    instructions: str = ""


class MetaBody(BaseModel):
    description: str | None = None
    license: str | None = None
    compatibility: str | None = None
    mcps: list[str] | None = None


class DirBody(BaseModel):
    path: str


class FileBody(BaseModel):
    content: str = ""


@router.get("")
async def list_skills(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"skills": list_skill_summaries()}


@router.get("/stats")
async def skill_stats(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await get_db().fetch(
        "SELECT skill_name, agent_key, calls, last_at FROM skill_stats ORDER BY calls DESC"
    )
    by_skill: dict[str, dict[str, Any]] = {}
    total = 0
    for r in rows:
        name = str(r["skill_name"])
        bucket = by_skill.setdefault(
            name,
            {"skill_name": name, "calls": 0, "last_at": None, "by_agent": {}},
        )
        calls = int(r["calls"] or 0)
        total += calls
        bucket["calls"] += calls
        bucket["by_agent"][str(r["agent_key"] or "")] = calls
        la = r["last_at"]
        if la and (bucket["last_at"] is None or la > bucket["last_at"]):
            bucket["last_at"] = la
    installed = len(list_skill_summaries())
    return {
        "skills": list(by_skill.values()),
        "total_calls": total,
        "installed_count": installed,
        "invoked_count": len(by_skill),
    }


@router.post("", status_code=201)
async def create_skill(
    body: CreateSkillBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    try:
        write_skill_md(
            body.name,
            description=body.description,
            license=body.license,
            compatibility=body.compatibility,
            mcps=body.mcps,
            instructions=body.instructions,
        )
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"name": body.name}


@router.post("/upload", status_code=201)
async def upload_skill(
    file: UploadFile = File(...),
    overwrite: bool = Query(False),
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    suffix = Path(file.filename or "skill.zip").suffix or ".zip"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        data = await file.read()
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        name, count = install_skill_zip(tmp_path, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    finally:
        tmp_path.unlink(missing_ok=True)
    return {"name": name, "files": count}


@router.delete("/{name}")
async def remove_skill(
    name: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    try:
        delete_skill(name)
    except FileNotFoundError:
        raise HTTPException(404, "skill not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"deleted": name}


@router.put("/{name}/meta")
async def skill_meta(
    name: str,
    body: MetaBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    try:
        update_skill_meta(name, body.model_dump(exclude_none=True))
    except FileNotFoundError:
        raise HTTPException(404, "skill not found") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"ok": True}


@router.post("/{name}/dirs", status_code=201)
async def create_dir(
    name: str,
    body: DirBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    root = ensure_skill_root()
    skill = _safe_join(root, name)
    if not skill.is_dir():
        raise HTTPException(404, "skill not found")
    rel = body.path.strip().lstrip("/")
    if not rel or ".." in Path(rel).parts:
        raise HTTPException(400, "invalid path")
    target = _safe_join(skill, *Path(rel).parts)
    target.mkdir(parents=True, exist_ok=True)
    return {"dir": rel.rstrip("/") + "/"}


@router.get("/{name}/files")
async def list_files(
    name: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    root = ensure_skill_root()
    skill = _safe_join(root, name)
    if not skill.is_dir():
        raise HTTPException(404, "skill not found")
    return {"files": _list_rel_files(skill)}


@router.get("/{name}/files/{file_path:path}")
async def read_file(
    name: str,
    file_path: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    root = ensure_skill_root()
    skill = _safe_join(root, name)
    if not skill.is_dir():
        raise HTTPException(404, "skill not found")
    try:
        target = _safe_join(skill, *Path(file_path).parts)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not target.is_file():
        raise HTTPException(404, "file not found")
    return {"content": target.read_text(encoding="utf-8", errors="replace"), "file": file_path}


@router.put("/{name}/files/{file_path:path}")
async def write_file(
    name: str,
    file_path: str,
    body: FileBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    root = ensure_skill_root()
    skill = _safe_join(root, name)
    if not skill.is_dir():
        raise HTTPException(404, "skill not found")
    try:
        target = _safe_join(skill, *Path(file_path).parts)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True}


@router.delete("/{name}/files/{file_path:path}")
async def delete_path(
    name: str,
    file_path: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, str]:
    root = ensure_skill_root()
    skill = _safe_join(root, name)
    if not skill.is_dir():
        raise HTTPException(404, "skill not found")
    try:
        target = _safe_join(skill, *Path(file_path).parts)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if not target.exists():
        raise HTTPException(404, "not found")
    if target.is_dir():
        import shutil

        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": file_path}
