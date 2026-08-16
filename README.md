<p align="center">
  <img src="design/brand/avatar/inner-terrain-avatar.svg" width="120" alt="内在地形" />
</p>

<h1 align="center">内在地形 · habit_list</h1>

<p align="center">
  一个陪伴式自我记录 App —— 不是打卡工具，不是日记本，<br/>
  是「被一个越来越懂你的人记住」这件事本身。
</p>

<p align="center">
  <img src="https://github.com/ioooss/habit_list/actions/workflows/backend-ci.yml/badge.svg" alt="backend-ci" />
  <img src="https://img.shields.io/badge/python-3.12-3776ab" alt="python 3.12" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT" />
</p>

---

## 这是什么

**内在地形**（Inner Terrain）是一款正在打磨中的陪伴式手机 App 原型。你向一个角色倾诉、留下生活碎片、记一句备忘，它理解并记住，越用越懂你——而它记住的一切，你都看得见、改得动。

产品由两半组成：

- **[`app.html`](app.html)** —— 单文件交互原型，五个 Tab：共处 ◉ / 生活 🗓 / 备忘 📑 / 河·见 🌊 / 它 👤。深色暗系、衬线宋体、暖琥珀光、7.2 秒真人节律的呼吸。浏览器直接打开即用，无需构建。
- **[`habit_list_backend/`](habit_list_backend/)** —— FastAPI + PostgreSQL/pgvector 后端，定位是「可信记忆引擎」：四层递进认知（Working → Episodic → Semantic → Procedural）、三重检索（向量 + BM25 + 图谱）、证据链与用户可纠正的记忆 Claim。

## 不太寻常的一件事：可执行的美学基线

这个仓库把「界面应该长什么样」写成了 **500+ 条守卫测试**（[`tests/test_aesthetic_baseline.py`](habit_list_backend/tests/test_aesthetic_baseline.py)），与 [`内在地形-美学基线-v1.md`](内在地形-美学基线-v1.md) 互相校验——文档是判据，代码是事实，谁也不许自说自话。

举三个例子：

- **投影浮多高是一把梯子，不是一个旋钮** —— 61 层投影的 y 偏移只许站在 11 个刻度上（1·2·3·4·6·8·12·18·24·30·40px），相邻刻度比值 ≥ 1.25，逐层对表、双向相等。
- **深度是材料的，亮度是状态的** —— 两枚 44px 玻璃键的全部状态（静息 / 悬停 / 录音 / 呼吸峰）共享同一个内光深度 16px，状态只动 alpha；梯子 16·60·80·100·160 由文件自己的比值缝裂出来，不从外部校。
- **光的 alpha 不走档梯** —— 亮度是连续的，长度是分档的；两件事分开治。

守卫的尺子直接从基线文档里读出梯子刻度与逐档负载数——改文档或改代码任一侧，测试都会红。美学在这里不是评审意见，是可回归的工程契约。

## 快速开始

**看原型（零依赖）：**

```
直接用浏览器打开 app.html
```

**跑后端与全部测试：**

```bash
cd habit_list_backend
pip install . && pip install "pytest>=8.3,<9" "pytest-asyncio>=0.23,<1"
pytest          # 500+ 守卫：美学基线 / 记忆引擎 / 接口 / 迁移
```

**起服务（本地 SQLite 即可）：**

```bash
uvicorn app.main:create_app --factory --reload --port 8780
```

真实密钥只放被 gitignore 的 `.env`，示例见 [`.env.example`](.env.example)；DashScope API Key 只存服务器，不下发客户端。

## 文档地图

| 文档 | 内容 |
|---|---|
| [`内在地形-产品基线-v2.md`](内在地形-产品基线-v2.md) | 现行产品基线（页面架构 / 记忆模型 / 隐私架构） |
| [`内在地形-美学基线-v1.md`](内在地形-美学基线-v1.md) | 可执行美学判据（光 / 色彩 / 梯子 / 厚度），与 500+ 守卫互校 |
| [`内在地形-地形页视觉规范-v1.md`](内在地形-地形页视觉规范-v1.md) | 地形页地层剖面视觉规范 |
| [`内在地形-声音基线-v1.md`](内在地形-声音基线-v1.md) | 语音与声音交互基线 |
| [`habit_list_backend/docs/`](habit_list_backend/docs/) | 记忆引擎 V2/V3、身份与管理、部署与预览 |

## 仓库结构

```
app.html                      # 单文件五 Tab 交互原型（共处/生活/备忘/河·见/它）
habit_list_backend/
  app/                        # FastAPI 应用（api / memory / memory_v2 / moments / retrieval …）
  tests/                      # 500+ 守卫：美学基线、记忆、接口、迁移
  migrations/                 # Alembic 迁移
  deploy/                     # Docker / Nginx / staging 部署
design/brand/                 # 品牌资产（等高线头像）
内在地形-*.md                  # 产品 / 美学 / 声音 / 视觉基线文档
```

## 产品原则

1. **记录而非逼迫** —— 不打卡、不断签警告、不积分惩罚；备忘过期只是淡一点灰一点。
2. **记忆可见可改** —— AI 记住的一切用户都看得见、改得动，任何「黑箱记忆」都会被清除。
3. **越用越懂** —— 第一周它是陌生人，第三个月它比你自己更懂你的模式。

---

<p align="center">深墨蓝与暖琥珀 · 安静、克制、有温度</p>
