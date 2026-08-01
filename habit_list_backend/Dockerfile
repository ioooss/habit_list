FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl sqlite3 libsqlite3-0 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先用最小包安装并缓存运行依赖。业务代码变化不会让整套依赖重新下载。
COPY pyproject.toml README.md ./
RUN mkdir -p app \
    && touch app/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf /app/app

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
# 再安装真实应用包；依赖已在上一层解析完成。
RUN pip install --no-cache-dir --no-deps --force-reinstall .

RUN groupadd --system app && useradd --system --gid app --home-dir /app app \
    && mkdir -p /app/data \
    && chown -R app:app /app

EXPOSE 8780
USER app
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8780", "--workers", "1", "--timeout-keep-alive", "60"]
