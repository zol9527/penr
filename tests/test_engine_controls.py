"""M3：steer / kill / drain / register 单元测（无需 PG）。"""

from __future__ import annotations

import asyncio

import pytest

from penr.orchestration.task_engine import TaskEngine


@pytest.mark.asyncio
async def test_steer_and_kill_queue():
    eng = TaskEngine.__new__(TaskEngine)
    eng._work_cancel = {}
    eng._work_task = {}
    eng._steer_box = {}
    eng._deadline = {}
    eng._coord_started = set()

    with pytest.raises(ValueError):
        eng.steer_work(1, "hello")
    with pytest.raises(ValueError):
        eng.kill_work(1)

    evt = eng.register_work(42, task_id=1)
    assert eng.is_work_running(42)
    assert not eng.is_work_killed(42)

    eng.steer_work(42, "先扫目录")
    eng.steer_work(42, "再测注入")
    assert eng.drain_steer(42) == "先扫目录"
    assert eng.drain_steer(42) == "再测注入"
    assert eng.drain_steer(42) is None

    eng.kill_work(42)
    assert evt.is_set()
    assert eng.is_work_killed(42)
    eng.unregister_work(42)
    assert not eng.is_work_running(42)


@pytest.mark.asyncio
async def test_kill_wakes_waiter():
    eng = TaskEngine.__new__(TaskEngine)
    eng._work_cancel = {}
    eng._work_task = {}
    eng._steer_box = {}
    eng._deadline = {}
    eng._coord_started = set()

    evt = eng.register_work(7, task_id=1)

    async def killer():
        await asyncio.sleep(0.05)
        eng.kill_work(7)

    asyncio.create_task(killer())
    await asyncio.wait_for(evt.wait(), timeout=1.0)
    assert eng.is_work_killed(7)


@pytest.mark.asyncio
async def test_pre_tool_gate_steer(monkeypatch):
    from penr.orchestration import task_engine as te
    from penr.tools.graph_tools import GraphToolContext, _pre_tool_gate

    eng = TaskEngine.__new__(TaskEngine)
    eng._work_cancel = {}
    eng._work_task = {}
    eng._steer_box = {}
    eng._deadline = {}
    eng._coord_started = set()
    monkeypatch.setattr(te, "_engine", eng)

    eng.register_work(99, task_id=3)
    eng.steer_work(99, "改用 POST")

    ctx = GraphToolContext(
        exploration=None,  # type: ignore[arg-type]
        assets=None,  # type: ignore[arg-type]
        findings=None,  # type: ignore[arg-type]
        task_id=3,
        intent_id=99,
    )
    msg = _pre_tool_gate(ctx)
    assert msg is not None and "规划者实时纠偏" in msg
    assert _pre_tool_gate(ctx) is None  # drained

