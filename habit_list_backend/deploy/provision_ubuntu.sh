#!/usr/bin/env bash
# 首次 SSH 进 81.70.177.186 的初始化（docker + compose + certbot + 目录）
# 手动一行行跑也可以，或 `bash deploy/provision_ubuntu.sh`
set -euo pipefail

sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y ca-certificates curl gnupg lsb-release rsync

# Docker
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"

# 持久化数据
sudo mkdir -p /opt/habit_list_backend /var/lib/habit_list
sudo chown -R "$USER:$USER" /opt/habit_list_backend /var/lib/habit_list

# Certbot（等域名解析好了再跑：sudo certbot --nginx -d YOUR_DOMAIN）
sudo apt-get install -y certbot python3-certbot-nginx

echo "✅ 初始化完成。登出/重登一次让 docker 组生效，再跑 deploy/deploy.sh。"
