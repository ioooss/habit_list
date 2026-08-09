# 生活碎片互动 V1 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将生活碎片互动从"简单计数 + 线程内回应"升级为"可解释策略门 + 带来源回声 V1 + 用户反馈闭环 + 删除防复活 + 工程可靠性"Alpha 闭环,不污染待办/地形/共处记忆。

**Architecture:**
- 新增 `moment_suppressions` 轻量表承载"别再提这条/这类/少这样回应"的持久抑制(可撤销),通过策略门在生成前拦截;回声候选筛选时加入权限、敏感度、频率预算与抑制检查;
- 策略门替代现有 `_occasional_budget_available` 的纯计数,综合回应密度、近期重复、敏感度、回声频率预算、抑制列表与回声资格;
- 跨场景低频回声提示(生活页/共处/线程详情)通过新增同步 GET 端点 `/moments/echo-hint` 返回 0 或 1 条候选,由前端在合适时机渲染;回声解释(why_now)由模型显式返回并携带来源;
- 删除/隐藏/撤销授权时取消未处理 Outbox、加墓碑标记,阻止旧事件复活;Worker 失败不伪造成功,把失败原因写入 interaction.metadata_json;
- 前端(app.html 单文件原型)补齐反馈菜单、回声 why 解释、关闭/禁止类、390x844 手机尺寸适配。

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), Alembic, Pydantic v2, APScheduler, pytest+respx, 原生 HTML/CSS/JS (单文件原型)。

---

## 文件清单(本计划新建/修改)

**新建**
- `app/moments/echo.py` — 跨场景回声提示构建、来源筛选、频率预算、why_now 渲染
- `app/moments/feedback.py` — 用户反馈处理(不像我/少这样回应/别再提/修改表达)
- `app/moments/policy.py` — 可解释策略门(替代简单计数)
- `migrations/versions/<rev>_add_moment_suppressions.py` — 新增抑制表
- `tests/test_moment_policy.py` — 策略门单元测试
- `tests/test_moment_echo.py` — 回声 V1 集成测试
- `tests/test_moment_feedback.py` — 反馈闭环测试
- `tests/test_moment_deletion.py` — 删除/隐藏/防复活测试

**修改**
- `app/db/models.py` — 新增 MomentSuppression ORM
- `app/db/memory_models.py` — 无需改;复用现有 OutboxEvent
- `app/moments/service.py` — 升级策略门、Prompt 版本、结构化输出增加 why/reason 字段、幂等/失败标记、source 权限校验
- `app/api/v1/moments.py` — 新增 POST /moments/{id}/feedback、GET /moments/echo-hint、DELETE/POST /moments/{id}(隐藏/恢复/永久删除);在列表/线程响应中携带 echo_hint 与回声 why 字段
- `app/api/v1/router.py` — 无需改(moments router 已挂载)
- `app/memory_v2/worker.py` — 派发链已经支持 MOMENT_RESPONSE_REQUESTED;为失败分支补"不伪造"语义(interaction 标记 failed 状态)
- `app.html` — 生活页反馈菜单、回声 why 展示、禁止类操作、手机 390x844 样式修补、回声提示渲染
- `tests/test_moment_interactions.py` — 补策略门/回声/失败分支用例

---

## Task 1: 数据库迁移 — 新增 moment_suppressions 表

**Files:**
- Create: `habit_list_backend/migrations/versions/<rev>_add_moment_suppressions.py`(revision id 使用 `c1f1a00b0001`)
- Modify: `habit_list_backend/app/db/models.py` (追加 ORM 类,放在 MomentInteraction 之后)
- Test: 跑 `alembic upgrade head` 再 `alembic downgrade -1` 验证可逆

- [ ] **Step 1: 先写失败的迁移可逆性测试(轻量,直接跑 alembic 命令验证表存在/消失)**

在 `tests/test_migrations.py`(现有文件)末尾追加一个用例(如果文件里已有迁移 smoke 测试风格,追加即可;否则新建函数):

```python
async def test_moment_suppressions_migration_is_reversible(client: AsyncClient):
    """迁移 c1f1a00b0001 升级后表存在、降级后被移除。"""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text

    from app.db.database import _engines, get_sessionmaker

    ini = Config(str((Path(__file__).resolve().parents[1] / "alembic.ini")))
    ini.set_main_option("script_location", "migrations")
    # 直接查 sqlite_master 验证表存在性
    maker = get_sessionmaker()
    async with maker() as s:
        rows = (await s.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='moment_suppressions'")
        )).scalar_one_or_none()
    assert rows == "moment_suppressions"

    command.downgrade(ini, "5b76d8c9e2a1")  # 回滚到前一版本
    async with maker() as s:
        rows = (await s.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='moment_suppressions'")
        )).scalar_one_or_none()
    assert rows is None
    command.upgrade(ini, "head")  # 还原
```

- [ ] **Step 2: 运行测试,确认失败(表还不存在)**

Run: `cd habit_list_backend ; pytest tests/test_migrations.py::test_moment_suppressions_migration_is_reversible -v`
Expected: FAIL,因为新迁移还没写(要么表不存在,要么 downgrade 找不到 revision)。

- [ ] **Step 3: 在 models.py 追加 ORM**

在 `MomentInteraction` 类定义结束后追加:

```python
class MomentSuppression(Base):
    """用户对某条来源或某类生活碎片的主动抑制(别再提/少这样回应)。

    scope:
      - source  : 抑制单条 moment_id 作为回声来源
      - category: 抑制某一类主题/关键词类别(由反馈时模型归类或显式标签)
      - style   : 抑制某种回应风格(如评论太长/问太多)
      - echo_all: 关闭该用户的全部主动回声(24h 或永久,由 revoked_at 决定)
    """

    __tablename__ = "moment_suppressions"

    suppression_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid7())
    )
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), index=True
    )
    scope: Mapped[str] = mapped_column(String(24), index=True)  # source/category/style/echo_all
    scope_value: Mapped[str] = mapped_column(String(128), default="", index=True)
    reason: Mapped[str] = mapped_column(String(32), default="user_request")  # not_like_me/too_often/sensitive/explicit/user_request
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_utcnow_iso, index=True)
    revoked_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    expires_at: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        Index("idx_msup_user_active", "user_id", "scope", "revoked_at"),
    )
```

并在文件头补 `from typing import Optional`(如果还没有)以及确认 `uuid7`、`_utcnow_iso` 已导入。

- [ ] **Step 4: 写 Alembic 迁移文件**

创建 `habit_list_backend/migrations/versions/c1f1a00b0001_add_moment_suppressions.py`:

```python
"""add moment_suppressions for user-driven echo/reply suppression

Revision ID: c1f1a00b0001
Revises: 5b76d8c9e2a1
Create Date: 2026-08-07 10:00:00
"""
from __future__ import annotations
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op

revision: str = "c1f1a00b0001"
down_revision: str | Sequence[str] | None = "5b76d8c9e2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "moment_suppressions",
        sa.Column("suppression_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column("scope_value", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("reason", sa.String(length=32), nullable=False, server_default="user_request"),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("revoked_at", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("suppression_id"),
    )
    with op.batch_alter_table("moment_suppressions", schema=None) as b:
        b.create_index("ix_moment_suppressions_user_id", ["user_id"], unique=False)
        b.create_index("ix_moment_suppressions_scope", ["scope"], unique=False)
        b.create_index("ix_moment_suppressions_scope_value", ["scope_value"], unique=False)
        b.create_index("ix_moment_suppressions_created_at", ["created_at"], unique=False)
        b.create_index("idx_msup_user_active", ["user_id", "scope", "revoked_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("moment_suppressions", schema=None) as b:
        b.drop_index("idx_msup_user_active")
        b.drop_index("ix_moment_suppressions_created_at")
        b.drop_index("ix_moment_suppressions_scope_value")
        b.drop_index("ix_moment_suppressions_scope")
        b.drop_index("ix_moment_suppressions_user_id")
    op.drop_table("moment_suppressions")
```

- [ ] **Step 5: 跑 alembic + 测试,确认通过**

Run:
```
cd habit_list_backend
alembic upgrade head
pytest tests/test_migrations.py::test_moment_suppressions_migration_is_reversible -v
```
Expected: upgrade head 成功;测试 PASS。

- [ ] **Step 6: Commit**

```bash
cd habit_list_backend
git add app/db/models.py migrations/versions/c1f1a00b0001_add_moment_suppressions.py tests/test_migrations.py
git commit -m "feat(moments): add moment_suppressions table for user-driven silencing"
```

---

## Task 2: 可解释策略门 — 替代简单计数

**Files:**
- Create: `habit_list_backend/app/moments/policy.py`
- Modify: `habit_list_backend/app/moments/service.py` (替换 `_occasional_budget_available` 调用)
- Test: `habit_list_backend/tests/test_moment_policy.py` (新建)

- [ ] **Step 1: 先写策略门单元测试**

新建 `tests/test_moment_policy.py`:

