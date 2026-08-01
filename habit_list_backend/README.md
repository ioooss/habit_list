# habit_list_backend · 内在地形后端

- 技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0 async · PostgreSQL/pgvector（生产）· SQLite（本地）· Alembic · APScheduler · DashScope
- 定位：陪伴式手机 App 的后端代理 + **可信记忆引擎**（事件、Claim、Evidence、Revision、受控召回）
- 部署：Docker + Nginx 反代到 81.70.177.186，DashScope API Key **只存服务器 `.env`，不下发客户端**

## 本地快速跑起来

项目使用仓库根目录下的独立 Conda 前缀 `.conda`，不使用公共 `work` 环境。当前工作区已经创建好，可直接在 PowerShell 中运行：

```powershell
Set-Location F:\every_day_progress\habit_list\habit_list_backend

# 测试
& '..\.conda\python.exe' -m pytest

# 启服务（factory 模式）
& '..\.conda\python.exe' -m uvicorn app.main:create_app --factory --reload --port 8780

# 浏览器打开健康检查
Invoke-WebRequest http://127.0.0.1:8780/health
```

如需重建环境，应把 Conda 包缓存、pip 缓存和临时目录都指向 F 盘，再执行 `pip install -e .` 与开发依赖安装；不要在 C 盘或公共 Conda 环境中安装本项目依赖。真实密钥只放在已忽略的 `.env`，示例配置见 `.env.example`。

## 接口速查（都挂 `/api/v1`）

所有请求要求 Header：
```
Authorization: Bearer <API_AUTH_TOKEN>
Content-Type: application/json
```

| Method | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 存活检查（免鉴权，不访问数据库） |
| GET  | `/ready` | 就绪检查（免鉴权，验证数据库连接） |
| POST | `/chat/completions` | **共处页聊天**，SSE 流式输出，System1 三重检索 |
| GET  | `/memories` | Memory V2 列表、搜索、状态筛选与游标分页 |
| GET  | `/memories/{claim_id}/evidence` | 查看该记忆对应的用户原话证据 |
| PATCH | `/memories/{claim_id}` | 用户纠正、置顶或调整主动引用权限 |
| POST | `/memories/{claim_id}/confirm` | 确认候选；冲突版本在此时才替代旧版本 |
| POST | `/memories/{claim_id}/reject` | 否认候选，不再参与召回 |
| POST | `/memories/{claim_id}/hide` | 隐藏并禁止主动引用 |
| DELETE | `/memories/{claim_id}` | 硬删除 Claim 及派生数据并写不可逆墓碑 |
| POST | `/memos/detect` | 只跑备忘识别，返回 dueText / importance / cleanText / offset，不入库 |
| GET  | `/memos` | 备忘列表（按过期/今日/本周/之后/已完成分组 + 重要度筛选） |
| POST | `/memos` | 手动新建一条备忘 |
| PATCH | `/memos/{mid}` | 改文字/时间/重要度/状态 |
| POST | `/memos/batch_done` | 批量勾选完成 |
| GET  | `/pebbles` | Episodic 石子时间线（按日期分组，河·见·记忆河流读） |
| PATCH | `/pebbles/{pid}` | 改心情/改分类（kind），留下 kind_fixed_from 修正轨迹 |
| DELETE | `/pebbles/{pid}` | 软删除（episodic.status=archived，Ledger 仍保留） |
| GET  | `/insights` | 发现页卡片（默认 pending + confirmed 最近 30 条） |
| POST | `/insights/{iid}/confirm` | 点「确认」→ 写入 Semantic / Procedural |
| POST | `/insights/{iid}/deny` | 点「不对」→ 写反例进 Ledger，不再重复推荐 |
| GET  | `/me/profile` | 默认用户资料 + 风格参数（procedural 全部）+ 学到的调整轨迹 |
| PATCH | `/me/profile` | 改风格参数（直接调 procedural.confidence=1.0） |
| POST | `/asr/transcribe` | 语音转文字（multipart，file=wav/mp3/m4a → paraformer-v2） |
| POST | `/tts/synthesize` | 文字转语音（cosyvoice-v1，返回 audio/mpeg 或直接 audio/wav） |

详细 Schema（请求/响应字段）跑起来以后看 `http://127.0.0.1:8780/docs`（Swagger UI）。

## 测试

```powershell
..\.conda\python.exe -m pytest
```

Memory V2 的算法、开关、灰度顺序和删除语义见 [`docs/memory-v2.md`](docs/memory-v2.md)。默认 `MEMORY_V2_MODE=shadow_write`，只双写和抽取，不改变当前 AI 回复。生产拓扑、迁移、部署、备份底线和上线门槛见 [`docs/production-foundation.md`](docs/production-foundation.md)。

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

脚本不删除远端文件，会保留上一版生产配置，但不会自动签发 TLS 证书。完整步骤和公开上线前门槛见生产运行手册。当前固定 Bearer token 只是过渡边界，不能作为正式多用户/管理员鉴权方案。

## 项目结构

详见 `app/`：
```
app/
├─ main.py
├─ core/       (config / security 鉴权中间件)
├─ db/         (database 引擎 + 迁移边界 + legacy / Memory V2 models)
├─ providers/  (dashscope LLM/Embedding/ASR/TTS)
├─ retrieval/  (bm25 + sqlite-vss + NetworkX 2hop + RRF)
├─ memory/     (system1 / system2 / consolidate / forgetting / conflict / memo_utils)
├─ memory_v2/  (extractor / reconcile / service / retrieval / worker)
├─ worker/     (独立后台进程 + 心跳健康检查)
└─ api/v1/     (chat / memories / memos / pebbles / insights / me / speech)
```
