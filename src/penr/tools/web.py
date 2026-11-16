"""WebFetch / WebSearch 工具。"""

from __future__ import annotations

from typing import Any

import httpx


async def web_fetch(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    max_body: int = 100_000,
) -> dict[str, Any]:
    proxies = proxy
    async with httpx.AsyncClient(proxy=proxies, follow_redirects=True, timeout=timeout) as client:
        resp = await client.request(method.upper(), url, headers=headers, content=body)
        text = resp.text
        if len(text) > max_body:
            text = text[:max_body] + f"\n...[truncated {len(text) - max_body} chars]"
        return {
            "status_code": resp.status_code,
            "headers": dict(resp.headers),
            "body": text,
            "url": str(resp.url),
        }


async def web_search_ddgs(query: str, *, max_results: int = 5) -> list[dict[str, str]]:
    """无密钥搜索：DuckDuckGo HTML 简版（失败则返回空）。"""
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "AntiFox-Agents/0.1"},
            )
            # 极简解析：避免强依赖第三方库
            import re

            links = re.findall(
                r'uddg=([^&"]+).*?class="result__a"[^>]*>(.*?)</a>',
                resp.text,
                flags=re.S,
            )
            out: list[dict[str, str]] = []
            from urllib.parse import unquote

            for href, title in links[:max_results]:
                out.append(
                    {
                        "title": re.sub(r"<[^>]+>", "", title).strip(),
                        "url": unquote(href),
                    }
                )
            return out
    except Exception as exc:  # noqa: BLE001 — 搜索失败不应打断 worker
        return [{"title": "search_error", "url": "", "error": str(exc)}]