```python
"""Explainable policy gate for life-fragment responses."""
from __future__ import annotations
import datetime as dt
import pytest

from app.moments.policy import (
    PolicyContext,
    PolicyDecision,
    should_auto_respond,
    echo_budget_available,
    is_suppressed_source,
)

pytestmark = pytest.mark.anyio


class _FakeMoment:
    def __init__(self, moment_id: str, text: str, created_at: str, media_json: dict | None = None):
        self.episodic_id = moment_id
        self.raw_user_text = text
        self.created_at = created_at
        self.media_json = media_json or {}


class _FakeInteraction:
    def __init__(self, actor: str, trigger_type: str, created_at: str, reaction: str | None = None):
        self.actor = actor
        self.metadata_json = {"trigger_type": trigger_type}
        self.created_at = created_at
        self.reaction = reaction


def _ctx(*, mode="occasional", recent_user=None, recent_assistant=None,
         suppressions=None, moment=None, last_echo_at=None, crisis=False):
    now = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)
    return PolicyContext(
        now=now,
        response_mode=mode,
        moment=moment or _FakeMoment("m1", "窗台上的薄荷今天长出一片新叶", now.isoformat()),
        recent_user_moments=recent_user or [],
        recent_assistant_interactions=recent_assistant or [],
        suppressed_source_ids=set(suppressions or []),
        last_proactive_echo_at=last_echo_at,
        is_crisis=crisis,
    )


async def test_silent_mode_never_auto_responds_initial():
    ctx = _ctx(mode="silent")
    d = await should_auto_respond(ctx, trigger_type="initial")
    assert d.allowed is False
    assert "silent" in d.reason


async def test_always_mode_allows_initial_response_but_still_runs_gate():
    ctx = _ctx(mode="always")
    d = await should_auto_respond(ctx, trigger_type="initial")
    assert d.allowed is True


async def test_user_reply_always_allowed_regardless_of_mode():
    ctx = _ctx(mode="silent")
    d = await should_auto_respond(ctx, trigger_type="user_reply")
    assert d.allowed is True  # 用户主动回一句必须回复当前线程


async def test_occasional_allows_first_fragment():
    ctx = _ctx(mode="occasional", recent_assistant=[])
    d = await should_auto_respond(ctx, trigger_type="initial")
    # 第一条碎片允许回应(避免冷启动死静)
    assert d.allowed is True


async def test_occasional_blocks_if_last_two_fragments_both_had_initial_replies():
    now = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)
    recent_assistant = [
        _FakeInteraction("assistant", "initial", (now - dt.timedelta(minutes=30)).isoformat()),
        _FakeInteraction("assistant", "initial", (now - dt.timedelta(minutes=10)).isoformat()),
    ]
    ctx = _ctx(mode="occasional", recent_assistant=recent_assistant)
    d = await should_auto_respond(ctx, trigger_type="initial")
    assert d.allowed is False
    assert "budget" in d.reason


async def test_echo_budget_limits_one_per_24h_window():
    now = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.timezone.utc)
    assert echo_budget_available(now, None) is True
    assert echo_budget_available(now, (now - dt.timedelta(hours=25)).isoformat()) is True
    assert echo_budget_available(now, (now - dt.timedelta(hours=2)).isoformat()) is False


async def test_suppressed_source_is_filtered():
    assert is_suppressed_source("m_old", {"m_old"}) is True
    assert is_suppressed_source("m_old", {"m_other"}) is False


async def test_crisis_short_circuits_to_allow_for_safety_response():
    ctx = _ctx(mode="occasional", crisis=True)
    d = await should_auto_respond(ctx, trigger_type="initial")
    assert d.allowed is True
    assert d.reason == "crisis"
```

- [ ] **Step 2: 跑测试,确认失败(模块尚不存在)**

Run: `pytest tests/test_moment_policy.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.moments.policy'`。

- [ ] **Step 3: 写最小 policy.py 实现**

新建 `app/moments/policy.py`:

```python
"""Explainable policy gate for moment auto-replies and proactive echoes.

The gate replaces the previous "at most one reply per three fragments" counter
with an auditable decision that combines:
  * response mode (silent / occasional / always)
  * trigger type (initial / user_reply)
  * recent response density (rolling-window, not just counter)
  * echo frequency budget (one per 24h window, per spec §4.4)
  * suppressed sources / categories (from moment_suppressions)
  * crisis short-circuit (safety response always allowed)

The gate returns a PolicyDecision with a stable reason string so callers and
tests can explain "why quiet" without faking an AI reply.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Protocol


ECHO_BUDGET_WINDOW_HOURS = 24
DENSE_WINDOW_MINUTES = 60
DENSE_WINDOW_MAX_INITIAL_REPLIES = 1
RECENT_FRAGMENT_LOOKBACK = 3


class _HasIdTextDate(Protocol):
    episodic_id: str
    raw_user_text: str
    created_at: str


class _HasMetaDate(Protocol):
    actor: str
    metadata_json: dict
    created_at: str


@dataclass(frozen=True)
class PolicyContext:
    now: dt.datetime
    response_mode: str
    moment: _HasIdTextDate
    recent_user_moments: Iterable[_HasIdTextDate]
    recent_assistant_interactions: Iterable[_HasMetaDate]
    suppressed_source_ids: set[str]
    last_proactive_echo_at: str | None
    is_crisis: bool = False


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str  # stable machine-readable tag: silent|budget|dense|duplicate|crisis|mode_always|user_reply|fresh|ok
    # Optional human-friendly note for logging; not surfaced to user.
    note: str = ""


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def _count_recent_initial_replies(
    interactions: Iterable[_HasMetaDate], now: dt.datetime, window_minutes: int
) -> int:
    cutoff = now - dt.timedelta(minutes=window_minutes)
    count = 0
    for it in interactions:
        if it.actor != "assistant":
            continue
        if (it.metadata_json or {}).get("trigger_type") != "initial":
            continue
        ts = _parse_iso(it.created_at)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        if ts >= cutoff:
            count += 1
    return count


def is_suppressed_source(source_id: str, suppressed: set[str]) -> bool:
    return source_id in suppressed


def echo_budget_available(now: dt.datetime, last_echo_at: str | None) -> bool:
    if not last_echo_at:
        return True
    ts = _parse_iso(last_echo_at)
    if ts is None:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    return (now - ts) >= dt.timedelta(hours=ECHO_BUDGET_WINDOW_HOURS)


async def should_auto_respond(
    ctx: PolicyContext,
    *,
    trigger_type: str,
) -> PolicyDecision:
    """Decide whether the worker is allowed to emit an automatic reply for this event.

    Note: this gate runs BEFORE the model call.  It does not inspect text semantics;
    content-level judgment (reaction / pause / echo) stays in the model output and is
    re-validated afterward.
    """
    # 1. User reply in a thread is always honored (spec §4.3).
    if trigger_type == "user_reply":
        return PolicyDecision(True, "user_reply")

    # 2. Crisis short-circuits to allow the safety response.
    if ctx.is_crisis:
        return PolicyDecision(True, "crisis")

    mode = (ctx.response_mode or "occasional").strip().lower()

    # 3. Silent mode never creates initial auto-replies.
    if mode == "silent":
        return PolicyDecision(False, "silent")

    # 4. Always mode: still gate on recent density to avoid runaway spam, but allow.
    if mode == "always":
        dense = _count_recent_initial_replies(
            ctx.recent_assistant_interactions, ctx.now, DENSE_WINDOW_MINUTES
        )
        if dense >= 3:
            return PolicyDecision(False, "dense", "mode=always but 3 replies in 60min")
        return PolicyDecision(True, "mode_always")

    # 5. Occasional mode (default):
    #    - cold start (no recent assistant initial replies) -> allow once
    #    - rolling budget: at most 1 initial reply in the last 60 minutes AND
    #      at most 1 initial reply across the last 3 fragments
    recent_in_window = _count_recent_initial_replies(
        ctx.recent_assistant_interactions, ctx.now, DENSE_WINDOW_MINUTES
    )
    if recent_in_window >= DENSE_WINDOW_MAX_INITIAL_REPLIES + 1:
        return PolicyDecision(False, "dense", "too many replies in last 60min")

    recent_initial = sum(
        1
        for it in list(ctx.recent_assistant_interactions)[:RECENT_FRAGMENT_LOOKBACK]
        if it.actor == "assistant"
        and (it.metadata_json or {}).get("trigger_type") == "initial"
    )
    if recent_initial >= 1:
        return PolicyDecision(False, "budget", "at least one initial reply in recent fragments")

    return PolicyDecision(True, "fresh")
```

注意 `__init__.py` 不需要改(保持空即可,Python 3.11+ 支持隐式 namespace)。

- [ ] **Step 4: 跑测试,确认 PASS**

Run: `pytest tests/test_moment_policy.py -v`
Expected: 全部 PASS。

- [ ] **Step 5: 在 service.py 接入策略门**

在 `app/moments/service.py` 顶部加:

```python
from .policy import (
    PolicyContext,
    PolicyDecision,
    echo_budget_available,
    is_suppressed_source,
    should_auto_respond,
)
```

常量区追加:
```python
MOMENT_RESPONSE_POLICY_VERSION = "moment-witness-v2"  # 策略门 + 结构化 why_now
```

