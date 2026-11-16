"""CrewAI Planner：单轮产意图。"""

from __future__ import annotations

import json
import logging
from typing import Any

from penr.crews.llm import has_llm_credentials, resolve_crew_llm
from penr.crews.prompts import PLANNER_SYSTEM
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore
from penr.skills.loader import inject_skills_block, list_skill_summaries, record_skill_stat
from penr.tools.graph_tools import GraphToolContext, build_planner_tools

logger = logging.getLogger(__name__)


async def run_planner(
    *,
    task_id: int,
    goal: str,
    exploration: ExplorationStore,
    assets: AssetStore,
    findings: FindingsStore,
    done_intents: list[int] | None = None,
    work_dir: str | None = None,
    proxy_addr: str | None = None,
    notify: Any = None,
) -> dict[str, Any]:
    overview = await exploration.graph_overview()
    # 无 LLM 时走确定性兜底，保证主环可测
    if not has_llm_credentials():
        return await _fallback_plan(exploration, overview, goal)

    ctx = GraphToolContext(
        exploration=exploration,
        assets=assets,
        findings=findings,
        task_id=task_id,
        worker_name="planner",
        work_dir=work_dir,
        proxy_addr=proxy_addr,
        notify=notify,
    )
    tools = build_planner_tools(ctx)
    prompt = PLANNER_SYSTEM.format(
        goal=goal,
        overview=json.dumps(overview, ensure_ascii=False, default=str),
        done_intents=",".join(f"#{i}" for i in (done_intents or [])) or "无",
    )
    prompt = inject_skills_block(prompt, agent_key="planner")
    try:
        for s in list_skill_summaries()[:5]:
            await record_skill_stat(exploration.db, s["name"], "planner")
    except Exception:  # noqa: BLE001
        pass

    try:
        from crewai import Agent, Crew, Process, Task

        agent = Agent(
            role="Planner",
            goal="根据探索态势产出克制的新意图或判定目标",
            backstory="你是 AntiFox 规划者，只派真正有价值的未覆盖方向。",
            tools=tools,
            llm=resolve_crew_llm(),
            verbose=False,
            allow_delegation=False,
        )
        task = Task(
            description=prompt + "\n\n请开始本轮规划。",
            expected_output="本轮结论摘要；若有新意图已通过 add_intent 提交",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
        # kickoff 多为同步；放到线程避免阻塞事件环
        import asyncio

        result = await asyncio.to_thread(crew.kickoff)
        raw = str(result)
        await exploration.append_activity(
            kind="planner",
            summary="planner round complete",
            worker="planner",
            detail=raw[:4000],
        )
        met = overview.get("task", {}).get("status") == "achieved"
        # 重新读图看是否已 met
        overview2 = await exploration.graph_overview()
        met = overview2.get("task", {}).get("status") == "achieved"
        return {"met": met, "reason": raw[:500], "mode": "crewai"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("planner crew failed: %s", exc)
        await exploration.append_activity(
            kind="planner",
            summary=f"planner error: {exc}",
            worker="planner",
            is_error=True,
        )
        return await _fallback_plan(exploration, overview, goal)


async def _fallback_plan(
    exploration: ExplorationStore, overview: dict[str, Any], goal: str
) -> dict[str, Any]:
    """无密钥或 Crew 失败时：若无 open/running 意图则派一条初始侦察意图。"""
    open_n = len(overview.get("open_intents") or [])
    run_n = len(overview.get("running_intents") or [])
    if open_n + run_n == 0:
        iid = await exploration.add_intent(
            summary=f"对目标做初始侦察与攻击面盘点：{goal}",
            priority=10,
        )
        await exploration.append_activity(
            kind="planner",
            summary=f"fallback add_intent #{iid}",
            worker="planner",
            node_id=iid,
        )
        return {"met": False, "reason": "fallback initial intent", "mode": "fallback", "intent_id": iid}
    return {"met": False, "reason": "fallback idle (frontier covered)", "mode": "fallback"}
