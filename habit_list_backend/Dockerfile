FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    POETRY_HOME=/opt/poetry \
    PATH=/opt/poetry/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl sqlite3 libsqlite3-0 libgomp1 tini \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSL https://install.python-poetry.org | python3 -

WORKDIR /app

# 仅复制依赖描述先装，优化 layer 缓存
COPY pyproject.toml poetry.lock* ./
RUN poetry install --no-root --no-interaction --no-ansi --only main \
    || pip install --no-cache-dir -r /dev/stdin <<'EOF'
fastapi>=0.111,<0.116
uvicorn[standard]>=0.30
pydantic>=2.8,<3
pydantic-settings>=2.4,<3
sqlalchemy[asyncio]>=2.0.31,<3
aiosqlite>=0.20,<1
apscheduler>=3.10.4,<4
httpx>=0.27,<1
tenacity>=8.5,<9
python-multipart>=0.0.9,<1
rank-bm25>=0.2.2,<1
jieba>=0.42.1,<1
networkx>=3.3,<4
numpy>=1.26,<2
python-dotenv>=1.0,<2
orjson>=3.10,<4
EOF

# 可选：sqlite-vss 预编译 wheel（Linux x86_64, Debian）
# 若安装失败（比如 arm64）就跳过，不影响基础服务启动
RUN pip install --no-cache-dir "sqlite-vss>=0.1.2-alpha.22" || true

# 再拷业务代码
COPY app ./app
COPY tests ./tests

RUN poetry install --only-root --no-interaction --no-ansi || true

EXPOSE 8780
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8780", "--workers", "1", "--timeout-keep-alive", "60"]
