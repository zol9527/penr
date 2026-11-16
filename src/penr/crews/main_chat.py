"""Main / Chat Crew：人机操舵与对话。"""

from __future__ import annotations

import json
import logging
from typing import Any

from penr.crews.llm import has_llm_credentials, resolve_crew_llm
from penr.crews.prompts import CHAT_SYSTEM, MAIN_SYSTEM
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore
from penr.tools.graph_tools import GraphToolContext

logger = logging.getLogger(__name__)


async def run_main_chat(
    *,
    message: str,
    task_id: int | None = None,
    exploration: ExplorationStore | None = None,
    assets: AssetStore | None = None,
    findings: FindingsStore | None = None,
    goal: str = "",
    mode: str = "main",
) -> dict[str, Any]:
    overview: dict[str, Any] = {}
    if exploration is not None:
        overview = await exploration.graph_overview()

    if not has_llm_credentials():
        return await _fallback_main(message, exploration, overview)

    try:
        from crewai import Agent, Crew, Process, Task
        from crewai.tools import tool
        import asyncio

        tools: list[Any] = []
        if exploration is not None and assets is not None and findings is not None and task_id is not None:
            ctx = GraphToolContext(
                exploration=exploration,
                assets=assets,
                findings=findings,
                task_id=task_id,
                worker_name="main",
            )

            @tool("add_hint")
            async def add_hint(summary: str) -> str:
                hid = await ctx.exploration.add_hint(summary)
                await ctx.exploration.append_activity(
                    kind="hint", summary=summary, worker="main", node_id=hid, tool="add_hint"
                )
                return json.dumps({"hint_id": hid})

            @tool("add_intent")
            async def add_intent(summary: str) -> str:
                iid = await ctx.exploration.add_intent(summary=summary, owner="main")
                await ctx.exploration.append_activity(
                    kind="intent", summary=summary, worker="main", node_id=iid, tool="add_intent"
                )
                return json.dumps({"intent_id": iid})

            @tool("graph_overview")
            async def graph_overview() -> str:
                data = await ctx.exploration.graph_overview()
                return json.dumps(data, ensure_ascii=False, default=str)

            @tool("steer_work")
            async def steer_work(intent_id: int, message: str) -> str:
                """向运行中的意图投递纠偏；随后引擎会被唤醒。"""
                from penr.orchestration.task_engine import get_engine

                try:
                    get_engine().steer_work(int(intent_id), message)
                except ValueError as exc:
                    return f"error: {exc}"
                return "ok"

            @tool("kill_work")
            async def kill_work(intent_id: int) -> str:
                """终止运行中的意图。"""
                from penr.orchestration.task_engine import get_engine

                try:
                    get_engine().kill_work(int(intent_id))
                except ValueError as exc:
                    return f"error: {exc}"
                return "ok"

            tools = [add_hint, add_intent, graph_overview, steer_work, kill_work]

        system = (
            MAIN_SYSTEM.format(
                goal=goal or overview.get("task", {}).get("goal", ""),
                overview=json.dumps(overview, ensure_ascii=False, default=str),
            )
            if mode == "main"
            else CHAT_SYSTEM
        )

        agent = Agent(
            role="MainAgent" if mode == "main" else "Assistant",
            goal="帮助用户观察与操舵渗透任务",
            backstory="AntiFox 主控助手",
            tools=tools,
            llm=resolve_crew_llm(),
            verbose=False,
            allow_delegation=False,
        )
        task = Task(
            description=f"{system}\n\n用户消息：{message}",
            expected_output="对用户的简洁回复",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        raw = await asyncio.to_thread(crew.kickoff)
        return {"reply": str(raw), "mode": "crewai"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("main chat failed: %s", exc)
        return await _fallback_main(message, exploration, overview)


async def _fallback_main(
    message: str,
    exploration: ExplorationStore | None,
    overview: dict[str, Any],
) -> dict[str, Any]:
    import re

    lower = message.lower()
    # 简易操舵：`steer #12 改用 api` / `kill #12`
    m_steer = re.search(r"(?:steer|纠偏)\s*#?(\d+)\s+(.+)", message, flags=re.I)
    if m_steer:
        from penr.orchestration.task_engine import get_engine

        iid, msg = int(m_steer.group(1)), m_steer.group(2).strip()
        try:
            get_engine().steer_work(iid, msg)
            return {"reply": f"已纠偏 intent #{iid}", "mode": "fallback", "steered": iid}
        except ValueError as exc:
            return {"reply": str(exc), "mode": "fallback", "error": True}
    m_kill = re.search(r"(?:kill|终止)\s*#?(\d+)", message, flags=re.I)
    if m_kill:
        from penr.orchestration.task_engine import get_engine

        iid = int(m_kill.group(1))
        try:
            get_engine().kill_work(iid)
            return {"reply": f"已终止 intent #{iid}", "mode": "fallback", "killed": iid}
        except ValueError as exc:
            return {"reply": str(exc), "mode": "fallback", "error": True}
    if exploration is not None and ("hint" in lower or "提示" in message):
        hid = await exploration.add_hint(message)
        return {"reply": f"已添加 hint #{hid}", "mode": "fallback", "hint_id": hid, "wake": True}
    if exploration is not None and ("intent" in lower or "意图" in message):
        iid = await exploration.add_intent(summary=message, owner="main")
        return {"reply": f"已添加 intent #{iid}", "mode": "fallback", "intent_id": iid, "wake": True}
    facts = overview.get("facts", 0)
    findings_n = overview.get("findings", 0)
    return {
        "reply": (
            f"（无 LLM）当前态势：facts={facts}, findings={findings_n}, "
            f"open_intents={len(overview.get('open_intents') or [])}. "
            f"收到：{message}"
        ),
        "mode": "fallback",
        "overview": overview,
    }
