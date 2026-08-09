"""Product Baseline V2 Phase 0 guards for the legacy single-file prototype."""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_HTML = PROJECT_ROOT / "app.html"


def _submit_user_source(html: str) -> str:
    start_marker = "async function submitUser(){"
    end_marker = "// 发送按钮："
    start = html.index(start_marker)
    end = html.index(end_marker, start)
    return html[start:end]


def test_companion_submit_cannot_auto_create_memo_or_timeline_entry():
    html = APP_HTML.read_text(encoding="utf-8")
    submit_source = _submit_user_source(html)

    assert "mode:'confide'" in submit_source
    assert "detectMemo(" not in submit_source
    assert "addToTimeline(" not in submit_source
    assert "memo_detected" not in submit_source
    assert "kind_hint" not in submit_source
    assert "memoAutoHint" not in html


def test_the_legacy_history_layer_is_gone_not_just_hidden():
    """旧长河页（石子时间线 + 我发现的 + 旧「我」页）整层已删除。

    它原先靠 `.phase0-legacy-hooks{display:none!important}` 隔离在 DOM 里，只为了让
    旧选择器还能命中。但没有任何测试引用那些选择器，而它污染每一次全局审计：
    24 个零引用 token、36 种圆角里有相当一部分只属于这层看不见的标记。
    更要紧的是它带着一整条活的死链路——`renderTimeline()` 把 `/pebbles` 拉回来的数据
    渲染进一个永远不可见的容器，`/insights/{id}/confirm` 是唯一的校正入口却点不到。
    地形页现在自己有完整的校正链路（像我 / 不像我 / 暂时不知道 / 共同命名 / 删除，
    走 `/memories/...`），所以这层不是「还没接上」，是已经被取代了。
    """

    html = APP_HTML.read_text(encoding="utf-8")

    # 隔离本身：一个专门用来藏东西的 class，是「这里有死 DOM」的自白。
    assert "phase0-legacy-hooks" not in html

    # 标记
    for ghost in (
        'id="riverSearch"', 'id="pane-river"', 'id="pane-insight"',
        'id="timeline"', 'id="insightList"', 'id="pebbleDetail"',
        'class="me-screen', "river-tab", "insight-hero",
    ):
        assert ghost not in html, ghost

    # 只喂那层标记的 JS：留着它们就等于留着一条看不见的数据链路。
    for ghost in (
        "pebbleData", "renderTimeline", "renderInsights", "switchRiverPane",
        "syncRiverFromBackend", "addToTimeline", "openPebbleDetail",
        "/insights", "/pebbles",
    ):
        assert ghost not in html, ghost

    # 取代它的东西必须在：地形自己的校正链路，以及一条真的地平线。
    assert "api('/terrain'" in html
    assert "/memories/" in html
    assert 'data-action="confirm"' in html and 'data-action="reject"' in html
    assert "这里还没有起伏。" in html


def test_no_fabricated_history_is_left_anywhere():
    html = APP_HTML.read_text(encoding="utf-8")

    assert "那些你说过的话" not in html
    assert "都沉成了河里的石子" not in html
    assert "基于 6 周数据" not in html


def test_phase1_navigation_and_explicit_controls_are_real_not_demo_data():
    html = APP_HTML.read_text(encoding="utf-8")

    assert 'data-screen="companion"' in html
    assert 'data-screen="life"' in html
    assert 'data-screen="terrain"' in html
    assert 'data-screen="we"' in html
    assert "data-screen=\"river\"" not in html
    assert "m_demo" not in html
    assert "便利店的关东煮阿姨" not in html
    assert "inner-terrain-avatar-256.png" in html
    assert "这是一个 AI 陪伴者" in html
    assert 'id="lifeReplyChoices"' in html
    assert 'id="momentUseTerrain"' in html
    assert 'id="momentUseTerrain" checked' not in html


def test_phase1_chat_moment_and_terrain_contracts_are_wired():
    html = APP_HTML.read_text(encoding="utf-8")
    submit_source = _submit_user_source(html)

    assert "intent:'stay'" not in submit_source
    assert "no_trace:false" not in submit_source
    assert 'id="noTraceBtn"' not in html
    assert 'class="intent-rail"' not in html
    assert 'id="defaultNoTrace"' not in html
    assert "session_id:companionSessionId" in submit_source
    assert "api('/moments'" in html
    assert "api('/terrain'" in html
    assert "data-action=\"defer\"" in html
    assert "uploadBrowserMedia" in html
    assert "audio_asset_id:audioAssetId" in submit_source
    assert 'id="lapImageInput"' in html


