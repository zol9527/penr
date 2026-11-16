"""Finding enrichment：按 exploration node_id 读写 + 异步 AI/启发式富化。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

from penr.db.engine import Database

logger = logging.getLogger(__name__)

_enrich_jobs: dict[int, dict[str, Any]] = {}
_enrich_lock = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_enrich_job(node_id: int) -> dict[str, Any] | None:
    job = _enrich_jobs.get(node_id)
    return dict(job) if job else None


def _enrichment_dto(row: Any) -> dict[str, Any]:
    def _j(val: Any) -> Any:
        if isinstance(val, str):
            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return val
        return val

    return {
        "node_id": int(row["node_id"]),
        "name": row["name"] or "",
        "target": row["target"] or "",
        "module": row["module"] or "",
        "severity": row["severity"] or "",
        "status": row["status"] or "open",
        "cvss_score": float(row["cvss_score"] or 0),
        "cvss_vector": row["cvss_vector"] or "",
        "cvss_metrics": _j(row["cvss_metrics"]) or {},
        "description": _j(row["description"]) or [],
        "test_process": _j(row["test_process"]) or [],
        "risk_analysis": _j(row["risk_analysis"]) or [],
        "remediation": _j(row["remediation"]) or [],
        "source": row["source"] or "ai",
        "updated_at": row["updated_at"],
    }


class EnrichService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_node(self, node_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT id, exploration_id, kind, payload, state, origin, created_at
            FROM exploration_nodes WHERE id = $1
            """,
            node_id,
        )
        return dict(row) if row else None

    async def get_finding_row(self, node_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            """
            SELECT id, task_id, node_id, vulnclass, severity, summary, evidence,
                   worker, asset_ids, reproduction, created_at
            FROM findings WHERE node_id = $1
            ORDER BY id DESC LIMIT 1
            """,
            node_id,
        )
        return dict(row) if row else None

    async def get_enrichment(self, node_id: int) -> dict[str, Any] | None:
        row = await self.db.fetchrow(
            "SELECT * FROM finding_enrichment WHERE node_id = $1",
            node_id,
        )
        return _enrichment_dto(row) if row else None

    async def detail(self, node_id: int) -> dict[str, Any]:
        node = await self.get_node(node_id)
        if not node:
            raise LookupError("node not found")
        finding = await self.get_finding_row(node_id)
        payload = node.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        finding_dto = finding or {
            "id": None,
            "node_id": node_id,
            "summary": payload.get("summary") or "",
            "vulnclass": payload.get("vulnclass") or "",
            "severity": payload.get("severity") or "",
            "evidence": payload.get("evidence") or "",
            "reproduction": payload.get("reproduction") or {},
        }
        enrichment = await self.get_enrichment(node_id)
        repro = finding_dto.get("reproduction") if finding_dto else payload.get("reproduction")
        return {
            "finding": finding_dto,
            "enrichment": enrichment,
            "reproduction": repro or {},
        }

    async def upsert_manual(self, node_id: int, patch: dict[str, Any]) -> dict[str, Any]:
        cur = await self.get_enrichment(node_id)
        base = cur or {
            "node_id": node_id,
            "name": "",
            "target": "",
            "module": "",
            "severity": "",
            "status": "open",
            "cvss_score": 0,
            "cvss_vector": "",
            "cvss_metrics": {},
            "description": [],
            "test_process": [],
            "risk_analysis": [],
            "remediation": [],
            "source": "manual",
        }
        for k in ("name", "target", "module", "severity", "status", "cvss_vector"):
            if k in patch and patch[k] is not None:
                base[k] = patch[k]
        if "cvss_score" in patch and patch["cvss_score"] is not None:
            base["cvss_score"] = float(patch["cvss_score"])
        for k in ("description", "test_process", "risk_analysis", "remediation", "cvss_metrics"):
            if k in patch and patch[k] is not None:
                base[k] = patch[k]
        base["source"] = "manual" if cur and cur.get("source") == "manual" else base.get("source") or "manual"
        if patch.get("source"):
            base["source"] = patch["source"]

        await self.db.execute(
            """
            INSERT INTO finding_enrichment
                (node_id, name, target, module, severity, status, cvss_score, cvss_vector,
                 cvss_metrics, description, test_process, risk_analysis, remediation, source, updated_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10::jsonb,$11::jsonb,$12::jsonb,$13::jsonb,$14, now())
            ON CONFLICT (node_id) DO UPDATE SET
                name=EXCLUDED.name, target=EXCLUDED.target, module=EXCLUDED.module,
                severity=EXCLUDED.severity, status=EXCLUDED.status, cvss_score=EXCLUDED.cvss_score,
                cvss_vector=EXCLUDED.cvss_vector, cvss_metrics=EXCLUDED.cvss_metrics,
                description=EXCLUDED.description, test_process=EXCLUDED.test_process,
                risk_analysis=EXCLUDED.risk_analysis, remediation=EXCLUDED.remediation,
                source=EXCLUDED.source, updated_at=now()
            """,
            node_id,
            base["name"],
            base["target"],
            base.get("module") or "",
            base["severity"],
            base["status"],
            float(base.get("cvss_score") or 0),
            base.get("cvss_vector") or "",
            json.dumps(base.get("cvss_metrics") or {}),
            json.dumps(base.get("description") or [], ensure_ascii=False),
            json.dumps(base.get("test_process") or [], ensure_ascii=False),
            json.dumps(base.get("risk_analysis") or [], ensure_ascii=False),
            json.dumps(base.get("remediation") or [], ensure_ascii=False),
            base.get("source") or "manual",
        )
        out = await self.get_enrichment(node_id)
        assert out is not None
        return out

    async def start_enrich(
        self,
        node_id: int,
        *,
        locale: str = "zh-CN",
        company: str = "",
    ) -> dict[str, Any]:
        async with _enrich_lock:
            cur = _enrich_jobs.get(node_id)
            if cur and cur.get("status") == "analyzing":
                return dict(cur)
            job = {
                "status": "analyzing",
                "error": None,
                "started_at": _now_iso(),
                "enrichment": None,
            }
            _enrich_jobs[node_id] = job

        asyncio.create_task(self._run_enrich(node_id, locale=locale, company=company))
        return dict(job)

    async def status(self, node_id: int) -> dict[str, Any]:
        live = get_enrich_job(node_id)
        if live:
            return live
        enrichment = await self.get_enrichment(node_id)
        if enrichment:
            return {
                "status": "done",
                "error": None,
                "started_at": None,
                "enrichment": enrichment,
            }
        return {"status": "idle", "error": None, "started_at": None, "enrichment": None}

    async def _run_enrich(self, node_id: int, *, locale: str, company: str) -> None:
        try:
            detail = await self.detail(node_id)
            finding = detail["finding"] or {}
            summary = finding.get("summary") or ""
            severity = finding.get("severity") or "medium"
            evidence = finding.get("evidence") or ""
            vulnclass = finding.get("vulnclass") or ""
            # 启发式富化（无 LLM 时仍可写结构化字段；有 LLM 可后续扩展）
            enrichment = {
                "name": summary or f"Finding #{node_id}",
                "target": "",
                "module": vulnclass,
                "severity": severity,
                "status": "open",
                "cvss_score": _severity_to_score(severity),
                "cvss_vector": "",
                "cvss_metrics": {},
                "description": [
                    summary,
                    evidence[:1000] if evidence else "",
                ],
                "test_process": [
                    {"type": "text", "content": f"依据节点 #{node_id} 的复现信息与证据进行验证。"},
                ],
                "risk_analysis": [
                    f"严重性评估为 {severity}。",
                    "建议结合业务影响进一步确认。",
                ],
                "remediation": [
                    "修复相关漏洞并回归验证。",
                    "补充访问控制与输入校验。",
                ],
                "source": "ai",
            }
            # 过滤空 description
            enrichment["description"] = [d for d in enrichment["description"] if d]
            saved = await self.upsert_manual(node_id, enrichment)
            # upsert_manual 会标 manual；强制 source=ai
            await self.db.execute(
                "UPDATE finding_enrichment SET source='ai', updated_at=now() WHERE node_id=$1",
                node_id,
            )
            saved = await self.get_enrichment(node_id)
            async with _enrich_lock:
                _enrich_jobs[node_id] = {
                    "status": "done",
                    "error": None,
                    "started_at": _enrich_jobs.get(node_id, {}).get("started_at"),
                    "enrichment": saved,
                }
        except Exception as exc:  # noqa: BLE001
            logger.exception("enrich failed node=%s: %s", node_id, exc)
            async with _enrich_lock:
                _enrich_jobs[node_id] = {
                    "status": "failed",
                    "error": str(exc)[:500],
                    "started_at": _enrich_jobs.get(node_id, {}).get("started_at"),
                    "enrichment": None,
                }


def _severity_to_score(sev: str) -> float:
    m = {
        "critical": 9.5,
        "high": 8.0,
        "medium": 5.5,
        "low": 3.0,
        "info": 0.0,
        "informational": 0.0,
    }
    return m.get((sev or "").lower(), 5.0)
