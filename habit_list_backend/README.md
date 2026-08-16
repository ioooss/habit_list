# habit_list_backend · 内在地形后端

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0 async · PostgreSQL/pgvector（生产）· SQLite（本地）· Alembic · APScheduler · DashScope
- 定位：陪伴式手机 App 的后端代理 + **可信记忆引擎**（事件、Claim、Evidence、Revision、受控召回）
- 部署：Docker + Nginx 反代（服务器地址经 `SERVER_HOST` 环境变量注入，不入库），DashScope API Key **只存服务器 `.env`，不下发客户端**；当前阶段只做本地验证

## 本地快速跑起来

项目使用仓库根目录下的独立 Conda 前缀 `.conda`，不使用公共 `work` 环境。当前工作区已经创建好，可直接在 PowerShell 中运行：

```powershell
Set-Location F:\every_day_progress\habit_list\habit_list_backend

# 测试
& '..\.conda\python.exe' -m pytest

# 也可以从仓库根目录运行（根目录 pytest.ini 会加载异步测试配置）
Set-Location ..
& '.\.conda\python.exe' -m pytest
Set-Location .\habit_list_backend

# 启服务（factory 模式）
& '..\.conda\python.exe' -m uvicorn app.main:create_app --factory --reload --port 8780

# 浏览器打开健康检查
Invoke-WebRequest http://127.0.0.1:8780/health

# 生成可重复的本地预览数据（只允许 dev + SQLite；不会触碰未标记的真实记录）
& '..\.conda\python.exe' -m scripts.seed_preview --reset

# 若当前预览服务使用独立数据库，可显式指定它
$env:DATABASE_URL = 'sqlite+aiosqlite:///./data/preview_local.db'
& '..\.conda\python.exe' -m scripts.seed_preview --reset
Remove-Item Env:DATABASE_URL
```

如需重建环境，应把 Conda 包缓存、pip 缓存和临时目录都指向 F 盘，再执行 `pip install -e .` 与开发依赖安装；不要在 C 盘或公共 Conda 环境中安装本项目依赖。真实密钥只放在已忽略的 `.env`，示例配置见 `.env.example`。

## 接口速查（都挂 `/api/v1`）

除登录/刷新接口外，用户请求要求 Header：
```
Authorization: Bearer <USER_ACCESS_TOKEN>
Content-Type: application/json
```

| Method | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 存活检查（免鉴权，不访问数据库） |
| GET  | `/ready` | 就绪检查（免鉴权，验证数据库连接） |
| POST | `/api/v1/auth/challenges` | 创建一次性 Apple 登录挑战（免鉴权） |
| POST | `/api/v1/auth/apple` | Sign in with Apple token 交换（免鉴权） |
| POST | `/api/v1/auth/refresh` | 轮换刷新令牌（免鉴权） |
| GET | `/api/v1/auth/sessions` | 当前用户设备会话列表 |
| POST | `/api/v1/auth/logout-all` | 撤销当前用户全部会话 |
| POST | `/chat/completions` | **共处页聊天**，默认 `confide`；接受文字、原始语音或两者；普通共处不自动创建备忘或 Episodic 石子 |
| POST | `/media/upload` | 上传用户图片、Live Photo 组成部分、动态影像或原始语音；ASR 只作为可选元数据，原文件始终保留 |
| GET/DELETE | `/media/{asset_id}` | 播放或删除当前用户的媒体资产 |
| POST | `/media/{asset_id}/transcribe` | 用户主动为原始语音生成可编辑转写，不替换原音频 |
| GET | `/moments` | 私密生活碎片流；包含最新 AI 回应、回应数量与异步处理中状态 |
| POST | `/moments` | 用户明确“留下一刻”；文字、原始语音、图片和 Live Photo 可任意组合，每片最多 9 个可见媒体内容 |
| GET | `/moments/{moment_id}/interactions` | 读取单条碎片的独立回应线程 |
| POST | `/moments/{moment_id}/interactions` | 用户在碎片下回一句；只写当前线程，不进入待办、地形或共处记忆 |
| GET | `/terrain` | 达到 3 条独立证据、7 天、2 情境门槛的地形投影；不返回模型小数置信度 |
| GET  | `/memories` | Memory V2 列表、搜索、状态筛选与游标分页 |
| GET  | `/memories/{claim_id}/evidence` | 查看该记忆对应的用户原话证据 |
| PATCH | `/memories/{claim_id}` | 用户纠正、置顶或调整主动引用权限 |
| POST | `/memories/{claim_id}/confirm` | 确认候选；冲突版本在此时才替代旧版本 |
| POST | `/memories/{claim_id}/reject` | 否认候选，不再参与召回 |
| POST | `/memories/{claim_id}/defer` | 暂不判断候选，不用于强断言 |
| POST | `/memories/{claim_id}/hide` | 隐藏并禁止主动引用 |
| DELETE | `/memories/{claim_id}` | 硬删除 Claim 及派生数据并写不可逆墓碑 |
| POST | `/memos/detect` | 手动备忘表单的解析辅助，返回 dueText / importance / cleanText / offset，不入库 |
| GET  | `/memos` | 备忘列表（按过期/今日/本周/之后/已完成分组 + 重要度筛选） |
| POST | `/memos` | 手动新建一条备忘 |
| PATCH | `/memos/{mid}` | 改文字/时间/重要度/状态 |
| POST | `/memos/batch_done` | 批量勾选完成 |
| GET  | `/pebbles` | Legacy Episodic 时间线兼容接口；保留旧数据访问，不再接收普通共处自动写入 |
| PATCH | `/pebbles/{pid}` | 改心情/改分类（kind），留下 kind_fixed_from 修正轨迹 |
| DELETE | `/pebbles/{pid}` | 软删除（episodic.status=archived，Ledger 仍保留） |
| GET  | `/insights` | 发现页卡片（默认 pending + confirmed 最近 30 条） |
| POST | `/insights/{iid}/confirm` | 点「确认」→ 写入 Semantic / Procedural |
| POST | `/insights/{iid}/deny` | 点「不对」→ 写反例进 Ledger，不再重复推荐 |
| GET  | `/me/profile` | 默认用户资料 + 风格参数（procedural 全部）+ 学到的调整轨迹 |
| PATCH | `/me/profile` | 改风格参数（直接调 procedural.confidence=1.0） |
| POST | `/speech/transcriptions` | 语音转文字（multipart，file=wav/mp3/m4a；服务端以内联 `qwen3-asr-flash` 处理，不上传到 DashScope OSS） |
| POST | `/speech/synthesize` | 文字转语音（DashScope 非实时 TTS，返回 audio/mpeg 或 audio/wav） |

