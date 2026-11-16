"""M5：RBAC users/roles/permissions + 管理面权限中间件（受保护路径默认拒绝）。"""

from __future__ import annotations

import secrets
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from penr.api.auth import (
    RBAC_PERMISSIONS,
    current_user,
    extract_token,
    hash_password,
    require_user,
    seed_rbac_catalog,
)
from penr.db.engine import get_db

router = APIRouter(tags=["rbac"])

# 管理面路径前缀：未映射到权限 → 默认拒绝（与 Go「未映射放行」不同，这里对 mgmt 收紧）
_PROTECTED_PREFIXES = (
    "/api/rbac",
    "/api/settings",
    "/api/llm",
    "/api/intercept",
)

_PUBLIC_PREFIXES = (
    "/api/auth/",
    "/api/health",
    "/health",
    "/api/setup",
    "/api/login",
)


def _crud_perm(method: str, module: str) -> str:
    m = method.upper()
    if m in ("GET", "HEAD"):
        return f"{module}:read"
    if m == "DELETE":
        key = f"{module}:delete"
        return key if key in RBAC_PERMISSIONS else f"{module}:write"
    return f"{module}:write"


def permission_for_path(method: str, path: str) -> str | None:
    """返回所需权限；None = 不在受保护管理面（交由路由自身鉴权）。"""
    p = path
    if not p.startswith("/api/"):
        return None
    if any(p.startswith(x) or p == x.rstrip("/") for x in _PUBLIC_PREFIXES):
        return None
    if p == "/api/rbac/me":
        return "auth:self"
    if p.startswith("/api/rbac"):
        return "rbac:read" if method.upper() in ("GET", "HEAD") else "rbac:write"
    if p.startswith("/api/settings"):
        return _crud_perm(method, "config")
    if p.startswith("/api/llm"):
        return _crud_perm(method, "llm")
    if p.startswith("/api/intercept"):
        return _crud_perm(method, "intercept")
    if any(p.startswith(pref) for pref in _PROTECTED_PREFIXES):
        # 未知管理子路径：默认拒绝
        return "__deny__"
    return None


async def resolve_access(user_id: str) -> dict[str, Any]:
    db = get_db()
    user = await db.fetchrow(
        "SELECT id, username, display_name, enabled, is_builtin, created_at, updated_at "
        "FROM rbac_users WHERE id = $1",
        user_id,
    )
    if not user:
        raise HTTPException(401, detail="用户不存在")
    roles = await db.fetch(
        """
        SELECT r.id, r.name, r.description, r.scope, r.is_system, r.created_at, r.updated_at
        FROM rbac_roles r
        JOIN rbac_user_roles ur ON ur.role_id = r.id
        WHERE ur.user_id = $1
        """,
        user_id,
    )
    perms = await db.fetch(
        """
        SELECT DISTINCT rp.permission_key
        FROM rbac_role_permissions rp
        JOIN rbac_user_roles ur ON ur.role_id = rp.role_id
        WHERE ur.user_id = $1
        """,
        user_id,
    )
    perm_set = {r["permission_key"] for r in perms}
    is_admin = any(r["id"] == "role_admin" or r["name"] == "admin" for r in roles)
    if is_admin:
        perm_set = set(RBAC_PERMISSIONS.keys())
    scope = "own"
    for r in roles:
        s = r["scope"] or "own"
        if s == "all" or scope == "all":
            scope = "all"
        elif s == "assigned" or scope == "assigned":
            scope = "assigned"
    return {
        "user": dict(user),
        "roles": [dict(r) for r in roles],
        "permissions": sorted(perm_set),
        "perm_set": perm_set,
        "scope": scope,
        "is_admin": is_admin,
    }


