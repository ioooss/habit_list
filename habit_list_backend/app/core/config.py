"""全局配置（pydantic-settings 读 .env）。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE if ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    app_env: str = Field(default="dev", pattern=r"^(dev|staging|prod)$")
    app_host: str = "0.0.0.0"
    app_port: int = 8780
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    process_role: str = Field(default="all", pattern=r"^(api|worker|all)$")
    cors_allowed_origins: str = "*"

    # ---- 鉴权 ----
    api_auth_token: str = Field(default="dev-only-change-me")
    admin_token: str = Field(default="dev-only-admin")

    # ---- 数据库 ----
    database_url: str = "sqlite+aiosqlite:///./data/habit_list.db"
    database_schema_mode: str = Field(
        default="auto_create",
        pattern=r"^(auto_create|alembic)$",
    )
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=20, ge=0, le=200)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_pool_recycle_seconds: int = Field(default=1800, ge=60, le=86400)
    sqlite_vss_ext_path: Optional[str] = None
    fts5_tokenizer: str = "unicode61"

    # ---- DashScope ----
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = ""
    dashscope_llm_model: str = "qwen-plus"
    dashscope_embedding_model: str = "qwen3.7-text-embedding"
    # 默认随 dashscope_embedding_model 的默认维度；qwen3.7-text-embedding=1024, text-embedding-v3=1536
    dashscope_embedding_dim: int = 1024
    dashscope_asr_model: str = "paraformer-v2"
    dashscope_tts_model: str = "cosyvoice-v1"
    dashscope_moderation_model: str = "qwen-moderation-plus"
    dashscope_timeout_sec: int = 120
    dashscope_max_retry: int = 2
    rpm_limit_per_min: int = 60

    # ---- 记忆 OS ----
    system1_n_retrieval_topk: int = 5
    system1_context_window_rounds: int = 16
    system2_sleep_consolidation_cron: str = "0 3 * * 0"
    system2_ebbinghaus_cron: str = "30 4 * * *"
    system2_auto_confirm_conf: float = 0.85
    system2_insight_conf_min: float = 0.6
    forget_strength: float = 0.86

    # ---- Memory V2（默认影子写入，不影响当前回复）----
    memory_v2_mode: str = Field(
        default="shadow_write",
        pattern=r"^(off|shadow_write|shadow_retrieve|active)$",
    )
    memory_v2_extractor_mode: str = Field(
        default="rules",
        pattern=r"^(rules|hybrid|llm)$",
    )
    memory_v2_policy_version: str = "terrain-memory-v1"
    memory_v2_extractor_version: str = "terrain-extractor-v1"
    memory_v2_retrieval_topk: int = Field(default=2, ge=1, le=2)
    memory_v2_candidate_limit: int = Field(default=200, ge=20, le=2000)
    memory_v2_min_retrieval_score: float = Field(default=0.30, ge=0.0, le=1.0)
    memory_v2_auto_confirm_conf: float = Field(default=0.86, ge=0.0, le=1.0)
    memory_v2_worker_interval_seconds: int = Field(default=15, ge=5, le=3600)
    memory_v2_outbox_batch_size: int = Field(default=20, ge=1, le=200)
    memory_v2_outbox_max_attempts: int = Field(default=5, ge=1, le=20)
    memory_v2_embedding_enabled: bool = False

    # ---- Worker 运行时 ----
    worker_heartbeat_path: str = "./data/worker-heartbeat.json"
    worker_heartbeat_interval_seconds: int = Field(default=10, ge=2, le=300)
    worker_heartbeat_stale_seconds: int = Field(default=45, ge=10, le=900)

    # ---- 默认用户 ----
    default_user_id: str = "01920000-0000-0000-0000-000000000001"
    default_user_locale: str = "zh-CN"
    default_user_timezone: str = "Asia/Shanghai"

    # ---- 部署 ----
    deploy_domain: str = "localhost"
    deploy_email: str = ""

    # ---- 校验/派生 ----
    @field_validator("api_prefix")
    @classmethod
    def _prefix(cls, v: str) -> str:
        return "/" + v.strip().strip("/") if v.strip() else "/api/v1"

    @field_validator("api_auth_token", "admin_token")
    @classmethod
    def _tokens(cls, v: str) -> str:
        # 防止 .env 末尾空格/tab 导致鉴权失败
        return (v or "").strip()

    @field_validator("dashscope_base_url")
    @classmethod
    def _url_strip(cls, v: str) -> str:
        return (v or "").strip().rstrip("/")

    @field_validator("system2_sleep_consolidation_cron", "system2_ebbinghaus_cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        parts = v.split()
        if len(parts) != 5:
            raise ValueError("cron 必须是标准 5 段：分 时 日 月 周")
        return v

    @field_validator("dashscope_api_key")
    @classmethod
    def _key(cls, v: str) -> str:
        stripped = (v or "").strip()
        if not stripped:
            # 没填的话 dev 允许（用 mock），prod 直接报错
            if os.getenv("APP_ENV", "dev") == "prod":
                raise ValueError("生产环境必须设置 DASHSCOPE_API_KEY")
        return stripped

    @model_validator(mode="after")
    def _production_database_guardrails(self):
        if self.app_env == "prod":
            if self.database_url.startswith("sqlite"):
                raise ValueError("生产环境禁止使用 SQLite，必须配置 PostgreSQL")
            if self.database_schema_mode != "alembic":
                raise ValueError("生产环境必须使用 DATABASE_SCHEMA_MODE=alembic")
            if self.process_role == "all":
                raise ValueError("生产环境必须显式拆分 PROCESS_ROLE=api 或 worker")
            if not self.cors_origins or "*" in self.cors_origins:
                raise ValueError("生产环境必须显式配置 CORS_ALLOWED_ORIGINS")
            weak_markers = ("dev-only", "replace_with", "change-me")
            tokens = (self.api_auth_token, self.admin_token)
            if any(len(token) < 32 or any(marker in token.lower() for marker in weak_markers) for token in tokens):
                raise ValueError("生产环境 API_AUTH_TOKEN 和 ADMIN_TOKEN 必须是不同的高强度随机值")
            if self.api_auth_token == self.admin_token:
                raise ValueError("生产环境 API_AUTH_TOKEN 和 ADMIN_TOKEN 不能相同")
            if not self.dashscope_api_key or "replace_with" in self.dashscope_api_key.lower():
                raise ValueError("生产环境必须配置真实 DASHSCOPE_API_KEY")
        if (
            self.database_url.startswith("postgresql")
            and self.memory_v2_embedding_enabled
            and self.dashscope_embedding_dim != 1024
        ):
            raise ValueError("当前 PostgreSQL pgvector schema 固定为 1024 维")
        return self

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

    @property
    def database_is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite+")

    @property
    def database_is_postgresql(self) -> bool:
        return self.database_url.startswith("postgresql+")

    @property
    def cors_origins(self) -> list[str]:
        """Comma-separated browser origins; native mobile clients do not need CORS."""
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def _sqlite_local_path(self) -> Optional[str]:
        """从 DATABASE_URL 里抽出 sqlite 文件绝对路径（用于 mkdir）。"""
        m = re.match(r"^sqlite\+aiosqlite:///(.+)$", self.database_url)
        if not m:
            return None
        raw = m.group(1)
        p = Path(raw)
        return str(p.resolve()) if not p.is_absolute() else str(p)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例配置。"""
    return Settings()