详细 Schema（请求/响应字段）跑起来以后看 `http://127.0.0.1:8780/docs`（Swagger UI）。

## 局域网手机预览（不部署）

开发机上的后端可以继续只监听 `127.0.0.1:8780`，另起同源预览网关把页面
和 API 暴露给手机。网关从本地 `.env` 读取 legacy 预览令牌并在服务端转发，
不会把令牌放进页面或 URL：

```powershell
Set-Location F:\every_day_progress\habit_list\habit_list_backend
& ..\.conda\python.exe -m uvicorn scripts.preview_server:app --host 0.0.0.0 --port 8081
```

然后让手机和电脑连同一个局域网，访问 `http://电脑局域网IP:8081`。如果本地
配置使用 `AUTH_MODE=sessions`，请额外设置仅存在于当前终端的
`INNER_TERRAIN_PREVIEW_TOKEN`，否则网关会拒绝启动。此网关只用于局域网验收，
不要把 8081 端口映射到公网。

## 测试

```powershell
..\.conda\python.exe -m pytest
```

Memory V2 的算法、开关、灰度顺序和删除语义见 [`docs/memory-v2.md`](docs/memory-v2.md)。默认 `MEMORY_V2_MODE=shadow_write`，只双写和抽取，不改变当前 AI 回复。用户身份、设备会话、刷新令牌与管理员 MFA/RBAC 见 [`docs/identity-and-admin.md`](docs/identity-and-admin.md)。生产拓扑、迁移、部署、备份底线和上线门槛见 [`docs/production-foundation.md`](docs/production-foundation.md)。

## 生产部署基线

生产固定使用 PostgreSQL + pgvector、Alembic、独立 API/Worker；不会复用本地 SQLite 数据库。首次 SSH 初始化：

   ```bash
   bash deploy/provision_ubuntu.sh
   ```

把 `.env.production.example` 复制为不入库的 `.env.production`，填入真实值并备份数据库后，从后端目录显式确认部署：

   ```bash
   export DEPLOY_CONFIRM=inner-terrain-production
   bash deploy/deploy.sh
   ```

脚本只发布已提交的 Git 版本，以独立 release 目录保留上一版，但不会自动签发 TLS 证书。完整步骤和公开上线前门槛见生产运行手册。生产配置强制使用 Sign in with Apple、可撤销用户会话以及独立的管理员密码 + TOTP + RBAC；固定 Bearer token 只允许本地旧原型使用。

## 手机 staging 预览

项目早期的手机联调使用独立 staging，而不是伪装成生产上线。它复用 PostgreSQL、Alembic、API/Worker 分离和容器安全基线，但使用独立数据卷、独立测试用户、独立镜像与受密码保护的 HTTPS 入口；当前单文件 Web 原型仍通过 legacy token 过渡，token 只在 Nginx 内部注入。准备、部署、访问和边界见 [`docs/staging-preview.md`](docs/staging-preview.md)。

## 项目结构

详见 `app/`：
```
app/
├─ main.py
├─ core/       (config / security 鉴权中间件)
├─ identity/   (Apple OIDC / 设备 / Session / Refresh Rotation)
├─ admin/      (独立管理员身份 / TOTP / RBAC / 审计)
├─ db/         (database 引擎 + 迁移边界 + legacy / Memory V2 models)
├─ providers/  (dashscope LLM/Embedding/ASR/TTS)
├─ retrieval/  (bm25 + sqlite-vss + NetworkX 2hop + RRF)
├─ memory/     (system1 / system2 / consolidate / forgetting / conflict / memo_utils)
├─ memory_v2/  (extractor / reconcile / service / retrieval / worker)
├─ moments/    (生活碎片选择性回应 / 来源回声 / 异步处理)
├─ worker/     (独立后台进程 + 心跳健康检查)
├─ api/v1/     (auth / chat / moments / terrain / memories / memos / pebbles / insights / me / speech)
└─ api/admin/  (独立管理员 API)
```
