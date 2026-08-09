# Memory V3：形成层设计（Formation）

> 版本：v1.0
> 日期：2026-08-08
> 状态：设计已定，待实现
> 上游依据：`内在地形-产品基线-v2.md` 第 1.5、7、8 章
> 关系：本文档不替代 `memory-v2.md`。V2 描述的基础设施层（UserEvent / Claim / Evidence / Revision / Tombstone / 检索硬过滤）全部保留且不修改语义。本文档补上 V2 缺失的**形成层**，并修正两处与产品基线冲突的实现。

---

## 0. 为什么需要这一层

产品基线 1.5 节承诺的惊喜时刻是：

> 它发现了一个我**没有明确说过**、但从几段真实经历中确实能看见的变化。

当前实现无法做到，原因不是质量不足，而是**范畴错误**：

| 环节 | 当前实现 | 能力边界 |
|---|---|---|
| 提取 | `extractor.py:45-87` 六条正则 | 只能抓显式陈述。"没明确说过"的内容在原理上抓不到 |
| 提取（LLM 模式） | `_EXTRACTION_SYSTEM_PROMPT:99` | prompt 第 5 条明确写「跨事件推断不在本任务执行」 |
| 「跨时间形成」 | `terrain.py:342-346` | 三个计数器。它给已有 claim 盖章，不产生新洞察 |

产出的地貌形如「喜欢跑步（说过 3 次，跨 9 天，2 个会话）」。这是"它记得我说过三次"，恰好是基线 1.5 明确排除的那件事。

**计数不是形成。** 形成层就是补这一步。

### 0.1 两种质量必须分开评估

- **不出错的质量**：不编造、不复活、不越界。当前 V2 做得好，本设计不得削弱任何一条。
- **有价值的质量**：能发现真东西。当前接近零，本设计的全部目标。

这两者互相拉扯。当前设计极度偏向前者，以至于不产出任何东西——这不是安全，是用安全换掉了产品。本设计的核心命题是：**在不放松任何一条安全约束的前提下，把发现能力从零抬起来。**

---

## 1. P0 决策：共处证据进入地形

### 1.1 现状冲突

`terrain.py:216-217` 与 `313-314` 硬过滤：

```python
UserEvent.source == "moment",
UserEvent.mode == "moment",
```

即**共处对话产生的证据永远进不了地形**。这与基线 7 章核心循环图直接冲突（图中写「真实共处**或**主动留下一刻 → … → 地貌浮现」）。后果是用户只聊天永远长不出任何东西，产品从陪伴产品退化为记录产品。

### 1.2 决策

**共处证据默认进入证据池，但"进入"仅指成为不可见证据；任何可见结论仍需过门槛 + 用户确认。**

用户的控制点从**入口**移到**出口**。

### 1.3 为什么不能逐条授权

最初的直觉方案是给共处也做逐条勾选（像生活碎片那样）。这个方案必须否决，理由是它**会杀死产品的核心价值**：

如果要用户逐条勾选，他只会勾选自己已经知道重要的话。而"我没说出口的变化"恰好藏在他不会勾的那些话里。逐条授权把发现范围缩减到用户的自我认知边界之内，惊喜时刻在定义上就不可能发生。

### 1.4 不对称是有意的

| 来源 | 性质 | 默认是否进地形 | 理由 |
|---|---|---|---|
| 生活碎片 | 刻意留下，用户有明确意图 | **否**（需明确勾选） | 他知道自己在做什么，控制权应该给他 |
| 共处对话 | 自然流露，无意识材料 | **是**（但不可见） | 价值恰在无意识部分；且它不产生任何可见物直到成熟 |

**刻意的东西给用户控制，无意识的东西给系统发现权，但结论权一律在用户。**

这条不对称需要 Alpha 验证。验证问题：用户知道共处内容在被沉淀后，是感到被理解还是被监控。

### 1.5 共处进入的门禁（全部为硬约束）

共处证据要成为 `terrain_eligible`，必须同时满足：

1. 内容是用户原文。AI 回复、系统提示、模型总结一律排除（`service.py` 已保证，只收 `content=user_text`）
2. `sensitivity != crisis`（`reconcile.py:280-282` 已保证）
3. `sensitivity != sensitive` —— **共处来源的敏感内容不进地形**。这条比生活碎片更严，因为共处没有逐条授权
4. 用户未开启记忆暂停（新增全局开关，见 1.7）
5. 不处于危机会话窗口内（见 P3，本文档 5.2）
6. 不是低置信 ASR 转写（见 P3，本文档 5.1）

### 1.6 实现：一个字段解决三个问题

`UserEvent` 新增：