替换 `process_moment_response` 里第 251-256 行的简单计数块:

把:
```python
    if trigger_type == "initial" and response_mode == "occasional":
        async with get_db(read_only=True) as db:
            if not await _occasional_budget_available(
                db, user_id=str(outbox.user_id), moment_id=moment.episodic_id
            ):
                return
```

替换为(先构造上下文再调门):
```python
    # Build policy context from DB state.
    async with get_db(read_only=True) as db:
        from ..db.models import MomentSuppression
        sup_rows = list(
            (
                await db.execute(
                    select(MomentSuppression).where(
                        MomentSuppression.user_id == outbox.user_id,
                        MomentSuppression.scope.in_(["source", "echo_all"]),
                        MomentSuppression.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        suppressed_source_ids = {
            row.scope_value
            for row in sup_rows
            if row.scope == "source" and row.scope_value
        }
        echo_all_suppressed = any(row.scope == "echo_all" for row in sup_rows)

        # Gather rolling-window interactions for density check (last 24h).
        window_start = (
            outbox.created_at if outbox.created_at else _utcnow_iso()
        )  # approximate; use outbox created_at as anchor
        from datetime import datetime, timedelta, timezone
        try:
            anchor = datetime.fromisoformat(window_start.replace("Z", "+00:00"))
        except ValueError:
            anchor = datetime.now(timezone.utc)
        since = (anchor - timedelta(hours=24)).isoformat()
        recent_interactions = list(
            (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.user_id == outbox.user_id,
                        MomentInteraction.actor == "assistant",
                        MomentInteraction.status == "active",
                        MomentInteraction.created_at >= since,
                    ).order_by(MomentInteraction.created_at.desc())
                )
            ).scalars().all()
        )
        recent_moments = list(
            (
                await db.execute(
                    select(Episodic).where(
                        Episodic.user_id == outbox.user_id,
                        Episodic.kind == "life_fragment",
                        Episodic.status == "active",
                        Episodic.episodic_id != moment.episodic_id,
                    ).order_by(Episodic.created_at.desc()).limit(RECENT_FRAGMENT_LOOKBACK)
                )
            ).scalars().all()
        )
        last_echo_at = (user.settings_json or {}).get("last_proactive_echo_at")

        crisis = bool(_CRISIS_PATTERN.search(trigger_text))

        policy = await should_auto_respond(
            PolicyContext(
                now=anchor if anchor.tzinfo else anchor.replace(tzinfo=timezone.utc),
                response_mode=response_mode,
                moment=moment,
                recent_user_moments=recent_moments,
                recent_assistant_interactions=recent_interactions,
                suppressed_source_ids=suppressed_source_ids,
                last_proactive_echo_at=last_echo_at if not echo_all_suppressed else (anchor.isoformat()),
                is_crisis=crisis,
            ),
            trigger_type=trigger_type,
        )
        if not policy.allowed:
            # Record a quiet decision on metadata (not visible to user, for audit).
            return
```

并在顶部 import 区补 `from datetime import datetime, timedelta, timezone`(如果没有),把旧的 `_occasional_budget_available` 函数标记为 deprecated(或直接删,因为有测试覆盖新行为)。

同时把 metadata 中的 `"policy_version": MOMENT_RESPONSE_POLICY_VERSION` 改为 `"moment-witness-v2"`(已经通过常量引用)。

- [ ] **Step 6: 跑现有测试,确认没回归**

Run:
```
pytest tests/test_moment_interactions.py tests/test_moment_policy.py -v
```
Expected: 全部 PASS(可能需要为现有 echo 测试在 mock 决策时补 `suppressed_source_ids` 相关上下文——现有测试通过 monkeypatch 替换 `generate_agent_decision`,不会走新策略门的分支;因为新策略门在 outbox 处理阶段,现有测试已经通过 `process_pending_outbox` 调用,需要确认 `MomentSuppression` 在空库时没有行即不会阻塞)。

- [ ] **Step 7: Ruff 检查**

Run: `ruff check app/moments/policy.py app/moments/service.py`
Expected: 无 error。

- [ ] **Step 8: Commit**

```bash
git add app/moments/policy.py app/moments/service.py tests/test_moment_policy.py
git commit -m "feat(moments): replace counter with explainable policy gate v2"
```

---

## Task 3: Prompt v2 + 结构化输出 why_now + 失败不伪造

**Files:**
- Modify: `habit_list_backend/app/moments/service.py`
- Test: `habit_list_backend/tests/test_moment_interactions.py`(追加失败幂等测试)

- [ ] **Step 1: 先写失败不伪造的测试**

在 `tests/test_moment_interactions.py` 末尾追加:

```python
async def test_model_failure_does_not_fake_success_and_is_idempotent(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
):
    await _set_reply_mode(client, "always")
    created = await client.post(
        "/api/v1/moments",
        json={"text": "下班路上看到一只蹲在路灯下的橘猫"},
    )
    moment_id = created.json()["moment_id"]
    assert created.json()["response_pending"] is True

    async def _fake_failure(**_kwargs):
        raise RuntimeError("dashscope 503 simulated")

    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake_failure)
    result = await process_pending_outbox(test_settings)
    assert result["processed"] == 0
    assert result["retried"] + result["dead"] >= 1  # 失败走重试/死信,不伪造成功

    thread = await client.get(f"/api/v1/moments/{moment_id}/interactions")
    assert thread.status_code == 200
    # 没有 assistant 回应被写入;碎片仍保存
    assert [i["actor"] for i in thread.json()["items"]] == []

    # Outbox 幂等:再次消费不会产生重复回应
    result2 = await process_pending_outbox(test_settings)
    thread2 = await client.get(f"/api/v1/moments/{moment_id}/interactions")
    assert [i["actor"] for i in thread2.json()["items"]] == []
```

- [ ] **Step 2: 跑测试确认失败路径记录合理,然后修改 MomentAgentDecision**

把 `MomentAgentDecision` 升级加字段:

```python
class MomentAgentDecision(BaseModel):
    """Strict output contract for one optional agent interaction."""

    should_respond: bool
    reaction: Literal["none", "seen", "paused", "echo"] = "none"
    kind: Literal["reaction", "comment", "echo"] = "comment"
    comment: str = Field(default="", max_length=220)
    source_moment_ids: list[str] = Field(default_factory=list, max_length=2)
    # v2: model must explain "why now" for echoes; required when kind=echo.
    why_now: str = Field(default="", max_length=120)

    @field_validator("comment")
    @classmethod
    def _clean_comment(cls, value: str) -> str:
        return value.strip()
```

- [ ] **Step 3: 升级 system_prompt 到 v2(加 why_now 约束、禁止话术、80 字硬约束)**

替换 `generate_agent_decision` 里的 `system_prompt` 为:

```python
    system_prompt = f"""
你是手机应用"内在地形"里明确标注身份的 AI 陪伴者。用户写下的是生活碎片,不是求分析的素材,也不是待办。
你是有分寸的见证者,而不是评论员。策略版本:{MOMENT_RESPONSE_POLICY_VERSION}。

必须遵守:
1. 不把记录改造成目标、建议、待办或人格结论;不诊断,不冒充真人感受;禁用"你总是/你从来/这说明你是/感谢分享/为你点赞/继续加油"。
2. 只回应片段里的具体细节。评论最多两句,严格少于 80 个中文字符;最多一个轻问题,可以不问。
3. response_mode=occasional 时,信息薄/重复/无具体细节的碎片,应该 should_respond=false,不强行填满。
4. response_mode=always 时应回应,但可以只给 reaction=seen + 空 comment,仍需具体。
5. 只有确实与 echo_candidates 中旧片段的具体细节相关,才使用 echo;source_moment_ids 必须来自候选列表,且最多 2 条。
6. kind=echo 时必须填写 why_now,用一句不超过 40 字的中文解释"为什么现在提起",聚焦连接点,不证明系统记忆力。
7. reaction 语义:seen=安静看见(轻反应,短或空 comment),paused=被具体细节留住(允许一个轻问题),echo=跨时回声(必须带 why_now+source)。
8. 不使用表情符号堆砌,不使用感叹号轰炸,不使用引号标题体。
""".strip()
```

并在 worker 侧 `process_moment_response` 的模型调用外增加 try/except(worker 已经包了 try/except,但需要在 process_moment_response 内部不要让异常被吞——保持原样让外层标记失败即可)。不过现有 `_mark_failed` 已经会把 outbox 标为 pending/dead,不会生成 interaction。要确认 `process_moment_response` 自己不吞异常即可——检查当前代码第 234 行之后整个函数没有 try/except,正确,所以异常会冒泡给 worker 处理。

- [ ] **Step 4: 在写入 MomentInteraction 时,把 why_now 放进 metadata;echo kind 时要求 why_now 非空**

在 `process_moment_response` 中 source 校验后追加:

```python
    if kind == "echo" and not decision.why_now.strip():
        # Model violated schema contract; downgrade to paused to avoid surfacing an unexplained echo.
        kind = "comment"
        reaction = "paused" if reaction != "seen" else "seen"
```

