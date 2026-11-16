"""MCP 客户端：stdio / streamable HTTP + CrewAI 工具装配。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import httpx

from penr.db.engine import Database

logger = logging.getLogger(__name__)


class MCPHttpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: float = 30.0) -> None:
        self.url = url.rstrip("/")
        self.headers = headers or {}
        self.timeout = timeout

    async def list_tools(self) -> list[dict[str, Any]]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, json=payload, headers=self.headers)
            resp.raise_for_status()
            data = resp.json()
            return list(((data.get("result") or {}).get("tools")) or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.url, json=payload, headers=self.headers)
            resp.raise_for_status()
            return resp.json()


class MCPStdioClient:
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None) -> None:
        self.command = command
        self.args = args or []
        self.env = env or {}

    async def _rpc(self, method: str, params: dict[str, Any], req_id: int = 1) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=None if not self.env else {**os.environ, **self.env},
        )
        assert proc.stdin and proc.stdout
        req = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
        try:
            proc.stdin.write(req.encode())
            await proc.stdin.drain()
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=20)
            return json.loads(line.decode() or "{}")
        finally:
            proc.kill()
            await proc.wait()

    async def list_tools(self) -> list[dict[str, Any]]:
        data = await self._rpc("tools/list", {})
        return list(((data.get("result") or {}).get("tools")) or [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments}, req_id=2)


def _as_dict(val: Any) -> dict[str, Any]:
    if val is None:
        return {}
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    if isinstance(val, dict):
        return dict(val)
    return {}


def _as_list(val: Any) -> list[Any]:
    if val is None:
        return []
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return list(parsed) if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    if isinstance(val, list):
        return list(val)
    return []


def _client_from_row(row: Any) -> MCPHttpClient | MCPStdioClient:
    if row["transport"] == "http":
        headers = _as_dict(row["headers"])
        env = _as_dict(row["env"])
        merged = {**headers, **{k: str(v) for k, v in env.items() if k.lower().startswith("x-")}}
        return MCPHttpClient(
            row["url"] or "",
            headers=merged,
            timeout=float(row["timeout_seconds"] or 30),
        )
    return MCPStdioClient(
        row["command"] or "",
        args=[str(a) for a in _as_list(row["args"])],
        env={str(k): str(v) for k, v in _as_dict(row["env"]).items()},
    )


async def refresh_mcp_cache(db: Database, server_id: int) -> list[dict[str, Any]]:
    row = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not row:
        raise ValueError("mcp server not found")
    tools: list[dict[str, Any]] = []
    try:
        client = _client_from_row(row)
        tools = await client.list_tools()
        await db.execute(
            "UPDATE mcp_servers SET status='ok', status_at=now(), status_error='' WHERE id=$1",
            server_id,
        )
    except Exception as exc:  # noqa: BLE001
        await db.execute(
            "UPDATE mcp_servers SET status='error', status_at=now(), status_error=$2 WHERE id=$1",
            server_id,
            str(exc)[:500],
        )
        raise

    await db.execute("DELETE FROM mcp_tools_cache WHERE server_id = $1", server_id)
    for t in tools:
        await db.execute(
            """
            INSERT INTO mcp_tools_cache (server_id, tool_name, description, schema)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (server_id, tool_name) DO UPDATE
            SET description = EXCLUDED.description, schema = EXCLUDED.schema, discovered_at = now()
            """,
            server_id,
            t.get("name") or "",
            t.get("description") or "",
            json.dumps(t.get("inputSchema") or t.get("schema") or {}),
        )
    return tools


async def health_check_mcp(db: Database, server_id: int) -> dict[str, Any]:
    row = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not row:
        raise ValueError("mcp server not found")
    try:
        client = _client_from_row(row)
        await client.list_tools()
        await db.execute(
            "UPDATE mcp_servers SET status='ok', status_at=now(), status_error='' WHERE id=$1",
            server_id,
        )
        return {"status": "ok"}
    except Exception as exc:  # noqa: BLE001
        await db.execute(
            "UPDATE mcp_servers SET status='error', status_at=now(), status_error=$2 WHERE id=$1",
            server_id,
            str(exc)[:500],
        )
        return {"status": "error", "error": str(exc)[:500]}


async def record_mcp_stat(db: Database, server_name: str, tool_name: str, agent_key: str) -> None:
    await db.execute(
        """
        INSERT INTO mcp_stats (server_name, tool_name, agent_key, calls, last_at)
        VALUES ($1, $2, $3, 1, now())
        ON CONFLICT (server_name, tool_name, agent_key) DO UPDATE
        SET calls = mcp_stats.calls + 1, last_at = now()
        """,
        server_name,
        tool_name,
        agent_key or "",
    )


async def call_mcp_tool(
    db: Database,
    *,
    server_id: int,
    tool_name: str,
    arguments: dict[str, Any],
    agent_key: str = "worker",
) -> Any:
    row = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not row:
        raise ValueError("mcp server not found")
    if not row["enabled"]:
        raise ValueError("mcp server disabled")
    client = _client_from_row(row)
    result = await client.call_tool(tool_name, arguments)
    await record_mcp_stat(db, str(row["name"]), tool_name, agent_key)
    return result


async def list_enabled_mcp_tools(db: Database) -> list[dict[str, Any]]:
    """返回已启用 server 的缓存工具（用于装配 CrewAI）。"""
    rows = await db.fetch(
        """
        SELECT s.id AS server_id, s.name AS server_name, c.tool_name, c.description, c.schema
        FROM mcp_servers s
        JOIN mcp_tools_cache c ON c.server_id = s.id
        WHERE s.enabled = true
        ORDER BY s.id, c.tool_name
        """
    )
    return [dict(r) for r in rows]


def mcp_tool_crew_name(server_name: str, tool_name: str) -> str:
    """对齐 Go：mcp__<server>__<tool>，CrewAI 工具名需相对安全。"""
    safe_server = re_sub_safe(server_name)
    safe_tool = re_sub_safe(tool_name)
    return f"mcp__{safe_server}__{safe_tool}"


def re_sub_safe(s: str) -> str:
    import re

    return re.sub(r"[^a-zA-Z0-9_]+", "_", s).strip("_") or "x"


async def build_mcp_crewai_tools(
    db: Database,
    *,
    agent_key: str = "worker",
) -> list[Any]:
    """把已启用 MCP 工具包装成 CrewAI tools；无工具时返回空列表。"""
    cached = await list_enabled_mcp_tools(db)
    if not cached:
        return []

    from crewai.tools import tool

    tools_out: list[Any] = []
    # 按 (server_id, tool_name) 闭包绑定，避免循环变量泄漏
    for item in cached:
        server_id = int(item["server_id"])
        server_name = str(item["server_name"])
        tool_name = str(item["tool_name"])
        desc = str(item.get("description") or f"MCP {server_name}/{tool_name}")
        crew_name = mcp_tool_crew_name(server_name, tool_name)

        def _make(sid: int, sname: str, tname: str, cname: str, description: str):
            @tool(cname)
            async def mcp_call(arguments_json: str = "{}") -> str:
                f"""MCP 工具 {sname}/{tname}：{description}"""
                try:
                    args = json.loads(arguments_json or "{}")
                    if not isinstance(args, dict):
                        args = {"value": args}
                except json.JSONDecodeError as exc:
                    return f"invalid arguments_json: {exc}"
                try:
                    result = await call_mcp_tool(
                        db,
                        server_id=sid,
                        tool_name=tname,
                        arguments=args,
                        agent_key=agent_key,
                    )
                    return json.dumps(result, ensure_ascii=False, default=str)
                except Exception as exc:  # noqa: BLE001
                    return f"mcp error: {exc}"

            mcp_call.__doc__ = f"MCP {sname}/{tname}: {description}. Pass arguments_json as JSON object string."
            return mcp_call

        tools_out.append(_make(server_id, server_name, tool_name, crew_name, desc))
    return tools_out


async def mcp_stats_summary(db: Database) -> dict[str, Any]:
    servers = await db.fetch("SELECT id, name, enabled, status FROM mcp_servers ORDER BY id")
    stats = await db.fetch(
        """
        SELECT server_name, tool_name, agent_key, calls, last_at
        FROM mcp_stats
        ORDER BY calls DESC
        """
    )
    by_server: dict[str, dict[str, Any]] = {}
    total_calls = 0
    for s in servers:
        by_server[str(s["name"])] = {
            "server_name": s["name"],
            "calls": 0,
            "last_at": None,
            "by_tool": {},
            "by_agent": {},
            "enabled": bool(s["enabled"]),
            "status": s["status"] or "",
        }
    for row in stats:
        name = str(row["server_name"])
        bucket = by_server.setdefault(
            name,
            {
                "server_name": name,
                "calls": 0,
                "last_at": None,
                "by_tool": {},
                "by_agent": {},
            },
        )
        calls = int(row["calls"] or 0)
        total_calls += calls
        bucket["calls"] += calls
        bucket["by_tool"][str(row["tool_name"])] = bucket["by_tool"].get(str(row["tool_name"]), 0) + calls
        bucket["by_agent"][str(row["agent_key"] or "")] = (
            bucket["by_agent"].get(str(row["agent_key"] or ""), 0) + calls
        )
        la = row["last_at"]
        if la and (bucket["last_at"] is None or la > bucket["last_at"]):
            bucket["last_at"] = la
    enabled_count = sum(1 for s in servers if s["enabled"])
    return {
        "servers": list(by_server.values()),
        "total_calls": total_calls,
        "enabled_count": enabled_count,
        "total_count": len(servers),
    }
