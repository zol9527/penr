"""本仓内置 OpenViking 客户端（长期知识库）。

刻意只连 Settings.openviking_url（默认 127.0.0.1:1933 的 compose 服务），
不读取 ~/.openviking/ovcli.conf，避免误写到个人全局实例。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from penr.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OVHit:
    uri: str
    abstract: str = ""
    score: float = 0.0
    context_type: str = ""
    content: str = ""


class OpenVikingClient:
    """对接本仓 docker compose 的 openviking 服务。"""

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        enabled: bool | None = None,
    ) -> None:
        s = get_settings()
        self.enabled = s.openviking_enabled if enabled is None else enabled
        self.url = (url or s.openviking_url).rstrip("/")
        self.api_key = api_key if api_key is not None else (s.openviking_api_key or s.openviking_root_api_key)
        self.uri_prefix = s.openviking_uri_prefix.rstrip("/")
        self._client: Any = None

    def available(self) -> bool:
        return bool(self.enabled and self.url)

    async def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openviking_sdk import AsyncHTTPClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("openviking-sdk not installed") from exc
        self._client = AsyncHTTPClient(url=self.url, api_key=self.api_key or None)
        try:
            await self._client.initialize()
        except Exception as exc:  # noqa: BLE001 — 首次可能尚未建账户
            logger.debug("openviking initialize soft-fail: %s", exc)
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def health(self) -> dict[str, Any]:
        if not self.available():
            return {"ok": False, "reason": "disabled", "url": self.url}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5.0) as http:
                r = await http.get(f"{self.url}/health")
                data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
                return {"ok": r.status_code == 200, "url": self.url, "status": data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": self.url, "reason": str(exc)}

    async def write_knowledge_doc(
        self,
        *,
        relative_path: str,
        content: str,
        mode: str = "create",
        wait: bool = False,
    ) -> dict[str, Any]:
        """写入长期知识资源文档。"""
        if not self.available():
            return {"ok": False, "skipped": True, "reason": "openviking unavailable"}
        uri = f"{self.uri_prefix}/{relative_path.lstrip('/')}"
        client = await self._get_client()
        try:
            result = await client.write(uri, content, mode=mode, wait=wait)
            return {"ok": True, "uri": uri, "result": result}
        except Exception as exc:  # noqa: BLE001
            # create 失败时尝试 replace（目录已存在同名）
            if mode == "create":
                try:
                    result = await client.write(uri, content, mode="replace", wait=wait)
                    return {"ok": True, "uri": uri, "result": result, "mode": "replace"}
                except Exception as exc2:  # noqa: BLE001
                    logger.warning("openviking write failed: %s / %s", exc, exc2)
                    return {"ok": False, "uri": uri, "error": str(exc2)}
            return {"ok": False, "uri": uri, "error": str(exc)}

    async def add_memory(self, content: str) -> dict[str, Any]:
        """通过会话消息提交可抽取的长期记忆素材。

        SDK 无独立 remember；用临时 session + add_message + commit 触发抽取，
        同时把原文落到 knowledge 资源树，保证可追溯。
        """
        if not self.available():
            return {"ok": False, "skipped": True}
        client = await self._get_client()
        try:
            # 资源落盘（可检索）
            import hashlib
            from datetime import datetime, timezone

            digest = hashlib.sha256(content.encode()).hexdigest()[:12]
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            await self.write_knowledge_doc(
                relative_path=f"memories/{day}/{digest}.md",
                content=content,
                mode="create",
            )
            # 尝试走 session 抽取（失败不阻断）
            try:
                sess = await client.create_session()
                sid = sess.get("id") or sess.get("session_id") or ""
                if sid:
                    await client.add_message(sid, content)
                    await client.commit_session(sid)
            except Exception as exc:  # noqa: BLE001
                logger.debug("session memory extract soft-fail: %s", exc)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    async def find(self, query: str, *, limit: int = 8) -> list[OVHit]:
        if not self.available():
            return []
        client = await self._get_client()
        try:
            data = await client.find(query=query, target_uri=self.uri_prefix, limit=limit)
            return _parse_find(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("openviking find failed: %s", exc)
            return []

    async def search_context(self, query: str, *, limit: int = 8) -> list[OVHit]:
        if not self.available():
            return []
        client = await self._get_client()
        try:
            if hasattr(client, "search_context"):
                data = await client.search_context(query=query, target_uri=self.uri_prefix, limit=limit)
                return _parse_find(data)
            return await self.find(query, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("openviking search_context failed: %s", exc)
            return []


def _parse_find(data: Any) -> list[OVHit]:
    if data is None:
        return []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("results") or data.get("items") or data.get("hits") or data.get("nodes") or []
        if not items and data.get("uri"):
            items = [data]
    else:
        return []
    hits: list[OVHit] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        hits.append(
            OVHit(
                uri=str(it.get("uri") or it.get("id") or ""),
                abstract=str(it.get("abstract") or it.get("overview") or it.get("summary") or ""),
                score=float(it.get("score") or 0),
                context_type=str(it.get("context_type") or it.get("type") or ""),
                content=str(it.get("content") or it.get("text") or ""),
            )
        )
    return hits
