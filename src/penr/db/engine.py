"""异步 PostgreSQL 连接与 schema 迁移。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import asyncpg

from penr.config import Settings, get_settings

_SCHEMA_CANDIDATES = (
    Path(__file__).resolve().parents[3] / "schema.sql",
    Path(__file__).resolve().parents[3] / "db" / "schema.sql",
)


class Database:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if self.pool is not None:
            return
        dsn = self.settings.resolve_dsn()
        self.pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("database not connected; call await db.connect()")
        return self.pool

    async def migrate(self) -> None:
        """执行幂等 schema.sql（与 Go 端一致）。"""
        schema_path = next((p for p in _SCHEMA_CANDIDATES if p.is_file()), None)
        if schema_path is None:
            raise FileNotFoundError("schema.sql not found")
        sql = schema_path.read_text(encoding="utf-8")
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(sql)

    async def ping(self) -> bool:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True

    async def fetchrow(self, query: str, *args: Any) -> asyncpg.Record | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def execute(self, query: str, *args: Any) -> str:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db


def set_db(db: Database | None) -> None:
    global _db
    _db = db