```python
terrain_eligible: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
```

这一个字段同时解决：

1. **P0 架构决策**：共处写入时按 1.5 的门禁计算该字段
2. **权限绕过风险**：生活碎片的 `use_for_terrain` 从只存在于 `moments.py:537-541` 应用层，下沉为数据库列。任何绕过 `moments.py` 的路径都无法伪造权限
3. **source 过滤 hack**：`terrain.py` 的 `source == "moment"` 过滤替换为 `UserEvent.terrain_eligible.is_(True)`

写入责任：
- `moments.py:537-541` 的 `effective_use_for_terrain` 直接写入该列
- `system1.py:274-282` 调用 `enqueue_user_event` 时按 1.5 门禁计算并写入
- `service.py:enqueue_user_event()` 增加参数 `terrain_eligible: bool`，**无默认值**，强制调用方显式决定

撤回：用户事后撤回某条来源的地形用途时，将该列置 `False`，并触发依赖该证据的 claim 重算（复用现有降级/删除路径）。

### 1.7 记忆暂停开关

基线 P7 要求「用户可以暂停所有新形成，同时继续正常共处」。当前 `service.py:104-105` 只检查全局 `memory_v2_mode == "off"`，那是运维开关不是用户开关。

新增用户级设置 `memory_formation_paused`，位于「我们」页。暂停时：
- `terrain_eligible` 一律为 `False`
- 已有证据和地貌不受影响，不删除
- 共处、生活记录、回声（基于已有已确认地貌）照常工作

---

## 2. 形成层架构

```
UserEvent (用户原文，terrain_eligible 已判定)
   ↓ extractor（现有，逐事件、显式）
MemoryClaim [source_type=user_explicit]        ←── 产品语言里的「印记」
   ↓ MemoryEvidence（现有，逐字定位到原文）
   │
   ├─────────────────────────────────────────┐
   ↓                                          │
【Stage 1 信号聚类】确定性，零模型成本         │
   ↓ 输出：合格候选簇（已过 3/7/2 门槛）      │
【Stage 2 假设生成】LLM，schema 强约束         │
   ↓                                          │
MemoryClaim [source_type=formation]  ←────────┘  ←── 产品语言里的「地貌」
   证据 = 源簇内全部证据的并集（继承，不新生成）
   user_status = proposed，allow_proactive = False
   ↓
【Stage 3 投影】terrain.py，只负责展示与状态机
```

### 2.1 产品语言终于有了技术指称

基线 4 章定义的「印记」和「地貌」此前没有技术对应物，两者都是 `MemoryClaim`。形成层给了它们精确区分：

| 产品语言 | 定义（基线 4 章） | 技术指称 |
|---|---|---|
| 印记 | 从少量证据形成、仍不稳定的变化假设 | `source_type = user_explicit` 的 Claim |
| 地貌 | 多条独立证据支持、跨时间出现的形成性模式 | `source_type = formation` 的 Claim |

这不是命名游戏。它意味着「印记升级为地貌」不再是状态字段的变化，而是**一次真实的推断行为**。

---

## 3. Stage 1：信号聚类

**原则：门槛在这一步卡死。** 不满足 3 条独立证据 / 跨 7 天 / 2 个场景的簇，根本不进入 Stage 2。这样 LLM 永远看不到不合格的材料，**在结构上不可能编出没有证据的结论**。这是把安全约束前置，而不是事后过滤。

全部信号可由现有表计算，零模型成本。

### 3.1 六种信号

输入范围：该用户所有 `user_status ∈ {confirmed, corrected, proposed}`、`deleted_at IS NULL`、`sensitivity == normal` 的 Claim，及其 `terrain_eligible = True` 的证据。

| 信号 | 计算方式 | 映射地貌类别 |
|---|---|---|
| **语义聚集** | 复用 `MemoryEmbedding` 的 claim 级向量，余弦相似度 ≥ 阈值聚为一簇。≥2 个不同 slot_key 的 claim 聚在一起才算有效 | 正在长出来 |
| **跨场景重现** | 单个 claim 的 supports 证据落在 ≥2 个不同 `session_id`，且跨 ≥7 天 | 反复回到 |
| **矛盾共存** | 同一 claim 或同一簇内同时存在 `supports` 与 `contradicts` 证据，且两侧各 ≥1 条来自不同事件 | 两股力量 |
| **时间反差** | 同一 `slot_key` 存在 `supersedes_claim_id` 链，或 `valid_to` 被设置过；表述随时间改变 | 正在松动 |
| **加速** | 簇内证据在最近 14 天的出现频率显著高于此前（≥2 倍且最近至少 2 条） | 正在长出来 |
| **消退** | 曾达到成熟的簇，最近 `TERRAIN_FADE_DAYS` 内无新证据 | 季节里的「沉寂」，不产出新地貌 |

