FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl sqlite3 libsqlite3-0 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 使用 PEP 517 从 pyproject 安装，避免构建时下载并执行远程 Poetry 安装脚本。
COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN pip install --no-cache-dir .

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

EXPOSE 8780
USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8780", "--workers", "1", "--timeout-keep-alive", "60"]
