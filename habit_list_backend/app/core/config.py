"""全局配置（pydantic-settings 读 .env）。"""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
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

    # ---- 鉴权 ----
    api_auth_token: str = Field(default="dev-only-change-me")
    admin_token: str = Field(default="dev-only-admin")

    # ---- 数据库 ----
    database_url: str = "sqlite+aiosqlite:///./data/habit_list.db"
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

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"

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
