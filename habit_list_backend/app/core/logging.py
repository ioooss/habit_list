"""日志（JSON 结构化 + 控制台漂亮打印）。"""
from __future__ import annotations

import logging
import logging.config
import uuid
from collections.abc import Callable
from time import perf_counter_ns

from fastapi import Request, Response


LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO", app_env: str = "dev") -> None:
    level_val = getattr(logging, level.upper(), logging.INFO)
    cfg = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console": {"format": LOG_FORMAT, "datefmt": "%Y-%m-%d %H:%M:%S"},
            "json": {"()": "app.core.logging.JsonFormatter"},
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": level_val,
                "formatter": "json" if app_env == "prod" else "console",
            }
        },
        "loggers": {
            "habit_list": {"handlers": ["console"], "level": level_val, "propagate": False},
            "httpx": {"handlers": ["console"], "level": logging.WARNING, "propagate": False},
            "apscheduler": {"handlers": ["console"], "level": logging.INFO, "propagate": False},
        },
        "root": {"handlers": ["console"], "level": logging.WARNING},
    }
    logging.config.dictConfig(cfg)


class JsonFormatter(logging.Formatter):
    """生产的极简 JSON 行日志。"""

    import orjson  # 延迟导入，Windows 本地没装也不崩

    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "lv": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return __class__.orjson.dumps(payload).decode()


def trace_middleware() -> Callable:
    """给每个请求打 X-Request-ID + 执行耗时，和鉴权中间件串起来。"""
    import time as _time

    async def _mw(request: Request, call_next):
        start = perf_counter_ns()
        req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        request.state.request_id = req_id
        log = logging.getLogger("habit_list")
        log.info(
            "req_in %s %s ip=%s",
            request.method,
            request.url.path,
            request.client.host if request.client else "-",
            extra={"req_id": req_id},
        )
        resp: Response = await call_next(request)
        resp.headers["X-Request-ID"] = req_id
        ms = (perf_counter_ns() - start) / 1_000_000
        log.info("req_out status=%s cost=%.2fms", resp.status_code, ms, extra={"req_id": req_id})
        return resp

    return _mw
