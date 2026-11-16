"""运行时配置：环境变量优先，其次 config.json。"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """对齐 AntiFox config + .env 的关键字段。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务
    host: str = Field(default="0.0.0.0", alias="ANTIFOX_HOST")
    port: int = Field(default=8787, alias="ANTIFOX_PORT")
    data_dir: Path = Field(default_factory=lambda: _project_root() / "data", alias="ANTIFOX_DATA_DIR")
    skill_dir: Path | None = Field(default=None, alias="ANTIFOX_SKILL_DIR")
    config_path: Path | None = Field(default=None, alias="ANTIFOX_CONFIG")

    # PostgreSQL
    pg_dsn: str | None = Field(default=None, alias="ANTIFOX_PG_DSN")
    pg_host: str = Field(default="127.0.0.1", alias="POSTGRES_HOST")
    pg_port: int = Field(default=5432, alias="POSTGRES_PORT")
    pg_user: str = Field(default="antifox", alias="POSTGRES_USER")
    pg_password: str = Field(default="antifox", alias="POSTGRES_PASSWORD")
    pg_db: str = Field(default="antifox", alias="POSTGRES_DB")
    pg_sslmode: str = Field(default="disable", alias="POSTGRES_SSLMODE")

    # LLM
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    llm_provider: str = Field(default="", alias="ANTIFOX_LLM_PROVIDER")
    llm_model: str = Field(default="", alias="ANTIFOX_LLM_MODEL")
    llm_base_url: str | None = Field(default=None, alias="ANTIFOX_LLM_BASE_URL")
    llm_proxy: str | None = Field(default=None, alias="ANTIFOX_LLM_PROXY")

    # 安全 / JWT
    jwt_secret: str = Field(default="", alias="ANTIFOX_JWT_SECRET")
    jwt_ttl_hours: int = Field(default=72, alias="ANTIFOX_JWT_TTL_HOURS")

    # 引擎
    planner_debounce_ms: int = Field(default=800, alias="ANTIFOX_PLANNER_DEBOUNCE_MS")
    worker_concurrency: int = Field(default=3, alias="ANTIFOX_WORKER_CONCURRENCY")
    traffic_proxy_addr: str = Field(default="127.0.0.1:8788", alias="ANTIFOX_TRAFFIC_PROXY")
    traffic_db_path: Path | None = Field(default=None, alias="ANTIFOX_TRAFFIC_DB")

    # Web 静态
    web_dist: Path | None = Field(default=None, alias="ANTIFOX_WEB_DIST")

    # 本仓内置 OpenViking（长期知识库；不要连个人全局实例）
    openviking_enabled: bool = Field(default=True, alias="OPENVIKING_ENABLED")
    openviking_url: str = Field(default="http://127.0.0.1:1933", alias="OPENVIKING_URL")
    openviking_api_key: str = Field(default="", alias="OPENVIKING_API_KEY")
    openviking_root_api_key: str = Field(default="", alias="OPENVIKING_ROOT_API_KEY")
    openviking_uri_prefix: str = Field(
        default="viking://resources/penr/knowledge",
        alias="OPENVIKING_URI_PREFIX",
    )
    openviking_data_dir: Path | None = Field(default=None, alias="OPENVIKING_DATA_DIR")
    openviking_auto_promote: bool = Field(default=True, alias="OPENVIKING_AUTO_PROMOTE")

    def resolve_skill_dir(self) -> Path:
        if self.skill_dir is not None:
            return Path(self.skill_dir)
        bundled = _project_root() / "skills"
        if bundled.is_dir():
            return bundled
        return Path(self.data_dir) / "skills"

    def resolve_dsn(self) -> str:
        if self.pg_dsn:
            return self.pg_dsn
        cfg = self._load_json_config()
        db = cfg.get("database") or {}
        if dsn := (db.get("dsn") or "").strip():
            return dsn
        host = db.get("host") or self.pg_host
        port = int(db.get("port") or self.pg_port)
        user = db.get("user") or self.pg_user
        password = db.get("password") if db.get("password") is not None else self.pg_password
        dbname = db.get("dbname") or self.pg_db
        sslmode = db.get("sslmode") or self.pg_sslmode
        return (
            f"postgresql://{user}:{password}@{host}:{port}/{dbname}?sslmode={sslmode}"
        )

    def resolve_async_dsn(self) -> str:
        dsn = self.resolve_dsn()
        if dsn.startswith("postgresql://"):
            return "postgresql+asyncpg://" + dsn[len("postgresql://") :]
        if dsn.startswith("postgres://"):
            return "postgresql+asyncpg://" + dsn[len("postgres://") :]
        return dsn

    def resolve_traffic_db(self) -> Path:
        if self.traffic_db_path is not None:
            return Path(self.traffic_db_path)
        return Path(self.data_dir) / "traffic" / "index.sqlite"

    def resolve_openviking_data_dir(self) -> Path:
        if self.openviking_data_dir is not None:
            return Path(self.openviking_data_dir)
        return Path(self.data_dir) / "openviking"

    def ensure_dirs(self) -> None:
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        self.resolve_skill_dir().mkdir(parents=True, exist_ok=True)
        self.resolve_traffic_db().parent.mkdir(parents=True, exist_ok=True)
        self.resolve_openviking_data_dir().mkdir(parents=True, exist_ok=True)

    def _load_json_config(self) -> dict[str, Any]:
        candidates: list[Path] = []
        if self.config_path:
            candidates.append(Path(self.config_path))
        candidates.append(Path.cwd() / "config.json")
        candidates.append(_project_root() / "config.json")
        for path in candidates:
            if path.is_file():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return {}
        return {}


@lru_cache
def get_settings() -> Settings:
    # 允许本地覆盖：ANTIFOX_CONFIG 未设时也读 CWD/config.json
    if not os.environ.get("ANTIFOX_CONFIG"):
        cwd_cfg = Path.cwd() / "config.json"
        if cwd_cfg.is_file():
            os.environ.setdefault("ANTIFOX_CONFIG", str(cwd_cfg))
    settings = Settings()
    settings.ensure_dirs()
    return settings