def require_perm(perm: str) -> Callable[..., Any]:
    async def _dep(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
        access = await resolve_access(user["id"])
        if access["is_admin"] or perm in access["perm_set"]:
            return user
        raise HTTPException(403, detail=f"缺少权限: {perm}")

    return _dep


class RBACEnforceMiddleware(BaseHTTPMiddleware):
    """对 /api/rbac|settings|llm|intercept 做权限强制；无权限 → 403。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        needed = permission_for_path(request.method, path)
        if needed is None:
            return await call_next(request)
        # 鉴权
        try:
            token = extract_token(request)
            if not token:
                return JSONResponse({"error": "未授权"}, status_code=401)
            # 复用 current_user 逻辑
            user = await current_user(request)
        except HTTPException as exc:
            return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "未授权"}, status_code=401)

        if needed == "__deny__":
            return JSONResponse({"error": "禁止访问"}, status_code=403)

        try:
            access = await resolve_access(user["id"])
        except HTTPException as exc:
            return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)

        if not access["is_admin"] and needed not in access["perm_set"]:
            return JSONResponse({"error": f"缺少权限: {needed}"}, status_code=403)

        request.state.rbac_access = access
        return await call_next(request)


# ---- request bodies ----


class CreateUserBody(BaseModel):
    username: str
    display_name: str = ""
    password: str = Field(min_length=6)
    enabled: bool = True
    role_ids: list[str] = Field(default_factory=list)


class UpdateUserBody(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    role_ids: list[str] | None = None
    password: str | None = None


class RoleBody(BaseModel):
    name: str
    description: str = ""
    scope: str = "assigned"
    permissions: list[str] = Field(default_factory=list)


async def _user_view(user_id: str) -> dict[str, Any]:
    db = get_db()
    row = await db.fetchrow(
        """
        SELECT id, username, display_name, enabled, is_builtin, created_at, updated_at
        FROM rbac_users WHERE id = $1
        """,
        user_id,
    )
    if not row:
        raise HTTPException(404, detail="user not found")
    roles = await db.fetch(
        "SELECT role_id FROM rbac_user_roles WHERE user_id = $1", user_id
    )
    d = dict(row)
    d["role_ids"] = [r["role_id"] for r in roles]
    d["created_at"] = str(d.get("created_at") or "")
    d["updated_at"] = str(d.get("updated_at") or "")
    return d


async def _role_view(role_id: str) -> dict[str, Any]:
    db = get_db()
    row = await db.fetchrow("SELECT * FROM rbac_roles WHERE id = $1", role_id)
    if not row:
        raise HTTPException(404, detail="role not found")
    perms = await db.fetch(
        "SELECT permission_key FROM rbac_role_permissions WHERE role_id = $1", role_id
    )
    d = dict(row)
    d["permissions"] = [p["permission_key"] for p in perms]
    d["created_at"] = str(d.get("created_at") or "")
    d["updated_at"] = str(d.get("updated_at") or "")
    return d


@router.get("/api/rbac/me")
async def rbac_me(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    access = await resolve_access(user["id"])
    u = access["user"]
    return {
        "user": {
            "id": u["id"],
            "username": u["username"],
            "display_name": u.get("display_name") or "",
        },
        "roles": [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "scope": r.get("scope") or "own",
                "is_system": bool(r.get("is_system")),
                "created_at": str(r.get("created_at") or ""),
                "updated_at": str(r.get("updated_at") or ""),
            }
            for r in access["roles"]
        ],
        "permissions": access["permissions"],
        "scope": access["scope"],
    }


@router.get("/api/rbac/metadata")
async def rbac_metadata(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    await seed_rbac_catalog()
    roles = await get_db().fetch("SELECT * FROM rbac_roles ORDER BY name")
    return {
        "permissions": dict(RBAC_PERMISSIONS),
        "roles": [
            {
                "id": r["id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "scope": r.get("scope") or "own",
                "is_system": bool(r.get("is_system")),
                "created_at": str(r.get("created_at") or ""),
                "updated_at": str(r.get("updated_at") or ""),
            }
            for r in roles
        ],
        "scopes": ["all", "assigned", "own"],
    }


@router.get("/api/rbac/users")
async def rbac_users(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await get_db().fetch("SELECT id FROM rbac_users ORDER BY username")
    users = [await _user_view(r["id"]) for r in rows]
    return {"users": users}


@router.post("/api/rbac/users")
async def rbac_create_user(
    body: CreateUserBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    uid = secrets.token_hex(16)
    try:
        await db.execute(
            """
            INSERT INTO rbac_users (id, username, display_name, password_hash, enabled)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uid,
            body.username.strip(),
            body.display_name or body.username,
            hash_password(body.password),
            body.enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, detail=f"创建失败: {exc}") from exc
    for rid in body.role_ids:
        await db.execute(
            """
            INSERT INTO rbac_user_roles (user_id, role_id) VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            """,
            uid,
            rid,
        )
    return {"user": await _user_view(uid)}


