"""mitmproxy addon：把请求/响应写入流量索引。

用法：
  mitmdump -s src/penr/traffic/addon.py -p 8788
"""

from __future__ import annotations

import asyncio
from typing import Any


def _run(coro: Any) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(coro)
        else:
            loop.run_until_complete(coro)
    except RuntimeError:
        asyncio.run(coro)


class AntiFoxTrafficAddon:
    def response(self, flow: Any) -> None:  # mitmproxy http.HTTPFlow
        try:
            from penr.traffic.index import record_exchange

            req = flow.request
            resp = flow.response
            _run(
                record_exchange(
                    method=req.method,
                    url=req.pretty_url,
                    status=resp.status_code if resp else 0,
                    req_headers=dict(req.headers),
                    req_body=req.get_text(strict=False) or "",
                    resp_headers=dict(resp.headers) if resp else {},
                    resp_body=(resp.get_text(strict=False) or "") if resp else "",
                    task_id=req.headers.get("X-Antifox-Task", ""),
                )
            )
        except Exception:  # noqa: BLE001
            pass


addons = [AntiFoxTrafficAddon()]
