# 手机 staging 预览

这套环境用于项目早期的真机联调，不是生产上线：

- 使用独立 Compose project、独立 PostgreSQL volume、独立后端镜像和独立默认用户；
- 复用生产级 PostgreSQL + Alembic + API/Worker 分离拓扑；
- 为兼容当前单文件 HTML 原型，暂时使用 `AUTH_MODE=legacy`；生产仍强制使用 Apple 会话登录；
- 浏览器只拿到 Basic Auth，真正的后端 Bearer token 由 Nginx 在服务器内注入；
- 入口使用 Let’s Encrypt 公网 IP 短证书，证书约 6 天有效，由 Certbot 每 12 小时检查续期；
- 测试环境只应填写测试数据，不应视为备份完备或可承载真实用户的生产系统。

## 首次准备

在 Git Bash 中运行：

```bash
cd /f/every_day_progress/habit_list/habit_list_backend
bash deploy/prepare_staging_secrets.sh
```

生成的文件只位于仓库根目录 `.secrets/staging/`，已经被 `.gitignore` 排除。脚本不会在终端打印 DashScope Key 或访问密码。

## 部署

部署脚本只接受已提交的 `habit_list_backend/` 与根目录 `app.html`，不会把 `design/`、本地数据库、测试代码或任何 `.env` 打包上传。首次签发证书前，操作者还必须显式确认 staging 部署与 Let’s Encrypt 条款：

```bash
export DEPLOY_CONFIRM=inner-terrain-staging
export LETSENCRYPT_AGREE_TOS=letsencrypt-subscriber-agreement
bash deploy/deploy_staging.sh
```

访问凭据保存在 `.secrets/staging/access.txt`。手机访问 `https://81.70.177.186`，输入其中的用户名和密码。

## 边界

当前页面仍是 Web 原型，不是已安装到 Android/iOS 的原生 App。这个入口可验证触屏布局、真实 API、SSE 对话、备忘和记忆数据链路；推送、原生权限、应用商店登录、离线同步和正式设备会话仍需后续客户端工程。