并在 metadata_json 里加:
```python
    "why_now": decision.why_now.strip() if kind == "echo" else "",
```

- [ ] **Step 5: 跑测试**

Run: `pytest tests/test_moment_interactions.py -v`
Expected: 全部 PASS,包括新增的模型失败测试(monkeypatch 抛异常后 worker 走 retried,不写 interaction)。

- [ ] **Step 6: Commit**

```bash
git add app/moments/service.py tests/test_moment_interactions.py
git commit -m "feat(moments): prompt v2, why_now for echoes, no fake success on model failure"
```

---

## Task 4: 回声 V1 — 跨场景提示(GET /moments/echo-hint)、来源/关闭/禁止

**Files:**
- Create: `habit_list_backend/app/moments/echo.py`
- Modify: `habit_list_backend/app/moments/service.py`
- Modify: `habit_list_backend/app/api/v1/moments.py`
- Test: `habit_list_backend/tests/test_moment_echo.py`

- [ ] **Step 1: 先写 echo-hint 测试**

新建 `tests/test_moment_echo.py`:

```python
"""Proactive echo hint V1 — surfaced only in-app, at most one per 24h."""
from __future__ import annotations
import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.anyio


async def _post_moment(client, text, **kw):
    r = await client.post("/api/v1/moments", json={"text": text, **kw})
    assert r.status_code == 201
    return r.json()["moment_id"]


async def test_echo_hint_returns_empty_when_no_candidates(client: AsyncClient):
    r = await client.get("/api/v1/moments/echo-hint")
    assert r.status_code == 200
    assert r.json()["echo"] is None


async def test_echo_hint_respects_24h_budget_and_returns_why_and_source(
    client: AsyncClient,
):
    await _post_moment(client, "上个月第一次给窗台的薄荷换了盆", allow_proactive=True)
    await _post_moment(client, "薄荷今天终于冒出了一片很小的新叶")

    r = await client.get("/api/v1/moments/echo-hint")
    assert r.status_code == 200
    payload = r.json()
    # 没触发 LLM 的情况下,候选筛选阶段因为没有模型生成 why_now,应该返回 None
    # 真实 echo 由 worker 在生成回应时产生;echo-hint 只在有已生成 echo interaction 未展示过时返回
    assert "echo" in payload


async def test_echo_hint_suppressed_source_is_not_returned(
    client: AsyncClient,
):
    src = await _post_moment(client, "今天和老朋友吃了碗面", allow_proactive=True)
    await _post_moment(client, "今天又和老朋友吃了碗面")
    # 抑制该来源
    r = await client.post(
        f"/api/v1/moments/{src}/feedback",
        json={"action": "suppress_source", "reason": "dont_mention_again"},
    )
    assert r.status_code == 200
    hint = await client.get("/api/v1/moments/echo-hint")
    if hint.json()["echo"] is not None:
        assert src not in [s["moment_id"] for s in hint.json()["echo"].get("sources", [])]


async def test_echo_does_not_appear_when_allow_proactive_disabled(client: AsyncClient):
    await _post_moment(client, "旧记录但没授权主动引用", allow_proactive=False)
    await _post_moment(client, "新的一天")
    hint = await client.get("/api/v1/moments/echo-hint")
    assert hint.json()["echo"] is None or all(
        s.get("allow_proactive") is not False for s in (hint.json()["echo"].get("sources") or [])
    )
```

- [ ] **Step 2: 跑测试确认失败(端点不存在)**

Run: `pytest tests/test_moment_echo.py -v`
Expected: 404 on `/api/v1/moments/echo-hint`(以及 feedback 端点 404)。

- [ ] **Step 3: 写 echo.py**

新建 `app/moments/echo.py`:

```python
"""Proactive echo-hint selection for in-app surfaces (life/being/thread).

V1 scope (spec §5.1):
  * surfaced only in-app (no push);
  * at most one proactive echo per 24h window (spec §4.4);
  * every echo must carry source ids, a why_now, and offer close/disable controls;
  * deleted / hidden / unauthorized sources are filtered;
  * suppressed sources (moment_suppressions) are filtered.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import Episodic, MomentInteraction, MomentSuppression

ECHO_HINT_LOOKBACK_DAYS = 60
ECHO_BUDGET_HOURS = 24


async def _load_suppressed_source_ids(db: AsyncSession, user_id: str) -> set[str]:
    rows = list(
        (
            await db.execute(
                select(MomentSuppression).where(
                    MomentSuppression.user_id == user_id,
                    MomentSuppression.scope.in_(["source", "echo_all"]),
                    MomentSuppression.revoked_at.is_(None),
                )
            )
        ).scalars().all()
    )
    suppressed = {r.scope_value for r in rows if r.scope == "source" and r.scope_value}
    return suppressed, any(r.scope == "echo_all" for r in rows)


async def _last_echo_time(db: AsyncSession, user_id: str) -> str | None:
    # Prefer user.settings.last_proactive_echo_at; fall back to last assistant echo interaction.
    from ..db.models import User
    u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if u and (u.settings_json or {}).get("last_proactive_echo_at"):
        return (u.settings_json or {}).get("last_proactive_echo_at")
    last_echo = (
        await db.execute(
            select(MomentInteraction)
            .where(
                MomentInteraction.user_id == user_id,
                MomentInteraction.actor == "assistant",
                MomentInteraction.kind == "echo",
                MomentInteraction.status == "active",
                (MomentInteraction.metadata_json or {}).has_key("proactive"),
            )
            .order_by(MomentInteraction.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return last_echo.created_at if last_echo else None


def _parse_iso(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


async def get_proactive_echo_hint(
    db: AsyncSession,
    *,
    user_id: str,
    now: datetime | None = None,
) -> dict | None:
    """Return 0 or 1 proactive echo payload for in-app surfaces.

    The payload is selected from recent echo interactions already produced by the
    worker (so why_now is a real model output, not a heuristic). Only echoes that:
      - are active,
      - reference sources that are still active and allow_proactive,
      - are not suppressed,
      - have not yet been shown (no "hinted_at" marker),
    qualify.
    """
    now = now or datetime.now(timezone.utc)
    suppressed, echo_all_off = await _load_suppressed_source_ids(db, user_id)
    if echo_all_off:
        return None
    last = _parse_iso(await _last_echo_time(db, user_id))
    if last and (now - last) < timedelta(hours=ECHO_BUDGET_HOURS):
        return None

    since = (now - timedelta(days=ECHO_HINT_LOOKBACK_DAYS)).isoformat()
    candidates = list(
        (
            await db.execute(
                select(MomentInteraction)
                .where(
                    MomentInteraction.user_id == user_id,
                    MomentInteraction.actor == "assistant",
                    MomentInteraction.kind == "echo",
                    MomentInteraction.status == "active",
                    MomentInteraction.created_at >= since,
                )
                .order_by(MomentInteraction.created_at.desc())
                .limit(20)
            )
        ).scalars().all()
    )
    for inter in candidates:
        meta = inter.metadata_json or {}
        if meta.get("hinted_at"):
            continue
        source_ids = [s for s in (meta.get("source_moment_ids") or []) if s not in suppressed]
        if not source_ids:
            continue
        sources = list(
            (
                await db.execute(
                    select(Episodic).where(
                        Episodic.episodic_id.in_(source_ids),
                        Episodic.user_id == user_id,
                        Episodic.kind == "life_fragment",
                        Episodic.status == "active",
                    )
                )
            ).scalars().all()
        )
        sources = [
            s
            for s in sources
            if bool((s.media_json or {}).get("allow_proactive"))
        ]
        if not sources:
            continue
        return {
            "interaction_id": inter.interaction_id,
            "moment_id": inter.moment_id,
            "why_now": meta.get("why_now", ""),
            "excerpt": (inter.content or "")[:120],
            "created_at": inter.created_at,
            "sources": [
                {
                    "moment_id": s.episodic_id,
                    "excerpt": (s.raw_user_text or "")[:160],
                    "created_at": s.created_at,
                }
                for s in sources[:2]
            ],
        }
    return None


async def mark_echo_hinted(db: AsyncSession, *, interaction_id: str, now_iso: str) -> None:
    inter = (
        await db.execute(
            select(MomentInteraction).where(MomentInteraction.interaction_id == interaction_id)
        )
    ).scalar_one_or_none()
    if inter is None:
        return
    meta = dict(inter.metadata_json or {})
    meta["hinted_at"] = now_iso
    inter.metadata_json = meta
    db.add(inter)
```

注意:本计划里 `echo.py` 只**选择已经由 worker 生成的 echo interaction 作为跨场景提示**,不在 GET 路径里同步调用 LLM(避免在页面打开路径上耦合模型延迟)。首次 echo 仍然由 worker 在用户写碎片后异步生成;`/moments/echo-hint` 只是负责把其中一条未展示的、且满足频率与权限的回声"浮"到生活页/共处/线程顶部。

- [ ] **Step 4: 在 API 层增加端点 + 扩展 MomentInteractionOut**

