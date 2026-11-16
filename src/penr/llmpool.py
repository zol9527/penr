"""LLM profile 池：健康检查 + 故障转移 + 角色绑定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from penr.db.engine import Database
from penr.db.stores import get_setting, get_setting_bool, set_setting

LLM_ROLE_KEYS = ("planner", "worker", "mainagent")


@dataclass
class ProfileHealth:
    fails: int = 0
    trips: int = 0
    open_until: datetime | None = None
    last_error: str = ""


@dataclass
class LLMPool:
    db: Database
    health: dict[int, ProfileHealth] = field(default_factory=dict)

    async def list_profiles(self) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, name, format, base_url, proxy, model, api_key_hint,
                   session_header_key, is_default, priority, pool_exclude,
                   rate_per_second, rate_per_minute, context_window_k, reasoning_effort,
                   (api_key IS NOT NULL AND api_key <> '') AS key_set
            FROM llm_profiles
            ORDER BY is_default DESC, priority DESC, id ASC
            """
        )
        out = []
        for r in rows:
            d = dict(r)
            d["id"] = int(d["id"])
            d["key_set"] = bool(d.pop("key_set", False))
            out.append(d)
        return out

    async def get_profile(self, profile_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT id, name, format, base_url, proxy, model, api_key, api_key_hint,
                   session_header_key, is_default, priority, pool_exclude,
                   rate_per_second, rate_per_minute, context_window_k, reasoning_effort
            FROM llm_profiles WHERE id = $1
            """,
            profile_id,
        )
        return dict(row) if row else None

    async def upsert_profile(self, data: dict[str, Any]) -> int:
        # 空 api_key 在更新时保留原值
        fmt = data.get("format") or data.get("provider") or "openai"
        if fmt in ("openai", "anthropic"):
            pass
        else:
            # web 可能传 provider 名；落库仍用 openai/anthropic
            fmt = "openai" if "anthropic" not in str(fmt).lower() else "anthropic"

        hint = data.get("api_key_hint") or ""
        api_key = data.get("api_key")
        if api_key:
            hint = hint or (api_key[:4] + "…" + api_key[-4:] if len(api_key) > 8 else "****")

        if data.get("id"):
            pid = int(data["id"])
            existing = await self.get_profile(pid)
            if not existing:
                raise ValueError(f"profile {pid} not found")
            keep_key = existing.get("api_key") if not api_key else api_key
            keep_hint = existing.get("api_key_hint") or "" if not api_key else hint
            if data.get("is_default"):
                await self.db.execute("UPDATE llm_profiles SET is_default = false WHERE id <> $1", pid)
            await self.db.execute(
                """
                UPDATE llm_profiles SET
                    name=$2, format=$3, base_url=$4, proxy=$5, model=$6, api_key=$7,
                    api_key_hint=$8, is_default=$9, priority=$10, pool_exclude=$11,
                    rate_per_second=$12, rate_per_minute=$13, context_window_k=$14,
                    reasoning_effort=$15, session_header_key=$16, updated_at=now()
                WHERE id=$1
                """,
                pid,
                data["name"],
                fmt,
                data.get("base_url"),
                data.get("proxy") or "",
                data["model"],
                keep_key,
                keep_hint,
                bool(data.get("is_default") or existing.get("is_default")),
                int(data.get("priority") if data.get("priority") is not None else existing.get("priority") or 0),
                bool(data.get("pool_exclude") if data.get("pool_exclude") is not None else existing.get("pool_exclude")),
                float(data.get("rate_per_second") or existing.get("rate_per_second") or 0),
                float(data.get("rate_per_minute") or existing.get("rate_per_minute") or 0),
                int(data.get("context_window_k") or existing.get("context_window_k") or 0),
                data.get("reasoning_effort") if data.get("reasoning_effort") is not None else (existing.get("reasoning_effort") or ""),
                data.get("session_header_key") if data.get("session_header_key") is not None else (existing.get("session_header_key") or ""),
            )
            return pid

        if data.get("is_default"):
            await self.db.execute("UPDATE llm_profiles SET is_default = false")
        row = await self.db.fetchrow(
            """
            INSERT INTO llm_profiles
                (name, format, base_url, proxy, model, api_key, api_key_hint, is_default,
                 priority, pool_exclude, rate_per_second, rate_per_minute,
                 context_window_k, reasoning_effort, session_header_key)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            RETURNING id
            """,
            data["name"],
            fmt,
            data.get("base_url"),
            data.get("proxy") or "",
            data["model"],
            api_key,
            hint,
            bool(data.get("is_default")),
            int(data.get("priority") or 0),
            bool(data.get("pool_exclude")),
            float(data.get("rate_per_second") or 0),
            float(data.get("rate_per_minute") or 0),
            int(data.get("context_window_k") or 0),
            data.get("reasoning_effort") or "",
            data.get("session_header_key") or "",
        )
        assert row is not None
        return int(row["id"])

    async def delete_profile(self, profile_id: int) -> int:
        r = await self.db.execute("DELETE FROM llm_profiles WHERE id = $1", profile_id)
        try:
            return int(r.split()[-1])
        except Exception:  # noqa: BLE001
            return 0

    async def activate_profile(self, profile_id: int) -> None:
        await self.db.execute("UPDATE llm_profiles SET is_default = false")
        await self.db.execute(
            "UPDATE llm_profiles SET is_default = true, updated_at = now() WHERE id = $1",
            profile_id,
        )

    async def set_profile_pool(self, profile_id: int, priority: int, exclude: bool) -> None:
        await self.db.execute(
            """
            UPDATE llm_profiles
            SET priority = $2, pool_exclude = $3, updated_at = now()
            WHERE id = $1
            """,
            profile_id,
            priority,
            exclude,
        )

    async def pick_active(self, *, include_key: bool = False) -> dict[str, Any] | None:
        if include_key:
            rows = await self.db.fetch(
                """
                SELECT id, name, format, base_url, proxy, model, api_key, api_key_hint,
                       is_default, priority, pool_exclude, rate_per_second, rate_per_minute,
                       context_window_k, reasoning_effort
                FROM llm_profiles
                ORDER BY is_default DESC, priority DESC, id ASC
                """
            )
            profiles = [dict(r) for r in rows]
        else:
            profiles = await self.list_profiles()
        now = datetime.now(timezone.utc)
        # 优先 is_default
        for p in profiles:
            if p.get("pool_exclude"):
                continue
            h = self.health.get(int(p["id"]))
            if h and h.open_until and h.open_until > now:
                continue
            return p
        return profiles[0] if profiles else None

    async def active_config_view(self) -> dict[str, Any]:
        """GET /api/llm 形状（不暴露 key）。"""
        p = await self.pick_active(include_key=True)
        if not p:
            return {
                "configured": False,
                "provider": "",
                "model": "",
                "base_url": "",
                "proxy": "",
                "key_set": False,
                "rate_per_second": 0,
                "rate_per_minute": 0,
                "context_window_k": 0,
                "reasoning_effort": "",
            }
        return {
            "configured": True,
            "provider": p.get("format") or "openai",
            "model": p.get("model") or "",
            "base_url": p.get("base_url") or "",
            "proxy": p.get("proxy") or "",
            "key_set": bool(p.get("api_key")),
            "rate_per_second": float(p.get("rate_per_second") or 0),
            "rate_per_minute": float(p.get("rate_per_minute") or 0),
            "context_window_k": int(p.get("context_window_k") or 0),
            "reasoning_effort": p.get("reasoning_effort") or "",
        }

    async def pool_enabled(self) -> bool:
        return await get_setting_bool(self.db, "llm_pool_enabled", False)

    async def set_pool_enabled(self, enabled: bool) -> None:
        await set_setting(self.db, "llm_pool_enabled", "true" if enabled else "false")

    async def pool_status(self) -> dict[str, Any]:
        profiles = await self.list_profiles()
        now = datetime.now(timezone.utc)
        chain = []
        for p in profiles:
            pid = int(p["id"])
            h = self.health.get(pid) or ProfileHealth()
            cooldown = 0
            state = "ok"
            if h.open_until and h.open_until > now:
                cooldown = int((h.open_until - now).total_seconds())
                state = "tripped"
            elif h.fails > 0:
                state = "degraded"
            chain.append(
                {
                    "profile_id": str(pid),
                    "name": p.get("name") or "",
                    "model": p.get("model") or "",
                    "format": p.get("format") or "",
                    "priority": int(p.get("priority") or 0),
                    "active": bool(p.get("is_default")),
                    "excluded": bool(p.get("pool_exclude")),
                    "state": state,
                    "fails": h.fails,
                    "trips": h.trips,
                    "cooldown_secs": cooldown,
                    "last_error": h.last_error or None,
                }
            )
        return {"enabled": await self.pool_enabled(), "chain": chain}

    async def reset_pool(self, profile_id: int = 0) -> None:
        if profile_id:
            self.health.pop(profile_id, None)
            await self.db.execute(
                "DELETE FROM llm_profile_health WHERE profile_id = $1", profile_id
            )
        else:
            self.health.clear()
            await self.db.execute("DELETE FROM llm_profile_health")

    async def list_roles(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for key in LLM_ROLE_KEYS:
            row = await self.db.fetchrow(
                "SELECT llm_profile_id FROM agents WHERE key = $1", key
            )
            pid = "0"
            if row and row["llm_profile_id"]:
                pid = str(int(row["llm_profile_id"]))
            out.append({"role": key, "profile_id": pid})
        report = await get_setting(self.db, "llm_role_report", "0")
        out.append({"role": "report", "profile_id": report or "0"})
        return out

    async def set_role(self, role: str, profile_id: int) -> None:
        if role in LLM_ROLE_KEYS:
            # 确保内置 agent 行存在
            await self.db.execute(
                """
                INSERT INTO agents (key, name, role, builtin)
                VALUES ($1, $1, $2, true)
                ON CONFLICT (key) DO NOTHING
                """,
                role,
                "main" if role == "mainagent" else role,
            )
            # mainagent 的 role 约束是 'main'
            role_col = "main" if role == "mainagent" else role
            await self.db.execute(
                """
                INSERT INTO agents (key, name, role, builtin, llm_profile_id)
                VALUES ($1, $1, $2, true, NULLIF($3, 0))
                ON CONFLICT (key) DO UPDATE SET llm_profile_id = NULLIF($3, 0), updated_at = now()
                """,
                role,
                role_col,
                profile_id,
            )
        elif role == "report":
            await set_setting(self.db, "llm_role_report", str(profile_id))
        else:
            raise ValueError(f"未知角色: {role}")

    def mark_failure(self, profile_id: int, error: str) -> None:
        h = self.health.setdefault(profile_id, ProfileHealth())
        h.fails += 1
        h.last_error = error
        if h.fails >= 3:
            h.trips += 1
            h.open_until = datetime.now(timezone.utc) + timedelta(seconds=min(300, 30 * h.trips))
            h.fails = 0

    def mark_success(self, profile_id: int) -> None:
        h = self.health.setdefault(profile_id, ProfileHealth())
        h.fails = 0
        h.open_until = None
        h.last_error = ""