def test_life_fragments_v1_contracts_are_present():
    """生活碎片互动 v1 必须具备的 UI 契约：回声、反馈、pending/失败态、共处回声。"""
    html = APP_HTML.read_text(encoding="utf-8")

    # 生活页：回声 banner 带 why_now + dismiss
    assert 'id="lifeEchoBanner"' in html
    assert 'id="lifeEchoBannerText"' in html
    assert 'id="lifeEchoBannerDismiss"' in html
    assert "为什么是现在" in html
    # 反馈菜单 5 个动作
    assert 'id="momentFeedbackSheet"' in html
    assert "stop_source" in html
    assert "stop_category" in html
    assert "less_responses" in html
    assert "not_like_me" in html
    assert "unsure" in html
    # 回应线程面板
    assert 'id="momentThreadPanel"' in html
    # 删除走规范端点
    assert "/moments/" in html and "method:'DELETE'" in html
    # 共处页回声卡片
    assert 'id="compEchoHint"' in html
    assert "comp-echo-text" in html
    assert "comp-echo-why" in html
    # 待处理/失败态文案
    assert "它正在看" in html
    assert "回应没接住" in html
    # 390/420 移动端适配媒体查询
    assert "@media (max-width:420px)" in html or "@media (max-width: 420px)" in html


def test_first_run_onboarding_contract_is_present():
    """首次进入必须出现 4 句话说明：AI 身份、可只聊不记录、可能看错、可删除校正。"""
    html = APP_HTML.read_text(encoding="utf-8")

    # 弹层节点
    assert 'id="onboardModal"' in html
    assert 'id="onboardAckBtn"' in html
    # 四句话的关键短语（与任务书 §6.1 对齐）
    assert "不是一个人" in html
    assert "也不会假装成为人" in html
    assert "可能看错" in html
    assert "解释权在你" in html
    # 本地持久化 + 后端 settings 同步
    assert "innerTerrain.onboardedAt" in html
    assert "onboarded_at" in html
    # “我们”页可重新查看
    assert 'id="weReplayOnboarding"' in html
    assert "__showOnboarding" in html


def test_failed_moment_retry_contract_is_present():
    """失败态必须提供可见的重试入口，且调用 POST /moments/{id}/retry。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "le-retry-btn" in html or "le-retry-inline" in html
    assert "retryMomentResponse" in html
    assert "/moments/" in html and "/retry" in html
    # 失败态只允许这一句（生活页无边界布局规范 §3.5）：不写服务异常/网络错误，
    # 用户只需要知道这不是他的错、以及下一步能做什么。
    assert "这一次它没能回应。" in html
    assert "再试" in html


def test_rewrite_expression_contract_is_present():
    """§6：用户必须能当场改写 AI 的回应，并保留原版以供审计。"""
    html = APP_HTML.read_text(encoding="utf-8")
    # 反馈菜单里有入口
    assert "修改这句表达" in html
    # 前端调用 PATCH /moments/{id}/interactions/{iid} 提交新文案
    assert "_submitRewrite" in html
    assert "method:'PATCH'" in html
    # 有内联输入区
    assert "momentFeedbackText" in html
    assert "momentFeedbackRewrite" in html
    # 改写过的回应有可视标记
    assert "le-agent-copy rewritten" in html or ".le-agent-copy.rewritten" in html
    assert "（你改过这句）" in html


def test_rewrite_endpoint_exists_and_preserves_original():
    """后端 PATCH 端点必须存在，且把原文保留在 metadata.original_content 中。"""
    from app.api.v1 import moments as moments_router
    from inspect import unwrap

    # 端点注册在路由里
    routes = [
        (list(r.methods), r.path)
        for r in moments_router.router.routes
        if hasattr(r, "methods")
    ]
    patch_routes = [
        p for methods, p in routes if "PATCH" in methods and "interactions" in p
    ]
    assert patch_routes, "PATCH /moments/{id}/interactions/{iid} endpoint missing"

    # 关键行为：原模型输出保留在 metadata["original_content"]
    src = (PROJECT_ROOT / "habit_list_backend" / "app" / "api" / "v1" / "moments.py").read_text(
        encoding="utf-8"
    )
    assert 'metadata["original_content"]' in src or "metadata.get(\"original_content\")" in src
    assert "rewritten_by_user" in src
    assert "moment_interaction_rewrite" in src  # 留痕到 raw_ledger


# --- 地形页：地层剖面（内在地形-地形页视觉规范-v1.md）--------------------------


def test_terrain_page_is_a_profile_not_cards_or_a_map():
    """§1 定案：地形页的形状是横向时间轴 + 跨在轴上的带。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert 'id="trProfile"' in html
    assert 'id="trAxis"' in html and 'id="trLanes"' in html and 'id="trMonths"' in html
    # 现在点是轴上唯一有颜色的刻度
    assert "tr-now" in html
    # 旧的宣言 + 常驻门槛条 + 玻璃卡列表必须已经不在
    assert 'id="terrainRule"' not in html
    assert 'id="terrainList"' not in html


