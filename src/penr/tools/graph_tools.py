"""探索图 / 资产图领域工具，供 CrewAI Agent 调用。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from penr.db.stores import AssetStore, ExplorationStore, FindingsStore
from penr.tools.bash import run_bash
from penr.tools.web import web_fetch, web_search_ddgs


NotifyFn = Callable[[dict[str, Any]], Awaitable[None] | None]


def _pre_tool_gate(ctx: "GraphToolContext") -> str | None:
    """工具调用前：kill 优先；否则 drain 一条 steer（对齐 Go steerHooks.PreToolUse）。"""
    if ctx.intent_id is None:
        return None
    try:
        from penr.orchestration.task_engine import get_engine

        engine = get_engine()
        if engine.is_work_killed(ctx.intent_id):
            return "KILLED: work cancelled by planner kill_work"
        msg = engine.drain_steer(ctx.intent_id)
        if msg:
            return (
                "【规划者实时纠偏】"
                + msg
                + "\n（这是规划者对本意图的即时指令；本次工具调用未执行，请据此调整下一步。"
                "若与你当前打算冲突，以此为准。）"
            )
    except Exception:  # noqa: BLE001
        return None
    return None


def _task_egress_headers(ctx: "GraphToolContext") -> dict[str, str]:
    """worker 出流注入 X-Antifox-Task，供 mitm addon 归属流量。"""
    return {"X-Antifox-Task": str(ctx.task_id)}


@dataclass
class GraphToolContext:
    exploration: ExplorationStore
    assets: AssetStore
    findings: FindingsStore
    task_id: int
    worker_name: str = "worker"
    intent_id: int | None = None
    work_dir: str | None = None
    proxy_addr: str | None = None
    notify: NotifyFn | None = None
    pending_intents: list[dict[str, Any]] = field(default_factory=list)

    async def _emit(self, kind: str, summary: str, **extra: Any) -> None:
        await self.exploration.append_activity(
            kind=kind,
            summary=summary,
            worker=self.worker_name,
            tool=extra.get("tool", ""),
            detail=extra.get("detail", ""),
            node_id=extra.get("node_id"),
            is_error=bool(extra.get("is_error", False)),
        )
        if self.notify:
            maybe = self.notify(
                {
                    "task_id": self.task_id,
                    "kind": kind,
                    "summary": summary,
                    "worker": self.worker_name,
                    **extra,
                }
            )
            if maybe is not None:
                await maybe


def build_planner_tools(ctx: GraphToolContext) -> list[Any]:
    """返回 CrewAI 兼容的工具列表（planner）。"""
    from crewai.tools import tool

    @tool("graph_overview")
    async def graph_overview() -> str:
        """返回当前探索图态势摘要。"""
        data = await ctx.exploration.graph_overview()
        return json.dumps(data, ensure_ascii=False, default=str)

    @tool("add_intent")
    async def add_intent(intents_json: str) -> str:
        """批量添加探索意图。参数 intents_json 为 JSON 数组，元素含 summary/asset_ids/parent_ids。"""
        try:
            items = json.loads(intents_json)
        except json.JSONDecodeError as exc:
            return f"invalid json: {exc}"
        if isinstance(items, dict):
            items = [items]
        created: list[int] = []
        for item in items[:4]:
            iid = await ctx.exploration.add_intent(
                summary=str(item.get("summary", "")).strip(),
                asset_ids=list(item.get("asset_ids") or []),
                parent_ids=list(item.get("parent_ids") or []),
                priority=int(item.get("priority") or 0),
            )
            created.append(iid)
            await ctx._emit("intent", f"add_intent #{iid}: {item.get('summary', '')}", tool="add_intent", node_id=iid)
        return json.dumps({"created": created}, ensure_ascii=False)

    @tool("prove_goal")
    async def prove_goal(goal_id: int, reason: str = "") -> str:
        """将目标标记为已达成（需核实测态势）。"""
        await ctx.exploration.prove_goal(int(goal_id), reason=reason)
        await ctx._emit("goal", f"prove_goal #{goal_id}: {reason}", tool="prove_goal", node_id=int(goal_id))
        return "ok"

    @tool("list_assets")
    async def list_assets(q: str = "", asset_type: str = "", limit: int = 30) -> str:
        """搜索/列出资产。"""
        rows = await ctx.assets.list_assets(
            q=q or None,
            asset_type=asset_type or None,
            task_id=ctx.task_id,
            limit=min(int(limit), 100),
        )
        return json.dumps(rows, ensure_ascii=False, default=str)

    @tool("Bash")
    async def bash(command: str) -> str:
        """仅用于开局极少量只读探测；不要在此完成利用。"""
        from penr.guard.guard import check_tool

        decision = await check_tool(
            tool_name="Bash",
            tool_input={"command": command},
            task_id=str(ctx.task_id),
            agent_name="planner",
        )
        if decision.action == "deny":
            return f"DENIED: {decision.message}"
        if decision.action == "ask":
            return f"PENDING_HITL: {decision.message} (id={decision.pending_id})"
        result = await run_bash(
            command,
            cwd=ctx.work_dir,
            http_proxy=ctx.proxy_addr,
            env={"ANTIFOX_TASK_ID": str(ctx.task_id)},
        )
        await ctx._emit("tool", f"Bash: {command[:120]}", tool="Bash", detail=result.stdout[:2000])
        return json.dumps(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
            ensure_ascii=False,
        )

    @tool("steer_work")
    async def steer_work(intent_id: int, message: str) -> str:
        """向运行中的意图投递实时纠偏（不杀进程；下次工具调用前注入）。"""
        from penr.orchestration.task_engine import get_engine

        try:
            get_engine().steer_work(int(intent_id), message)
        except ValueError as exc:
            return f"error: {exc}"
        await ctx._emit(
            "steer",
            f"steer intent #{intent_id}: {message[:200]}",
            tool="steer_work",
            node_id=int(intent_id),
        )
        return "ok"

    @tool("kill_work")
    async def kill_work(intent_id: int) -> str:
        """终止运行中的指定意图 work。"""
        from penr.orchestration.task_engine import get_engine

        try:
            get_engine().kill_work(int(intent_id))
        except ValueError as exc:
            return f"error: {exc}"
        await ctx._emit(
            "kill",
            f"kill intent #{intent_id}",
            tool="kill_work",
            node_id=int(intent_id),
        )
        return "ok"

    return [graph_overview, add_intent, prove_goal, list_assets, bash, steer_work, kill_work]


def build_worker_tools(ctx: GraphToolContext) -> list[Any]:
    """返回 CrewAI 兼容的工具列表（worker）。"""
    from crewai.tools import tool

    @tool("record_fact")
    async def record_fact(summary: str, confidence: str = "observed", detail: str = "") -> str:
        """把探索结论写回探索图。"""
        fid = await ctx.exploration.record_fact(
            summary=summary,
            from_intent=ctx.intent_id,
            confidence=confidence,
            detail=detail,
        )
        await ctx._emit("fact", summary, tool="record_fact", node_id=fid)
        return json.dumps({"fact_id": fid})

    @tool("report_finding")
    async def report_finding(
        summary: str,
        vulnclass: str = "",
        severity: str = "medium",
        evidence: str = "",
        reproduction_json: str = "{}",
    ) -> str:
        """记录确认漏洞，并写入 findings 表。"""
        try:
            reproduction = json.loads(reproduction_json or "{}")
        except json.JSONDecodeError:
            reproduction = {"raw": reproduction_json}
        node_id = await ctx.exploration.report_finding_node(
            summary=summary,
            vulnclass=vulnclass,
            severity=severity,
            evidence=evidence,
            from_intent=ctx.intent_id,
            reproduction=reproduction,
        )
        # 尝试关联本任务最近流量
        traffic_ids: list[int] = []
        try:
            from penr.traffic.index import recent_exchange_ids

            traffic_ids = await recent_exchange_ids(limit=5, task_id=str(ctx.task_id))
        except Exception:  # noqa: BLE001
            traffic_ids = []
        finding_id = await ctx.findings.create(
            task_id=ctx.task_id,
            node_id=node_id,
            vulnclass=vulnclass,
            severity=severity,
            summary=summary,
            evidence=evidence,
            worker=ctx.worker_name,
            reproduction=reproduction,
            traffic_ids=traffic_ids,
        )
        await ctx._emit(
            "finding",
            summary,
            tool="report_finding",
            node_id=node_id,
            detail=json.dumps({"finding_id": finding_id, "traffic_ids": traffic_ids}),
        )
        return json.dumps({"node_id": node_id, "finding_id": finding_id, "traffic_ids": traffic_ids})

    @tool("insert_assets")
    async def insert_assets(assets_json: str) -> str:
        """插入新发现的资产。"""
        try:
            assets = json.loads(assets_json)
        except json.JSONDecodeError as exc:
            return f"invalid json: {exc}"
        if isinstance(assets, dict):
            assets = [assets]
        ids = await ctx.assets.insert_assets(assets, task_id=ctx.task_id)
        await ctx._emit("assets", f"inserted {len(ids)} assets", tool="insert_assets")
        return json.dumps({"asset_ids": ids})

    @tool("Bash")
    async def bash(command: str) -> str:
        """执行命令（渗透工具）；危险操作可能被拦截。"""
        gate = _pre_tool_gate(ctx)
        if gate:
            return gate
        from penr.guard.guard import check_tool

        decision = await check_tool(
            tool_name="Bash",
            tool_input={"command": command},
            task_id=str(ctx.task_id),
            agent_name=ctx.worker_name,
        )
        if decision.action == "deny":
            return f"DENIED: {decision.message}"
        if decision.action == "ask":
            return f"PENDING_HITL: {decision.message} (id={decision.pending_id})"
        result = await run_bash(
            command,
            cwd=ctx.work_dir,
            http_proxy=ctx.proxy_addr,
            env={"ANTIFOX_TASK_ID": str(ctx.task_id)},
        )
        await ctx._emit("tool", f"Bash: {command[:120]}", tool="Bash", detail=result.stdout[:2000])
        return json.dumps(
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
            ensure_ascii=False,
        )

    @tool("WebFetch")
    async def webfetch(url: str, method: str = "GET") -> str:
        """经录流代理抓取 URL。"""
        gate = _pre_tool_gate(ctx)
        if gate:
            return gate
        from penr.guard.guard import check_tool

        decision = await check_tool(
            tool_name="WebFetch",
            tool_input={"url": url, "method": method},
            task_id=str(ctx.task_id),
            agent_name=ctx.worker_name,
        )
        if decision.action == "deny":
            return f"DENIED: {decision.message}"
        if decision.action == "ask":
            return f"PENDING_HITL: {decision.message} (id={decision.pending_id})"
        headers = _task_egress_headers(ctx)
        data = await web_fetch(url, method=method, headers=headers, proxy=ctx.proxy_addr)
        await ctx._emit("tool", f"WebFetch {method} {url}", tool="WebFetch")
        return json.dumps(data, ensure_ascii=False)

    @tool("WebSearch")
    async def websearch(query: str) -> str:
        """公开网页搜索。"""
        results = await web_search_ddgs(query)
        return json.dumps(results, ensure_ascii=False)

    @tool("graph_overview")
    async def graph_overview() -> str:
        data = await ctx.exploration.graph_overview()
        return json.dumps(data, ensure_ascii=False, default=str)

    return [
        record_fact,
        report_finding,
        insert_assets,
        bash,
        webfetch,
        websearch,
        graph_overview,
    ]
