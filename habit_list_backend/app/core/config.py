"""全局配置（pydantic-settings 读 .env）。"""

from __future__ import annotations

import os
import re
from base64 import urlsafe_b64decode
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

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
    auth_mode: str = Field(default="legacy", pattern=r"^(legacy|sessions)$")
    # 仅为本地旧原型保留；session 模式不会读取这两个固定 token。
    api_auth_token: str = Field(default="dev-only-change-me")
    admin_token: str = Field(default="dev-only-admin")
    auth_token_pepper: str = ""
    pii_encryption_key: str = ""
    auth_access_ttl_seconds: int = Field(default=900, ge=300, le=3600)
    auth_refresh_ttl_days: int = Field(default=30, ge=1, le=90)
    auth_challenge_ttl_seconds: int = Field(default=600, ge=120, le=900)
    auth_max_sessions_per_user: int = Field(default=10, ge=1, le=50)
    apple_client_ids: str = ""
    apple_jwks_cache_seconds: int = Field(default=3600, ge=300, le=86400)

    # ---- 管理员身份（与用户身份完全分离）----
    admin_mfa_encryption_key: str = ""
    admin_access_ttl_seconds: int = Field(default=900, ge=300, le=1800)
    admin_max_failed_attempts: int = Field(default=5, ge=3, le=10)
    admin_lockout_seconds: int = Field(default=900, ge=60, le=86400)
    admin_totp_issuer: str = "Inner Terrain Admin"

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

    # ---- 本地媒体 ----
    # 预览阶段落在项目 F 盘的数据目录；生产阶段可替换为对象存储适配器。
    media_root: str = "./data/media"
    media_max_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    media_max_image_bytes: int = Field(default=12 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    media_max_audio_seconds: int = Field(default=600, ge=5, le=3600)
    # A picker upload is initially unattached. Abandoned uploads must not
    # accumulate forever when the user closes the composer before saving.
    media_unattached_retention_minutes: int = Field(default=60, ge=5, le=7 * 24 * 60)

    # ---- DashScope ----
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = ""
    dashscope_llm_model: str = "qwen-plus"
    dashscope_embedding_model: str = "qwen3.7-text-embedding"
    # 默认随 dashscope_embedding_model 的默认维度；qwen3.7-text-embedding=1024, text-embedding-v3=1536
    dashscope_embedding_dim: int = 1024
    dashscope_asr_model: str = "paraformer-v2"
    # 本地媒体以 bytes 进入 API，使用内联 Qwen ASR，避免把私密语音上传到公共对象地址。
    dashscope_asr_inline_model: str = "qwen3-asr-flash"
    dashscope_tts_model: str = "qwen-audio-3.0-tts-flash"
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

    # ---- Memory V2（正式启用：记忆可被召回并进入回复）----
    # active 是产品形态，不是实验开关：记忆是本产品的核心差异点，影子模式下
    # 用户永远看不到"它记得我"。降级路径仍然完整（shadow_* / off 可回退）。
    memory_v2_mode: str = Field(
        default="active",
        pattern=r"^(off|shadow_write|shadow_retrieve|active)$",
    )
    # hybrid：LLM 抽取 + 规则兜底。规则单独无法抽出"反复回到"这类需要语义的
    # 原子；LLM 失败或没有 API key 时自动退回纯规则，不会中断写入链路。
    memory_v2_extractor_mode: str = Field(
        default="hybrid",
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
    # 形成层的语义聚类依赖向量：关掉 embedding 时，聚类退化为"同一 slot_key
    # 单独成簇"，只能形成 recurring 一种地形。
    memory_v2_embedding_enabled: bool = True

    # ---- Memory V3 形成层 ----
    # 门槛只能调高，不能调低：产品基线 11.1 要求安全下限不可由运营配置降低。
    memory_v3_formation_enabled: bool = True
    memory_v3_min_evidence: int = Field(default=3, ge=3, le=20)
    memory_v3_min_span_days: int = Field(default=7, ge=7, le=365)
    memory_v3_min_contexts: int = Field(default=2, ge=2, le=20)
    memory_v3_max_hypotheses_per_scan: int = Field(default=2, ge=1, le=5)
    memory_v3_cluster_similarity: float = Field(default=0.62, ge=0.30, le=0.99)
    memory_v3_scan_debounce_seconds: int = Field(default=21600, ge=60, le=604800)
    memory_v3_policy_version: str = "terrain-formation-v1"
    # 低于该置信度的语音转写不得成为地形证据（基线 8.3）。
    memory_v3_min_asr_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    # 危机会话窗口：命中危机后，同会话内这段时间的内容全部隔离在形成层之外。
    memory_v3_crisis_window_minutes: int = Field(default=180, ge=10, le=1440)

    # ---- 此刻天气 ----
    # 天气是会话级的短期状态，不是可累积的序列（基线 §11 限制长期化）。
    # 超过这个窗口就当作已经散掉，地形页的天气槽位重新变空。
    terrain_weather_enabled: bool = True
    terrain_weather_ttl_hours: int = Field(default=12, ge=1, le=72)

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
            if any(not self._is_secure_origin(origin) for origin in self.cors_origins):
                raise ValueError("生产环境 CORS_ALLOWED_ORIGINS 必须是无路径的 HTTPS Origin")
            if self.auth_mode != "sessions":
                raise ValueError("生产环境必须使用 AUTH_MODE=sessions")
            weak_markers = ("dev-only", "replace_with", "change-me", "placeholder")
            if len(self.auth_token_pepper) < 32 or any(
                marker in self.auth_token_pepper.lower() for marker in weak_markers
            ):
                raise ValueError("生产环境必须配置高强度 AUTH_TOKEN_PEPPER")
            self._validate_fernet_key(self.pii_encryption_key, "PII_ENCRYPTION_KEY")
            self._validate_fernet_key(
                self.admin_mfa_encryption_key,
                "ADMIN_MFA_ENCRYPTION_KEY",
            )
            if self.pii_encryption_key == self.admin_mfa_encryption_key:
                raise ValueError("PII_ENCRYPTION_KEY 与 ADMIN_MFA_ENCRYPTION_KEY 必须不同")
            if not self.apple_client_id_list or any(
                marker in client_id.lower()
                for client_id in self.apple_client_id_list
                for marker in weak_markers
            ):
                raise ValueError("生产环境必须配置真实 APPLE_CLIENT_IDS")
            if not self.dashscope_api_key or "replace_with" in self.dashscope_api_key.lower():
                raise ValueError("生产环境必须配置真实 DASHSCOPE_API_KEY")
            if not self._is_deploy_domain(self.deploy_domain):
                raise ValueError("生产环境 DEPLOY_DOMAIN 必须是合法的真实 DNS 域名")
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
    def apple_client_id_list(self) -> list[str]:
        return [
            client_id.strip() for client_id in self.apple_client_ids.split(",") if client_id.strip()
        ]

    @staticmethod
    def _validate_fernet_key(value: str, name: str) -> None:
        try:
            decoded = urlsafe_b64decode(value.encode("ascii"))
        except Exception as exc:  # noqa: BLE001 - normalize config error
            raise ValueError(f"{name} 必须是 urlsafe-base64 Fernet key") from exc
        if len(decoded) != 32:
            raise ValueError(f"{name} 必须解码为 32 字节")

    @staticmethod
    def _is_secure_origin(value: str) -> bool:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.netloc
            and not parsed.username
            and not parsed.password
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _is_deploy_domain(value: str) -> bool:
        normalized = value.strip().casefold()
        if normalized == "localhost" or any(
            marker in normalized for marker in ("example", "replace_with", "placeholder")
        ):
            return False
        return bool(
            len(normalized) <= 253
            and re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}",
                normalized,
            )
        )

    @property
    def _sqlite_local_path(self) -> Optional[str]:
        """从 DATABASE_URL 里抽出 sqlite 文件绝对路径（用于 mkdir）。"""
        m = re.match(r"^sqlite\+aiosqlite:///(.+)$", self.database_url)
        if not m:
            return None
        raw = m.group(1)
        p = Path(raw)
        return str(p.resolve()) if not p.is_absolute() else str(p)

    @property
    def media_root_path(self) -> Path:
        """Resolve local media storage relative to the backend project root."""
        path = Path(self.media_root)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        return path.resolve()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """全局单例配置。"""
    return Settings()