def test_terrain_band_height_is_a_constant_not_a_confidence_bar():
    """§2.2 硬规则：带高是常量。随证据数变化就是在画置信度柱状图。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "--tr-band-h: 6px" in html
    assert "--tr-lane-h: 44px" in html
    # 带的几何只允许由时间驱动：left/width 来自 first_seen_at / last_seen_at
    assert "band.style.left=left+'%'" in html
    # 高度不出现在 JS 里（不由 evidence_count 推导）
    assert "band.style.height" not in html


def test_terrain_uses_shape_not_colour_to_classify():
    """§3：颜色不分类，形状分类——颜色分类需要图例，图例是说明书。"""
    html = APP_HTML.read_text(encoding="utf-8")
    for shape in ("growing", "recurring", "loosening", "two_forces", "unnamed"):
        assert f".tr-band.{shape}" in html
    # 五种形状共用同一个色相
    assert html.count("--river-rose-rgb") >= 5
    # 消退淡化落在泳道上，才能与「其余带降到 .34」相乘而不是被覆盖
    assert ".tr-lane.faded{opacity:.28}" in html


def test_terrain_empty_state_is_the_profile_itself():
    """§5：空态不是另一个界面，空态就是剖面本身。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "这里还没有起伏。" in html
    assert "我们可以只相处，不急着得出结论。" in html
    # 拒绝把门槛变成任务条
    assert "还差" not in html.split('id="terrainEmpty"')[1][:600]
    # 页面加载即画出地平线，不等后端
    assert "renderTerrain({items:[],candidates:[],recent_changes:[]})" in html


def test_terrain_threshold_is_not_page_furniture():
    """§0.2 / §6.4：门槛只在展开卡的「为什么现在出现」里讲一次。"""
    html = APP_HTML.read_text(encoding="utf-8")
    # 常驻的「至少 3 个独立时刻 · 跨 7 天 · 2 段情境」必须已删除
    assert "至少 3 个独立时刻" not in html
    # 唯一的门槛叙述来自后端 why_now
    assert "item.why_now" in html


def test_terrain_card_expands_in_place_and_hides_controls():
    """§6：原位展开不弹层；校正露在外面，控制收进 ⋯，删除需二次确认。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "lane.insertAdjacentElement('afterend',card)" in html
    assert "tr-more-menu" in html
    assert "再点一次，永久删除" in html
    # 共同命名在「已确认但还没有自己的名字」时升为签名动作
    assert "confirmed&&!named" in html


def test_terrain_weather_slot_is_hidden_until_it_has_data():
    """§4：没有数据源时不给天气编一个词。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert 'id="trWeather" hidden' in html


def test_terrain_dots_are_positional_not_a_count():
    """§2.3：时刻点暴露的是「在这些时候发生过」，不是一个分数。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "item.evidence_at" in html
    assert "tr-dot" in html
    # 线索与最近变化的模板都不插入条数与门槛（§5.5 不展示内容数量）。
    # 先剥掉注释：块里有一条注释正是在说「不要列 N 个时刻」，它不是违规。
    block = html[html.index("const recentWrap=") : html.index("if(_trOpenClaimId)")]
    templates = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    for banned in ("evidence_count", "span_days", "context_count", "个时刻"):
        assert banned not in templates, banned


def test_terrain_api_exposes_evidence_times_and_why_now():
    """带上的点与卡上的解释都必须来自后端，不能由前端编。"""
    src = (PROJECT_ROOT / "habit_list_backend" / "app" / "api" / "v1" / "terrain.py").read_text(
        encoding="utf-8"
    )
    assert "evidence_at: list[str]" in src
    assert "why_now: str" in src
    assert "evidence_at=[_iso(moment) for moment in observed]" in src
    # why_now 只取形成层自己写下的理由，取不到就留空而不是造一句
    assert 'return ""' in src


# --- 共同命名：P0 签名动作（地形页视觉规范 §6.2）-------------------------------


def test_shared_naming_is_the_primary_action_and_never_a_browser_prompt():
    """签名动作不能长成系统弹框：那是浏览器的字体、按钮和语言，不是这个产品的。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "window.prompt" not in html
    assert "_trEnterNaming" in html and "tr-naming-input" in html
    # 已确认但还没有自己的名字时，共同命名占主按钮位
    assert 'if(confirmed&&!named)parts.push(\'<button type="button" class="tr-act primary" data-action="name">共同命名</button>\')' in html


def test_naming_form_opens_in_the_card_and_keeps_the_section_order():
    """§8.5 七项顺序不可改：命名表单开在校正区，不是插在表达和状态之间。"""
    html = APP_HTML.read_text(encoding="utf-8")
    assert "card.querySelector('.tr-acts').insertAdjacentElement('beforebegin',form)" in html
    # 命名进行中，页面上只剩这一个决定：校正行与 ⋯ 菜单都让位
    assert ".tr-card.naming .tr-acts,.tr-card.naming .tr-more-menu{display:none}" in html


def test_model_suggestions_are_seeds_not_one_tap_confirmations():
    """一按即定会把「共同命名」变成「替它按确认」：种子只填进输入框。"""
    html = APP_HTML.read_text(encoding="utf-8")
    block = html[html.index("function _trEnterNaming") : html.index("function _trOpenTerrain")]
    assert "input.value=seed" in block
    # 种子的点击回调里不允许直接提交
    seed_handler = block[block.index("button.addEventListener('click'") : block.index("const acts=")]
    assert "submit" not in seed_handler and "api(" not in seed_handler
    # 装不进输入框的说法不做种子，而不是被无声截断
    assert "seed.length<=input.maxLength" in block
    assert ".slice(0,input.maxLength)" not in block