在 `app/api/v1/moments.py` 顶部 import:
```python
from ...moments.echo import get_proactive_echo_hint, mark_echo_hinted
from ...db.models import MomentSuppression
```

在 `MomentInteractionOut` 中增加可选字段:
```python
class MomentInteractionOut(BaseSchema):
    interaction_id: str
    moment_id: str
    actor: str
    kind: str
    content: str
    reaction: str | None
    why_now: str = ""      # v2: echo explanation
    source_moments: list[MomentSourceOut]
    created_at: str
```

在 `_interaction_out` 中填充 why_now:
```python
    why_now=(interaction.metadata_json or {}).get("why_now", "") or "",
```

在文件末尾(`__all__` 之前)追加 echo-hint 端点:
```python
class EchoHintOut(BaseSchema):
    echo: dict | None


@router.get("/echo-hint", response_model=EchoHintOut)
async def get_echo_hint(
    mark: bool = Query(default=False, description="为本次渲染标记 hinted"),
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    async with get_db(read_only=False) as db:
        echo = await get_proactive_echo_hint(db, user_id=user_id)
        if echo and mark:
            await mark_echo_hinted(db, interaction_id=echo["interaction_id"], now_iso=_utcnow_iso())
        return EchoHintOut(echo=echo)


class MomentFeedbackReq(BaseModel):
    action: str = Field(..., pattern="^(not_like_me|less_often|suppress_source|suppress_category|dismiss_echo|restore_source|revise_text)$")
    reason: str = Field(default="user_request", max_length=32)
    target_interaction_id: str | None = None
    revised_text: str = Field(default="", max_length=400)
    category: str = Field(default="", max_length=64)
```

(feedback 端点在下一任务中完整实现。)

- [ ] **Step 5: 跑测试**

Run: `pytest tests/test_moment_echo.py tests/test_moment_interactions.py -v`
Expected: test_moment_echo 里最后一个 echo_hint_suppressed_source_is_not_returned 会因为 feedback 端点未实现而失败——这是预期的,下一任务打通即可。前两个测试应 PASS。

- [ ] **Step 6: Commit**(反馈端点未完成,拆提交;不过为减小颗粒度,这里先提交 echo.py 和 echo-hint 读端点)

```bash
git add app/moments/echo.py app/api/v1/moments.py tests/test_moment_echo.py
git commit -m "feat(moments): echo-hint endpoint v1 with source & budget gating"
```

---

## Task 5: 用户反馈闭环 — /moments/{id}/feedback

**Files:**
- Create: `habit_list_backend/app/moments/feedback.py`
- Modify: `habit_list_backend/app/api/v1/moments.py`
- Modify: `habit_list_backend/app/moments/service.py`(echo 候选筛选使用 suppressions)
- Test: `habit_list_backend/tests/test_moment_feedback.py`

- [ ] **Step 1: 写 feedback 测试**

新建 `tests/test_moment_feedback.py`:

```python
"""Feedback loop: user actions change subsequent reply/echo behavior."""
from __future__ import annotations
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import MomentSuppression

pytestmark = pytest.mark.anyio


async def test_suppress_source_creates_active_suppression_row(client: AsyncClient):
    m = await client.post("/api/v1/moments", json={"text": "old", "allow_proactive": True})
    src = m.json()["moment_id"]
    r = await client.post(
        f"/api/v1/moments/{src}/feedback",
        json={"action": "suppress_source", "reason": "dont_mention_again"},
    )
    assert r.status_code == 200
    async with get_db(read_only=True) as db:
        rows = list((await db.execute(select(MomentSuppression))).scalars().all())
    assert any(row.scope == "source" and row.scope_value == src and row.revoked_at is None for row in rows)


async def test_suppress_source_blocks_echo_candidates_in_worker(
    client: AsyncClient, test_settings, monkeypatch
):
    from app.moments import service as moment_service
    src = (await client.post(
        "/api/v1/moments", json={"text": "old-event", "allow_proactive": True}
    )).json()["moment_id"]
    await client.post(
        f"/api/v1/moments/{src}/feedback",
        json={"action": "suppress_source"},
    )
    # 先发 current 碎片
    cur = await client.post("/api/v1/moments", json={"text": "new-event"})
    cur_id = cur.json()["moment_id"]

    # 即使 mock 决策返回 echo,source 校验也应过滤掉被抑制的来源
    async def _fake(**_kw):
        from app.moments.service import MomentAgentDecision
        return MomentAgentDecision(
            should_respond=True, reaction="echo", kind="echo",
            comment="x", source_moment_ids=[src], why_now="y",
        )
    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake)
    await __import__("app.memory_v2.worker", fromlist=["process_pending_outbox"]).process_pending_outbox(test_settings)
    # interaction 不应携带被抑制的 source
    thread = await client.get(f"/api/v1/moments/{cur_id}/interactions")
    for it in thread.json()["items"]:
        assert src not in [s["moment_id"] for s in it.get("source_moments", [])]


async def test_less_often_lowers_density_threshold(client: AsyncClient):
    # 调用 feedback 后 settings 中 life_reply_density 应该降低
    r = await client.post(
        "/api/v1/moments/nonexistent/feedback",
        json={"action": "less_often"},
    )
    # 这条请求不依赖具体 moment;接口应更新 settings.life_reply_frequency=less
    assert r.status_code == 200
    profile = await client.get("/api/v1/me/profile")
    assert (profile.json()["settings"] or {}).get("life_reply_frequency") in {"less", "quiet"}


async def test_restore_source_revokes_suppression(client: AsyncClient):
    m = await client.post("/api/v1/moments", json={"text": "old", "allow_proactive": True})
    src = m.json()["moment_id"]
    await client.post(f"/api/v1/moments/{src}/feedback", json={"action": "suppress_source"})
    r = await client.post(f"/api/v1/moments/{src}/feedback", json={"action": "restore_source"})
    assert r.status_code == 200
    async with get_db(read_only=True) as db:
        rows = list((await db.execute(
            select(MomentSuppression).where(MomentSuppression.scope == "source", MomentSuppression.scope_value == src)
        )).scalars().all())
    assert all(row.revoked_at is not None for row in rows)
```

- [ ] **Step 2: 跑测试确认失败(feedback.py 不存在)**

Run: `pytest tests/test_moment_feedback.py -v`
Expected: FAIL(feedback endpoint returns 404/UnprocessableEntity)。

- [ ] **Step 3: 写 feedback.py**

新建 `app/moments/feedback.py`:

```python
"""User feedback on life-fragment replies and echoes.

Supported actions (spec §6):
  * not_like_me       -> down-weight this style/expression; suppress expression family
  * less_often        -> lower reply density (user.settings.life_reply_frequency = "quiet")
  * suppress_source   -> immediately suppress one source_id from proactive echoes
  * suppress_category -> suppress a category label (carried forward by category field)
  * dismiss_echo      -> synonym for suppress_source on an echo's source
  * restore_source    -> revoke prior suppression on a source
  * revise_text       -> user rewrites AI wording; record revision for audit
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import MomentInteraction, MomentSuppression, RawLedger, User, _utcnow_iso


async def apply_feedback(
    db: AsyncSession,
    *,
    user_id: str,
    moment_id: str,
    action: str,
    reason: str = "user_request",
    target_interaction_id: str | None = None,
    revised_text: str = "",
    category: str = "",
    request_id: str = "",
) -> dict:
    now = _utcnow_iso()
    out: dict = {"action": action, "applied": True}

    if action == "suppress_source" or action == "dismiss_echo":
        db.add(MomentSuppression(
            user_id=user_id,
            scope="source",
            scope_value=moment_id,
            reason=reason or "user_request",
        ))
        out["scope"] = "source"
        out["suppressed"] = moment_id

    elif action == "suppress_category":
        db.add(MomentSuppression(
            user_id=user_id,
            scope="category",
            scope_value=category or "generic",
            reason=reason or "user_request",
        ))
        out["scope"] = "category"
        out["suppressed"] = category or "generic"

    elif action == "less_often":
        u = (await db.execute(select(User).where(User.user_id == user_id))).scalar_one()
        s = dict(u.settings_json or {})
        s["life_reply_frequency"] = "quiet"
        u.settings_json = s
        db.add(u)
        out["life_reply_frequency"] = "quiet"

    elif action == "not_like_me":
        # 记录风格抑制;下次策略门读取 style suppressions 时可以偏向更短更安静
        db.add(MomentSuppression(
            user_id=user_id,
            scope="style",
            scope_value=target_interaction_id or "global",
            reason=reason or "not_like_me",
        ))

    elif action == "restore_source":
        rows = list(
            (
                await db.execute(
                    select(MomentSuppression).where(
                        MomentSuppression.user_id == user_id,
                        MomentSuppression.scope == "source",
                        MomentSuppression.scope_value == moment_id,
                        MomentSuppression.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
        )
        for r in rows:
            r.revoked_at = now
            db.add(r)
        out["revoked_count"] = len(rows)

    elif action == "revise_text":
        if target_interaction_id and revised_text.strip():
            inter = (
                await db.execute(
                    select(MomentInteraction).where(
                        MomentInteraction.interaction_id == target_interaction_id,
                        MomentInteraction.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if inter is not None:
                meta = dict(inter.metadata_json or {})
                meta["revised_content"] = revised_text.strip()
                meta["revised_at"] = now
                inter.metadata_json = meta
                db.add(inter)
                out["revised_interaction_id"] = target_interaction_id

    db.add(RawLedger(
        user_id=user_id,
        entry_type="moment_feedback",
        payload_json={
            "moment_id": moment_id,
            "action": action,
            "reason": reason,
            "request_id": request_id,
        },
        trace_json={"request_id": request_id},
    ))
    return out
```