@router.put("/api/rbac/users/{user_id}")
async def rbac_update_user(
    user_id: str,
    body: UpdateUserBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    existing = await db.fetchrow("SELECT * FROM rbac_users WHERE id = $1", user_id)
    if not existing:
        raise HTTPException(404, detail="user not found")
    if body.display_name is not None:
        await db.execute(
            "UPDATE rbac_users SET display_name = $2, updated_at = now() WHERE id = $1",
            user_id,
            body.display_name,
        )
    if body.enabled is not None:
        await db.execute(
            """
            UPDATE rbac_users
            SET enabled = $2, token_version = token_version + 1, updated_at = now()
            WHERE id = $1
            """,
            user_id,
            body.enabled,
        )
    if body.password:
        await db.execute(
            """
            UPDATE rbac_users
            SET password_hash = $2, token_version = token_version + 1, updated_at = now()
            WHERE id = $1
            """,
            user_id,
            hash_password(body.password),
        )
    if body.role_ids is not None:
        await db.execute("DELETE FROM rbac_user_roles WHERE user_id = $1", user_id)
        for rid in body.role_ids:
            await db.execute(
                """
                INSERT INTO rbac_user_roles (user_id, role_id) VALUES ($1, $2)
                ON CONFLICT DO NOTHING
                """,
                user_id,
                rid,
            )
    return {"ok": True}


@router.delete("/api/rbac/users/{user_id}")
async def rbac_delete_user(
    user_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT is_builtin FROM rbac_users WHERE id = $1", user_id)
    if not row:
        raise HTTPException(404, detail="user not found")
    if row["is_builtin"]:
        raise HTTPException(400, detail="不能删除内置用户")
    await get_db().execute("DELETE FROM rbac_users WHERE id = $1", user_id)
    return {"ok": True}


@router.get("/api/rbac/roles")
async def rbac_roles(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    rows = await get_db().fetch("SELECT id FROM rbac_roles ORDER BY name")
    return {"roles": [await _role_view(r["id"]) for r in rows]}


@router.post("/api/rbac/roles")
async def rbac_create_role(
    body: RoleBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    rid = "role_" + secrets.token_hex(8)
    db = get_db()
    await db.execute(
        """
        INSERT INTO rbac_roles (id, name, description, scope, is_system)
        VALUES ($1, $2, $3, $4, false)
        """,
        rid,
        body.name,
        body.description,
        body.scope or "assigned",
    )
    for p in body.permissions:
        if p in RBAC_PERMISSIONS:
            await db.execute(
                """
                INSERT INTO rbac_role_permissions (role_id, permission_key)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
                """,
                rid,
                p,
            )
    return {"role": await _role_view(rid)}


@router.put("/api/rbac/roles/{role_id}")
async def rbac_update_role(
    role_id: str,
    body: RoleBody,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    row = await db.fetchrow("SELECT is_system FROM rbac_roles WHERE id = $1", role_id)
    if not row:
        raise HTTPException(404, detail="role not found")
    await db.execute(
        """
        UPDATE rbac_roles
        SET name = $2, description = $3, scope = $4, updated_at = now()
        WHERE id = $1
        """,
        role_id,
        body.name,
        body.description,
        body.scope or "assigned",
    )
    await db.execute("DELETE FROM rbac_role_permissions WHERE role_id = $1", role_id)
    for p in body.permissions:
        if p in RBAC_PERMISSIONS:
            await db.execute(
                """
                INSERT INTO rbac_role_permissions (role_id, permission_key)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
                """,
                role_id,
                p,
            )
    return {"role": await _role_view(role_id)}


@router.delete("/api/rbac/roles/{role_id}")
async def rbac_delete_role(
    role_id: str,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    row = await get_db().fetchrow("SELECT is_system FROM rbac_roles WHERE id = $1", role_id)
    if not row:
        raise HTTPException(404, detail="role not found")
    if row["is_system"]:
        raise HTTPException(400, detail="不能删除系统角色")
    await get_db().execute("DELETE FROM rbac_roles WHERE id = $1", role_id)
    return {"ok": True}
