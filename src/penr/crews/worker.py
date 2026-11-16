"""CrewAI Worker：认领并执行单条意图。"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from penr.crews.llm import has_llm_credentials, resolve_crew_llm
from penr.crews.prompts import WORKER_SYSTEM
from penr.db.stores import AssetStore, ExplorationStore, FindingsStore
from penr.skills.loader import inject_skills_block, record_skill_stat, list_skill_summaries
from penr.tools.bash import run_bash
from penr.tools.graph_tools import GraphToolContext, build_worker_tools
from penr.tools.mcp import build_mcp_crewai_tools

logger = logging.getLogger(__name__)


async def run_worker(
    *,
    task_id: int,
    exploration: ExplorationStore,
    assets: AssetStore,
    findings: FindingsStore,
    worker_name: str = "work#1",
    work_dir: str | None = None,
    proxy_addr: str | None = None,
    notify: Any = None,
) -> dict[str, Any] | None:
    intent = await exploration.claim_intent(worker=worker_name)
    if not intent:
        return None

    intent_id = int(intent["id"])
    payload = intent.get("payload") or {}
    summary = str(payload.get("summary") or "")
    await exploration.append_activity(
        kind="claim",
        summary=f"{worker_name} claimed intent #{intent_id}: {summary}",
        worker=worker_name,
        node_id=intent_id,
    )

    # 注册 per-intent kill/steer 槽位（对齐 Go registerWork）
    from penr.orchestration.task_engine import get_engine

    engine = get_engine()
    kill_evt = engine.register_work(intent_id, task_id=task_id)
    try:
        if kill_evt.is_set():
            await exploration.complete_intent(intent_id, "stopped")
            return {"intent_id": intent_id, "killed": True, "mode": "killed"}

        if not has_llm_credentials():
            result = await _fallback_execute(
                exploration=exploration,
                findings=findings,
                assets=assets,
                task_id=task_id,
                intent_id=intent_id,
                summary=summary,
                worker_name=worker_name,
                work_dir=work_dir,
                proxy_addr=proxy_addr,
                kill_evt=kill_evt,
            )
            if kill_evt.is_set() or result.get("killed"):
                await exploration.complete_intent(intent_id, "stopped")
                return {"intent_id": intent_id, "killed": True, "mode": "killed"}
            await exploration.complete_intent(intent_id, "done")
            return result

        ctx = GraphToolContext(
            exploration=exploration,
            assets=assets,
            findings=findings,
            task_id=task_id,
            worker_name=worker_name,
            intent_id=intent_id,
            work_dir=work_dir,
            proxy_addr=proxy_addr,
            notify=notify,
        )
        tools = build_worker_tools(ctx)
        # 启用的 MCP server → 挂到 worker 工具集
        try:
            mcp_tools = await build_mcp_crewai_tools(exploration.db, agent_key=worker_name)
            tools = [*tools, *mcp_tools]
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp tools wire skipped: %s", exc)

        prompt = WORKER_SYSTEM.format(
            intent_id=intent_id,
            intent_summary=summary,
            work_dir=work_dir or ".",
            proxy_addr=proxy_addr or "direct",
        )
        prompt = inject_skills_block(prompt, agent_key=worker_name)
        try:
            for s in list_skill_summaries()[:5]:
                await record_skill_stat(exploration.db, s["name"], worker_name)
        except Exception:  # noqa: BLE001
            pass

        try:
            from crewai import Agent, Crew, Process, Task

            agent = Agent(
                role="Worker",
                goal=f"完成探索意图：{summary}",
                backstory="你是 AntiFox worker，只完成当前认领的一条意图并写回事实。",
                tools=tools,
                llm=resolve_crew_llm(),
                verbose=False,
                allow_delegation=False,
            )
            task = Task(
                description=prompt,
                expected_output="一句话总结本意图执行结果；事实/漏洞已写回",
                agent=agent,
            )
            crew = Crew(agents=[agent], tasks=[task], process=Process.sequential, verbose=False)
            # 与 kill 竞速：被 kill 时尽快收尾
            work = asyncio.create_task(asyncio.to_thread(crew.kickoff))
            kill_wait = asyncio.create_task(kill_evt.wait())
            done, pending = await asyncio.wait(
                {work, kill_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            if kill_evt.is_set():
                await exploration.complete_intent(intent_id, "stopped")
                await exploration.append_activity(
                    kind="worker",
                    summary=f"{worker_name} killed intent #{intent_id}",
                    worker=worker_name,
                    node_id=intent_id,
                )
                return {"intent_id": intent_id, "killed": True, "mode": "killed"}
            raw = work.result() if work in done else await work
            await exploration.complete_intent(intent_id, "done")
            await exploration.append_activity(
                kind="worker",
                summary=f"{worker_name} done intent #{intent_id}",
                worker=worker_name,
                node_id=intent_id,
                detail=str(raw)[:4000],
            )
            return {"intent_id": intent_id, "result": str(raw)[:1000], "mode": "crewai"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("worker crew failed: %s", exc)
            if kill_evt.is_set():
                await exploration.complete_intent(intent_id, "stopped")
                return {"intent_id": intent_id, "killed": True, "mode": "killed"}
            result = await _fallback_execute(
                exploration=exploration,
                findings=findings,
                assets=assets,
                task_id=task_id,
                intent_id=intent_id,
                summary=summary,
                worker_name=worker_name,
                work_dir=work_dir,
                proxy_addr=proxy_addr,
                kill_evt=kill_evt,
            )
            if kill_evt.is_set() or result.get("killed"):
                await exploration.complete_intent(intent_id, "stopped")
                return {"intent_id": intent_id, "killed": True, "mode": "killed"}
            await exploration.complete_intent(intent_id, "done")
            await exploration.append_activity(
                kind="worker",
                summary=f"{worker_name} fallback after error: {exc}",
                worker=worker_name,
                node_id=intent_id,
                is_error=True,
            )
            return result
    finally:
        engine.unregister_work(intent_id)


async def _fallback_execute(
    *,
    exploration: ExplorationStore,
    findings: FindingsStore,
    assets: AssetStore,
    task_id: int,
    intent_id: int,
    summary: str,
    worker_name: str,
    work_dir: str | None,
    proxy_addr: str | None,
    kill_evt: asyncio.Event | None = None,
) -> dict[str, Any]:
    """无 LLM 时的最小执行：可选 ping 目标并写回事实。"""
    if kill_evt and kill_evt.is_set():
        return {"intent_id": intent_id, "killed": True, "mode": "killed"}

    note = f"fallback executed intent: {summary}"
    m = re.search(r"https?://[^\s]+", summary)
    detail = ""
    if m:
        url = m.group(0).rstrip(".,)")
        # 经代理出流时带上任务头，便于流量归属
        result = await run_bash(
            f'curl -sS -m 10 -I -H "X-Antifox-Task: {task_id}" {url}',
            cwd=work_dir,
            http_proxy=proxy_addr,
            timeout=15,
            env={"ANTIFOX_TASK_ID": str(task_id)},
        )
        detail = f"curl -I {url}\n{result.stdout}\n{result.stderr}"
        await assets.insert_assets(
            [{"type": "service", "url": url, "service_type": "http"}],
            task_id=task_id,
        )
    if kill_evt and kill_evt.is_set():
        return {"intent_id": intent_id, "killed": True, "mode": "killed"}
    fact_id = await exploration.record_fact(
        summary=note,
        from_intent=intent_id,
        confidence="inferred",
        detail=detail[:4000],
    )
    return {"intent_id": intent_id, "fact_id": fact_id, "mode": "fallback", "result": note}
