"""流量索引（SQLite FTS），供 finding 步骤关联与分页查询。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiosqlite

from penr.config import get_settings

# 注意：存量库可能缺 host 列。CREATE INDEX(host) 必须在 ALTER 补列之后，
# 因此 host 索引不放进 executescript，而在 init_traffic_db 里后置创建。
_SCHEMA = """
CREATE TABLE IF NOT EXISTS exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    method TEXT,
    url TEXT,
    host TEXT,
    status INTEGER,
    req_headers TEXT,
    req_body TEXT,
    resp_headers TEXT,
    resp_body TEXT,
    task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_ex_ts ON exchanges(ts);
CREATE INDEX IF NOT EXISTS idx_ex_task ON exchanges(task_id);
CREATE VIRTUAL TABLE IF NOT EXISTS exchanges_fts USING fts5(
    url, req_body, resp_body, content='exchanges', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS exchanges_ai AFTER INSERT ON exchanges BEGIN
  INSERT INTO exchanges_fts(rowid, url, req_body, resp_body)
  VALUES (new.id, new.url, new.req_body, new.resp_body);
END;
"""


def _db_path() -> Path:
    return get_settings().resolve_traffic_db()


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


async def init_traffic_db() -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_SCHEMA)
        # 存量库补 host 列（必须先于 idx_ex_host）
        cols = {
            str(r[1])
            for r in await (await db.execute("PRAGMA table_info(exchanges)")).fetchall()
        }
        if "host" not in cols:
            await db.execute("ALTER TABLE exchanges ADD COLUMN host TEXT")
            await db.execute(
                """
                UPDATE exchanges SET host = lower(
                    CASE
                      WHEN instr(substr(url, instr(url, '://')+3), '/') > 0
                        THEN substr(
                          substr(url, instr(url, '://')+3),
                          1,
                          instr(substr(url, instr(url, '://')+3), '/')-1
                        )
                      ELSE substr(url, instr(url, '://')+3)
                    END
                )
                WHERE host IS NULL OR host = ''
                """
            )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_ex_host ON exchanges(host)")
        await db.commit()


async def record_exchange(
    *,
    method: str,
    url: str,
    status: int = 0,
    req_headers: dict[str, Any] | None = None,
    req_body: str = "",
    resp_headers: dict[str, Any] | None = None,
    resp_body: str = "",
    task_id: str = "",
    host: str | None = None,
) -> int:
    await init_traffic_db()
    host_v = (host or _host_of(url) or "").lower()
    # 头里也可能带任务 id（大小写不敏感）
    if not task_id and req_headers:
        for k, v in req_headers.items():
            if str(k).lower() == "x-antifox-task":
                task_id = str(v)
                break
    async with aiosqlite.connect(_db_path()) as db:
        cur = await db.execute(
            """
            INSERT INTO exchanges
                (ts, method, url, host, status, req_headers, req_body, resp_headers, resp_body, task_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                method,
                url,
                host_v,
                status,
                json.dumps(req_headers or {}),
                req_body[:200_000],
                json.dumps(resp_headers or {}),
                resp_body[:200_000],
                task_id,
            ),
        )
        await db.commit()
        return int(cur.lastrowid or 0)