### 3.2 簇的准入门槛（硬约束，不可配置到更低）

对每个候选簇，取其内部全部 claim 的 supports 证据并集，计算：

```python
evidence_ids   = 并集，按 (event_id) 去重
span_days      = max(occurred_at).date - min(occurred_at).date
contexts       = {session_id} 的基数
contradictions = contradicts 角色的证据数
```

准入条件（全部满足）：

1. `len(evidence_ids) >= 3`
2. `span_days >= 7`
3. `len(contexts) >= 2`
4. 每条证据的 `UserEvent.status == "active"` 且 `terrain_eligible is True` 且 `deleted_at IS NULL`
5. 簇内不存在 `sensitivity != normal` 的 claim
6. 该簇的 `formation_fingerprint` 无墓碑（见 3.4）

条件 1-3 是从 `terrain.py:342-346` **前移**过来的。前移之后 `terrain.py` 不再需要重复判定成熟度。

配置项 `memory_v3_min_evidence` / `min_span_days` / `min_contexts` 允许调高，**下限锁死在 3 / 7 / 2**，与基线 11.1「安全下限不能由普通运营配置降低」一致。

### 3.3 冲突不阻断，冲突是产物

基线 8.4 明确「冲突不是错误数据」。因此 `contradictions > 0` **不作为准入否决条件**，而是把该簇标记为 `two_forces` 候选，交给 Stage 2 生成「两股力量」表达。

只有一种情况否决：簇内存在**用户已明确否认（`user_status == rejected`）**的 claim。此时该 claim 被剔出簇，剔除后若不再满足门槛则整簇作废。这一条保证 P5「用户显式校正高于模型置信度」。

### 3.4 幂等与防复活

每个簇计算一个指纹：

```python
formation_fingerprint = sha256(user_id + "|" + "|".join(sorted(evidence_event_ids)))
```

用途：

- **幂等**：同一簇不重复生成假设。已有 `source_type=formation` 的 claim 携带同一指纹 → 跳过 Stage 2，只更新证据数与状态
- **防复活**：用户永久删除一个形成型地貌时，除现有的 claim 墓碑与 claim-evidence 绑定墓碑外，额外写一条 `resource_type="formation"` 的指纹墓碑。Stage 1 在准入时检查该墓碑，命中则整簇作废

这样"删掉的地貌下次扫描又长回来"这个必然会发生的 bug 被结构性排除。这是形成层特有的复活路径，V2 的两类墓碑覆盖不到。

---

## 4. Stage 2：假设生成

只有合格簇进入。输入**只有**证据原文、时间、场景标签。

### 4.1 绝对不给模型看的东西

- AI 的任何历史回复
- 用户的其他敏感 claim
- 模型自己此前的推断（防止推断叠推断）
- 置信度数值（防止模型对齐到数字而非内容）

### 4.2 证据是继承的，不是生成的

这是本设计最重要的安全性质。

形成型 claim 的证据 = 源簇内全部证据的并集。每条证据**此前已经被 `reconcile.py:114-131` 的 `ground_evidence()` 逐字定位过**。

因此 LLM **不需要输出任何字符偏移**，只需要给每条已定位证据贴一个角色标签。

结论：**形成层在结构上不可能编造证据位置**。V2 最强的那条硬约束被完整继承，且不依赖模型的诚实。

### 4.3 输出 schema（强约束）

```python
class FormationHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terrain_kind: Literal[
        "growing",      # 正在长出来
        "recurring",    # 反复回到
        "loosening",    # 正在松动
        "two_forces",   # 两股力量
        "unnamed",      # 尚未命名
    ]
    claim_text: str = Field(min_length=4, max_length=200)   # 一句形成性表达
    why_now: str = Field(min_length=4, max_length=200)      # 为什么现在浮现
    evidence_roles: list[EvidenceLabel]                     # 每条输入证据一个标签
```

### 4.4 作废规则（校验失败即丢弃，不重写）

| 条件 | 处理 |
|---|---|
| `supports` 标签数 < 3 | **整个假设作废**。模型自己都凑不出 3 条支持，说明簇是噪声 |
| 输出未覆盖全部输入证据 | 作废 |
| `claim_text` 命中禁止词表 | 重试一次，再失败则丢弃 |
| `terrain_kind == "two_forces"` 但 `contradicts` 标签为 0 | 作废（自相矛盾） |
| schema 校验失败 | 作废，不做修复式重试 |

