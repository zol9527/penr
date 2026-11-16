"""领域存储：任务、探索图、资产图、漏洞发现。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from penr.db.engine import Database


def _record_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


def _ts(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def node_to_dto(n: Any) -> dict[str, Any]:
    """对齐 web TaskNode / Go taskNodeDTO。"""
    payload = n["payload"] if "payload" in n.keys() else None
    if isinstance(payload, (dict, list)):
        payload_s = json.dumps(payload, ensure_ascii=False)
    elif payload is None:
        payload_s = ""
    else:
        payload_s = str(payload)
    return {
        "id": str(int(n["id"])),
        "type": n["kind"],
        "payload": payload_s or None,
        "priority": int(n["priority"] or 0),
        "state": n["state"] or "",
        "origin": n["origin"] or "",
        "ts": _ts(n["created_at"]),
    }


def activity_to_dto(a: Any, *, include_detail: bool = True) -> dict[str, Any]:
    """对齐 web Activity / Go activityDTO。"""
    d = _record_to_dict(a)
    out: dict[str, Any] = {
        "seq": int(d["id"]),
        "worker": d.get("worker") or "",
        "ts": _ts(d.get("created_at")),
        "kind": d.get("kind") or "",
        "is_error": bool(d.get("is_error")),
        "summary": d.get("summary") or "",
    }
    if d.get("node_id") is not None:
        out["intent_id"] = str(int(d["node_id"]))
    if d.get("tool"):
        out["tool"] = d["tool"]
    if d.get("tool_use_id"):
        out["tool_use_id"] = d["tool_use_id"]
    if include_detail and d.get("detail"):
        out["detail"] = d["detail"]
    for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        if d.get(key) is not None:
            out[key] = int(d[key])
    return out


def edge_to_dto(e: Any) -> dict[str, Any]:
    return {
        "src": str(int(e["src_id"])),
        "dst": str(int(e["dst_id"])),
        "rel": e["rel"],
    }


@dataclass
class TaskRecord:
    id: int
    name: str
    description: str
    goal: str
    exploration_id: int
    status: str
    paused: bool
    company_id: int | None = None
    llm_profile_id: int | None = None
    created_at: datetime | None = None
    timeout_seconds: int = 0
    deadline_at: datetime | None = None
    first_run_at: datetime | None = None


class TaskStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        description: str,
        goal: str,
        name: str = "",
        company_id: int | None = None,
        llm_profile_id: int | None = None,
        timeout_seconds: int = 0,
    ) -> TaskRecord:
        # 1) 创建 exploration
        exp = await self.db.fetchrow(
            """
            INSERT INTO explorations (description, goal, status)
            VALUES ($1, $2, 'open')
            RETURNING id
            """,
            description,
            goal,
        )
        assert exp is not None
        exp_id = int(exp["id"])

        # 2) begin + origin fact + goal 节点
        begin = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes (exploration_id, kind, payload, state)
            VALUES ($1, 'begin', '{}'::jsonb, 'open')
            RETURNING id
            """,
            exp_id,
        )
        assert begin is not None
        origin = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes (exploration_id, kind, payload, state, origin)
            VALUES ($1, 'fact', $2::jsonb, 'origin', 'system')
            RETURNING id
            """,
            exp_id,
            json.dumps({"summary": description or goal, "role": "origin"}),
        )
        assert origin is not None
        await self.db.execute(
            """
            INSERT INTO exploration_nodes (exploration_id, kind, payload, state)
            VALUES ($1, 'goal', $2::jsonb, 'open')
            """,
            exp_id,
            json.dumps({"summary": goal}),
        )
        await self.db.execute(
            """
            INSERT INTO exploration_edges (exploration_id, src_id, dst_id, rel)
            VALUES ($1, $2, $3, 'spawns')
            """,
            exp_id,
            int(begin["id"]),
            int(origin["id"]),
        )

        task = await self.db.fetchrow(
            """
            INSERT INTO tasks
                (name, description, goal, exploration_id, status, company_id, llm_profile_id, timeout_seconds)
            VALUES ($1, $2, $3, $4, 'created', $5, $6, $7)
            RETURNING id, name, description, goal, exploration_id, status, paused,
                      company_id, llm_profile_id, created_at, timeout_seconds, deadline_at, first_run_at
            """,
            name or goal[:80],
            description,
            goal,
            exp_id,
            company_id,
            llm_profile_id,
            max(0, int(timeout_seconds or 0)),
        )
        assert task is not None
        return TaskRecord(**_record_to_dict(task))

    async def get(self, task_id: int) -> TaskRecord | None:
        row = await self.db.fetchrow(
            """
            SELECT id, name, description, goal, exploration_id, status, paused,
                   company_id, llm_profile_id, created_at, timeout_seconds, deadline_at, first_run_at
            FROM tasks WHERE id = $1 AND deleted_at IS NULL
            """,
            task_id,
        )
        return TaskRecord(**_record_to_dict(row)) if row else None

    async def list(self, limit: int = 50, offset: int = 0) -> list[TaskRecord]:
        rows = await self.db.fetch(
            """
            SELECT id, name, description, goal, exploration_id, status, paused,
                   company_id, llm_profile_id, created_at, timeout_seconds, deadline_at, first_run_at
            FROM tasks WHERE deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT $1 OFFSET $2
            """,
            limit,
            offset,
        )
        return [TaskRecord(**_record_to_dict(r)) for r in rows]

    async def set_status(self, task_id: int, status: str, *, paused: bool | None = None) -> None:
        if paused is None:
            await self.db.execute(
                "UPDATE tasks SET status = $2, updated_at = now() WHERE id = $1",
                task_id,
                status,
            )
        else:
            await self.db.execute(
                "UPDATE tasks SET status = $2, paused = $3, updated_at = now() WHERE id = $1",
                task_id,
                status,
                paused,
            )

    async def mark_first_run(self, task_id: int) -> float | None:
        """盖章 first_run_at；若带 timeout 且尚未有 deadline，则写入绝对截止时刻。

        返回 deadline unix 秒；无超时返回 None。
        """
        row = await self.db.fetchrow(
            """
            UPDATE tasks SET
                first_run_at = COALESCE(first_run_at, now()),
                deadline_at = CASE
                    WHEN timeout_seconds > 0 AND deadline_at IS NULL
                        THEN COALESCE(first_run_at, now())
                             + (timeout_seconds * INTERVAL '1 second')
                    ELSE deadline_at
                END,
                status = 'running',
                paused = false,
                updated_at = now()
            WHERE id = $1
            RETURNING deadline_at, timeout_seconds
            """,
            task_id,
        )
        if not row or not row["timeout_seconds"] or row["deadline_at"] is None:
            return None
        dl = row["deadline_at"]
        return dl.timestamp() if hasattr(dl, "timestamp") else float(dl)

    async def clear_deadline(self, task_id: int) -> None:
        """rerun 时清掉绝对 deadline，下次 mark_first_run 重新盖章。"""
        await self.db.execute(
            """
            UPDATE tasks
            SET deadline_at = NULL, first_run_at = NULL, updated_at = now()
            WHERE id = $1
            """,
            task_id,
        )


class ExplorationStore:
    def __init__(self, db: Database, exploration_id: int) -> None:
        self.db = db
        self.exploration_id = exploration_id

    async def origin_fact_id(self) -> int | None:
        row = await self.db.fetchrow(
            """
            SELECT id FROM exploration_nodes
            WHERE exploration_id = $1 AND kind = 'fact' AND state = 'origin'
            ORDER BY id LIMIT 1
            """,
            self.exploration_id,
        )
        return int(row["id"]) if row else None

    async def add_intent(
        self,
        *,
        summary: str,
        asset_ids: list[int] | None = None,
        parent_ids: list[int] | None = None,
        priority: int = 0,
        owner: str = "planner",
    ) -> int:
        payload = {"summary": summary, "asset_ids": asset_ids or []}
        row = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes
                (exploration_id, kind, payload, priority, state, origin, owner)
            VALUES ($1, 'intent', $2::jsonb, $3, 'open', $4, $5)
            RETURNING id
            """,
            self.exploration_id,
            json.dumps(payload, ensure_ascii=False),
            priority,
            "planner",
            owner,
        )
        assert row is not None
        intent_id = int(row["id"])
        origin = await self.origin_fact_id()
        if origin:
            await self.db.execute(
                """
                INSERT INTO exploration_edges (exploration_id, src_id, dst_id, rel)
                VALUES ($1, $2, $3, 'spawns')
                ON CONFLICT DO NOTHING
                """,
                self.exploration_id,
                origin,
                intent_id,
            )
        for pid in parent_ids or []:
            await self.db.execute(
                """
                INSERT INTO exploration_edges (exploration_id, src_id, dst_id, rel)
                VALUES ($1, $2, $3, 'spawns')
                ON CONFLICT DO NOTHING
                """,
                self.exploration_id,
                pid,
                intent_id,
            )
        for aid in asset_ids or []:
            await self.db.execute(
                """
                INSERT INTO exploration_anchors (node_id, asset_id)
                VALUES ($1, $2) ON CONFLICT DO NOTHING
                """,
                intent_id,
                aid,
            )
        return intent_id

    async def claim_intent(self, *, worker: str) -> dict[str, Any] | None:
        """原子认领一条 open intent（FOR UPDATE SKIP LOCKED）。"""
        row = await self.db.fetchrow(
            """
            WITH cte AS (
                SELECT id FROM exploration_nodes
                WHERE exploration_id = $1 AND kind = 'intent' AND state = 'open'
                ORDER BY priority DESC, id ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE exploration_nodes n
            SET state = 'running', owner = $2, updated_at = now()
            FROM cte WHERE n.id = cte.id
            RETURNING n.id, n.payload, n.priority, n.owner, n.state
            """,
            self.exploration_id,
            worker,
        )
        if not row:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return {
            "id": int(row["id"]),
            "payload": payload,
            "priority": int(row["priority"]),
            "owner": row["owner"],
            "state": row["state"],
        }

    async def complete_intent(self, intent_id: int, state: str = "done") -> None:
        await self.db.execute(
            """
            UPDATE exploration_nodes
            SET state = $2, completed_at = now(), updated_at = now()
            WHERE id = $1 AND exploration_id = $3
            """,
            intent_id,
            state,
            self.exploration_id,
        )

    async def record_fact(
        self,
        *,
        summary: str,
        from_intent: int | None = None,
        confidence: str = "observed",
        detail: str = "",
    ) -> int:
        payload = {
            "summary": summary,
            "confidence": confidence,
            "detail": detail,
            "from_intent": from_intent,
        }
        row = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes
                (exploration_id, kind, payload, state, origin)
            VALUES ($1, 'fact', $2::jsonb, 'confirmed', 'worker')
            RETURNING id
            """,
            self.exploration_id,
            json.dumps(payload, ensure_ascii=False),
        )
        assert row is not None
        fact_id = int(row["id"])
        if from_intent:
            await self.db.execute(
                """
                INSERT INTO exploration_edges (exploration_id, src_id, dst_id, rel)
                VALUES ($1, $2, $3, 'yields')
                ON CONFLICT DO NOTHING
                """,
                self.exploration_id,
                from_intent,
                fact_id,
            )
        return fact_id

    async def report_finding_node(
        self,
        *,
        summary: str,
        vulnclass: str = "",
        severity: str = "",
        evidence: str = "",
        from_intent: int | None = None,
        reproduction: dict[str, Any] | None = None,
    ) -> int:
        payload = {
            "summary": summary,
            "vulnclass": vulnclass,
            "severity": severity,
            "evidence": evidence,
            "reproduction": reproduction or {},
        }
        row = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes
                (exploration_id, kind, payload, state, origin)
            VALUES ($1, 'finding', $2::jsonb, 'confirmed', 'worker')
            RETURNING id
            """,
            self.exploration_id,
            json.dumps(payload, ensure_ascii=False),
        )
        assert row is not None
        finding_id = int(row["id"])
        if from_intent:
            await self.db.execute(
                """
                INSERT INTO exploration_edges (exploration_id, src_id, dst_id, rel)
                VALUES ($1, $2, $3, 'yields')
                ON CONFLICT DO NOTHING
                """,
                self.exploration_id,
                from_intent,
                finding_id,
            )
        return finding_id

    async def add_hint(self, summary: str) -> int:
        row = await self.db.fetchrow(
            """
            INSERT INTO exploration_nodes
                (exploration_id, kind, payload, state, origin)
            VALUES ($1, 'hint', $2::jsonb, 'active', 'main')
            RETURNING id
            """,
            self.exploration_id,
            json.dumps({"summary": summary}, ensure_ascii=False),
        )
        assert row is not None
        return int(row["id"])

    async def graph_overview(self) -> dict[str, Any]:
        nodes = await self.db.fetch(
            """
            SELECT id, kind, payload, priority, state, owner, created_at, completed_at
            FROM exploration_nodes WHERE exploration_id = $1
            ORDER BY id
            """,
            self.exploration_id,
        )
        exp = await self.db.fetchrow(
            "SELECT id, goal, status, description FROM explorations WHERE id = $1",
            self.exploration_id,
        )

        def _payload(n: Any) -> dict[str, Any]:
            p = n["payload"]
            if isinstance(p, str):
                return json.loads(p)
            return dict(p) if p else {}

        intents = [n for n in nodes if n["kind"] == "intent"]
        facts = [n for n in nodes if n["kind"] == "fact"]
        findings = [n for n in nodes if n["kind"] == "finding"]
        goals = [n for n in nodes if n["kind"] == "goal"]

        def _brief(n: Any) -> dict[str, Any]:
            p = _payload(n)
            return {
                "id": int(n["id"]),
                "summary": p.get("summary", ""),
                "state": n["state"],
                "priority": n["priority"],
                "owner": n["owner"],
            }

        return {
            "task": {
                "exploration_id": self.exploration_id,
                "goal": exp["goal"] if exp else "",
                "status": exp["status"] if exp else "",
                "description": exp["description"] if exp else "",
            },
            "goals": [_brief(g) for g in goals],
            "open_intents": [_brief(i) for i in intents if i["state"] == "open"],
            "running_intents": [_brief(i) for i in intents if i["state"] == "running"],
            "recent_done_intents": [
                _brief(i) for i in intents if i["state"] in ("done", "blocked", "exhausted", "stopped")
            ][-20:],
            "facts": len(facts),
            "findings": len(findings),
            "recent_facts": [
                {
                    "id": int(f["id"]),
                    "summary": _payload(f).get("summary", ""),
                    "confidence": _payload(f).get("confidence"),
                    "from_intent": _payload(f).get("from_intent"),
                }
                for f in facts
                if f["state"] != "origin"
            ][-30:],
            "active_hints": [
                _brief(h) for h in nodes if h["kind"] == "hint" and h["state"] == "active"
            ],
        }

    async def append_activity(
        self,
        *,
        kind: str,
        summary: str,
        worker: str = "",
        tool: str = "",
        detail: str = "",
        node_id: int | None = None,
        is_error: bool = False,
        tool_use_id: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        broadcast: bool = True,
    ) -> int:
        row = await self.db.fetchrow(
            """
            INSERT INTO activity
                (exploration_id, node_id, worker, kind, tool, tool_use_id, is_error,
                 summary, detail, input_tokens, output_tokens,
                 cache_read_tokens, cache_write_tokens)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
            RETURNING id, exploration_id, node_id, worker, kind, tool, tool_use_id,
                      is_error, summary, detail, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, created_at
            """,
            self.exploration_id,
            node_id,
            worker,
            kind,
            tool,
            tool_use_id,
            is_error,
            summary,
            detail,
            input_tokens,
            output_tokens,
            cache_read_tokens,
            cache_write_tokens,
        )
        assert row is not None
        aid = int(row["id"])
        if broadcast:
            # 广播对齐 Go ActivityDTO，供 /exploration/activity/stream 推送
            try:
                from penr.orchestration.broadcast import get_broadcaster

                task_id = await self.db.fetchval(
                    "SELECT id FROM tasks WHERE exploration_id = $1 AND deleted_at IS NULL",
                    self.exploration_id,
                )
                if task_id is not None:
                    dto = activity_to_dto(row, include_detail=True)
                    await get_broadcaster().publish(str(int(task_id)), dto)
            except Exception:  # noqa: BLE001
                pass
        return aid

    async def list_activity_since(self, since_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, exploration_id, node_id, worker, kind, tool, tool_use_id, is_error,
                   summary, detail, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, created_at
            FROM activity
            WHERE exploration_id = $1 AND id > $2
            ORDER BY id ASC
            LIMIT $3
            """,
            self.exploration_id,
            since_id,
            limit,
        )
        return [_record_to_dict(r) for r in rows]

    async def activity_list(
        self,
        *,
        since: int = 0,
        limit: int = 300,
        intent_id: int | None = None,
        include_detail: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """增量 activity；返回 (dto 列表, cursor=最大 id)。"""
        args: list[Any] = [self.exploration_id, since]
        where = "exploration_id = $1 AND id > $2"
        if intent_id is not None:
            args.append(intent_id)
            where += f" AND node_id = ${len(args)}"
        args.append(max(1, min(limit, 2000)))
        rows = await self.db.fetch(
            f"""
            SELECT id, exploration_id, node_id, worker, kind, tool, tool_use_id, is_error,
                   summary, detail, input_tokens, output_tokens,
                   cache_read_tokens, cache_write_tokens, created_at
            FROM activity
            WHERE {where}
            ORDER BY id ASC
            LIMIT ${len(args)}
            """,
            *args,
        )
        items = [activity_to_dto(r, include_detail=include_detail) for r in rows]
        cursor = int(rows[-1]["id"]) if rows else since
        return items, cursor

    async def frontier(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, kind, payload, priority, state, origin, created_at
            FROM exploration_nodes
            WHERE exploration_id = $1 AND kind = 'intent' AND state = 'open'
            ORDER BY priority DESC, id ASC
            LIMIT $2
            """,
            self.exploration_id,
            max(1, min(limit, 500)),
        )
        return [node_to_dto(r) for r in rows]

    async def list_by_kind(self, kind: str, limit: int = 300) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, kind, payload, priority, state, origin, created_at
            FROM exploration_nodes
            WHERE exploration_id = $1 AND kind = $2
            ORDER BY id DESC
            LIMIT $3
            """,
            self.exploration_id,
            kind,
            max(1, min(limit, 2000)),
        )
        return [node_to_dto(r) for r in rows]

    async def graph_nodes_edges(
        self, *, node_limit: int = 2000, edge_limit: int = 5000
    ) -> dict[str, Any]:
        nodes = await self.db.fetch(
            """
            SELECT id, kind, payload, priority, state, origin, created_at
            FROM exploration_nodes WHERE exploration_id = $1
            ORDER BY id ASC LIMIT $2
            """,
            self.exploration_id,
            max(1, min(node_limit, 5000)),
        )
        edges = await self.db.fetch(
            """
            SELECT src_id, dst_id, rel
            FROM exploration_edges WHERE exploration_id = $1
            ORDER BY created_at ASC LIMIT $2
            """,
            self.exploration_id,
            max(1, min(edge_limit, 10000)),
        )
        return {
            "nodes": [node_to_dto(n) for n in nodes],
            "edges": [edge_to_dto(e) for e in edges],
        }

    async def token_stats_by_worker(self) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows = await self.db.fetch(
            """
            SELECT COALESCE(worker, '') AS worker,
                   COALESCE(SUM(input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                   COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens
            FROM activity
            WHERE exploration_id = $1
            GROUP BY worker
            ORDER BY worker
            """,
            self.exploration_id,
        )
        workers = [
            {
                "worker": r["worker"] or "",
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "cache_read_tokens": int(r["cache_read_tokens"] or 0),
                "cache_write_tokens": int(r["cache_write_tokens"] or 0),
            }
            for r in rows
        ]
        total = {
            "input_tokens": sum(w["input_tokens"] for w in workers),
            "output_tokens": sum(w["output_tokens"] for w in workers),
            "cache_read_tokens": sum(w["cache_read_tokens"] for w in workers),
            "cache_write_tokens": sum(w["cache_write_tokens"] for w in workers),
        }
        return workers, total

    async def exploration_counts(self) -> dict[str, int]:
        rows = await self.db.fetch(
            """
            SELECT kind, state, COUNT(*)::int AS n
            FROM exploration_nodes WHERE exploration_id = $1
            GROUP BY kind, state
            """,
            self.exploration_id,
        )
        out = {
            "intents_open": 0,
            "intents_running": 0,
            "intents_done": 0,
            "facts": 0,
            "findings": 0,
            "goals_total": 0,
            "goals_met": 0,
        }
        for r in rows:
            kind, state, n = r["kind"], r["state"], int(r["n"])
            if kind == "intent":
                if state == "open":
                    out["intents_open"] += n
                elif state == "running":
                    out["intents_running"] += n
                elif state in ("done", "blocked", "exhausted", "stopped"):
                    out["intents_done"] += n
            elif kind == "fact" and state != "origin":
                out["facts"] += n
            elif kind == "finding":
                out["findings"] += n
            elif kind == "goal":
                out["goals_total"] += n
                if state == "met":
                    out["goals_met"] += n
        return out

    async def prove_goal(self, goal_id: int, reason: str = "") -> None:
        await self.db.execute(
            """
            UPDATE exploration_nodes
            SET state = 'met',
                payload = payload || $3::jsonb,
                updated_at = now(),
                completed_at = now()
            WHERE id = $1 AND exploration_id = $2 AND kind = 'goal'
            """,
            goal_id,
            self.exploration_id,
            json.dumps({"prove_reason": reason}, ensure_ascii=False),
        )
        open_goals = await self.db.fetchval(
            """
            SELECT COUNT(*) FROM exploration_nodes
            WHERE exploration_id = $1 AND kind = 'goal' AND state = 'open'
            """,
            self.exploration_id,
        )
        if open_goals == 0:
            await self.db.execute(
                "UPDATE explorations SET status = 'achieved', updated_at = now() WHERE id = $1",
                self.exploration_id,
            )


class AssetStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_assets(self, assets: list[dict[str, Any]], *, task_id: int | None = None) -> list[int]:
        ids: list[int] = []
        for a in assets:
            asset_type = a.get("type") or "endpoint"
            url = a.get("url")
            domain = a.get("domain")
            ip = a.get("ip")
            method = a.get("method")
            port = a.get("port")
            company_id = a.get("company_id")
            extra = a.get("extra") or {}
            row = await self.db.fetchrow(
                """
                INSERT INTO assets (type, company_id, task_ids, domain, ip, port, url, method, extra)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                asset_type,
                company_id,
                [task_id] if task_id else [],
                domain,
                ip,
                port,
                url,
                method,
                json.dumps(extra, ensure_ascii=False),
            )
            if row:
                ids.append(int(row["id"]))
            elif url:
                existing = await self.db.fetchval(
                    "SELECT id FROM assets WHERE url = $1 LIMIT 1", url
                )
                if existing:
                    ids.append(int(existing))
        return ids

    async def list_assets(
        self,
        *,
        q: str | None = None,
        asset_type: str | None = None,
        task_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses = ["TRUE"]
        args: list[Any] = []
        if asset_type:
            args.append(asset_type)
            clauses.append(f"type = ${len(args)}")
        if task_id is not None:
            args.append(task_id)
            clauses.append(f"${len(args)} = ANY(task_ids)")
        if q:
            args.append(f"%{q}%")
            n = len(args)
            clauses.append(
                f"(domain ILIKE ${n} OR url ILIKE ${n} OR ip ILIKE ${n} OR app_name ILIKE ${n})"
            )
        args.extend([limit, offset])
        where = " AND ".join(clauses)
        rows = await self.db.fetch(
            f"""
            SELECT id, type, company_id, task_ids, domain, ip, port, url, method,
                   service_name, page_title, technologies, extra, created_at, last_seen
            FROM assets
            WHERE {where}
            ORDER BY last_seen DESC
            LIMIT ${len(args) - 1} OFFSET ${len(args)}
            """,
            *args,
        )
        return [_record_to_dict(r) for r in rows]

    async def count(self) -> int:
        return int(await self.db.fetchval("SELECT COUNT(*) FROM assets") or 0)

    async def counts_by_type(self) -> dict[str, int]:
        rows = await self.db.fetch(
            "SELECT type, COUNT(*)::int AS n FROM assets GROUP BY type"
        )
        return {str(r["type"]): int(r["n"]) for r in rows}


class FindingsStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        task_id: int | None,
        node_id: int | None,
        vulnclass: str,
        severity: str,
        summary: str,
        evidence: str = "",
        worker: str = "",
        asset_ids: list[int] | None = None,
        reproduction: dict[str, Any] | None = None,
        traffic_ids: list[int] | None = None,
    ) -> int:
        repro = reproduction or {}
        if traffic_ids:
            repro = {**repro, "traffic_ids": traffic_ids}
        row = await self.db.fetchrow(
            """
            INSERT INTO findings
                (task_id, node_id, vulnclass, severity, summary, evidence, worker, asset_ids, reproduction)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)
            RETURNING id
            """,
            task_id,
            node_id,
            vulnclass,
            severity,
            summary,
            evidence,
            worker,
            json.dumps(asset_ids or []),
            json.dumps(repro, ensure_ascii=False),
        )
        assert row is not None
        finding_id = int(row["id"])
        # 自动证据链：写入 finding_traffic（source=auto）
        if node_id is not None and traffic_ids:
            await self.link_traffic(
                int(node_id),
                [str(x) for x in traffic_ids],
                source="auto",
            )
        return finding_id

    async def list_for_task(self, task_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, task_id, node_id, vulnclass, severity, summary, evidence,
                   worker, asset_ids, reproduction, created_at
            FROM findings WHERE task_id = $1
            ORDER BY created_at DESC
            """,
            task_id,
        )
        return [_record_to_dict(r) for r in rows]

    async def get(self, finding_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT id, task_id, node_id, vulnclass, severity, summary, evidence,
                   worker, asset_ids, reproduction, created_at
            FROM findings WHERE id = $1
            """,
            finding_id,
        )
        return _record_to_dict(row) if row else None

    async def attach_traffic(self, finding_id: int, traffic_ids: list[int]) -> None:
        row = await self.get(finding_id)
        if not row:
            return
        repro = row.get("reproduction") or {}
        if isinstance(repro, str):
            repro = json.loads(repro)
        repro = dict(repro)
        repro["traffic_ids"] = traffic_ids
        await self.db.execute(
            "UPDATE findings SET reproduction = $2::jsonb WHERE id = $1",
            finding_id,
            json.dumps(repro, ensure_ascii=False),
        )
        node_id = row.get("node_id")
        if node_id is not None:
            await self.link_traffic(int(node_id), [str(x) for x in traffic_ids], source="manual")

    async def link_traffic(
        self,
        node_id: int,
        exchange_ids: list[str],
        *,
        source: str = "manual",
    ) -> int:
        """把 exchange 关联到漏洞节点（幂等；对齐 Go LinkFindingTraffic）。"""
        linked = 0
        for eid in exchange_ids:
            eid = str(eid).strip()
            if not eid:
                continue
            await self.db.execute(
                """
                INSERT INTO finding_traffic (node_id, exchange_id, source)
                VALUES ($1, $2, $3)
                ON CONFLICT (node_id, exchange_id) DO NOTHING
                """,
                node_id,
                eid,
                source or "manual",
            )
            linked += 1
        return linked

    async def list_traffic_ids(self, node_id: int) -> list[str]:
        rows = await self.db.fetch(
            """
            SELECT exchange_id FROM finding_traffic
            WHERE node_id = $1
            ORDER BY created_at ASC
            """,
            node_id,
        )
        return [str(r["exchange_id"]) for r in rows]

    async def unlink_traffic(self, node_id: int, exchange_id: str) -> None:
        await self.db.execute(
            "DELETE FROM finding_traffic WHERE node_id = $1 AND exchange_id = $2",
            node_id,
            str(exchange_id),
        )

    async def resolve_node_id(self, finding_or_node_id: int) -> int | None:
        """兼容 finding.id 或 exploration node_id。"""
        row = await self.get(finding_or_node_id)
        if row and row.get("node_id") is not None:
            return int(row["node_id"])
        # 可能本身就是 node_id
        exists = await self.db.fetchval(
            "SELECT 1 FROM exploration_nodes WHERE id = $1 AND kind = 'finding'",
            finding_or_node_id,
        )
        return int(finding_or_node_id) if exists else None

    async def list_all(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT f.id, f.task_id, f.node_id, f.vulnclass, f.severity, f.summary,
                   f.evidence, f.worker, f.created_at, t.description AS task_description
            FROM findings f
            LEFT JOIN tasks t ON t.id = f.task_id
            ORDER BY f.created_at DESC
            LIMIT $1
            """,
            max(1, min(limit, 2000)),
        )
        return [_record_to_dict(r) for r in rows]


class CompanyStore:
    """企业 + company_scope 最小 CRUD（对齐 web /api/companies）。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _nkey(name: str) -> str:
        return " ".join(name.lower().split())

    async def list(self, task_id: int | None = None) -> list[dict[str, Any]]:
        if task_id:
            rows = await self.db.fetch(
                """
                SELECT c.id, c.name, c.nkey, c.logo, c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM assets a WHERE a.company_id = c.id)::int AS asset_count
                FROM companies c
                WHERE EXISTS (
                    SELECT 1 FROM assets a
                    WHERE a.company_id = c.id AND $1 = ANY(a.task_ids)
                )
                ORDER BY c.id
                """,
                task_id,
            )
        else:
            rows = await self.db.fetch(
                """
                SELECT c.id, c.name, c.nkey, c.logo, c.created_at, c.updated_at,
                       (SELECT COUNT(*) FROM assets a WHERE a.company_id = c.id)::int AS asset_count
                FROM companies c
                ORDER BY c.id
                """
            )
        out = []
        for r in rows:
            d = _record_to_dict(r)
            d["created_at"] = _ts(d.get("created_at"))
            d["updated_at"] = _ts(d.get("updated_at"))
            out.append(d)
        return out

    async def get(self, company_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT id, name, nkey, logo, created_at, updated_at FROM companies WHERE id = $1",
            company_id,
        )
        if not row:
            return None
        d = _record_to_dict(row)
        d["created_at"] = _ts(d.get("created_at"))
        d["updated_at"] = _ts(d.get("updated_at"))
        return d

    async def upsert(self, name: str, logo: str = "") -> tuple[int, bool]:
        nkey = self._nkey(name)
        row = await self.db.fetchrow(
            """
            INSERT INTO companies (name, nkey, logo)
            VALUES ($1, $2, NULLIF($3, ''))
            ON CONFLICT (nkey) DO UPDATE SET
                name = EXCLUDED.name,
                logo = COALESCE(EXCLUDED.logo, companies.logo),
                updated_at = now()
            RETURNING id, (xmax = 0) AS created
            """,
            name,
            nkey,
            logo,
        )
        assert row is not None
        return int(row["id"]), bool(row["created"])

    async def delete(self, company_id: int, *, delete_assets: bool = False) -> tuple[int, int]:
        assets_deleted = 0
        if delete_assets:
            r = await self.db.execute("DELETE FROM assets WHERE company_id = $1", company_id)
            # asyncpg: "DELETE N"
            try:
                assets_deleted = int(r.split()[-1])
            except Exception:  # noqa: BLE001
                assets_deleted = 0
        else:
            await self.db.execute(
                "UPDATE assets SET company_id = NULL WHERE company_id = $1", company_id
            )
        r = await self.db.execute("DELETE FROM companies WHERE id = $1", company_id)
        try:
            deleted = int(r.split()[-1])
        except Exception:  # noqa: BLE001
            deleted = 0
        return deleted, assets_deleted

    async def get_scope(self, company_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetch(
            """
            SELECT id, company_id, kind, domain, net::text AS net, raw, reason, created_at
            FROM company_scope WHERE company_id = $1 ORDER BY id
            """,
            company_id,
        )
        out = []
        for r in rows:
            d = _record_to_dict(r)
            d["created_at"] = _ts(d.get("created_at"))
            out.append(d)
        return out

    @staticmethod
    def _parse_scope_line(line: str) -> tuple[str, str | None, str | None] | None:
        """返回 (kind, domain, net)；无法解析则 None。"""
        raw = line.strip()
        if not raw or raw.startswith("#"):
            return None
        # 粗分：含 / 或纯 IPv4 → ip/cidr；否则当 domain
        if "/" in raw or (raw.replace(".", "").isdigit() and raw.count(".") == 3):
            kind = "cidr" if "/" in raw else "ip"
            return kind, None, raw
        domain = raw.lower().lstrip("*.")
        if not domain or " " in domain:
            return None
        return "domain", domain, None

    async def add_scope(
        self, company_id: int, lines: list[str], reason: str = "", *, reset: bool = False
    ) -> dict[str, Any]:
        if reset:
            await self.db.execute("DELETE FROM company_scope WHERE company_id = $1", company_id)
        added = skipped = invalid = 0
        errors: list[str] = []
        for line in lines:
            parsed = self._parse_scope_line(line)
            if not parsed:
                invalid += 1
                errors.append(f"{line}: invalid")
                continue
            kind, domain, net = parsed
            try:
                if kind == "domain":
                    r = await self.db.execute(
                        """
                        INSERT INTO company_scope (company_id, kind, domain, raw, reason)
                        VALUES ($1, 'domain', $2, $3, $4)
                        ON CONFLICT ON CONSTRAINT uq_sv2_domain DO NOTHING
                        """,
                        company_id,
                        domain,
                        line.strip(),
                        reason or "api",
                    )
                else:
                    r = await self.db.execute(
                        """
                        INSERT INTO company_scope (company_id, kind, net, raw, reason)
                        VALUES ($1, $2, $3::cidr, $4, $5)
                        ON CONFLICT ON CONSTRAINT uq_sv2_net DO NOTHING
                        """,
                        company_id,
                        kind,
                        net,
                        line.strip(),
                        reason or "api",
                    )
                n = int(r.split()[-1]) if r else 0
                if n > 0:
                    added += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                invalid += 1
                errors.append(f"{line}: {exc}")
        out: dict[str, Any] = {"added": added, "skipped": skipped, "invalid": invalid}
        if errors:
            out["errors"] = errors
        return out


async def get_setting(db: Database, key: str, default: str = "") -> str:
    row = await db.fetchval("SELECT value FROM settings WHERE key = $1", key)
    return str(row) if row is not None else default


async def set_setting(db: Database, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        key,
        value,
    )


async def get_setting_bool(db: Database, key: str, default: bool = False) -> bool:
    v = await get_setting(db, key, "true" if default else "false")
    return v.lower() in ("1", "true", "yes", "on")


async def resolve_task_by_param(db: Database, task_param: str | None) -> TaskRecord | None:
    """?task= 解析：空 → 最近非删除任务；数字 → 指定任务。"""
    store = TaskStore(db)
    if not task_param or task_param == "active":
        items = await store.list(limit=1, offset=0)
        return items[0] if items else None
    try:
        tid = int(task_param)
    except ValueError:
        return None
    return await store.get(tid)