def _row_to_meta(row: aiosqlite.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    return {
        "id": str(d["id"]),
        "ts": d.get("ts"),
        "host": d.get("host") or _host_of(str(d.get("url") or "")),
        "method": d.get("method"),
        "url": d.get("url"),
        "status": d.get("status"),
        "task_id": d.get("task_id") or "",
        "req_len": len(d.get("req_body") or ""),
        "resp_len": len(d.get("resp_body") or ""),
    }


async def list_exchanges(
    *,
    host: str | None = None,
    method: str | None = None,
    q: str | None = None,
    task_id: str | None = None,
    page: int = 0,
    size: int = 100,
) -> dict[str, Any]:
    """分页列表；返回 Go 兼容字段 count/total/page/size/exchanges/status_buckets。"""
    await init_traffic_db()
    page = max(0, int(page))
    size = max(1, min(int(size), 500))
    clauses: list[str] = ["TRUE"]
    args: list[Any] = []
    if host:
        clauses.append("host = ?")
        args.append(host.lower())
    if method:
        clauses.append("upper(method) = upper(?)")
        args.append(method)
    if task_id:
        clauses.append("task_id = ?")
        args.append(str(task_id))
    if q:
        clauses.append("(url LIKE ? OR req_body LIKE ? OR resp_body LIKE ?)")
        like = f"%{q}%"
        args.extend([like, like, like])
    where = " AND ".join(clauses)

    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        total_all = int(
            (await (await db.execute("SELECT COUNT(*) AS c FROM exchanges")).fetchone())["c"]
        )
        matched = int(
            (
                await (
                    await db.execute(f"SELECT COUNT(*) AS c FROM exchanges WHERE {where}", args)
                ).fetchone()
            )["c"]
        )
        cur = await db.execute(
            f"""
            SELECT id, ts, method, url, host, status, task_id, req_body, resp_body
            FROM exchanges
            WHERE {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            [*args, size, page * size],
        )
        rows = await cur.fetchall()
        exchanges = [_row_to_meta(r) for r in rows]

        # 状态码桶（2/3/4/5）
        bucket_cur = await db.execute(
            f"""
            SELECT (status / 100) AS bucket, COUNT(*) AS c
            FROM exchanges
            WHERE {where} AND status >= 100
            GROUP BY bucket
            """,
            args,
        )
        buckets = {int(r["bucket"]): int(r["c"]) for r in await bucket_cur.fetchall() if r["bucket"]}

    settings = get_settings()
    return {
        "enabled": True,
        "proxy": settings.traffic_proxy_addr,
        "count": total_all,
        "total": matched,
        "page": page,
        "size": size,
        "exchanges": exchanges,
        "status_buckets": buckets,
    }


async def recent_exchange_ids(limit: int = 5, task_id: str | None = None) -> list[int]:
    await init_traffic_db()
    async with aiosqlite.connect(_db_path()) as db:
        if task_id:
            cur = await db.execute(
                "SELECT id FROM exchanges WHERE task_id = ? ORDER BY id DESC LIMIT ?",
                (task_id, limit),
            )
        else:
            cur = await db.execute(
                "SELECT id FROM exchanges ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


async def get_exchange(exchange_id: int | str) -> dict[str, Any] | None:
    await init_traffic_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM exchanges WHERE id = ?", (int(exchange_id),))
        row = await cur.fetchone()
        if not row:
            return None
        d = dict(row)
        # 解析 headers JSON
        for key in ("req_headers", "resp_headers"):
            try:
                d[key] = json.loads(d.get(key) or "{}")
            except json.JSONDecodeError:
                pass
        d["meta"] = _row_to_meta(row)
        return d


async def get_exchange_bodies(exchange_id: int | str) -> dict[str, Any] | None:
    """对齐 Go /api/traffic/exchange：返回 {req, resp}。"""
    row = await get_exchange(exchange_id)
    if not row:
        return None
    return {
        "req": {
            "method": row.get("method"),
            "url": row.get("url"),
            "headers": row.get("req_headers") or {},
            "body": row.get("req_body") or "",
        },
        "resp": {
            "status": row.get("status"),
            "headers": row.get("resp_headers") or {},
            "body": row.get("resp_body") or "",
        },
    }


async def meta_by_ids(ids: list[str | int]) -> list[dict[str, Any]]:
    await init_traffic_db()
    if not ids:
        return []
    clean = [int(x) for x in ids if str(x).strip().isdigit()]
    if not clean:
        return []
    placeholders = ",".join("?" for _ in clean)
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            f"""
            SELECT id, ts, method, url, host, status, task_id, req_body, resp_body
            FROM exchanges WHERE id IN ({placeholders})
            """,
            clean,
        )
        by_id = {int(r["id"]): _row_to_meta(r) for r in await cur.fetchall()}
    # 保持入参顺序
    out: list[dict[str, Any]] = []
    for x in ids:
        try:
            m = by_id.get(int(x))
        except (TypeError, ValueError):
            m = None
        if m:
            out.append(m)
    return out


async def search_body(query: str, limit: int = 20) -> list[dict[str, Any]]:
    await init_traffic_db()
    async with aiosqlite.connect(_db_path()) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT e.* FROM exchanges_fts f
            JOIN exchanges e ON e.id = f.rowid
            WHERE exchanges_fts MATCH ?
            ORDER BY e.id DESC LIMIT ?
            """,
            (query, limit),
        )
        rows = await cur.fetchall()
        return [_row_to_meta(r) for r in rows]
