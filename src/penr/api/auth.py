"""JWT + RBAC 最小实现。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from penr.config import get_settings
from penr.db.engine import get_db


def _jwt_secret() -> str:
    s = get_settings()
    if s.jwt_secret:
        return s.jwt_secret
    key_path = s.data_dir / "jwt.key"
    if key_path.is_file():
        return key_path.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(secret, encoding="utf-8")
    return secret


def hash_password(password: str) -> str:
    # 直接用 bcrypt：passlib 1.7 与 bcrypt>=4.1 不兼容（__about__/72-byte 探测失败）
    raw = password.encode("utf-8")
    return bcrypt.hashpw(raw[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], password_hash.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def create_token(user: dict[str, Any]) -> str:
    s = get_settings()
    payload = {
        "sub": user["id"],
        "username": user["username"],
        "tv": int(user.get("token_version") or 0),
        "exp": datetime.now(timezone.utc) + timedelta(hours=s.jwt_ttl_hours),
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])


# 平台权限目录（与 Go db.RBACPermissions 对齐的核心子集）
RBAC_PERMISSIONS: dict[str, str] = {
    "auth:self": "管理自己的会话和密码",
    "dashboard:read": "查看仪表盘总览",
    "task:read": "查看任务 / 探索链路 / 会话",
    "task:write": "创建 / 控制任务、发送对话引导",
    "task:delete": "删除任务",
    "finding:read": "查看漏洞发现",
    "finding:write": "创建 / 更新漏洞发现",
    "finding:delete": "删除漏洞发现",
    "traffic:read": "查看流量录制",
    "asset:read": "查看资产 / 公司",
    "asset:write": "创建 / 导入 / 同步资产",
    "asset:delete": "删除资产",
    "llm:read": "查看 LLM 配置",
    "llm:write": "管理 LLM 配置与模型",
    "agent:read": "查看 Agent 配置",
    "agent:write": "管理 Agent / 提示词 / 触发器",
    "mcp:read": "查看 MCP 服务",
    "mcp:write": "管理 MCP 服务",
    "skill:read": "查看技能",
    "skill:write": "创建 / 更新技能",
    "skill:delete": "删除技能",
    "tool:read": "查看工具",
    "tool:write": "管理工具 / 自定义工具",
    "intercept:read": "查看拦截规则 / 审批",
    "intercept:write": "管理拦截规则 / 处理审批",
    "log:read": "查看后端日志 / 审计",
    "config:read": "查看系统配置",
    "config:write": "修改系统配置",
    "knowledge:read": "查看知识库 / 检索",
    "knowledge:write": "创建 / 更新知识库文档、配置 embedder",
    "knowledge:delete": "删除知识库文档",
    "report:read": "查看 / 下载报告、模版与报告设置",
    "report:write": "生成报告、管理模版与报告设置",
    "report:delete": "删除报告 / 模版",
    "rbac:read": "查看用户 / 角色 / 权限",
    "rbac:write": "管理用户 / 角色 / 权限",
}


async def seed_rbac_catalog() -> None:
    """幂等播种权限目录，并把全部权限挂到 role_admin。"""
    db = get_db()
    await db.execute(
        """
        INSERT INTO rbac_roles (id, name, description, scope, is_system)
        VALUES ('role_admin', 'admin', 'full access', 'all', true)
        ON CONFLICT DO NOTHING
        """
    )
    for key, desc in RBAC_PERMISSIONS.items():
        await db.execute(
            """
            INSERT INTO rbac_permissions (key, description) VALUES ($1, $2)
            ON CONFLICT (key) DO UPDATE SET description = EXCLUDED.description
            """,
            key,
            desc,
        )
        await db.execute(
            """
            INSERT INTO rbac_role_permissions (role_id, permission_key)
            VALUES ('role_admin', $1)
            ON CONFLICT DO NOTHING
            """,
            key,
        )


async def bootstrap_admin(username: str, password: str) -> dict[str, Any]:
    db = get_db()
    existing = await db.fetchval("SELECT COUNT(*) FROM rbac_users")
    if int(existing or 0) > 0:
        raise HTTPException(status_code=400, detail="already initialized")
    user_id = "admin"
    await seed_rbac_catalog()
    await db.execute(
        """
        INSERT INTO rbac_users (id, username, display_name, password_hash, enabled, is_builtin)
        VALUES ($1, $2, $3, $4, true, true)
        """,
        user_id,
        username,
        "Administrator",
        hash_password(password),
    )
    await db.execute(
        """
        INSERT INTO rbac_user_roles (user_id, role_id)
        VALUES ($1, 'role_admin')
        ON CONFLICT DO NOTHING
        """,
        user_id,
    )
    row = await db.fetchrow("SELECT * FROM rbac_users WHERE id = $1", user_id)
    assert row is not None
    return dict(row)


async def authenticate(username: str, password: str) -> dict[str, Any]:
    db = get_db()
    row = await db.fetchrow(
        "SELECT * FROM rbac_users WHERE username = $1 AND enabled = true",
        username,
    )
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="invalid credentials")
    return dict(row)


def extract_token(request: Request) -> str:
    """对齐 Go：Authorization Bearer → antifox_token cookie → ?token=（SSE 用）。"""
    auth = request.headers.get("Authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    cookie = request.cookies.get("antifox_token") or ""
    if cookie:
        return cookie
    return (request.query_params.get("token") or "").strip()


async def current_user(request: Request) -> dict[str, Any]:
    token = extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        payload = decode_token(token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail="invalid token") from exc
    db = get_db()
    row = await db.fetchrow(
        "SELECT * FROM rbac_users WHERE id = $1 AND enabled = true",
        payload.get("sub"),
    )
    if not row:
        raise HTTPException(status_code=401, detail="user disabled")
    if int(row.get("token_version") or 0) != int(payload.get("tv") or 0):
        raise HTTPException(status_code=401, detail="token revoked")
    return dict(row)


async def optional_user(request: Request) -> dict[str, Any] | None:
    try:
        return await current_user(request)
    except HTTPException:
        return None


def require_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return user


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(a.encode()).digest(),
        hashlib.sha256(b.encode()).digest(),
    )