**禁止词表**（对应基线 4.1 与 10.3）：

```
你总是 / 你从来 / 你就是 / 你这个人 / 本质上 / 诊断 / 症 / 障碍 /
人格 / 性格缺陷 / 应该 / 必须 / 我比你更
```

### 4.5 `unnamed` 是重要出口，不是失败

模型允许输出「尚未命名」——它看到了信号但说不清。这比强行命名正确得多，而且基线 4.1 已经把它列为五种合法地貌之一。

`unnamed` 类型的地貌卡展示为：证据可见、时间范围可见、表达位置写「这里有一些信号，但还说不清是什么」，并直接提供**共同命名**入口——用户可能一眼就知道那是什么。

这个出口把模型的无能转化为产品价值，而且它是「共同命名」最自然的触发场景。

### 4.6 写入

```python
MemoryClaim(
    source_type   = "formation",
    user_status   = "proposed",       # 必须用户确认
    allow_proactive = False,          # 确认前不得主动引用
    terrain_state = 由 terrain_kind 映射,
    confidence    = 不写入模型自报值，由证据数与跨度计算
    created_by_policy_version = "terrain-formation-v1",
)
```

`confidence` 不采用模型自报值。模型的自评与真实准确率无关，且基线 8.5 要求界面不展示小数置信度。改为由证据数、跨度、场景数、矛盾数计算的可解释分档。

---

## 5. 同期修补的三个安全漏洞（P3）

这三条在当前链路下"跑不到"，但形成层上线后立刻成为真实风险，已同批完成。

### 5.1 未核对的 ASR 转写

基线 8.3 要求「低置信 ASR 不能成为地貌证据」。实现时发现更严的一档：Qwen 的内联 ASR 端点**根本不返回 confidence**，而这正是产品实际走的那条路。

实现（`providers/dashscope.py` / `media/service.py`）：
- `asr_transcribe` 返回 `Transcription(text, confidence)`；confidence 取**分段里最差的那个分数**——一句话里错一个从句，就足以污染围绕它做出的推断
- confidence 落在 `media_assets.transcript_confidence`（媒体资产比单轮对话活得久，判定要能在授权变更时重算）
- `None` 视为**未经核对**，不是"大概没问题"：`transcript_is_terrain_trusted` 只在服务商明确背书且 ≥ `memory_v3_min_asr_confidence` 时放行
- 拦截点是三处入口：新建生活碎片、事后改授权（`PATCH /moments/{id}`）、陪伴对话的语音输入
- **这不是语音的死路**：客户端把转写交给用户过目，用户确认或改过的文字以 `text` 提交，那时它已经是用户自己的话，完全可用

原始音频与转写照常保留可播放。未核对只影响**能否成为地貌证据**。

### 5.2 危机干预过程未隔离

基线 8.3 要求排除「危机表达**及危机干预过程**」。危机表达本身早有关键词过滤，干预过程原先无定义。

实现（`memory_v2/formation.py::_crisis_windows`）：
- 窗口由 `sensitivity == crisis` 的 UserEvent 现场推算：同 session、从危机时刻起 `memory_v3_crisis_window_minutes` 分钟内的证据，在 `_load_evidence` 阶段被剔除
- **在读取侧过滤而非写入时打标**：打标只能管住之后写的行，而危机往往是在若干轮之后才被识别出来；现场推算能把已经落库的行一并追溯隔离
- **只向后不向前**：同一会话里危机之前说的话，是在事情发生之前说的，仍然可用
- 「我现在好些了」不含任何危机关键词，但落在窗口内，因此同样被排除——这正是本条要挡住的东西

### 5.3 门槛下沉

`terrain.py:342-346` 的三个计数器前移到 Stage 1 准入（本文档 3.2）。`terrain.py` 之后只负责：

- 投影可见的 claim（`user_status ∈ _VISIBLE_STATUSES`、未删除、`sensitivity == normal`）
- 状态机（forming / strengthening / loosening / faded）
- 证据与来源展示

不再判定"能不能算地貌"——那个判定发生在形成时。

---

## 6. 触发与成本

### 6.1 触发

复用现有 outbox 机制，新增事件类型 `formation.scan`：

- 用户有新的 `terrain_eligible = True` 证据落地时入队
- **去抖**：该用户已有 pending 的 `formation.scan` 时不再入队，且 `available_at = now + 6h`。一次宣泄连发五条只产生一次扫描
- worker 复用 `_claim_batch()` 的 `FOR UPDATE SKIP LOCKED`，继承现有并发安全

### 6.2 每次扫描的产出上限

基线第 7 章：单场会话最多 2 个候选，默认目标 0。