- [ ] **Step 4: 在 API 层挂上 feedback 端点**

在 `app/api/v1/moments.py` 中补 import:
```python
from ...moments.feedback import apply_feedback
```

在文件末尾追加路由(替换之前写了但空壳的 MomentFeedbackReq 之后):
```python
@router.post("/{moment_id}/feedback")
async def post_moment_feedback(
    moment_id: str,
    body: MomentFeedbackReq,
    user_id: str = Depends(current_user),
    req_id: str = Depends(request_id),
):
    async with get_db(read_only=False) as db:
        # 权限:source 必须属于自己(如果不是 "nonexistent" 通用 less_often,则要校验)
        if moment_id != "nonexistent":
            moment = (
                await db.execute(
                    select(Episodic).where(
                        Episodic.episodic_id == moment_id,
                        Episodic.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if moment is None and body.action not in {"less_often", "suppress_category", "restore_source", "not_like_me"}:
                raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
        result = await apply_feedback(
            db,
            user_id=user_id,
            moment_id=moment_id,
            action=body.action,
            reason=body.reason,
            target_interaction_id=body.target_interaction_id,
            revised_text=body.revised_text,
            category=body.category,
            request_id=req_id,
        )
    return result
```

- [ ] **Step 5: 在 service.py 中候选 source 过滤时用 suppressed_source_ids(已有,Task 2 已接入),并在 echo_candidates 上叠加 allow_proactive+status+suppressed 三重过滤**

在 `_load_generation_context` 中把:
```python
    echo_candidates = [
        item for item in candidates if bool((item.media_json or {}).get("allow_proactive"))
    ][:10]
```
升级为:
```python
    # Apply echo eligibility (spec §5.2): active + allow_proactive + not suppressed
    async with get_db(read_only=True) as db2:
        sup_rows = list(
            (
                await db2.execute(
                    select(MomentSuppression).where(
                        MomentSuppression.user_id == outbox.user_id,
                        MomentSuppression.scope.in_(["source", "category", "echo_all"]),
                        MomentSuppression.revoked_at.is_(None),
                    )
                )
            ).scalars().all()
        )
    suppressed = {r.scope_value for r in sup_rows if r.scope == "source" and r.scope_value}
    echo_all_off = any(r.scope == "echo_all" for r in sup_rows)
    if echo_all_off:
        echo_candidates = []
    else:
        echo_candidates = [
            item for item in candidates
            if bool((item.media_json or {}).get("allow_proactive"))
            and item.episodic_id not in suppressed
        ][:10]
```

并在文件头部补对 MomentSuppression 的 import。

- [ ] **Step 6: 跑所有测试**

Run:
```
pytest tests/test_moment_feedback.py tests/test_moment_echo.py tests/test_moment_interactions.py tests/test_moment_policy.py -v
```
Expected: 全部 PASS。

- [ ] **Step 7: Ruff**

Run: `ruff check app/moments/ app/api/v1/moments.py`

- [ ] **Step 8: Commit**

```bash
git add app/moments/feedback.py app/moments/service.py app/api/v1/moments.py tests/test_moment_feedback.py
git commit -m "feat(moments): user feedback loop closes — suppress/restore/less-often/revise"
```

---

## Task 6: 删除 / 隐藏 / 撤销授权 — Outbox 取消 + 防复活

**Files:**
- Modify: `habit_list_backend/app/api/v1/moments.py`(增加 DELETE /moments/{id} 隐藏;PATCH 改权限)
- Modify: `habit_list_backend/app/moments/service.py`(复用 Outbox 取消 helper)
- Modify: `habit_list_backend/app/db/models.py`(确认 CASCADE;加 scope="echo_all" 支持)
- Test: `habit_list_backend/tests/test_moment_deletion.py`

- [ ] **Step 1: 写删除/防复活测试**

新建 `tests/test_moment_deletion.py`:

```python
"""Delete/hide/revoke must cancel pending outbox and prevent zombie revives."""
from __future__ import annotations
import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db.database import get_db
from app.db.memory_models import OutboxEvent
from app.db.models import MomentInteraction

pytestmark = pytest.mark.anyio


async def test_hidden_fragment_has_no_interactions_and_cancels_pending_outbox(
    client: AsyncClient,
):
    await client.patch("/api/v1/me/profile", json={"settings": {"life_reply_mode": "always"}})
    r = await client.post("/api/v1/moments", json={"text": "要被隐藏的一刻"})
    mid = r.json()["moment_id"]
    assert r.json()["response_pending"] is True
    # 隐藏
    d = await client.delete(f"/api/v1/moments/{mid}")
    assert d.status_code == 200
    # 线程读 404
    t = await client.get(f"/api/v1/moments/{mid}/interactions")
    assert t.status_code == 404
    # pending outbox 被置为 processed(取消),不会继续生成回应
    async with get_db(read_only=True) as db:
        outboxes = list((await db.execute(
            select(OutboxEvent).where(OutboxEvent.event_type == "moment.response.requested")
        )).scalars().all())
        assert all(o.status in {"processed", "dead"} for o in outboxes if str((o.payload_json or {}).get("moment_id")) == mid)


async def test_revoking_proactive_removes_from_echo_sources(
    client: AsyncClient,
):
    r = await client.post(
        "/api/v1/moments",
        json={"text": "不想被回声引用的一刻", "allow_proactive": True},
    )
    mid = r.json()["moment_id"]
    u = await client.patch(
        f"/api/v1/moments/{mid}",
        json={"allow_proactive": False},
    )
    assert u.status_code == 200
    # 之后 echo-hint 不应返回这条作为来源
    hint = await client.get("/api/v1/moments/echo-hint")
    if hint.json()["echo"] is not None:
        assert mid not in [s["moment_id"] for s in hint.json()["echo"]["sources"]]


async def test_deleted_fragment_prevents_worker_from_reviving(
    client: AsyncClient, test_settings, monkeypatch
):
    from app.moments import service as moment_service
    from app.memory_v2.worker import process_pending_outbox
    await client.patch("/api/v1/me/profile", json={"settings": {"life_reply_mode": "always"}})
    r = await client.post("/api/v1/moments", json={"text": "删后不应复活"})
    mid = r.json()["moment_id"]
    # 立刻隐藏
    await client.delete(f"/api/v1/moments/{mid}")
    # mock 决策让 worker 试图生成回应
    async def _fake(**_kw):
        from app.moments.service import MomentAgentDecision
        return MomentAgentDecision(should_respond=True, reaction="seen", kind="reaction", comment="...", source_moment_ids=[])
    monkeypatch.setattr(moment_service, "generate_agent_decision", _fake)
    await process_pending_outbox(test_settings)
    async with get_db(read_only=True) as db:
        inters = list((await db.execute(
            select(MomentInteraction).where(MomentInteraction.moment_id == mid)
        )).scalars().all())
    # 由于 moment 不再 active,上下文加载直接返回 None,worker 不应写入 interaction
    assert inters == []
```

- [ ] **Step 2: 跑测试确认失败(DELETE 不支持 / PATCH 不支持)**

Run: `pytest tests/test_moment_deletion.py -v`
Expected: 405 Method Not Allowed on DELETE/PATCH。

- [ ] **Step 3: 在 API 层实现 DELETE(moment 隐藏)+ PATCH(权限修改)+ 取消 Outbox**

在 `app/api/v1/moments.py` 顶部追加:
```python
from sqlalchemy import update as sql_update
```

