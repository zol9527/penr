"""M5：LLM 配置 / profiles / pool / roles API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.llmpool import LLMPool

router = APIRouter(tags=["llm"])


class LLMSetBody(BaseModel):
    provider: str = "openai"
    model: str
    base_url: str = ""
    proxy: str = ""
    api_key: str = ""
    rate_per_second: float = 0
    rate_per_minute: float = 0
    context_window_k: int = 0
    reasoning_effort: str = ""


class LLMProfileBody(BaseModel):
    id: int | None = None
    name: str
    format: str = "openai"
    model: str
    base_url: str | None = None
    proxy: str | None = None
    session_header_key: str | None = None
    api_key: str | None = None
    api_key_hint: str = ""
    is_default: bool = False
    priority: int = 0
    pool_exclude: bool = False
    rate_per_second: float = 0
    rate_per_minute: float = 0
    context_window_k: int = 0
    reasoning_effort: str = ""


class ActivateBody(BaseModel):
    id: int


class PoolBody(BaseModel):
    enabled: bool


class PoolResetBody(BaseModel):
    profile_id: int = 0


class ProfilePoolBody(BaseModel):
    priority: int = 0
    exclude: bool = False


class RoleBody(BaseModel):
    role: str
    profile_id: int = 0


@router.get("/api/llm")
async def get_llm(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return await LLMPool(get_db()).active_config_view()


@router.post("/api/llm")
async def set_llm(
    body: LLMSetBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    pool = LLMPool(get_db())
    # 已有同名则更新，避免 UNIQUE(name) 冲突
    existing_id: int | None = None
    for p in await pool.list_profiles():
        if p.get("name") == "default":
            existing_id = int(p["id"])
            break
    pid = await pool.upsert_profile(
        {
            "id": existing_id,
            "name": "default",
            "format": body.provider or "openai",
            "model": body.model,
            "base_url": body.base_url or None,
            "proxy": body.proxy or None,
            "api_key": body.api_key or None,
            "api_key_hint": (body.api_key[-4:] if body.api_key else ""),
            "is_default": True,
            "rate_per_second": body.rate_per_second,
            "rate_per_minute": body.rate_per_minute,
            "context_window_k": body.context_window_k,
            "reasoning_effort": body.reasoning_effort,
        }
    )
    await pool.activate_profile(pid)
    return {"ok": True, "id": pid}


@router.get("/api/llm/profiles")
async def llm_profiles(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"profiles": await LLMPool(get_db()).list_profiles()}


@router.post("/api/llm/profiles")
async def save_llm_profile(
    body: LLMProfileBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    pid = await LLMPool(get_db()).upsert_profile(body.model_dump())
    return {"id": pid}


@router.delete("/api/llm/profiles/{profile_id}")
async def delete_llm_profile(
    profile_id: int, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    n = await LLMPool(get_db()).delete_profile(profile_id)
    return {"deleted": n}


@router.post("/api/llm/profiles/active")
async def activate_llm_profile(
    body: ActivateBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, bool]:
    await LLMPool(get_db()).activate_profile(body.id)
    return {"ok": True}


@router.get("/api/llm/pool")
async def llm_pool(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return await LLMPool(get_db()).pool_status()


@router.post("/api/llm/pool")
async def set_llm_pool(
    body: PoolBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, Any]:
    await LLMPool(get_db()).set_pool_enabled(body.enabled)
    return {"ok": True, "enabled": body.enabled}


@router.post("/api/llm/pool/reset")
async def reset_llm_pool(
    body: PoolResetBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, bool]:
    await LLMPool(get_db()).reset_pool(body.profile_id)
    return {"ok": True}


@router.post("/api/llm/profiles/{profile_id}/pool")
async def set_profile_pool(
    profile_id: int,
    body: ProfilePoolBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, bool]:
    await LLMPool(get_db()).set_profile_pool(profile_id, body.priority, body.exclude)
    return {"ok": True}


@router.get("/api/llm/roles")
async def llm_roles(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return {"roles": await LLMPool(get_db()).list_roles()}


@router.post("/api/llm/roles")
async def set_llm_role(
    body: RoleBody, user: dict[str, Any] = Depends(require_user)
) -> dict[str, bool]:
    try:
        await LLMPool(get_db()).set_role(body.role, body.profile_id)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return {"ok": True}