- 每次扫描最多进入 Stage 2 的簇数：**2**
- 多个合格簇时，按（证据数 × 跨度 × 场景数）排序取前 2，其余留到下次
- 零合格簇是**正常且常见**的结果，不记为失败，不告警

### 6.3 成本

Stage 1 是纯 SQL + 向量运算，零模型成本。只有合格簇进 Stage 2。

因此大部分用户在大部分时间的形成成本为零——这恰好与基线 P3「不形成记忆是正常结果」同构。**克制在这里同时是伦理约束和成本控制。**

---

## 7. 首次揭示与共同命名

形成型 claim 落地后 `user_status = proposed`，不主动弹出。

首次揭示（基线 6.3）：

> 有一个变化似乎正在你身上发生。现在想看看吗？

打开后按基线 8.5 的地貌卡契约展示：形成性表达 → 状态 → 时间范围 → 证据摘要 → 为什么现在出现（直接用 Stage 2 的 `why_now`）→ 校正 → 控制。

用户操作映射（复用现有 `service.py:transition_claim()`）：

| 操作 | 已有实现 | 形成层额外行为 |
|---|---|---|
| 像我 | `confirmed`，`allow_proactive = True` | 簇指纹记为已认可，后续扫描不重复生成 |
| 不像我 | `rejected`，`allow_proactive = False` | 簇内 claim 加负权，**相似簇降权而非换措辞重提**（基线第 7 章） |
| 暂时不知道 | `deferred` | 不主动推送，不作为强断言 |
| 共同命名 | `terrain_user_label` | 见下 |
| 永久删除 | 三类墓碑（含 3.4 的指纹墓碑） | 该簇永不再生成 |

**共同命名**：`terrain_user_label` 字段已存在但未被产品化。用户命名后，该词汇必须进入 Agent 的回复生成上下文，AI 之后用**用户自己的词**指称这块地貌。这是产品唯一可口头传播的差异化动作，应从 P1 提到 P0（见任务 11）。

---

## 8. 必须补的回归测试

区分「测了 happy path」和「测了违约会被拦住」。以下全部属于后者：

| 测试 | 验证 |
|---|---|
| 证据不足不得产出 | 2 条证据 / 跨 5 天 / 1 个场景，Stage 1 必须不放行，Stage 2 必须不被调用 |
| supports < 3 作废 | 模型返回只有 2 条 supports，必须丢弃且不写 claim |
| 禁止词表拦截 | 模型返回「你总是回避亲密」，必须重试后丢弃 |
| 证据继承不可伪造 | 模型输出中包含不在输入内的 event_id，必须整体作废 |
| 已否认 claim 剔出簇 | 簇内含 rejected claim，剔除后门槛不足则整簇作废 |
| 删除后不复活 | 删除形成型地貌，重跑扫描，指纹墓碑必须拦住 |
| 敏感内容不进形成 | `sensitivity=sensitive` 的 claim 不得出现在任何簇 |
| 危机窗口隔离 | 危机后同会话内的证据不进簇；危机之前说的话仍然可用 |
| 未核对转写隔离 | confidence 为 `None` 或 0.41 的语音碎片可正常保存播放，但拿不到地形授权；用户过目后以文字提交则完全可用 |
| 共处证据可进地形 | 纯共处产生的 3 条证据跨 7 天 2 场景，必须能形成地貌（P0 决策的正向验证） |
| 生活碎片默认不进 | 未勾选 `use_for_terrain` 的碎片，`terrain_eligible` 必须为 False |
| 记忆暂停生效 | 暂停期间新证据 `terrain_eligible` 全为 False，已有地貌不受影响 |
| 去抖 | 连发 5 条只产生 1 个 `formation.scan` |
| 扫描幂等 | 同一簇重跑扫描不产生第二个 claim |

---

## 9. 与产品基线的冲突与修订要求

本设计需要 `内在地形-产品基线-v2.md` 做两处修订：

1. **第 8.3 节**需明确共处证据的默认地形资格，以及"控制点在出口不在入口"这一决策（本文档 1.2-1.4）
2. **第 9 节 P1 列表**中的「共同命名」提升为 P0（本文档第 7 节）

第 18 节「仍待验证但不阻塞重构的问题」第 1 条（地形首页采用卡片、轻地图还是两者结合）不再适合归在"不阻塞"——它是产品唯一的视觉资产，应升为独立设计任务。

---

## 10. 版本记录

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-08-08 | 首版。定案共处证据进入地形（控制点从入口移到出口）；补形成层两阶段设计；证据继承而非生成；门槛前移至准入；新增形成指纹墓碑；同批修补 ASR 置信度与危机窗口 |
