#!/usr/bin/env bash
# --------------------------------------------------------------
# 一键部署到 81.70.177.186（首次执行前先改 .env）
#  1) rsync 代码到服务器 /opt/habit_list_backend
#  2) 服务器 docker compose up -d --build
#  3) 输出健康检查结果
# --------------------------------------------------------------
set -euo pipefail

SERVER="${SERVER_HOST:-81.70.177.186}"
USER="${SERVER_USER:-ubuntu}"
PASS="${SERVER_PASSWORD:-}"
REMOTE_DIR="/opt/habit_list_backend"

if [ -z "$PASS" ]; then
  echo "[warn] SERVER_PASSWORD 没设，期望你已配置 ssh key 免密"
fi

echo "[1/4] rsync 代码..."
rsync -az --delete \
  --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
  --exclude='data/*.sqlite*' --exclude='.env' --exclude='logs' \
  ./ "$USER@$SERVER:$REMOTE_DIR"

echo "[2/4] 把本地 .env 同步到服务器..."
if [ -f .env ]; then
  scp .env "$USER@$SERVER:$REMOTE_DIR/.env"
else
  echo "[warn] 本地没有 .env，请先复制 .env.example 填好"
  exit 1
fi

echo "[3/4] 服务器 compose 构建 & 启动..."
ssh "$USER@$SERVER" bash -lc "cd $REMOTE_DIR && docker compose up -d --build"

echo "[4/4] 健康检查..."
sleep 10
ssh "$USER@$SERVER" bash -lc "docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | head && echo '---' && curl -fsS http://127.0.0.1:8780/health"

echo ""
echo "✅ 部署完成。如果 DNS 指向这台机器，访问 http(s)://\$DEPLOY_DOMAIN/health 验证。"