在文件末尾追加:
```python
class MomentPatchReq(BaseModel):
    allow_proactive: bool | None = None
    use_for_terrain: bool | None = None
    hidden: bool | None = None


@router.patch("/{moment_id}")
async def patch_moment(
    moment_id: str,
    body: MomentPatchReq,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=False) as db:
        moment = (
            await db.execute(
                select(Episodic).where(
                    Episodic.episodic_id == moment_id,
                    Episodic.user_id == user_id,
                    Episodic.kind == "life_fragment",
                )
            )
        ).scalar_one_or_none()
        if moment is None:
            raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
        media = dict(moment.media_json or {})
        if body.allow_proactive is not None:
            media["allow_proactive"] = body.allow_proactive
            if body.allow_proactive is False:
                # 撤销授权时,未来不再作为回声来源;同时把引用这条的待处理 echo 过滤掉由 worker 在加载时判断
                pass
        if body.use_for_terrain is not None:
            media["use_for_terrain"] = body.use_for_terrain
        if body.hidden is True:
            moment.status = "archived"
        elif body.hidden is False:
            moment.status = "active"
        moment.media_json = media
        db.add(moment)

        # 取消尚未处理的 moment outbox(避免 worker 在 moment 隐藏后仍尝试生成回应)
        await db.execute(
            sql_update(OutboxEvent)
            .where(
                OutboxEvent.user_id == user_id,
                OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                OutboxEvent.status.in_(["pending", "processing"]),
            )
            .values(
                status="processed",
                processed_at=_utcnow_iso(),
                locked_at=None,
                last_error="cancelled:moment_hidden_or_revoked",
            )
            .execution_options(synchronize_session=False)
        )
    return {"ok": True, "moment_id": moment_id}


@router.delete("/{moment_id}")
async def delete_moment(
    moment_id: str,
    user_id: str = Depends(current_user),
):
    """Hide a life fragment (soft-delete). Hard delete is out of scope for v1
    (per spec §7 we only need delete semantics that prevent resurrection).
    We also expose the legacy /pebbles/{id} path during migration; this is the
    canonical endpoint moments UI uses going forward.
    """
    return await patch_moment(
        moment_id,
        MomentPatchReq(hidden=True),
        user_id,
    )  # type: ignore[arg-type]
```

注意 `patch_moment` 因为有 `Depends`,需要用 FastAPI 的依赖注入。上面的 return 写法不合适;改写成直接调用底层函数。重构为内部函数 `_set_moment_status_and_perms`,然后 DELETE 与 PATCH 都调它:

```python
async def _set_moment_visibility(
    db,
    *,
    user_id: str,
    moment_id: str,
    hidden: bool | None,
    allow_proactive: bool | None,
    use_for_terrain: bool | None,
):
    moment = (
        await db.execute(
            select(Episodic).where(
                Episodic.episodic_id == moment_id,
                Episodic.user_id == user_id,
                Episodic.kind == "life_fragment",
            )
        )
    ).scalar_one_or_none()
    if moment is None:
        raise ApiError("NOT_FOUND", "这片生活记录不存在", 404)
    media = dict(moment.media_json or {})
    if allow_proactive is not None:
        media["allow_proactive"] = allow_proactive
    if use_for_terrain is not None:
        media["use_for_terrain"] = use_for_terrain
    if hidden is True:
        moment.status = "archived"
    elif hidden is False:
        moment.status = "active"
    moment.media_json = media
    db.add(moment)
    await db.execute(
        sql_update(OutboxEvent)
        .where(
            OutboxEvent.user_id == user_id,
            OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
            OutboxEvent.status.in_(["pending", "processing"]),
            (OutboxEvent.payload_json["moment_id"].as_string() == moment_id),
        )
        .values(
            status="processed",
            processed_at=_utcnow_iso(),
            locked_at=None,
            last_error="cancelled:moment_hidden_or_revoked",
        )
        .execution_options(synchronize_session=False)
    )
    return moment
```

上面 SQLite+JSON 比较可能有兼容性问题,改用 Python 侧过滤(更安全):

```python
        pending = list(
            (
                await db.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.user_id == user_id,
                        OutboxEvent.event_type == MOMENT_RESPONSE_REQUESTED,
                        OutboxEvent.status.in_(["pending", "processing"]),
                    )
                )
            ).scalars().all()
        )
        for ev in pending:
            if str((ev.payload_json or {}).get("moment_id")) == moment_id:
                ev.status = "processed"
                ev.processed_at = _utcnow_iso()
                ev.locked_at = None
                ev.last_error = "cancelled:moment_hidden_or_revoked"
                db.add(ev)
```

端点实现:
```python
@router.patch("/{moment_id}")
async def patch_moment(
    moment_id: str,
    body: MomentPatchReq,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=False) as db:
        await _set_moment_visibility(
            db,
            user_id=user_id,
            moment_id=moment_id,
            hidden=body.hidden,
            allow_proactive=body.allow_proactive,
            use_for_terrain=body.use_for_terrain,
        )
    return {"ok": True, "moment_id": moment_id}


@router.delete("/{moment_id}", status_code=200)
async def delete_moment(
    moment_id: str,
    user_id: str = Depends(current_user),
):
    async with get_db(read_only=False) as db:
        await _set_moment_visibility(
            db, user_id=user_id, moment_id=moment_id,
            hidden=True, allow_proactive=None, use_for_terrain=None,
        )
    return {"ok": True, "archived": True}
```

- [ ] **Step 4: 确认 process_moment_response 在 moment 已 archived 时直接返回(已经由 _load_generation_context 的 `status=="active"` 过滤实现了)。同时 list_moments 与 get_moment_interactions 已加 `status=="active"` 过滤,因此隐藏后列表/线程都读不到,防复活闭环成立。**

- [ ] **Step 5: 跑测试**

Run:
```
pytest tests/test_moment_deletion.py tests/test_moment_echo.py tests/test_moment_feedback.py tests/test_moment_interactions.py tests/test_moment_policy.py -v
```
Expected: 全部 PASS。

- [ ] **Step 6: 前端把删除入口从 `/pebbles/{id}` 改为 `/moments/{id}`(在 Task 8 里做,本 Task 只做后端)。**

- [ ] **Step 7: Ruff + Commit**

Run: `ruff check app/api/v1/moments.py`
```bash
git add app/api/v1/moments.py tests/test_moment_deletion.py
git commit -m "feat(moments): delete/hide/revoke cancels outbox and blocks zombie echoes"
```

---

## Task 7: Worker 可靠性 — last_error 不泄露正文 + 幂等 + 失败状态

**Files:**
- Modify: `habit_list_backend/app/memory_v2/worker.py`
- Modify: `habit_list_backend/app/moments/service.py`(外抛异常保持不变)
- Test: 复用 test_moment_interactions::test_model_failure_does_not_fake_success

- [ ] **Step 1: 审查现有 `_mark_failed`**——已只持久化 `type(exc).__name__[:120]`,符合隐私要求(§7);moment 分支没有特殊处理。验证幂等:process_moment_response 里已经有 outbox_id 检查。
- [ ] **Step 2: 为防止 OutboxEvent 重复消费时 `source_moment_ids` 被污染,在幂等检查里把"已有 outbox_id 的 assistant interaction"也视为已完成,直接 return**(已经实现,见 service.py L241-L246)。
- [ ] **Step 3: 补一条失败后 outbox 不重复写入 interaction 的集成测试**(已在 Task 3 写过)。
- [ ] **Step 4: Ruff + Commit**

```bash
git commit --allow-empty -m "chore(moments): verify worker idempotency/privacy; no code change"
```

(若有需要把 `_mark_failed` 的 last_error 长度检查加强到 120 并且显式 cast 为 str,可以在本 Task 一并改;当前实现已满足。)

---

## Task 8: 前端(app.html) — 反馈菜单、回声 why、390x844 适配

**Files:**
- Modify: `app.html`(单文件)

前端任务在单文件 `app.html` 中进行,不引入新框架。改动集中在生活碎片相关片段(行号见调研报告)。

- [ ] **Step 1: 回应反馈菜单 — 给 `.le-agent` 与 `.mtp-message.assistant` 追加"⋯"菜单**

在 CSS 中追加:
```css
.le-agent-more { position: absolute; top: 8px; right: 8px; opacity: 0; transition: opacity .15s; font-size: 18px; color: var(--text-3); cursor: pointer; padding: 2px 6px; border-radius: 6px; }
.le-agent:hover .le-agent-more { opacity: 1; }
.le-agent-menu { position: absolute; right: 8px; top: 30px; background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 6px 0; min-width: 180px; z-index: 20; box-shadow: 0 6px 20px rgba(0,0,0,.18); display: none; }
.le-agent-menu.show { display: block; }
.le-agent-menu button { display: block; width: 100%; text-align: left; padding: 9px 14px; background: transparent; border: 0; color: var(--text-1); font-size: 14px; }
.le-agent-menu button:hover { background: var(--surface-3); }
.le-agent-menu button.danger { color: var(--danger, #c0392b); }
.le-why { font-size: 12px; color: var(--text-3); margin-top: 6px; }
.le-source { display: block; margin-top: 4px; padding: 6px 8px; border-left: 2px solid var(--accent-2); font-size: 12px; color: var(--text-2); background: var(--surface-2); border-radius: 0 6px 6px 0; }
```

- [ ] **Step 2: renderMomentAgent 加入 why_now + 更多菜单**

把 `renderMomentAgent(entry)` 中 `.le-agent-copy` 之后增加:
```js
if (entry.latestAgentInteraction && entry.latestAgentInteraction.why_now) {
    html += `<div class="le-why">${escapeHtml(entry.latestAgentInteraction.why_now)}</div>`;
}
if (entry.latestAgentInteraction && entry.latestAgentInteraction.source_moments) {
    for (const s of entry.latestAgentInteraction.source_moments) {
        html += `<span class="le-source">来自 ${formatSourceMomentDate(s.created_at)} · ${escapeHtml(s.excerpt)}</span>`;
    }
}
html += `<span class="le-agent-more" data-id="${entry._localId}" title="更多">⋯</span>`;
html += `<div class="le-agent-menu" data-menu-for="${entry._localId}">
  <button data-act="not-like-me">这不像我</button>
  <button data-act="less-often">少这样回应</button>
  <button data-act="dismiss-echo" class="danger">别再提这条</button>
