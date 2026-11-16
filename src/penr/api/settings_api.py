"""M5：/api/settings 读写（settings 表键值）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from penr.api.auth import require_user
from penr.db.engine import get_db
from penr.db.stores import get_setting, get_setting_bool, set_setting

router = APIRouter(tags=["settings"])

_KEYS = {
    "traffic_capture": "false",
    "web_search_enabled": "false",
    "web_search_backend": "ddgs",
    "brave_search_api_key": "",
    "tavily_search_api_key": "",
    "web_search_proxy": "",
    "global_proxy": "",
    "python_interpreter": "",
    "workers": "3",
}


class SettingsPatch(BaseModel):
    traffic_capture: bool | None = None
    web_search_enabled: bool | None = None
    web_search_backend: str | None = None
    brave_search_api_key: str | None = None
    tavily_search_api_key: str | None = None
    web_search_proxy: str | None = None
    global_proxy: str | None = None
    python_interpreter: str | None = None
    workers: int | None = None


async def _payload() -> dict[str, Any]:
    db = get_db()
    brave = await get_setting(db, "brave_search_api_key", "")
    tavily = await get_setting(db, "tavily_search_api_key", "")
    workers_s = await get_setting(db, "workers", "3")
    try:
        workers = int(workers_s)
    except ValueError:
        workers = 3
    return {
        "traffic_capture": await get_setting_bool(db, "traffic_capture", False),
        "web_search_enabled": await get_setting_bool(db, "web_search_enabled", False),
        "web_search_backend": await get_setting(db, "web_search_backend", "ddgs"),
        "brave_key_set": bool(brave.strip()),
        "tavily_key_set": bool(tavily.strip()),
        "web_search_proxy": await get_setting(db, "web_search_proxy", ""),
        "global_proxy": await get_setting(db, "global_proxy", ""),
        "python_interpreter": await get_setting(db, "python_interpreter", ""),
        "workers": workers,
    }


@router.get("/api/settings")
async def get_settings_api(user: dict[str, Any] = Depends(require_user)) -> dict[str, Any]:
    return await _payload()


@router.put("/api/settings")
async def put_settings_api(
    body: SettingsPatch,
    user: dict[str, Any] = Depends(require_user),
) -> dict[str, Any]:
    db = get_db()
    data = body.model_dump(exclude_unset=True)
    if "traffic_capture" in data and data["traffic_capture"] is not None:
        await set_setting(db, "traffic_capture", "true" if data["traffic_capture"] else "false")
    if "web_search_enabled" in data and data["web_search_enabled"] is not None:
        await set_setting(
            db, "web_search_enabled", "true" if data["web_search_enabled"] else "false"
        )
    if "web_search_backend" in data and data["web_search_backend"] is not None:
        await set_setting(db, "web_search_backend", data["web_search_backend"])
    # 密钥：省略=不改；空串=清空
    if "brave_search_api_key" in data and data["brave_search_api_key"] is not None:
        await set_setting(db, "brave_search_api_key", data["brave_search_api_key"])
    if "tavily_search_api_key" in data and data["tavily_search_api_key"] is not None:
        await set_setting(db, "tavily_search_api_key", data["tavily_search_api_key"])
    if "web_search_proxy" in data and data["web_search_proxy"] is not None:
        await set_setting(db, "web_search_proxy", data["web_search_proxy"])
    if "global_proxy" in data and data["global_proxy"] is not None:
        await set_setting(db, "global_proxy", data["global_proxy"])
    if "python_interpreter" in data and data["python_interpreter"] is not None:
        await set_setting(db, "python_interpreter", data["python_interpreter"].strip())
    if "workers" in data and data["workers"] is not None:
        n = max(1, int(data["workers"]))
        await set_setting(db, "workers", str(n))
    return await _payload()