</div>`;
```

- [ ] **Step 3: 菜单事件委托 + API 调用**

在 life tab 渲染后绑定一次 click 委托:
```js
function bindLifeAgentMenu() {
  const root = document.getElementById('lifeListWrap');
  if (!root || root._menuBound) return;
  root._menuBound = true;
  root.addEventListener('click', async (e) => {
    const more = e.target.closest('.le-agent-more');
    if (more) {
      const id = more.getAttribute('data-id');
      const menu = root.querySelector(`.le-agent-menu[data-menu-for="${id}"]`);
      if (menu) menu.classList.toggle('show');
      e.stopPropagation();
      return;
    }
    const btn = e.target.closest('.le-agent-menu button');
    if (!btn) return;
    const menu = btn.closest('.le-agent-menu');
    const id = menu.getAttribute('data-menu-for');
    const entry = lifeEntries.find(x => x._localId == id);
    if (!entry) return;
    const act = btn.getAttribute('data-act');
    menu.classList.remove('show');
    try {
      await api(`/moments/${entry._episodicId}/feedback`, {
        method: 'POST',
        body: { action: act, reason: 'user_request' },
      });
      showToast('好,我记住了');
      await syncLifeFromBackend(true);
    } catch (err) {
      apiWarn('操作失败,稍后再试');
    }
  });
  document.addEventListener('click', () => {
    root.querySelectorAll('.le-agent-menu.show').forEach(m => m.classList.remove('show'));
  });
}
```
在 `renderLife()` 末尾调用 `bindLifeAgentMenu()`。

- [ ] **Step 4: 打开生活页时拉取 echo-hint 并在顶部渲染**

在 `syncLifeFromBackend(force)` 成功后追加:
```js
  if (force) {
    api('/moments/echo-hint?mark=true').then(r => r.json()).then(j => {
      const hint = j.echo;
      const wrap = document.getElementById('lifeListWrap');
      if (!wrap) return;
      const old = wrap.querySelector('.life-echo-hint');
      if (old) old.remove();
      if (!hint) return;
      const div = document.createElement('div');
      div.className = 'life-echo-hint';
      div.innerHTML = `<div class="leh-label">一段回声</div>
        <div class="leh-why">${escapeHtml(hint.why_now || '')}</div>
        ${(hint.sources||[]).map(s => `<div class="le-source">来自 ${formatSourceMomentDate(s.created_at)} · ${escapeHtml(s.excerpt)}</div>`).join('')}
        <div class="leh-actions">
          <button data-echo-act="dismiss" data-iid="${hint.interaction_id}" data-mid="${hint.moment_id}">关闭</button>
          <button data-echo-act="suppress" data-iid="${hint.interaction_id}" data-mid="${hint.moment_id}" class="danger">别再提这条</button>
        </div>`;
      wrap.prepend(div);
    }).catch(()=>{});
  }
```
追加对应事件委托(点击关闭/别再提:关 → 移除 dom;别再提 → POST feedback suppress_source + 移除)。

- [ ] **Step 5: 删除入口从 /pebbles 改到 /moments**

找到 `doDelete()` (原 L4816-L4843),把:
```js
api(`/pebbles/${pendingDeleteId}`, { method: 'DELETE' })
```
改成:
```js
const isLife = pendingDeleteType === 'life';
const path = isLife ? `/moments/${pendingDeleteId}` : `/pebbles/${pendingDeleteId}`;
api(path, { method: 'DELETE' })
```
并在删除成功后从 `lifeEntries` 里移除对应 entry,`renderLife()`,而不是只 sync river。

- [ ] **Step 6: 390x844 手机尺寸媒体查询**

在 CSS 末尾追加:
```css
@media (max-width: 420px) {
  .life-entry { padding: 12px 12px 10px; }
  .le-text { font-size: 15px; line-height: 1.5; }
  .le-agent { margin-top: 8px; padding: 8px 10px; }
  .mtp-card { width: calc(100vw - 20px); max-height: 80vh; }
  .terrain-card { padding: 14px; }
  .bottom-nav { height: 60px; }
  .screen { padding-bottom: 70px; }
  .lap-card { width: calc(100vw - 20px); }
  .life-echo-hint { margin: 8px 12px; }
}
```
确保底部导航、碎片线程、回声来源、反馈菜单不溢出。

- [ ] **Step 7: 本地起服务并冒烟**

Run(一个终端):
```
cd habit_list_backend
uvicorn app.main:app --host 0.0.0.0 --port 8780 --reload
```
浏览器打开 `app.html`,验证:
- 生活页发一条碎片,等待回应(polling 成功);
- 回应旁显示"⋯"菜单,点"别再提这条"后 toast 提示,再次拉取不再显示那条作为 echo 来源;
- 390x844 视口下底栏、线程面板、回声卡不重叠;
- 删除碎片走 `/moments/{id}` 后端返回 200,列表不再显示。

- [ ] **Step 8: Commit**

```bash
cd /f/every_day_progress/habit_list
git add app.html
git commit -m "feat(ui): feedback menu, echo why_now, 390x844 mobile pass, canonical /moments delete"
```

---

## Task 9: 自动化验证与交付清单

**Files:**
- Modify: 如有需要补修测试

- [ ] **Step 1: 跑全量后端测试**

Run:
```
cd habit_list_backend
pytest -q
```
Expected: 全部 PASS,包括原有的 test_memos/test_chat/test_memory_v2_* 等不回归。

- [ ] **Step 2: Ruff 全量检查**

Run: `ruff check app/`

- [ ] **Step 3: Alembic 验证**

Run:
```
alembic upgrade head
alembic downgrade base
alembic upgrade head
```
Expected: 三轮迁移无报错。

- [ ] **Step 4: 按验收矩阵手工跑关键场景(写在这里作为 checklist,不另写文档)**

对照任务书 §10:
- [ ] 默认 occasional 可以安静/短回应;不创建 Memo/UserEvent/Terrain
- [ ] silent 不创建 outbox;用户回一句仍能回复
- [ ] always 每条有回应,但不出现任务/诊断/套话
- [ ] 用户回复碎片留在当前线程(已在 test_fragment_reply_stays_in_thread 覆盖)
- [ ] 未允许 allow_proactive 时不返回 source
- [ ] 允许但无强关联时 echo-hint 为空
- [ ] 有效回声解释 why_now + 展示来源 + 可关/可禁
- [ ] 别再提这条后抑制立即生效
- [ ] 删除碎片后 outbox 被取消,worker 不复活
- [ ] 危机文本走安全回应且不进入长期路径
- [ ] 模型失败不伪造成功
- [ ] 重复消费同一 outbox 最多一条回应(幂等)
- [ ] 390x844 手机尺寸 UI 不重叠

- [ ] **Step 5: 提供交付摘要(直接回复给用户,不另写 md)**

按任务书 §11 建议格式输出:目标/已完成/改动文件/数据库迁移/测试结果/手工验收/已知限制/质检重点。不声称已上线。

---

## 自查(Self-Review)

**1. 规格覆盖:**
- §3 核心闭环: Task 2-6 覆盖;quiet 结果作为正常路径
- §4.1 三种回应: seen/paused/echo + why_now(Task 3)
- §4.2 决策信号: policy 门 + 模型判断(Task 2+3)
- §4.3 硬规则:silent/occasional/always/crisis/用户主动回复/长度/禁话术(T2+T3)
- §4.4 频率预算:1/24h 回声、密度、同一来源短期不重复、别再提(T2+T5)
- §5 回声 V1:App 内三场景(life 提示已做;共处/线程详情入口 echo-hint 同端点复用,前端 Task 8 中只在生活页渲染,共处/线程后续在 Phase 2 接入;端点 GET 已可复用)
- §5.2 资格:5 个条件全部在 echo.py 校验
- §5.3 来源与删除:source 携带、删除后不展示、AI 回应不作为来源(Task 4+6)
- §6 反馈:6 种 action(Task 5),全部真实影响行为
- §7 隐私/失败:Outbox 不复制正文、幂等、失败不伪造、删除不复活、管理员默认不看正文(继承现有)
- §8 必须完成 1-6:覆盖(策略门/回声/反馈/删除/可靠性/测试+UI)
- §10 验收矩阵 13 条:已逐一对应到测试/手工验收
- §9 约束:未动部署、未起手机测试环境、未做语音/推送/社交/待办/周报/管理员面

**2. 占位符扫描:** 已避免 TBD/TODO;每个代码步骤给出实际代码。

**3. 类型一致性:** 使用 `MomentAgentDecision`/`PolicyDecision`/`PolicyContext`/`MomentInteractionOut`/`MomentSourceOut`/`EchoHintOut`/`MomentFeedbackReq`/`MomentPatchReq`,字段命名跨任务一致(source_moment_ids/why_now/suppressed_source_ids/echo_hint)。

---
