"""美学基线 §10 验收清单：token、时间、曲线、玻璃、颜色、字。

这一份守的是「同一套判断在每个组件上都成立」。审计 `app.html` 时的事实是：
token 都声明着，所以文件看起来是系统化的——但 24 个 token 引用次数为 0（其中两套
完整的玻璃系统），同时组件里手写了 22 种 backdrop-filter、30 个交互时长、36 种圆角。
于是 token 沦为装饰，真正生效的是 8900 行里散落的手写值。

所以这里检查的不是「有没有设计系统」，而是**有没有人遵守它**。
零引用的 token 不是资产，是误导：它让下一个人以为这里已经统一过了。
"""
from __future__ import annotations

import ast
import hashlib
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests import colour

APP_HTML = (Path(__file__).resolve().parents[2] / "app.html").read_text(encoding="utf-8")
CSS = re.search(r"<style>(.*?)</style>", APP_HTML, re.S).group(1)
ROOT = re.search(r":root\{(.*?)\n  \}", APP_HTML, re.S).group(1)

DECLARED = {m.group(1) for m in re.finditer(r"(--[\w-]+)\s*:", ROOT)}
# 一个 token 有两条被消费的通道：CSS 里的 var()，以及 JS 里的 getPropertyValue()。
# 只认前一条会把「只给画布用的 token」误判成零引用——那束光的色温梯度就是这样：
# canvas 的 fillStyle 不认 var()，所以它必须读一次再缓存，但它读的仍然是同一份声明。
REFERENCED = set(re.findall(r"var\((--[\w-]+)", APP_HTML)) | set(
    re.findall(r"(?:getPropertyValue|rgbToken)\(\s*['\"](--[\w-]+)", APP_HTML)
)
# 不带 fallback 的引用：这些必须在 :root 里有声明，否则那行声明什么都不做。
# 带 fallback 的可以是元素级局部变量（`--h` 就是逐根声波柱从 JS 写进去的），
# 它们不是设计 token，不该被要求出现在 :root。
REFERENCED_BARE = set(re.findall(r"var\((--[\w-]+)\s*\)", APP_HTML))

# CSS 注释里会出现被删掉的东西的名字——那是解释「为什么删掉它」的地方，
# 不能让它把检查绊倒。（声音基线那份测试就踩过一次同样的坑。）
CSS_NO_COMMENTS = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)

# 同理，JS 注释里也会引用被删掉的类名与色号。
SCRIPT = max(re.findall(r"<script>(.*?)</script>", APP_HTML, re.S), key=len)
SCRIPT_NO_COMMENTS = re.sub(r"(?s:/\*.*?\*/)|^\s*//.*", "", SCRIPT, flags=re.M)

# 标记本身也有注释（`<!-- ===== 备忘 ===== -->`），而删掉一个节点时解释性的注释常常
# 留在原地。所以「这个属性/类名还在不在」只能问这三份剥过注释的视图，不能问 APP_HTML。
MARKUP_NO_COMMENTS = re.sub(
    r"<!--.*?-->", "", re.sub(r"<(style|script)\b[^>]*>.*?</\1>", "", APP_HTML, flags=re.S), flags=re.S
)
LIVE_SOURCE = CSS_NO_COMMENTS + SCRIPT_NO_COMMENTS + MARKUP_NO_COMMENTS

# 「一个数长什么样」在这个文件里只许有这一份定义。CSS 与文档里同一个量有好几种
# 合法拼法（`.04` / `0.04` / `0.040`），而 `\d\.\d+` 这类尺子只认得其中一种：认不出
# 的那个声明是**悄悄**漏掉的，尺子仍然是绿的。这一份定义是给「要认出一个任意的数」
# 的地方拼用的片段，不编译——编译出来的正则是另一类账（§7.23）。
_NUM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"

# 只认写着小数点的数：光秃秃的整数在这个文件里常常是门牌号（`--o-1`、`200%`、`§7.15`），
# 规范化它们会把名字改坏。所以「`1` 写成 `1.0`」这一种拼法这道尺子看不见（归 #117）。
_QUANTITY = re.compile(r"(?<![\w.])[+-]?(?:\d+\.\d*|\.\d+)(?![\d.])")


def _by_quantity(text: str) -> str:
    """把一段文字里每一个数换成它唯一的规范拼法，比量而不比拼法。

    `.04` / `0.04` / `0.040` 是同一个量的三种合法拼法，而 `==` 和 `in` 只认其中一种。
    在**存在**型断言里认不出只是误报（大声地红）；在**不存在**型断言里认不出是
    **漏报**——那条断言保持绿，而它守的那件事已经不成立了（§7.15）。
    所以任何一条拿字面量去比一段文字的断言，两边都要先过这道规范化。
    """

    return _QUANTITY.sub(lambda m: repr(float(m.group(0))), text)


# 一个时间值，写成 `.25s` / `250ms` / `1.4s` 都要认出来。
# 曾经用 `(\d+(?:\.\d+)?)` 去匹配，`.25s` 被读成 25s，直方图整个是假的。
# 符号必须是时间值的一部分：`animation-delay` / `transition-delay` 允许负值
# （`-1.5s` 表示立即开始、跳过前 1.5 秒），把 `-1.5s` 读成 `1.5s` 是把一个数读成
# 另一个数（§7.15，与 `_NUM` 的 `[+-]?` 一致）。正号显式写（`+1.5s`）也合法。
# 数字部分由 `_NUM` 拼出来（v1.40 判据（二）：「一个数长什么样」只许有一份定义）——
# 顺带获得科学计数法（`1e-2s` 是合法 CSS 时间，`\d*\.?\d+` 读不到）。
TIME = re.compile(r"(?<![\w.])(" + _NUM + r")(ms|s)(?![\w])")

# 「认出一个东西」的尺子，每把只许有一份定义（§7.23：一个概念只有一把尺子）。
# 这六把都曾在好几个测试里抄过第二遍——抄第二遍迟早只改一份，而另一份还在静默地
# 读，两份抄本的行为一起变才算改对。定义成 compile 而不是字符串，因为收集器靠
# 「re.* 调用 + 字面量参数」数这把尺子（§7.15 的元守卫），compile 是它的唯一定义处。
# opacity 值域 [0,1]，`1e-2`（=0.01）是合法 CSS 数字、会真的渲染——它**不是**开关值，
# 读不到它是一条漏报（与负值的越界不同）。科学计数法的写法：`[0-9]*\.?[0-9]+` 加
# 可选的 `e±N`。不带符号：#115 判定负 opacity 是越界值（clamp 到 0），不认它。
_OPACITY_VALUE = re.compile(r"(?<![-\w])opacity\s*:\s*([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)")
_PLAIN_NUMBER = re.compile(r"[\d.]+")
_O_TOKEN_REF = re.compile(r"var\((--o-\d+)\)")
_DOC_BOLD_COUNT = re.compile(r"\*\*(\d+)\*\* 条")
_DOC_BOLD_NUM = re.compile(r"\*\*(\d+)\*\*")
_DOC_L_VALUE = re.compile(r"L=\*\*([\d.]+)\*\*")
EASE_KEYWORD = re.compile(
    r"cubic-bezier\([^)]*\)"
    r"|(?<![-a-z])(?:ease-in-out|ease-out|ease-in|ease|linear|step-\w+)(?![-a-z])"
)


def _comment_blanked_app() -> str:
    """把注释换成等长空白：偏移量不变，所以报出来的行号是真的行号。

    删掉一个东西时，解释它为什么被删的注释常常留在原地——那份注释里的三元组、
    类名、属性声明会让审计以为它还活着（本文件开头那条注释陷阱）。
    """
    src = APP_HTML
    for pat in (r"/\*.*?\*/", r"<!--.*?-->"):
        src = re.sub(pat, lambda m: re.sub(r"[^\n]", " ", m.group(0)), src, flags=re.S)
    return re.sub(r"^([ \t]*)//[^\n]*", lambda m: " " * len(m.group(0)), src, flags=re.M)


def _split_top_level(value: str) -> list[str]:
    """按顶层逗号切 transition 的多个段，括号里的逗号不算。"""

    out: list[str] = []
    depth = 0
    cur = ""
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    return out


def _transition_segments() -> list[str]:
    # 扫的是**注释抹白后**的全文：一条解释「那个 transition 为什么被删」的注释里
    # 照样写着 `transition:opacity`，而这条守卫会把它当成一条活着的声明去判时长。
    # （#32 删掉组标题那条减光时撞到：注释本身把守卫打红了。）
    segments = []
    for match in re.finditer(r"transition:\s*([^;}]*)", _comment_blanked_app()):
        for seg in _split_top_level(match.group(1)):
            seg = seg.strip()
            if seg and seg != "none":
                segments.append(seg)
    return segments


def _animation_lines() -> list[tuple[int, str]]:
    return [
        (i, line)
        for i, line in enumerate(_comment_blanked_app().splitlines(), 1)
        if re.search(r"animation:", line)
    ]


# --- #1：全量 token ---------------------------------------------------------


def test_no_token_is_declared_without_being_used():
    # 零引用的设计系统不是资产，是误导。这里曾经有 24 个，包括两套完整的玻璃系统。
    assert sorted(DECLARED - REFERENCED) == []


def test_no_token_is_used_without_being_declared():
    # `--text-main` 曾经连 fallback 都没写——那行 color 什么都没做。
    assert sorted(REFERENCED_BARE - DECLARED) == []


@pytest.mark.parametrize(
    "ghost",
    [
        # 两套零引用的玻璃系统
        "--glass-bg", "--glass-blur:", "--glass-shadow",
        "--glass-surface", "--glass-line-",
        # 五个零引用的兼容别名 + 两个几乎一样的文字色 + 一个没声明就被引用的
        "--light-warm", "--warm-glow", "--cold", "--memo-day", "--confide-warm",
        "--text-readable", "--text-main", "--bg-mid", "--glow-intensity",
        # 收敛掉的第五个光色
        "--light-glow", "--light-soft",
        # 第二条交互曲线：它服务的那个动作（石子从河底升起）已经不存在了
        "--ease-bounce:",
    ],
)
def test_dead_tokens_are_gone_for_good(ghost):
    assert ghost not in APP_HTML, ghost


def test_the_refusal_red_is_never_hand_written():
    # 这一条是被一次删除逼出来的：`--refuse` 唯一的引用在旧洞察卡片上，删掉旧河流之后
    # 它变成零引用——而产品里同时有 44 处手写的同一个红。token 在、没人用、手写值满地跑，
    # 正是 §0 那张审计表描述的病。
    assert ROOT.count("--refuse:#d57a7a") == 1
    assert APP_HTML.count("#d57a7a") == 1, "除了 :root 那一行，不该再有第二处写死"
    for ghost in ("rgba(213,122,122", "#e48e8e", "#b99090", "#c97b5f"):
        assert ghost not in APP_HTML, ghost


def test_the_recording_red_is_never_hand_written():
    # 「正在录音」是第三个功能色（§7.1）。它成立的前提是**录音是点一次开、再点一次停**：
    # 麦克风可以在没有任何手指碰着它的时候是活的，所以认错的代价是隐私，不是不好看。
    # 前提可证伪——改成按住不放，这个 token 就失去资格。
    assert ROOT.count("--recording:#dc5a5a") == 1
    assert APP_HTML.count("#dc5a5a") == 1, "除了 :root 那一行，不该再有第二处写死"
    for ghost in (
        "rgba(220,90,90",  # 共处麦克风钮原先手写的红
        "218,113,104", "#e8948a",  # 生活/线程胶囊钮那一套
        "#e8a0a0", "228,128,118", "238,148,138", "220,180,172",
    ):
        assert ghost not in APP_HTML, ghost


def test_the_recording_red_actually_wins_the_cascade_on_the_mic():
    # 浏览器里实测出来的缺陷，grep 看不出来：`#inputField:not(.expanded) .voice-ico-btn`
    # 里 `:not()` 的参数也算特异性，所以它是 (1,2,0)，和 `.voice-ico-btn.recording` 打平，
    # 而它写在后面——紧凑态（默认态）下录音红被整条盖掉，只剩 recPulse 的光晕透出来
    # （动画胜过普通声明），图标仍是奶白。「麦克风是活的」偏偏在最常见的那一态里看不见。
    # 修法是写成互斥，而不是靠源码顺序赢：顺序会被下一次搬动悄悄改掉，互斥不会。
    for selector in (
        "#inputField:not(.expanded) .voice-ico-btn:not(.recording){",
        "#inputField:not(.expanded) .voice-ico-btn:not(.recording) svg{",
    ):
        assert selector in CSS, selector
    assert "#inputField:not(.expanded) .voice-ico-btn{" not in CSS
    assert "#inputField:not(.expanded) .voice-ico-btn svg{" not in CSS


def test_recording_is_one_state_with_one_rhythm():
    # 原先两个胶囊钮各有一条循环（1.1s/5px 与 1.4s/6px），还各自手写了一个红。
    # 同一个状态不该有两种节奏。共处那颗圆钮走 recPulse（它缩放，因为整个身体就是
    # 可按的地方），并排的胶囊钮走 recRing（缩放会挤动旁边的东西）——同一个色，
    # 两种几何，不是两个决定。
    assert "pulse-red" not in APP_HTML
    assert "voicePulse" not in APP_HTML
    app = _by_quantity(APP_HTML)
    assert app.count(_by_quantity("animation:recRing 1.4s ease-in-out infinite")) == 2
    assert app.count(_by_quantity("animation:recPulse 1.4s ease-in-out infinite")) == 1


def test_the_degraded_notice_is_not_painted_as_an_error():
    # 声音基线 §3.2：降级说明要说的是「这不是你的错、你的话没有丢」。
    # 把它涂成红的，就变成了一条错误提示——而错误提示会让用户以为自己弄坏了什么。
    body = re.search(r"\.le-agent\.le-status\.failed \.le-agent-copy\{([^}]*)\}", CSS).group(1)
    assert "color:var(--text-dim)" in body, body
    assert "refuse" not in body and "recording" not in body, body


# --- #2：交互 transition ----------------------------------------------------


@pytest.mark.parametrize("segment", _transition_segments())
def test_every_transition_uses_the_duration_ladder(segment):
    assert not TIME.search(segment), f"手写时长：{segment}"
    assert re.search(r"var\(--t-(touch|move|morph|ambient)\)", segment), segment


def test_the_duration_ladder_is_closed():
    # 出现第四个交互时长即为缺陷。三档之间不插值：某个动作觉得 260 太快 460 太慢，
    # 说明它的语义没想清楚，不是需要 340ms。
    assert re.search(r"--t-touch:\s*120ms", ROOT)
    assert re.search(r"--t-move:\s*260ms", ROOT)
    assert re.search(r"--t-morph:\s*460ms", ROOT)
    # --t-ambient 不占交互的名额（§2.1），但它自己也只有一个值。
    assert re.search(r"--t-ambient:\s*1000ms", ROOT)
    assert sorted(t for t in DECLARED if t.startswith("--t-")) == [
        "--t-ambient", "--t-morph", "--t-move", "--t-touch",
    ]


# --- #3 / #4：曲线 ----------------------------------------------------------


def test_only_one_easing_curve_exists():
    # v1.0 有第二条 --ease-bounce，只服务「一个东西被从下面拿出来」（石子详情从河底升起）。
    # 那条旧河流删掉之后，这个语义边界在产品里不再存在，于是曲线也失去了资格（§4.1）。
    assert sorted(t for t in DECLARED if t.startswith("--ease-")) == ["--ease-silk"]


def _beziers(src: str = APP_HTML) -> list[tuple[float, ...]]:
    """全文每一条 cubic-bezier，读成它的四个量。

    一条曲线的身份是四个控制点，不是它被写成什么样子：`cubic-bezier(.34,1.56,.64,1)`
    与 `cubic-bezier(0.34,1.56,0.64,1.0)` 是同一条曲线。下面「回弹还是死的」那一条是
    **不存在**型断言，按字形比的话改一位写法就能悄悄绕过去。
    """

    return [
        tuple(float(n) for n in inside.split(","))
        for inside in re.findall(r"cubic-bezier\(([^)]*)\)", src)
    ]


def test_no_hand_written_bezier_survives_outside_the_one_declaration():
    # 只剩一条：token 自己的声明。曾经有 20 处 cubic-bezier(.2,.8,.2,1)，
    # 和 --ease-silk 肉眼无差。
    assert sorted(_beziers()) == [(0.22, 0.9, 0.3, 1.0)]


def test_every_transition_names_silk():
    for segment in _transition_segments():
        assert "var(--ease-silk)" in segment, segment


def test_the_bounce_curve_stays_dead_until_something_is_lifted_out():
    # 回弹是一次表演，而定线是「说完就停」。一个会回弹的按钮在替用户表达兴奋。
    # 它回来的条件写在 §4.1：必须能指名一个「把实体从下面托起」的一次性动作，
    # 而且必须是唯一一处。做不到，就不该有第二条曲线。
    assert (0.34, 1.56, 0.64, 1.0) not in _beziers()
    assert "var(--ease-bounce)" not in APP_HTML
    # 当年 tab 切换上的两处回弹：切页不是把东西拿出来。
    assert "transform 200ms var(--ease-bounce)" not in APP_HTML



def test_ease_keywords_appear_only_on_animations():
    # linear 与 ease-in-out 只允许在循环上：循环的接缝不能有顿点。
    # 交互曲线一律 silk。
    offenders = [
        (i, line.strip()[:90])
        for i, line in enumerate(APP_HTML.splitlines(), 1)
        if EASE_KEYWORD.search(line)
        and "animation" not in line
        and "linear-gradient" not in line
        and "--ease-" not in line
    ]
    assert offenders == []


def test_javascript_does_not_keep_its_own_copy_of_the_ladder():
    # 内联样式能写 var()，Web Animations API 不能——所以那条路必须把 token 读出来。
    # 在 JS 里另抄一份 260 和一条贝塞尔，等于给阶梯开了个后门：改 CSS 时这里不跟着变。
    script = max(re.findall(r"<script>(.*?)</script>", APP_HTML, re.S), key=len)
    assert "cubic-bezier" not in script
    assert "motionMs('--t-move')" in script or 'motionMs("--t-move")' in script


# --- #5 / #6：环境呼吸与实时指示 --------------------------------------------

# §3.1：一个循环要归到「实时指示」，必须能说出它对应的是哪个正在进行的动作，
# 以及那个动作什么时候结束。说不出来，它就是呼吸，就得 ≥ 3s。
# 录音是「再点一次结束」而不是按住不放——这三条早先写的是「松手即停」，是错的。
# 判据仍然成立（它确实随用户的动作结束而结束），但写错的理由会替下一个循环背书。
LIVE_INDICATORS = {
    "recPulse": "正在录音（共处的麦克风圆钮，缩放 + 红光），再点一次结束",
    "recRing": "正在录音（生活采集面板 / 线程弹层的胶囊钮，红环），再点一次结束",
    "mediaShimmer": "图片正在加载，加载完即停",
}
# `voiceWave` 曾在这张名单上，v1.9 随 §7.5 一起删了：进度点亮（`.lit`）说的是同一件事
# 而且说得更多（还说了播到哪），所以那条循环是「一个已经有承担者的状态的第二个名字」。
# 这张名单是反向守卫，删掉动画时它会先红——这次就是它先红的。


def test_time_reads_a_signed_duration():
    """TIME 把时间值连同符号一起读出来：`-1.5s` 是 `animation-delay` 的合法写法。

    符号不是拼法（`-1.5s` ≠ `1.5s`，是另一个量），所以 §7.15 的三种改写看不见它，
    必须由这一条自己钉住。正号显式写也合法。
    """

    assert TIME.findall("animation: breathe 5s ease -1.5s") == [("5", "s"), ("-1.5", "s")]
    assert TIME.findall("transition: all 1s ease-in-out -2s") == [("1", "s"), ("-2", "s")]
    assert TIME.findall("animation: x 0.5s linear +0.25s") == [("0.5", "s"), ("+0.25", "s")]
    # 数字部分由 `_NUM` 拼出来，所以科学计数法也认（`1e-2s` 是合法 CSS 时间值）。
    assert TIME.findall("animation: x 1e-2s linear") == [("1e-2", "s")]


def test_opacity_value_reads_scientific_notation():
    """`opacity:1e-2` 是合法 CSS 数字（=0.01，不是开关值），必须读得到。

    负值（越界，clamp 到 0）故意不认（v1.42），科学计数法（有效值）必须认——
    读不到它是一条漏报：守卫会说「这条规则没有静态 opacity」，而 0.01 在 [0,1]
    之间，不是开关。
    """

    assert _OPACITY_VALUE.search("opacity:1e-2").group(1) == "1e-2"
    assert _OPACITY_VALUE.search("opacity:.5").group(1) == ".5"
    assert _OPACITY_VALUE.search("opacity:0.35").group(1) == "0.35"
    assert _OPACITY_VALUE.search("opacity:-.1") is None


def test_no_unit_value_has_a_trailing_zero_after_its_integer_part():
    """`200.0ms` / `8.0px` / `200.0%` 是同一个量的另一种写法，而按字形比的断言认不出它。

    `_by_quantity` 只认带小数点的数（光秃秃的整数是门牌号——`--o-1`、`第 5 档`），
    所以「整数 + `.0` + 单位」落在它的盲区里：`200.0ms` 与 `200ms` 渲染同一个量，
    而按字形比的断言把它当另一个字。今天 `app.html` 里一处都没有——这一条守「别
    写出第一处」：堵入口比让每一条断言都宽容便宜（v1.42 归到 #117 的那笔）。

    只查 `app.html` 不查文档：文档里的数是抄本、按值比（拼法自由），§7.23 那句
    「`5.0px`=`5px`」本身就是教学例句——它不是要禁的形态，是在讲这种形态。
    """

    pat = re.compile(r"(?<![\w.])\d+\.0+(?:ms|s|px|%|deg|em|rem|vw|vh|fr)(?![\w.])")
    assert not pat.findall(APP_HTML), pat.findall(APP_HTML)


def _first_duration(line: str) -> float | None:
    """一条 animation 行里第一个时间值的毫秒数——CSS 简写语法里它就是 duration。

    `animation: name 5s ease -4s` 里的 `-4s` 是 delay（表示立即开始、跳过前 4 秒），
    不是周期。把 delay 当周期候选，`min(times)` 会在未来有人写 delay 时误报「太快」
    （v1.42 归到 #117 的那笔）。按顺序取第一个时间值：duration 在 delay 之前，而
    name 是标识符不是时间。
    """

    times = [float(a) * (1 if unit == "ms" else 1000) for a, unit in TIME.findall(line)]
    return times[0] if times else None


def test_ambient_loops_breathe_no_faster_than_three_seconds():
    too_fast = []
    for i, line in _animation_lines():
        if "infinite" not in line:
            continue
        name = re.search(r"animation:\s*([\w-]+)", line)
        duration = _first_duration(line)
        if duration is None or duration >= 3000:
            continue
        if name and name.group(1) in LIVE_INDICATORS:
            continue
        too_fast.append((i, line.strip()[:90]))
    # 低于 3s 会被读成「它在等我操作」，那是加载指示器的节奏，不是呼吸。
    assert too_fast == []
    # 判据的语义由构造样本钉住：第一个时间值是 duration，delay（正负都是）不参与。
    assert _first_duration("animation: breathe 5s ease -4s") == 5000
    assert _first_duration("animation: breathe 2.5s linear") == 2500
    assert _first_duration("animation: breathe 800ms ease 1.2s") == 800


def test_every_live_indicator_is_still_actually_used():
    # 豁免名单不许烂掉：一个不再存在的名字留在这里，就会替未来某个同名动画背书。
    for name in LIVE_INDICATORS:
        assert re.search(r"animation:\s*%s(?![\w-])" % re.escape(name), APP_HTML), name


def test_reduced_motion_stops_everything_and_lands_on_the_end_state():
    block = re.search(
        r"@media \(prefers-reduced-motion: reduce\)\{(.*?)\n  \}", APP_HTML, re.S
    ).group(1)
    # 曾经是按选择器一个个列的，于是共处页的呼吸、录音脉冲、加载微光全漏在外面,
    # 而且每加一个动画就会再漏一个。所以改成兜底。
    assert "*,*::before,*::after" in block
    assert "animation-iteration-count:1!important" in block
    # 关键：不能用 animation:none——那会让元素停在**起始**帧。
    # 天气消散那次缺陷就是这么来的：用户看到一个卡住的东西，520ms 后才被 JS 移除。
    assert _by_quantity("animation-duration:.001ms!important") in _by_quantity(block)
    assert "animation:none" not in re.sub(r"/\*.*?\*/", "", block, flags=re.S)


# --- #7：玻璃 --------------------------------------------------------------


def test_backdrop_filter_is_never_hand_written():
    values = {v.strip() for v in re.findall(r"backdrop-filter:\s*([^;}]*)", CSS)}
    # 曾经有 22 种手写值，同时那三个 blur token 零引用。
    assert values <= {"none", "var(--glass-blur-1)", "var(--glass-blur-2)", "var(--glass-blur-3)"}, values


def test_the_glass_system_is_one_system():
    assert sorted(t for t in DECLARED if t.startswith("--glass-")) == [
        "--glass-blur-1", "--glass-blur-2", "--glass-blur-3",
        "--glass-border",
    ]
    # `--glass-hi` 曾经在这张名单上。它的名字说「玻璃的顶部高光线」，可顶部高光是
    # §6.1 那 55 层厚度线，而它的值（白第 4 档 .30）在那 55 层里出现 0 次；它唯一
    # 那处引用画的是 `border`。玻璃只有一条边界线，不许有第二个名字（§6.1 判据八）。
    assert "--glass-hi" not in LIVE_SOURCE


# --- #8：圆角 --------------------------------------------------------------

# 三档之外还允许三个**形状**（§5）：圆、胶囊、方。形状回答的是「它是什么东西」，
# 档位回答的是「它有多圆」——只有后者会因为多一个值而让人猜。
# `"0 !important"` 曾经在这张名单上，为的是放过 `.tab{border-radius:0 !important}`。
# #28 把那条声明整个删了（裸 `<button>` 量出来 `border-radius:0` 本来就是它的值，
# 取消一个不存在的东西），于是这个白名单项成了死文档，一并拿掉——从此圆角上写
# `!important` 会被下面那条参数化的检查直接拦住，不需要再为它写一条专门的律。
RADIUS_SHAPES = {"50%", "999px", "0"}
# 抽屉从下面推上来，所以只有上面两角有弧度。这不是第四档，是同一档的一个方向。
RADIUS_DRAWER = "var(--r-sheet) var(--r-sheet) 0 0"
# 手写感元素允许不对称圆角，代价是必须在注释里说清它模拟什么（§5）。
# 加上桌面预览的手机外框——它是取景框，不是产品表面。
RADIUS_DOCUMENTED = {
    "4px 2px 12px 4px",   # 用户的便签：右下最圆因为 ::after 在那里画折角
    "0 0 10px 0",         #   同一张便签的折角投影
    "3px 16px 3px 18px",  # 它的气泡：左侧近乎方，它的话从左边那条金线里长出来
    "38px",               # .phone 预览外框
}


def _radius_declarations() -> list[tuple[int, str]]:
    """(行号, 值)。注释先剥掉但保留行数，否则注释里举的反例会把检查绊倒。"""

    css = re.sub(
        r"/\*.*?\*/", lambda m: re.sub(r"[^\n]", "", m.group(0)), CSS, flags=re.S
    )
    base = APP_HTML[: APP_HTML.index(CSS)].count("\n") + 1
    return [
        (base + css[: m.start()].count("\n"), m.group(1).strip())
        for m in re.finditer(r"border-radius:\s*([^;}]*)", css)
    ]


def test_radius_has_three_tiers_and_they_are_closed():
    assert re.search(r"--r-inline:\s*8px", ROOT)
    assert re.search(r"--r-card:\s*14px", ROOT)
    assert re.search(r"--r-sheet:\s*22px", ROOT)
    assert sorted(t for t in DECLARED if t.startswith("--r-")) == [
        "--r-card", "--r-inline", "--r-sheet",
    ]


@pytest.mark.parametrize("line,value", _radius_declarations())
def test_every_radius_is_a_tier_a_shape_or_a_documented_exception(line, value):
    # 曾经有 33 种写法，其中一大半是同一个语义各写了 12/13/14/15/16px——
    # 那不是三个决定，是三十次各自决定。
    allowed = (
        {"var(--r-inline)", "var(--r-card)", "var(--r-sheet)", RADIUS_DRAWER, "inherit"}
        | RADIUS_SHAPES
        | RADIUS_DOCUMENTED
    )
    assert value in allowed, f"app.html:{line} 手写圆角：{value}"


@pytest.mark.parametrize("value", sorted(RADIUS_DOCUMENTED))
def test_each_documented_radius_exception_says_what_it_simulates(value):
    # 「允许，但要写清理由」如果没人检查，一年后就只剩「允许」。
    lines = APP_HTML.splitlines()
    hits = [i for i, line in enumerate(lines) if f"border-radius:{value}" in line]
    assert len(hits) == 1, f"{value}: 期望恰好一处，实际 {len(hits)}"
    window = "\n".join(lines[max(0, hits[0] - 6) : hits[0]])
    assert "/*" in window, f"{value} 没有说明它模拟什么"


def _media_query_bodies(only_max_width: bool = True) -> list[tuple[str, str]]:
    """(@media 头, 去注释的块体)。默认只看 max-width——§5.3 与 §8.4 管的都是
    「屏幕变窄时」发生了什么。`min-width` 里那个 `.phone` 取景框（38px）是桌面预览的
    外框，不是产品表面，它在 §5 的白名单上。"""

    out = []
    for block in re.finditer(r"@media[^{]*\{", CSS):
        head = block.group(0).strip()
        depth, i = 1, block.end()
        while depth and i < len(CSS):
            depth += (CSS[i] == "{") - (CSS[i] == "}")
            i += 1
        if only_max_width and "max-width" not in head:
            continue
        body = re.sub(r"/\*.*?\*/", "", CSS[block.end() : i], flags=re.S)
        out.append((head, body))
    return out


def test_radius_never_changes_with_the_viewport():
    # 24px 在 421px 宽是对的、在 419px 宽变成 20px，说明它不是语义边界而是尺寸微调
    # （§1）。窄屏该调的是间距和字号，不是这个东西有多圆。曾有 7 处这样的缩水。
    offenders = []
    for _, body in _media_query_bodies():
        offenders += re.findall(r"border-radius:\s*([^;}]*)", body)
    assert offenders == []


def test_no_sentence_disappears_because_the_screen_got_narrower():
    # §8.4：`@media` 里不允许 `display:none`。一句话在 360px 消失、在 361px 出现，
    # 说明它的存在是一个尺寸决定；而这种消失总是发生在最挤的那块屏幕上——
    # 恰好是这句安慰最该在的地方。
    # 这一条是从「只留一样也可以」上换来的：它靠 margin-left:auto + text-align:right
    # + max-width:140px + ≤420px 降到 9px + ≤360px 整句消失，一共五条补丁，
    # 而实测它 361–430px 全程没被裁掉，是被旁边两个运行时会变长的按钮**挤**扁的。
    # 一句需要五条补丁才站得住的话，是这句话站错了地方。
    offenders = [
        (head, hit)
        for head, body in _media_query_bodies()
        for hit in re.findall(r"[^;{}]*\{[^{}]*display:\s*none[^{}]*\}", body)
    ]
    assert offenders == []
    # 那句话连同它的五条补丁一起走了；面板顶上的 `.moment-note` 才是承载这件事的地方。
    # 查渲染形态而不是裸字符串：上面那条 CSS 注释里正引用着这句话解释为什么删它。
    assert "lap-media-hint{" not in CSS
    assert ">只留一样也可以<" not in APP_HTML
    assert "先留下这一刻。其余的，晚点再说。" in APP_HTML


def test_no_spacing_token_moonlights_as_a_radius():
    # `.lap-card` 的抽屉角曾经写成 `var(--lf-voice) var(--lf-voice) 0 0`，
    # 理由是「去掉一个孤立的圆角常量」——但那把一个间距值派去当半径用。
    # 间距和圆角一起变，是因为它们碰巧相等，不是因为它们是同一个决定。
    for _, value in _radius_declarations():
        assert "--lf-" not in value, value


# --- 死规则与重名状态 ------------------------------------------------------

# 这些 class 早前就没人引用了，与 v1.2 删掉的那层无关。删每一个之前都先确认了
# 「取代它的东西是什么」，所以每一条删除都有理由，不是「没人用所以删」。
DEAD_CLASSES = {
    "comp-whisper": "共处的低语态从未接上，它的语义现在由 .msg-ai .bubble 的斜体承担",
    "dv-placeholder": "详情页占位从未渲染",
    "le-agent-meta": "生活流的落款：无边界布局定案是智能体不落款（生活页规范 §3.4）",
    "le-video-tile": "视频块改由 .gi-* 那套九宫格承担",
    "le-video-tile-label": "同上，改名为 .gi-video-label 跟着九宫格走",
    "life-capture-divider": "采集抽屉去掉了分隔线，改用间距分组（生活页规范 §2）",
    "memo-group": "备忘分组标题是 .memo-group-title 那一套，这个裸 class 没人写过",
    "memo-list": "列表容器实际是 #memoListWrap",
    "mi-img": "图片项直接是 <img>，没有额外一层",
    "we-row": "「我们」页的行改用 .we-ops-* 那套",
    "we-row-main": "同上",
    "we-row-sub": "同上",
}


@pytest.mark.parametrize("dead", sorted(DEAD_CLASSES))
def test_dead_classes_do_not_come_back(dead):
    # 必须按词边界匹配：`memo-group` 是死的，但 `memo-group-title`/`memo-group-count`
    # 是活的（JS 里拼出来）。裸 grep 会把 29 处活引用算成「它还在用」。
    hits = re.findall(r"(?<![\w-])%s(?![\w-])" % re.escape(dead), APP_HTML)
    assert hits == [], f"{dead}: {DEAD_CLASSES[dead]}"


def test_the_only_importance_class_is_the_one_css_reads():
    """反向守卫 + §7.3：`imp-red` 在 HTML 里一次都不出现，因为它是拼出来的。

    **静态审计看不见动态拼接的 class**——我自己就凭一次文本审计把这一族判成死的。
    这条测试的作用是让下一个人在删它之前先读到这行。
    同时它锁住 §7.3 的另一半：拼接点只允许写出 CSS 真的读的那一个名字。
    原先它无条件拼出 imp-red/imp-yellow/imp-green，而后两个没有任何样式读——
    一个运行时才出现的零引用 class，和零引用 token 是同一种缺陷。
    """
    assert "(m.importance==='red'?' imp-red':'')" in APP_HTML
    assert ".memo-item.imp-red::before" in CSS
    for ghost in ("imp-yellow", "imp-green"):
        assert f".{ghost}" not in CSS, ghost
        assert f"' {ghost}'" not in APP_HTML, ghost


# --- §7.3：重要度是一个词，不是一个色 -----------------------------------------

RETIRED_MEMO_COLOURS = {
    "#e9b96b": "重要度黄。它没能过 §7.1 那道门：把「重要」看成「轻松」的代价是不方便，"
               "不是另一个种类的代价。而每一行的 memo-imp-tag 已经把档位写成汉字了",
    "233,185,107": "同一个黄的 rgba() 形态",
    "#8aa68a": "重要度绿。它在这一页同时表示「轻松」和「已完成」——一个色说两件事",
    "138,166,138": "同一个绿的 rgba() 形态",
    "#d7e6d7": "完成勾的浅绿，跟着上面那个绿一起退役",
}


@pytest.mark.parametrize("colour", sorted(RETIRED_MEMO_COLOURS))
def test_the_retired_memo_colours_do_not_come_back(colour):
    # 查 CSS 声明而不是裸字符串：那几条解释「为什么删」的注释里正引用着这些色号，
    # 和 §8.4 里那句「只留一样也可以」是同一个陷阱。
    assert colour not in CSS_NO_COMMENTS, f"{colour}: {RETIRED_MEMO_COLOURS[colour]}"
    assert colour not in SCRIPT_NO_COMMENTS, colour


def test_the_memo_page_adds_no_fourth_functional_colour():
    """备忘页只剩两个色：页面身份色 --memo-blue，以及唯一的例外 --refuse。

    这一页原先是全app 手写色最密的地方——重要度三档各一个色、到期两档各一个色、
    完成态一个绿。全部走 token 之后，`.memo-*` / `.imp-dot` 规则里不该再出现任何色号。
    """
    rules = re.findall(r"(?m)^\s*\.(?:memo|imp-dot)[^{}]*\{[^{}]*\}", CSS_NO_COMMENTS)
    assert len(rules) > 20, len(rules)
    for rule in rules:
        hexes = re.findall(r"#[0-9a-fA-F]{3,8}", rule)
        assert hexes == [], f"手写色 {hexes}: {rule.strip()}"


def test_importance_tiers_are_carried_by_words_not_hues():
    """§7.3：三档写成三个词，选中态才用色。颜色分类需要图例，图例是说明书。"""
    # 选择器上三个胶囊都写着字
    for word in ("轻 松", "重 要", "紧 急"):
        assert f'tabindex="0">{word}</div>' in APP_HTML, word
    # 行内标签用同一套词（原先筛选 chip 写「放 松」、行内写「轻 松」，同一档两个名字）
    assert "{red:'紧 急',yellow:'重 要',green:'轻 松'}" in APP_HTML
    assert 'data-f="green">轻 松<' in APP_HTML
    assert "放 松" not in APP_HTML
    # 只有「紧急」有自己的色，其余走页面身份色
    assert ".memo-imp-tag.red{" in CSS
    assert ".imp-dot[data-p=\"red\"].on{" in CSS
    for ghost in (".memo-imp-tag.yellow", ".memo-imp-tag.green", ".imp-dot.red", ".imp-dot.green", ".imp-dot.yellow"):
        assert ghost not in CSS, ghost
    # 「今天到期」不再变色：组标题已经说了「今天」
    assert ".memo-due.today{" not in CSS
    assert ".memo-due.overdue{" in CSS


def test_a_word_that_carries_a_classification_may_not_be_broken():
    """§7.3（三）：把分类交给词，然后把词折断，等于两头都没做到。

    浏览器实测：五个筛选 chip 的自然宽度合计 352px，可用宽度 342px，于是它们各自
    `flex-shrink` 到 56px，并在标签中间那个全角空格上折成两行（高 48px 而非 32px）。
    这一排不缩、不折、横向滚动——滚动不是消失，§8.4 管的是后者。
    """
    chip = _rule_body(".memo-filter .chip")
    assert "flex:0 0 auto" in chip
    assert "white-space:nowrap" in chip
    rail = _rule_body(".memo-filter")
    assert "overflow-x:auto" in rail


def test_the_importance_picker_does_not_lie_to_a_screen_reader():
    """role=radio 是一句承诺：aria-checked 必须跟着选中态变，键盘必须能选。"""
    assert 'id="memoAddImp" role="radiogroup"' in APP_HTML
    picker = APP_HTML[APP_HTML.index('id="memoAddImp"') :]
    picker = picker[: picker.index("</div>\n      </div>")]
    assert picker.count('role="radio" aria-checked=') == 3
    assert picker.count('aria-checked="true"') == 1
    src = SCRIPT_NO_COMMENTS
    assert "function memoSetPrio(p){" in src
    assert "x.setAttribute('aria-checked',on?'true':'false')" in src
    # 只有这一个入口改选中态：点击、键盘、打开面板三处都走它
    assert src.count("memoSetPrio(") == 4
    assert "if(e.key==='Enter'||e.key===' ')" in src


def test_one_state_does_not_have_two_names():
    # `body.companion-focused` 有 3 条规则，而 JS 从来不加这个 class（浏览器里实测
    # `document.body.className === ""`）——它是 `companion-expanded` 的第二个名字。
    # §2.1 原先拿三样当环境过渡的示例（背景压深、暗纱下压、光球跟着聚焦变）。
    # 后来量出来只有两样在动：「背景压深」写的是 #121730 → #161a34，ΔE2000 只有 1.09
    # 且方向是变亮不是变深，所以那一条已经删掉（§7.9）。剩下的两样必须都在。
    assert "companion-focused #" not in CSS
    assert "companion-focused{" not in CSS
    for selector in (
        "body.companion-expanded #screen-companion::before{",
        "body.companion-expanded #screen-companion::after{",
    ):
        assert selector in CSS, selector
    # 删掉的那一条不许回来：底色不参与状态，它是房间，房间不会因为用户开始打字而换。
    assert "body.companion-expanded #screen-companion{" not in CSS


def test_the_veil_is_declared_once():
    # `#screen-companion::after` 原先有两份完整定义，相隔 580 行，写的是同一个伪元素。
    # 后一份靠源码顺序赢，所以前一份那套渐变一次都没渲染过——声明在、看着像在生效、
    # 其实是死的。这正是 §7.2 说的：文本审计证明「有人写了」，不证明「用户看得见」。
    assert CSS.count("\n  #screen-companion::after{") == 1


# --- §7.5：波形是进度条，不是幅值；而它只允许有一份形状 ----------------------

VOICE_WAVES = (".lap-voice-wave", ".le-voice-wave", ".dv-voice-wave")


def test_the_waveform_has_exactly_one_shape_definition():
    """同一段录音三个视图，形状只能有一份。

    收敛前是三份：这里 20 个值写在 CSS 的 nth-child 上，列表那份 20 个值写在 JS 的
    `LE_WAVE_HEIGHTS` 里，详情页那份是每次渲染 `Math.random()` 现编 28 根。前两份是
    抄的关系，而且**已经抄歪了 8 个值**——这正是 §7.4「读，不是抄」的又一次现形，
    只是这次抄的是形状而不是颜色。第三份最糟：它连「一份」都不是，同一段录音关掉
    再打开形状就变了，用户自己就能抓到（对照声音基线 §3.1「他会看见同一句诗出现第二次」）。
    """
    rules = re.findall(r"([^{}\n]*nth-child\(\d+\)[^{}]*)\{([^{}]*)\}", CSS_NO_COMMENTS)
    shape_rules = [(sel, body) for sel, body in rules if "--h:" in body]
    assert len(shape_rules) == 20, [sel for sel, _ in shape_rules]
    for index, (sel, body) in enumerate(shape_rules, start=1):
        for wave in VOICE_WAVES:
            assert f"{wave} span:nth-child({index})" in sel, (index, wave, sel)
        # 形状之外不许再夹带别的东西（原先每条还带一个 animation-delay）
        assert re.fullmatch(r"--h:\d+px", body.strip()), (index, body)
    # JS 一根柱子的高度都不写
    assert "LE_WAVE_HEIGHTS" not in SCRIPT_NO_COMMENTS
    assert "--h" not in SCRIPT_NO_COMMENTS
    # `Math.random` 在别处是正当的（粒子、ID），这里只管三条语音条附近
    voice_js = SCRIPT_NO_COMMENTS[
        SCRIPT_NO_COMMENTS.index("dvVoiceWave.innerHTML=''") : SCRIPT_NO_COMMENTS.index(
            "dvVoiceWave.innerHTML=''"
        )
        + 400
    ]
    assert "Math.random" not in voice_js


def test_all_three_views_of_one_recording_look_the_same():
    """柱子数不一样，本身就是「这里有第二份定义」的痕迹（原先 28 根 vs 20 根）。"""
    static_bars = re.search(
        r'<div class="lap-voice-wave">((?:<span></span>)+)</div>', APP_HTML
    )
    assert static_bars and static_bars.group(1).count("<span></span>") == 20
    assert "for(let i=0;i<20;i++) html+=`<span></span>`" in APP_HTML  # 列表内联
    assert "for(let i=0;i<20;i++)dvVoiceWave.appendChild" in APP_HTML
    for wave in VOICE_WAVES:
        assert f"{wave} span.lit{{" in CSS, wave


def test_the_bars_carry_progress_instead_of_pretending_to_read_the_audio():
    """柱子唯一的工作是进度。跳动的那条循环删了，理由不是省事。

    幅值回答的问题是「你当时有多大声」——没人对自己的日记问这个。用户对一条语音只问
    三件事：是语音吗（图标）、多长（旁边印着秒数）、播到哪了。前两件早有承担者，
    第三件是这些柱子唯一该做的事，而收敛前有两个视图根本没在做。
    """
    assert "@keyframes voiceWave" not in CSS
    # 三个视图的 span 规则里一条 animation 都没有：播放指示是一个 class，
    # 所以 `prefers-reduced-motion` 的兜底压不掉它——原先压得掉，于是关了动画的用户
    # 在列表里得不到任何「正在播」的提示（那条 `.playing` 规则是空转的）。
    for wave in VOICE_WAVES:
        assert "animation" not in _rule_body(f"{wave} span"), wave
    # 进度只有一个定义，三处调用
    assert SCRIPT_NO_COMMENTS.count("function paintVoiceBars(") == 1
    assert len(re.findall(r"paintVoiceBars\(", SCRIPT_NO_COMMENTS)) >= 7
    assert SCRIPT_NO_COMMENTS.count("classList.toggle('lit'") == 1
    # 播完、被别处暂停、加载失败都要把点亮收回，不能留一排亮着的柱子说它还在播
    for event in ("timeupdate", "pause", "ended", "error"):
        assert f"dvVoiceAudio.addEventListener('{event}'" in APP_HTML, event


def test_the_playing_hook_is_gone_from_all_three_voice_views():
    """`.playing` 在这三处退役：进度点亮已经在说「正在播」。

    退役是它自己招的——列表那条 `.le-voice.playing .le-voice-wave span` 写的值和基础
    规则一字不差，**一条什么都不改的规则**，等于 `.playing` 在这里从来没有过工作。
    （`.pv-slide.playing` 不在此列：它真的在做事，播视频时把 LIVE 角标藏起来。）
    """
    for ghost in (
        ".le-voice.playing",
        ".lap-voice-area.playing",
        ".dv-voice.playing",
        "lapVoiceArea.classList.add('playing')",
    ):
        assert ghost not in CSS_NO_COMMENTS and ghost not in SCRIPT_NO_COMMENTS, ghost
    assert ".pv-slide.playing" in CSS


def test_voice_in_gets_voice_out_and_silence_when_it_cannot_speak():
    """语音进、语音出（§3.2 的推论）：这轮带 audio_asset_id 的回复落定后自动朗读。

    两句话钉死在收尾处：一是 `audioAssetId && aiText` 才开口——文字进来的轮次
    不出声，朗读仍由手边的键决定；二是 `!degradedNotice`——接不上的那轮
    不假装在说话。转写回填的提示也要把预期说出口（「它会用语音回你」），
    不让这个默认行为成为用户猜不到的惊喜。
    """
    assert "if(audioAssetId && aiText && !degradedNotice){" in SCRIPT_NO_COMMENTS
    assert "speakAssistantText(aiText);" in SCRIPT_NO_COMMENTS
    assert "它会用语音回你" in SCRIPT_NO_COMMENTS


# --- §7.4：一束光允许有色温，一个状态不允许有色相 ----------------------------

# 共处页原先手写了 38 个暖色、90 处。收敛之后，这些值一个都不该回来。
# 键是 rgb 三元组的文本形态（`rgba(231,196,154,` 里的那一段）；值是它为什么退役。
# 有两个值不在这张表里，因为它们是被提拔而不是被退役的：244,211,94 在状态胶囊上
# 是假的（胶囊不是光里的一个位置），在画布上是真的，于是它成了 --glow-flame；
# 255,243,218 原是便签纸渐变的第一个停止点，于是它成了 --paper。
# 「这个值不该出现在这里」和「这个值不该存在」是两件事，混起来会逼出一次错误的删除。
RETIRED_WARM = {
    "231,196,154": "回声与在回态的「更亮的琥珀」。它不是另一个色——原来那行 "
                   "`background:var(--comp-warm,#e7c49a)` 自己承认了：只有把两个值当成"
                   "可以互相顶替，才会这么写 fallback",
    "229,152,102": "回声渐变的第二个停止点，与上面那个只差 3° 色相、alpha 0.10，看不出来",
    "201,123,95": "陶土。v1.2 说「产品从来没用过」，其实它在发送按钮上手写了 9 次——"
                  "但全在 alpha ≤ 0.15 的投影里，所以「一抹暖橙」从来没抹上去过。"
                  "它真正在做事的地方是光晕外缘，那一档现在叫 --glow-far",
    "198,174,130": "首次进入弹层的卡其。它出现在任何页面之前，但说话的是陪伴者",
    "217,196,160": "同一套卡其的按钮文字色",
    "236,217,181": "同一套卡其的 hover 文字色",
    "213,168,122": "我们页运维灯的琥珀警示色。「有东西不对」已经有色了，是 --refuse",
    "232,168,124": "生活详情无图卡上那一个琥珀停止点，夹在两个 --life-green 之间",
    "222,216,202": "顶栏英文小标题。压在近黑底上 alpha .58 渲染出来约等于 --text-faint，"
                   "用一个新色写出一个已经存在的档位",
    "245,234,215": "AI 气泡的正文色。与 --text(242,237,229) 差 (3,-3,-14)，"
                   "15px / weight 300 上没有人看得出来",
    "255,245,225": "暖白高光的九个写法之一",
    "255,242,220": "暖白高光的九个写法之一",
    "255,240,220": "同上",
    "255,240,215": "同上",
    "255,240,210": "同上",
    "255,235,200": "同上",
    "255,230,200": "同上",
    "255,230,190": "同上",
    "255,220,150": "在回态那一下闪光，也在这一档里",
    "250,230,190": "便签纸渐变的中段：一张纸的明暗该由透明度走完，不是换三个色相",
    "242,218,172": "便签纸渐变的暗端，同上",
    "140,105,60": "便签纸下缘的内阴影，属于纸的暗端，收进 --ink",
    "245,220,185": "AI 气泡玻璃渐变的中段：alpha 0.035 上的色相差不可见",
    "224,190,150": "同上，暗端",
    "246,216,175": "AI 气泡的描边，与金线 --gold-line(246,210,168) 差 6，是它被抄错的一次",
    "232,180,140": "CSS 光球核心。现在走梯度的 --glow-body",
    "190,140,110": "CSS 光球外缘。现在走 --glow-far",
    "240,190,148": "展开态光球核心。展开只是被拨亮，不是变成另一种光——那一档只抬透明度",
    "200,150,115": "展开态光球外缘，同上",
}


@pytest.mark.parametrize("triple", sorted(RETIRED_WARM))
def test_a_retired_warm_value_does_not_come_back(triple):
    for blob, where in ((CSS_NO_COMMENTS, "CSS"), (SCRIPT_NO_COMMENTS, "JS")):
        assert triple not in blob, f"{where} 里又出现了 {triple}：{RETIRED_WARM[triple]}"


_COLOUR_RE = re.compile(
    r"#([0-9a-fA-F]{6})(?![0-9a-fA-F])"
    r"|#([0-9a-fA-F]{3})(?![0-9a-fA-F])"
    r"|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)"
    # 裸数组也算。画布抄一份颜色时写的就是 `[168,156,192]`——只认 rgb()/#hex 的审计
    # 恰好看不见抄袭最常用的那个写法（变异测试里唯一漏掉的一条就是它）。
    r"|\[\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\]"
)


def _hand_written_colours() -> list[tuple[int, str, tuple[int, int, int]]]:
    """(行号, 原文, rgb)——:root 之外每一个手写死的颜色，CSS / JS / 标记全算。

    只扫 CSS 是不够的：这一轮里三块画布抄下来的六个三元组全在 `<script>` 里，
    还有一个 rgba 写在行内 style 上。
    """
    src = _comment_blanked_app()
    lo = src.index(":root{")
    hi = src.index("\n  }", lo)
    out = []
    for m in _COLOUR_RE.finditer(src):
        if lo <= m.start() <= hi:
            continue
        if m.group(1):
            rgb = tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
        elif m.group(2):
            rgb = tuple(int(c * 2, 16) for c in m.group(2))
        elif m.group(3):
            rgb = tuple(int(m.group(i)) for i in (3, 4, 5))
        else:
            rgb = tuple(int(m.group(i)) for i in (6, 7, 8))
        out.append((src[: m.start()].count("\n") + 1, m.group(0), rgb))
    return out


def test_no_hand_written_warm_colour_survives_outside_root():
    """暖色只允许在 :root 里出现一次；组件里一律走 token。

    这不是洁癖。收敛之前这一簇有 38 个不同的值散在 90 处，其中 10 个是彼此差
    ≤ 15 的暖白、4 个是同一束光的核心与外缘各写两遍。谁也不可能在读到第 7 个
    `rgba(255,240,2xx,0.0x)` 的时候还记得前 6 个长什么样，于是第 39 个必然出现。
    """
    strays = [(line, tok) for line, tok, (r, g, b) in _hand_written_colours() if r > b and r - b >= 20]
    assert strays == [], strays


# ===== 全色相覆盖：:root 之外不许有任何一个手写的彩色值 =====
# 上面那条只筛 `r - b >= 20`，也就是只看暖色。代价是整簇生活绿（31 处）、地形玫瑰、
# 烟霭紫、以及三块画布里手抄的三元组，一次都没被这个文件看见——
# **一个只覆盖一个色相的审计，会给其余色相发通行证。**
#
# 这里曾经挂着两张明账：APPENDIX_TEXT_OWED（26 值 / 26 处的冷灰文字色）与
# GROUND_OWED（36 值 / 66 处的近黑地色）。两张都已经收完，所以两张都删掉——
# 留一张空名单等于留一句「这里本来就该有例外」。
#
# 近黑那一簇的判据见美学基线 §7.9：一个房间、一种材料、三个位置
# （纱 ×0.5 / 地板 ×1.0 / 抬面 ×1.4，色度锁在 10:14:24 那条线上）。色相不在这三档里，
# 色相是每页自己那盏灯——它已经在每页的 radial 层和每张面板的 border-top 上说过了。


def test_no_hand_written_colour_survives_outside_root():
    """:root 之外只允许中性值（描边、高光、阴影），任何带色相的都必须走 token。

    `max - min > 6` 这个阈值把纯黑、白、各档中性灰放过去：它们是结构，不是身份。
    """
    seen = {
        f"{r},{g},{b}"
        for _, _, (r, g, b) in _hand_written_colours()
        if max(r, g, b) - min(r, g, b) > 6
    }
    assert seen == set(), sorted(seen)


# --- §7.9：一个房间，一种材料，三个位置 --------------------------------------


def _tier(name: str) -> tuple[int, int, int]:
    m = re.search(rf"{name}\s*:\s*#([0-9a-fA-F]{{6}})\s*;", ROOT)
    if m:
        return tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
    m = re.search(rf"{name}-rgb\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*;", ROOT)
    assert m, name
    return tuple(int(m.group(i)) for i in (1, 2, 3))


def test_the_three_ground_tiers_are_one_material():
    """三档必须锁在同一条色度线上——不同亮度是位置，不同色相是另一种材料。

    原先这一簇是 36 个手写近黑值散在 66 处，色相从 r-b=-4（偏紫）一路到 -30（偏蓝），
    而每一页的身份色**已经**由上面那层 radial 和每张面板的 border-top 说过一遍了。
    两个答案还互相矛盾：生活页上面那层是绿的，下面那层是蓝的。所以底色不许带色相。
    """
    veil, deep, lift = _tier("--bg-veil"), _tier("--bg-deep"), _tier("--bg-lift")
    # 三档各自的 hex 与 -rgb 孪生必须一致（写两遍就会有一遍先漂走）
    assert _tier("--bg-deep") == tuple(
        int(x) for x in re.search(r"--bg-deep-rgb\s*:\s*([\d,]+);", ROOT).group(1).split(",")
    )
    assert _tier("--bg-lift") == tuple(
        int(x) for x in re.search(r"--bg-lift-rgb\s*:\s*([\d,]+);", ROOT).group(1).split(",")
    )
    # 同一条色度线：每一档按各自的绿通道归一化后，r 与 b 的比值必须一致（容差 ±0.04
    # 是 8bit 取整的空间，不是自由度）。
    ratios = [(c[0] / c[1], c[2] / c[1]) for c in (veil, deep, lift)]
    for r, b in ratios[1:]:
        assert abs(r - ratios[0][0]) < 0.04, ratios
        assert abs(b - ratios[0][1]) < 0.04, ratios


def test_the_three_ground_tiers_are_far_enough_apart_to_be_tiers():
    """相邻两档的亮度比不得小于 1.35：低于这个比例，两档在近黑处根本分不出。

    这是量出来的，不是估的。收敛时先用「到 12 个成员的 ΔE2000 最大值最小」解出
    ×1.26，结果那个解让地板↔抬面只差 ΔE 1.99——低于 JND(2.3)，于是阶梯不成阶梯。
    ΔE 最小化找的是妥协点，不是档位。改成按阶距一致取值（×0.5 / ×1.0 / ×1.4），
    两级台阶分别是 ΔE 3.88 与 3.96。
    """
    tiers = sorted((sum(_tier(n)) for n in ("--bg-veil", "--bg-deep", "--bg-lift")))
    for lo, hi in zip(tiers, tiers[1:]):
        assert hi / lo >= 1.35, tiers


def test_no_mask_reaches_for_pure_black():
    """遮罩不许用 `rgba(0,0,0,α)`：纯黑是另一种材料，而房间只有一种。

    `.lap-mask` 与 `.map-mask` 原先就是这么绕过前一轮审计的——它们干的事和其余
    五个遮罩一字不差，只是拼法落在近黑色相的扫描范围之外。
    """
    for sel_text, body in _top_level_rules():
        if "mask" not in sel_text:
            continue
        assert "rgba(0,0,0" not in re.sub(r"\s+", "", body), sel_text.strip()


def test_every_page_ground_names_both_tiers_and_nothing_else():
    """每一页的底都是同一条斜坡：抬面在上，地板在下。变的只有那盏灯。

    四入口 + 备忘子页共五页。原先五页各有一套手写近黑，其中生活/备忘两页的顶端
    差 ΔE 1.46、地形/我们两页差 ΔE 1.64——都低于可辨阈，也就是同一档写了两遍。
    """
    pages = {}
    for sel_text, body in _top_level_rules():
        s = sel_text.strip()
        if re.fullmatch(r"#screen-(companion|life|memo|river|me)", s):
            pages[s] = body
    assert set(pages) == {
        "#screen-companion", "#screen-life", "#screen-memo", "#screen-river", "#screen-me"
    }, sorted(pages)
    for s, body in pages.items():
        assert "var(--bg-lift)" in body, s
        assert "var(--bg-deep)" in body, s
        # 「nothing else」得真的管住。变异测试里给 `#screen-memo` 塞一个
        # `background-color:#0c1120` 时这条没响，只有色相那条响了——也就是说
        # 塞一个中性的第三档近黑本来是塞得进去的。底是 token、灯是
        # `rgba(var(--X-rgb),α)`，两样都不需要手写三元组，所以字面颜色一个都不许有。
        assert not _COLOUR_RE.findall(body), (s, _COLOUR_RE.findall(body))


def test_the_companion_canvas_draws_light_not_ground():
    """画布不许铺底。一页的底只能被回答一次。

    原先共处页的底被画了两遍：CSS 一份 radial（椭圆在 50% 38%），画布另一份
    不透明 radial（圆在 50% 50%），而画布本身 opacity:0.65——于是 CSS 那份只渲染出
    35%，最终画面是 0.65 这个数字混出来的，没有人选过它。这是 §7.8 那条缺陷跨到了
    画布上：一份追加的覆盖，把原处变成了死文档。
    """
    assert "fillRect(0,0," not in SCRIPT_NO_COMMENTS.replace(" ", "")
    assert SCRIPT_NO_COMMENTS.count("clearRect(0,0,w,h)") == 3, "三块画布都只清、不铺"


def test_no_identity_colour_is_hand_written_outside_root():
    """任何一个已声明色 token 的自有值，在 :root 之外都不许再出现。

    这一条和上面那条的分工：上面管「有没有人另写了一个色」，这一条管「已经有名字的
    色不许再被抄一份」。canvas 是最容易破它的地方——`fillStyle` 不认 `var()`，
    所以抄一份三元组看着像唯一的出路。出路是 `rgbToken()`：读，不是抄。
    """
    declared: dict[str, str] = {}
    for m in re.finditer(r"(--[\w-]+)\s*:\s*#([0-9a-fA-F]{6})\b", ROOT):
        h = m.group(2)
        declared[",".join(str(int(h[i : i + 2], 16)) for i in (0, 2, 4))] = m.group(1)
    for m in re.finditer(r"(--[\w-]+)\s*:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*;", ROOT):
        declared[",".join(m.group(i) for i in (2, 3, 4))] = m.group(1)
    # 纯灰与白不是身份色，它们是结构（描边、高光、遮罩），到处出现是对的。
    declared = {
        v: name for v, name in declared.items()
        if max(int(x) for x in v.split(",")) - min(int(x) for x in v.split(",")) > 6
    }
    strays = [
        (line, tok, declared[f"{r},{g},{b}"])
        for line, tok, (r, g, b) in _hand_written_colours()
        if f"{r},{g},{b}" in declared
    ]
    assert strays == [], strays


def test_the_canvases_read_their_colours_instead_of_copying_them():
    """三块画布共用一个出口，token 名一律写字面量，而且不留 fallback。

    写成变量的 token 名，静态审计看不见——它会和零引用 token 一样被下一个人当成没人用
    而删掉（§7.2 的反面）。次数也得钉住：`--me-violet-rgb` 是两块画布各读一次
    （地形的光尘 + 「我们」的紫雾），只断言「有人读了」的话，其中一块偷偷抄回一份
    仍然能过（变异测试里就是这么漏掉的）。
    """
    assert "function rgbToken(name)" in SCRIPT_NO_COMMENTS, "带 fallback 就等于允许抄一份"
    assert SCRIPT_NO_COMMENTS.count("getPropertyValue") == 2, "只该有 motionToken 和 rgbToken 两个出口"
    reads = {
        "--warm-hi": 1, "--glow-flame": 1, "--glow-body": 1, "--glow-far": 1,
        "--river-rose-rgb": 1, "--me-violet-rgb": 2, "--me-star-rgb": 1,
    }
    for token, times in reads.items():
        assert SCRIPT_NO_COMMENTS.count(f"rgbToken('{token}')") == times, token
    assert SCRIPT_NO_COMMENTS.count("rgbToken(") == sum(reads.values()) + 1  # +1 是定义本身


def test_the_we_page_stars_are_a_second_rung_of_one_light_not_a_fifth_identity():
    """星点比紫雾亮，且是用 lighter 叠加画的：它是光源，雾是介质。

    交换两者，就得到一团比自己的光源更亮的雾——物理上不成立，所以这两个值是
    一个决定（「这一页的光」）内部的两档，不是第五个页面身份色（§7.4）。
    """
    star = [int(x) for x in re.search(r"--me-star-rgb:\s*([\d,]+);", ROOT).group(1).split(",")]
    haze = [int(x) for x in re.search(r"--me-violet-rgb:\s*([\d,]+);", ROOT).group(1).split(",")]
    assert all(s > h for s, h in zip(star, haze)), (star, haze)
    src = SCRIPT_NO_COMMENTS[SCRIPT_NO_COMMENTS.index("--me-star-rgb") :]
    src = src[: src.index("function drawMe") + 4000]
    assert "globalCompositeOperation='lighter'" in src
    # 只声明 rgb 三元组：CSS 里没有一处用得上 hex 孪生，零引用 token 是误导（§6）。
    assert "--me-star:" not in CSS


def test_thinking_dims_the_core_without_borrowing_a_hue():
    """节奏分状态，颜色不分状态（§7.4 推论）。

    thinking 原先在光核正中压一抹深紫 rgba(80,60,90)。在 0.09–0.15 的透明度下，
    它和同亮度的中性灰渲染出的结果差 2/1/3——那个色相看不见，它只是在做「暗一点」。
    """
    assert "80,60,90" not in SCRIPT_NO_COMMENTS and "80, 60, 90" not in SCRIPT_NO_COMMENTS
    # 前面还有一处 `if(state==='thinking')` 只调光强，画粒子的是最后那一处。
    src = SCRIPT_NO_COMMENTS[SCRIPT_NO_COMMENTS.rindex("if(state==='thinking')") :]
    src = src[: src.index("if(state==='responding')")]
    assert "rgba(0,0,0," in src, src
    assert "for(let i=0;i<7;i++)" in src, "thinking 靠 7 颗环行粒子被认出来，不靠色相"


def test_the_light_is_one_ramp_not_four_decisions():
    """四档色温是一个决定内部的四个位置，判据是：交换任意两档，画面物理上不成立。

    这条判据落到代码上就是**单调**：从热核往外，红分量只能一路降下去。
    一旦某一档比它外面那一档更冷，这四个值就不再是一束光，而是四个各自决定的琥珀
    ——那时它们就该被收成一个（§7.1）。
    """
    decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", ROOT))
    ramp = ["--warm-hi", "--glow-flame", "--glow-body", "--glow-far"]
    reds = []
    for token in ramp:
        assert token in decls, token
        parts = [int(x) for x in decls[token].split(",")]
        assert len(parts) == 3, (token, decls[token])
        reds.append(parts[0])
    assert reds == sorted(reds, reverse=True), dict(zip(ramp, reds))
    assert len(set(reds)) == len(reds), reds
    # 热核和玻璃高光是同一个值：镜面高光本来就是光源自己的倒影，不是第五档。
    assert "--glow-hot" not in decls
    assert "--warm-hi" in REFERENCED


def test_the_companion_state_is_carried_by_rhythm_not_hue():
    """三个状态，一个色。原先三个状态三个色相，而胶囊上早就写着字了。

    这与地形页的「颜色不分类，形状分类」、备忘的「重要度是一个词」是同一条：
    分类已经有承担者的时候，色相做第二遍只是把几个不可辨的琥珀摆在一起。
    """
    # 三态共用同一条颜色规则，没有任何一个状态自己拿一个色相
    shared = ".comp-state.listening,.comp-state.thinking,.comp-state.responding{"
    assert shared in CSS
    for state in ("listening", "thinking", "responding"):
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS_NO_COMMENTS):
            if sel.strip() != f".comp-state.{state}":
                continue
            assert not re.search(r"(?<!-)\bcolor\s*:|background\s*:", body), (sel, body)
    # 词还在，而且是三个不同的词
    for word in ("在 听", "在 想", "在 回"):
        assert f"compState.textContent='{word}'" in APP_HTML, word
    # 节奏才是区分：在听 3.8s、在想 5s、在回定住（没有 dotPulse）
    css = _by_quantity(CSS)
    assert _by_quantity(".comp-state.listening::before{animation:dotPulse 3.8s") in css
    assert _by_quantity(".comp-state.thinking::before{animation:dotPulse 5s") in css
    assert not re.search(r"\.comp-state\.responding::before\{[^}]*animation", CSS_NO_COMMENTS)
    # 画布那边靠形状分状态（在想是绕着转的粒子，在回是涟漪），也不靠色相
    assert "if(state==='thinking'){" in APP_HTML and "if(state==='responding'){" in APP_HTML


def test_no_canvas_style_is_written_as_a_var_that_silently_does_nothing():
    """fillStyle / strokeStyle 里不许出现 var()——canvas 不认它，写了就是一句静默失效。

    「读 token」那一半由 test_the_canvases_read_their_colours_instead_of_copying_them 管；
    这一条守的是另一半：不要以为写了 var() 就算走了 token。
    """
    for m in re.finditer(r"(?:fillStyle|strokeStyle)\s*=\s*([^;\n]+)", SCRIPT_NO_COMMENTS):
        assert "var(--" not in m.group(1), m.group(0)


def test_the_paper_is_the_only_place_a_fourth_text_colour_is_allowed():
    """--ink 是第四个文字色，它有资格，因为它是另一套的第一档，不是这一套的第四档。

    三档文字色全部是为暗底定的。便签是全 app 唯一的亮面，把 --text 放上去一个字
    都读不清。所以这条例外的范围必须锁死：只有那张纸。
    """
    assert "--ink" in DECLARED and "--paper" in DECLARED
    ink_rules = [
        sel
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS_NO_COMMENTS)
        if "--ink" in body and sel.strip() != ":root"
    ]
    assert [s.strip() for s in ink_rules] == [".msg-user .bubble"], ink_rules
    # 纸的明暗由透明度走完，一个色名
    paper = _rule_body(".msg-user .bubble")
    stops = re.findall(r"rgba\(var\(--paper\),([\d.]+)\)", paper)
    assert len(stops) == 3, paper
    assert [float(x) for x in stops] == sorted((float(x) for x in stops), reverse=True), stops


# --- §7.6：子页的眉标不是装饰，是回去的路 ------------------------------------


def test_the_way_back_is_the_sub_pages_eyebrow_not_a_thing_floating_over_it():
    """返回键与页名争同一个左上角，争的不是尺寸，是层级。

    实测过重叠：`#legacyBack` 占 y17–50，备忘的 `.title-en` 占 y18.5–30.5、
    `.title-cn` 占 y34.5–57.5，三者同在 x18——返回键压在页名的**两行**上。
    给标题加 padding 是把两样东西并排放；真正要答的是「一个子页要不要有自己的
    大标题」。定案：有名字，没有那顶冠。主页的眉标是一行装饰性英文
    （Slow Living / Us / Inner Terrain），子页的眉标是回去的路，占同一格。
    """
    start = MARKUP_NO_COMMENTS.index('id="screen-memo"')
    memo = MARKUP_NO_COMMENTS[start : start + 1400]
    assert '<button type="button" class="sub-back" id="subBack">' in memo
    topbar = memo[memo.index('class="topbar"') : memo.index("title-cn")]
    assert "title-en" not in topbar, "子页不戴那顶装饰性英文眉标"
    assert "title-cn" in memo

    # 它长在顶栏里，不是浮在整个 .phone 上：那个绝对定位胶囊连页都不属于，
    # 所以「回到哪」只能靠文案自己说一遍（原文案是「‹ 回到我们」）。
    for ghost in (".legacy-back", "legacy-open", "legacyBack", "openLegacyScreen", "data-legacy"):
        assert ghost not in LIVE_SOURCE, ghost
    body = _rule_body(".topbar .sub-back")
    assert "position:absolute" not in body and "z-index" not in body


def test_the_way_back_wears_exactly_the_type_rung_it_replaced():
    """占同一格就得是同一档字：新开一个字号才是又发明了一个层级。"""
    eyebrow = re.sub(r"\s+", "", _rule_body(".topbar .title-en"))
    back = re.sub(r"\s+", "", _rule_body(".topbar .sub-back"))
    for prop in ("font-size", "letter-spacing", "color"):
        want = re.search(prop + r":([^;]+)", eyebrow).group(1)
        assert f"{prop}:{want}" in back, (prop, want, back)
    # 中文要用中文的那套衬线，这是唯一允许不同的一项
    assert "font-family:var(--serif-cn)" in back
    # 触摸区靠自己的 padding 撑开，再用负 margin 收回，所以页名与它左对齐、
    # 两行之间也不因为那块触摸区多出一个间距（第六个值）。
    assert "padding:8px12px" in back and "margin:-8px-12px0" in back
    assert re.sub(r"\s+", "", _rule_body(".topbar .sub-back + .title-cn")) == "margin-top:0"


def test_there_is_one_sub_screen_and_no_branch_waiting_for_a_second():
    """一条永远收不到那个参数的分支，读起来像「这里还有第二个子页」。"""
    assert APP_HTML.count("data-sub=") == 1 and 'data-sub="memo"' in APP_HTML
    src = SCRIPT_NO_COMMENTS[SCRIPT_NO_COMMENTS.index("function openSubScreen") :]
    src = src[: src.index("\n}")]
    assert "if(target!=='memo') return;" in src
    # 生活是四个主入口之一（tabbar 里有它），从来不会从这条路进来
    assert "syncLifeFromBackend" not in src
    assert 'data-screen="life"' in APP_HTML


def test_the_memory_switch_has_exactly_one_implementation():
    """两套实现里有一套永远收不到状态，而它画的东西仍然在真控件上叠着。

    「暂停记忆形成」原先有两套：活的一套是 `<label class="we-switch"><input
    type="checkbox">` + `.we-switch-slider`，状态走 `:checked`；死的一套等着有人
    给 `.we-switch` 加一个 `on` class，而全 app 没有一行这么做。死的那套不是无害的
    ——它给 `.we-switch` 自己也画了底色、边框和一个 16px 的圆点，全都叠在活控件上。
    一个状态只能有一个真源；开关的真源是 `:checked`，不是一个 class。
    """
    assert ".we-switch.on" not in CSS_NO_COMMENTS
    assert re.search(r"we-switch['\"\s]*\)?\.classList", SCRIPT_NO_COMMENTS) is None
    assert ".we-switch input:checked + .we-switch-slider" in CSS_NO_COMMENTS
    # 圆点只能长在滑块上，不能长在外壳上——外壳是 <label>，它只负责尺寸和点击区。
    assert ".we-switch::after" not in CSS_NO_COMMENTS


def test_no_inline_style_carries_a_colour_or_a_corner():
    """内联样式绕过每一条轴，而它最容易长在最少被看的那段标记上。

    「永久删除全部数据」那一步的确认输入框原先是 JS 里一整串内联样式：
    `border-radius:12px`（不是三档圆角里的任何一档）、`color:#e5e7eb`（第四档文字色）、
    还有自己一套 padding。全 app 最不可逆的那个动作，长在最没被审过的标记上。
    留下的 6 处内联样式都只写几何或状态（`max-width`/`opacity`/`text-align`），
    颜色一律走 token——这是内联样式可以接受的用法边界。
    """
    for m in re.finditer(r'style\s*=\s*"([^"]*)"', SCRIPT_NO_COMMENTS + MARKUP_NO_COMMENTS):
        decls = m.group(1)
        assert "border-radius" not in decls, decls
        for c in re.finditer(r"(?<![-\w])(?:color|background)[-\w]*\s*:\s*([^;]+)", decls):
            assert "var(--" in c.group(1), decls


# --- 同一个选择器的第二份顶层声明：看着生效，其实一行都没渲染 ----------------

# 同特异性下后者胜。所以一个选择器在顶层被声明两次、且第二次重复了第一次的属性时，
# 第一次那几行**永远不渲染**——它们读起来像生效的设计决定，改它们不会有任何变化。
# 这一族缺陷在这个文件里已经咬过五次：重复的 `#screen-companion::after`、重复的
# `@keyframes voiceWave`、`.topbar`、`.topbar .title-en/.title-cn`，以及被 `.topbar`
# 那份重复声明连带杀掉的 `.river-topbar`（同特异性、不同选择器，审计看不见）。
#
# 曾经有过一张 33 条的未清债表，全都出自「V2.4 product polish」那段附录：当时的做法
# 是在文件末尾追加一份覆盖，而不是改原处，于是原处的值成了死文档。现已逐条并回，
# 表随之删除——留一张空表等于留一句「这里本来就该有例外」。
#
# 判据（这条守卫原先在这里报错）：一条规则的**选择器串**写了两遍，是重写；一条规则先给
# 一组元素定共同的底、再给其中一个改几处，是层。区别在于第二条是否**收窄了**选择器串。
# 收窄 = 有意的差异化；不收窄 = 同一个元素被说了两遍，前一遍作废。所以按整串归组，
# 不拆逗号：第 224 行那条 `.icon-btn,…,.tab{font:inherit;appearance:none}` 是**按钮归零层**,
# 一个归零层本来就是为了被组件覆盖。反过来，按整串归组还多抓到 5 条老规则漏掉的
# （`.screen`、`.life-screen`、`.life-add-panel.show .lap-mask` …）。
#
# 也不再要求「属性有重叠」才算：同一串写两遍本身就是缺陷——两份之间的属性今天不重叠，
# 明天在其中一份上加一行就静默失效，而读代码的人看不出哪一份是活的。


def _top_level_rules() -> list[tuple[str, str]]:
    """(选择器串, 规则体)，只要顶层的——@media/@keyframes 里的是另一个上下文。"""

    css = CSS_NO_COMMENTS
    out: list[tuple[str, str]] = []
    buf = ""
    i = 0
    while i < len(css):
        if css[i] == "{":
            sel, buf = buf.strip(), ""
            depth, j = 1, i + 1
            while j < len(css) and depth:
                depth += (css[j] == "{") - (css[j] == "}")
                j += 1
            if not sel.startswith("@"):
                out.append((sel, css[i + 1 : j - 1]))
            i = j
            continue
        if css[i] == "}":
            buf = ""
        else:
            buf += css[i]
        i += 1
    return out


def test_no_selector_gets_a_second_top_level_declaration():
    declared: dict[str, list[int]] = {}
    for idx, (sel_text, _body) in enumerate(_top_level_rules()):
        key = ", ".join(
            sorted(re.sub(r"\s+", " ", s).strip() for s in sel_text.split(","))
        )
        declared.setdefault(key, []).append(idx)

    twice = sorted(sel for sel, at in declared.items() if len(at) > 1)
    assert twice == [], twice


# --- #9 / #10：颜色 --------------------------------------------------------


def test_there_are_exactly_four_page_identity_colors():
    # 导航是四入口（共处 / 生活 / 地形 / 我们），所以页面身份色只能有四个。
    identity = {"--comp-warm", "--life-green", "--river-rose", "--me-violet"}
    assert identity <= DECLARED
    hex_tokens = {
        m.group(1)
        for m in re.finditer(r"(--[\w-]+)\s*:\s*#[0-9a-fA-F]{6}", ROOT)
    }
    functional = {"--memo-blue", "--refuse", "--recording"}
    # --bg-lift 与 --bg-deep 是同一种材料的两个位置（§7.9），不是两个身份：
    # 它们的色度锁在同一条线上，只有亮度不同。
    neutral = {"--bg-deep", "--bg-lift", "--text", "--text-dim", "--text-faint"}
    assert hex_tokens == identity | functional | neutral, sorted(hex_tokens)


def test_text_has_exactly_three_tiers():
    # --text-readable(#e8e4dc) 与 --text(#f2ede5) 相差不到 4%：两个几乎一样的
    # 文字色只会让下一个人猜该用哪个。
    # `-rgb` 后缀不是一档，它是同一个色的 rgba() 形态（四个身份色都是这么成对的）。
    tiers = {re.sub(r"-rgb$", "", t) for t in DECLARED if t.startswith("--text")}
    assert sorted(tiers) == ["--text", "--text-dim", "--text-faint"]
    # 纯白是第四档。它原先散在 8 处（详情页 + 照片查看 + 两个删除键），每一处都在深底
    # 或彩底上，看起来只是「白字」——但全 app 最亮的文字色是 --text(#f2ede5)，一个偏暖的
    # 米白。`#fff` 比它更亮更冷，所以那 8 处是这一页最亮的字，而没有一处需要成为最亮的字。
    assert "#fff" not in CSS_NO_COMMENTS


# 上面那句 `"#fff" not in CSS_NO_COMMENTS` 只钉住了白的**一种拼法**。`rgba(255,255,255,α)`
# 从它旁边走过去了 8 次——同一条法（文字色只有三档）、只盖住一个写法的守卫，会给其余
# 写法发通行证（§7.7）。所以这里改成正面的问法：**每一个 color: 都得经过 token**。
#
# 那 8 处曾经挂在一张 `WHITE_TEXT_OWED` 名单上当未清的债。#26 判完之后名单空了，
# 于是连名单一起删掉：留一张空名单等于留一句「这里本来就该有例外」（§10 第 11j 条）。


def test_every_text_colour_resolves_through_a_token():
    literal = set()
    for sel_text, body in _top_level_rules():
        if sel_text.strip() == ":root":
            continue
        for m in re.finditer(r"(?<![-\w])color\s*:\s*([^;}]+)", body):
            value = re.sub(r"\s+", " ", m.group(1)).strip()
            if "var(--" in value or value in ("inherit", "currentColor", "transparent"):
                continue
            literal.add(re.sub(r"\s+", " ", sel_text).strip())
    assert literal == set(), sorted(literal)


def test_icons_wear_the_text_ladder_too():
    """`stroke` / `fill` 是文字色的第三个属性名。

    §7.7：只盖住 `color:` 的审计，会给 `stroke:` 发通行证。全 app 的图标都跟着
    文字阶梯走（`.icon-btn svg{stroke:var(--text-dim)}`），所以这两个属性上也
    一个字面颜色都不许有——`currentColor` 除外，那是「跟着我的字走」。
    """
    bad = []
    # 只扫 <style>：JS 里的 `fill:'both'` 是 Web Animations 的填充模式，不是颜色。
    for m in re.finditer(r"(?<![-\w])(stroke|fill)\s*:\s*([^;}]+)", CSS_NO_COMMENTS):
        value = m.group(2).strip()
        if "var(--" in value or value in ("none", "currentColor", "transparent", "inherit"):
            continue
        if _PLAIN_NUMBER.fullmatch(value):  # stroke-width 之类被切进来的数字
            continue
        bad.append(m.group(0)[:70])
    assert bad == [], bad


# --- #26 / §7.10：字只有一种材料，纯白不是它的第四档 -----------------------


def _keyframe_bodies() -> dict[str, str]:
    """`@keyframes 名 { ... }` → 名 到 花括号内文本。

    用花括号计数而不是正则：keyframes 里还嵌着一层 `{0%{...}}`，
    带嵌套量词的正则在这份 8000 行的文件上会灾难性回溯（试过一次，测试直接挂住）。
    """
    src = CSS_NO_COMMENTS
    out: dict[str, str] = {}
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", src):
        depth = 1
        i = m.end()
        while i < len(src) and depth:
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
            i += 1
        out[m.group(1)] = src[m.end() : i - 1]
    return out


def _colour_of(selector: str) -> str:
    """取一条顶层规则的 `color:` 值（剥过注释）。"""

    for sel_text, body in _top_level_rules():
        if re.sub(r"\s+", " ", sel_text).strip() == selector:
            m = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
    raise AssertionError(f"找不到规则 {selector}")


def test_the_same_sentence_is_the_same_colour_in_both_views():
    """`.le-text` 与 `.dv-text` 是同一句话——用户自己写的那一句。

    先前流里是 `var(--text)`（暖白 242,237,229）、点开详情是
    `rgba(255,255,255,.85)`（中性 218,218,219），ΔE 6.24。字体、字重、字号三样
    全同，内容也全同，所以这里没有任何边界可以让颜色改变——它只是同一档被写了两遍。
    """
    for sel in (".le-text", ".dv-text"):
        assert _colour_of(sel) == "var(--text)", sel
    a = _rule_body(".le-text")
    b = _rule_body(".dv-text")
    for prop in ("font-family", "font-weight", "font-size"):
        va = re.search(rf"{prop}\s*:\s*([^;}}]+)", a)
        vb = re.search(rf"{prop}\s*:\s*([^;}}]+)", b)
        assert va and vb and va.group(1).strip() == vb.group(1).strip(), (prop, va, vb)


def test_dismissing_a_layer_is_one_rung_everywhere():
    """「关掉这一层」是一个动作，所以它是一档，不是四档。

    `.mtp-close` / `.lap-close` 早就是 `--text-faint` → hover `--text`；
    `.dv-close` / `.pv-close` 先前是 `rgba(255,255,255,.65/.62)`。其中 `.dv-close`
    的 hover 本来就是 `var(--text)`，而它的 transition 是 `all`——一个元素静止在
    中性色、hover 渐变到暖色，这不是边界，是它自己跟自己不一致。
    """
    for sel in (".mtp-close", ".lap-close", ".dv-close", ".pv-close"):
        assert _colour_of(sel) == "var(--text-faint)", (sel, _colour_of(sel))
    for sel in (".mtp-close:hover", ".lap-close:hover", ".dv-close:hover"):
        assert _colour_of(sel) == "var(--text)", (sel, _colour_of(sel))


def test_the_photo_overlay_is_one_material():
    """唯一真的「压在照片上」的一层，自己就把「压在照片上要纯白」否掉了三次。

    `.pv-live` 是 `var(--text)`、`.pv-count` 是 `--text-faint`、`.pv-caption`
    只靠一道黑渐变压在照片上也用 `--text-dim`。所以那个理由在唯一能验证它的地方
    三比一地不成立（§7.7 那个形状：一个只覆盖一个元素的理由，会给其余元素发通行证）。
    """
    rungs = {}
    for sel in (".pv-live", ".pv-count", ".pv-caption", ".pv-close"):
        value = _colour_of(sel)
        assert re.fullmatch(r"var\(--text(-dim|-faint)?\)", value), (sel, value)
        rungs[sel] = value
    # 三档都用上了才说明这一层是有层次的，而不是被压成一个亮度。
    assert len(set(rungs.values())) == 3, rungs


def test_deleting_something_saved_is_a_louder_rung_than_deleting_a_draft():
    """可逆与不可逆是一个真边界，中性与暖不是。

    `.lap-voice-delete` 删的是还没提交的录音，重录一次就回来了，所以它在
    `--text-faint`。`.le-delete` / `.memo-delete` 删的是已经存下来的记录，误触
    不可逆，所以静止态就得认得出——那要的是**更亮的一档**（`--text-dim`），
    而不是**另一个色系**。先前这两处还是 `.7` 和 `.5` 两个不同的白，同一个功能两个值。
    """
    assert _colour_of(".lap-voice-delete") == "var(--text-faint)"
    assert _colour_of(".le-delete") == "var(--text-dim)"
    assert _colour_of(".memo-delete") == "var(--text-dim)"


def _rgba_alphas(value: str) -> list[float]:
    """取出 `value` 里每一个 `rgba(...)` 的 alpha。

    必须按括号配平取最后一个顶层参数，不能用 `rgba\\([^)]*,\\s*([\\d.]+)\\)`：
    `rgba(var(--text-rgb),0.2)` 里 `[^)]*` 过不去 `var()` 的右括号，于是同一个
    透明度换个拼法就从守卫旁边走过去了（§7.7 那个形状，变异测试第 8 项抓到的）。
    """
    out: list[float] = []
    for m in re.finditer(r"rgba\(", value):
        depth, i = 1, m.end()
        while i < len(value) and depth:
            if value[i] == "(":
                depth += 1
            elif value[i] == ")":
                depth -= 1
            i += 1
        args, depth, cur = [], 0, ""
        for ch in value[m.end() : i - 1]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                args.append(cur)
                cur = ""
            else:
                cur += ch
        args.append(cur)
        try:
            out.append(float(args[-1].strip()))
        except ValueError:
            continue
    return out


def test_a_colour_alpha_is_never_multiplied_by_an_opacity_animation():
    """色的 alpha 和元素的 opacity 是两条通道，乘在一起没人算得过来。

    `.dv-hint`（「轻拨卡片 · 翻阅记忆」）先前 `color` 的 alpha 是 .2，
    `dvHintPulse` 的 opacity 又是 .2–.45，相乘之后有效 alpha 只有 .04–.09，
    实测对比 1.07–1.20：一句要读的指令等于没显示，而两条声明各自看起来都合理。

    这条正对照原先钉的是 `"dvHintPulse" in fading`。那个名字**已经不存在了**：
    §7.11 判定「亮度只由色档决定」之后，收掉 alpha 只让它回到 1.17–1.64:1，
    剩下的那个无限脉动仍然在改写色档，于是脉动本身被删掉（v1.16）。
    **一个钉在具体名字上的正对照，会在那个名字因为被治好而消失时报假警**——
    它量的其实是「关键帧扫描器还在工作」，所以改成钉住这件事本身。
    """
    keyframes = _keyframe_bodies()
    fading = {name for name, body in keyframes.items() if re.search(r"(?<![-\w])opacity\s*:", body)}
    assert len(fading) > 15, sorted(fading)
    bad = []
    for sel_text, body in _top_level_rules():
        colour = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        anim = re.search(r"(?<![-\w])animation\s*:\s*([^;}]+)", body)
        if not colour or not anim:
            continue
        value = colour.group(1)
        alphas = [a for a in _rgba_alphas(value) if a < 1]
        if not alphas:
            continue
        if any(name in anim.group(1) for name in fading):
            bad.append((re.sub(r"\s+", " ", sel_text).strip(), value.strip(), anim.group(1).strip()))
    assert bad == [], bad


def test_the_rgb_twin_is_byte_for_byte_the_same():
    """`--x-rgb` 必须就是 `--x`，一个字节都不许差。

    一个飘掉的 `-rgb` 是最难发现的一种第二个值：`color:var(--comp-warm)` 和
    `border-color:rgba(var(--comp-warm-rgb),.3)` 写在同一条规则里，看起来是
    同一个色的两种形态，但只要 `-rgb` 抄错一位，描边和文字就永远差着一点，
    而没有任何审计会报出来——它们各自都「引用了 token」（§7.2）。
    """
    decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", ROOT))
    twins = [t for t in decls if t.endswith("-rgb") and t[: -len("-rgb")] in decls]
    assert len(twins) >= 6, twins
    for twin in twins:
        base = decls[twin[: -len("-rgb")]].strip()
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", base), (twin, base)
        expected = ",".join(str(int(base[i : i + 2], 16)) for i in (1, 3, 5))
        assert decls[twin].strip().replace(" ", "") == expected, (twin, decls[twin], expected)


# --- #11：字重 -------------------------------------------------------------


def test_nothing_shouts():
    # 这个产品没有需要喊的东西；强调靠颜色与留白，不靠加粗。
    heavy = [
        (i, line.strip()[:90])
        for i, line in enumerate(APP_HTML.splitlines(), 1)
        for m in re.finditer(r"font-weight:\s*(\d+)", line)
        if int(m.group(1)) >= 500
    ]
    assert heavy == []


# --- #12：你的字直立，它的字倾斜 -------------------------------------------


def _rule_body(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\}", CSS, re.S)
    assert match, selector
    return match.group(1)


def test_the_users_own_words_are_never_set_as_a_quotation():
    # Noto Serif SC 没有真斜体，浏览器只能合成一个倾斜——那是一种「被加工过的字」。
    # 用在主角的原话上，就把他的原话变成了引述。
    assert "italic" not in _rule_body(".msg-user .bubble")


def test_what_it_says_is_slanted_everywhere_it_speaks():
    # 全app只剩一条判读轴：倾斜 = 它；直立 = 你，或系统。
    assert "italic" in _rule_body(".msg-ai .bubble")
    # 生活流里没有气泡，所以这条排版轴是那里唯一的区分。
    assert "italic" in _rule_body(".le-agent-copy")


def test_the_degraded_notice_belongs_to_neither_voice():
    # 它既不是用户的话，也不是它说的话（声音基线 §3.2）：直立、更小、无气泡。
    assert "font-style:normal" in _rule_body(".le-agent.le-status.failed .le-agent-copy")
    assert "italic" not in _rule_body("[data-degraded]>div")


def test_a_bubble_never_breaks_its_own_silhouette_over_a_long_word():
    """两种气泡都被允许长到 75% / 84% 宽，但一行粘不完的 URL 不得再把它们撑破。

    全 app 的文本容器（.tr-card-expr / .we-name / .we-choice 那一族）早就统一
    `overflow-wrap:anywhere`——气泡是最后两个没写的地方，而它们恰恰是用户
    最可能往里贴链接的地方。这条守卫守的是「约定没有例外」，不是某一处样式。
    """
    for sel in (".msg-user .bubble", ".msg-ai .bubble"):
        body = _rule_body(sel)
        assert "overflow-wrap:anywhere" in body, sel
        # anywhere 与 break-word 是两句话：后者只在量不尽时才断，min-content
        # 之下仍会溢出。写了弱的那档等于没写。
        assert "overflow-wrap:break-word" not in body, sel


def test_selection_is_the_same_pen_on_two_materials_not_the_browsers_blue():
    """选区是同一支笔在两种材料上的两句话：纸上荧光，玻璃上点亮。

    未写 ::selection 时浏览器给一块系统蓝——在 #06080F 的暖暗底与奶黄纸上
    都是异物。两处都只许动背景、不许动字色（字色回答「这是什么字」，与
    「你正选中它」无关），且 alpha 必须走 --o 档梯：纸上 o-5、玻璃上 o-4
    （玻璃底深，同样的 alpha 显得更亮，所以低一档——两句话，不互相抄）。
    """
    paper = _rule_body(".msg-user .bubble::selection")
    glass = _rule_body(".msg-ai .bubble::selection")
    assert paper.strip() == "background:rgba(var(--comp-warm-rgb),var(--o-5))", paper
    assert glass.strip() == "background:rgba(var(--comp-warm-rgb),var(--o-4))", glass
    # 全 app 只许这两处选区样式：多出来的任何一处都在回答没人问过的问题。
    extra = [
        sel.strip()
        for sel, _body in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS_NO_COMMENTS)
        if "::selection" in sel
    ]
    assert extra == [
        ".msg-user .bubble::selection",
        ".msg-ai .bubble::selection",
    ], extra


def test_the_annotation_rows_are_not_part_of_the_copyable_text():
    """时间戳与落款是批注，不是正文：选中复制时不许被带进去。

    `.msg-user .meta` 与 `.msg-ai .signature`（整行，含朗读键）都写
    `user-select:none`。朗读键本身没有字，但把它留在可选区里没有任何
    好处——选中一段话时旁边浮着一颗被选中的按钮，是噪音。
    """
    assert "user-select:none" in _rule_body(".msg-user .meta")
    assert "user-select:none" in _rule_body(".msg-ai .signature")


# --- #27：文字的亮度只由色档决定，opacity 这条通道留给状态 -------------------
#
# `--text-faint` 满强度压在 `--bg-deep` 上只有 3.73:1 —— 三档阶梯自己的地板已经在
# WCAG AA 4.5 之下。所以任何 α<1 都把最低一档压到地板之下（α=.9→3.23、α=.8→2.79）。
# 而这个旋钮的每一格只有两种结局，都不合法：ΔE<15（α≥.58）是付了代价换零比特；
# ΔE≥15（α≤.58）是在三档之下发明第四档，而那一档全部低于 2.01:1。中间没有第三种。
# 所以文字上没有「合适的透明度」这回事，只有「哪一档」。

# 「这条规则在说字怎么长」——只认字才有的属性，不点名任何一个 class。
# 故意不含 `color`：`color` 也在走 currentColor 喂 SVG 的 `stroke`，混进来会把图标
# 算成字（`.life-search-inner svg` 那一批就会被误判，而它们归 #29）。
TEXT_PROPERTY = re.compile(
    r"(?<![-\w])(font-size|font-family|font-weight|font-style|letter-spacing|"
    r"line-height|text-transform|text-align|text-shadow|white-space|text-decoration)\s*:"
)

# 「什么算一个状态记号」——这份文件里只许有一把尺子（§7.23）。
#
# v1.37 已经写下这条法（「同一个概念不许留两把尺子，所以三个调用点一起换」），而同一轮
# 留下的 `_covers(..., strip)` 那个参数正是它的反面：参数让调用方挑尺子，于是同一个概念
# 可以在两个调用点上长成两个样子，而没有一条断言会红。审计出来的是**四把**：
# `STATE_SELECTOR`、旧 `_STATE_TOKEN`、`_MATERIAL_STATE` 都并进了这一把，`_STATE_MARK`
# （§7.16 用）是这一把的真子集，它看不见的那几条声明有账（见它自己那一行）。
#
# 类名那一半是这个 app 的数据，会腐，所以它自己有两条守卫：名单里的每个名字必须在 CSS 里
# 真的出现（`.off` / `.closed` / `.selected` / `.loading` / `.error` 是这么删掉的），且脚本
# 用 `classList` 装卸过、CSS 里又有规则的类名必须在名单里（19 个漏报是这么补上的）。
# 伪类与状态属性那一半不受「必须出现」管：它编码的是 CSS / HTML / ARIA 的语义，一个今天
# 没用到的名字不是腐烂的证据，而漏掉它就是一个洞——漏报比误报危险（§7.15）。
STATE_CLASSES = frozenset({
    "active", "busy", "canvas-ready", "collapsed", "companion-expanded", "dispersing",
    "done", "dragging", "entering", "expanded", "faded", "failed", "flash", "fly-in",
    "focused", "group-collapsed", "has-open", "listening", "lit", "live-off",
    "long-pressing", "media-loading", "media-unavailable", "naming", "no-img", "on",
    "open", "overdue", "pending", "playing", "recording", "removing", "responding",
    "reveal", "rewritten", "scroll-locked", "show", "show-delete", "sub-open",
    "thinking", "typing", "visible", "warn", "with-why",
})

# 必须整词匹配，而边界只能写成 `(?![\w-])`：`\.on` 不加边界会咬进 `.onboard-modal` 的中间；
# 而 `\b` 也不行——`\.show\b` 会咬进 `.show-delete`（`w` 与 `-` 之间就是一个词边界）。
# `STATE_SELECTOR` 一直是 `\b`，于是它把那两条 `.show-delete` 规则**答对了、理由是错的**，
# 另外两把则整个看不见它们。
# `:not(...)` 整段剥掉：它是一个状态限定的**否定**，主体还是同一个主体。漏了这一条，
# `.gi:not(.media-unavailable):active::after` 的主体会算成 `.gi:not()::after`，于是它和
# 基线那条 `.gi::after` 认不成同一个东西。
# 属性那一支只收状态：HTML 的 `hidden` / `disabled`，加 ARIA 规范自己划出来的那一族
# **state**（`aria-label` 这类 property 说的是这个东西是什么，不是它现在怎么样）。
_STATE_TOKEN = re.compile(
    r":not\([^()]*\)"
    r"|:(?:hover|active|focus[\w-]*|disabled|checked|placeholder-shown)"
    r"|\.(?:" + "|".join(sorted(STATE_CLASSES)) + r")(?![\w-])"
    r"|\[(?:hidden|disabled)\]"
    r"|\[aria-(?:busy|checked|current|disabled|expanded|hidden|invalid|pressed|selected)[^\]]*\]"
)


def _module_level_regexes() -> dict[str, re.Pattern[str]]:
    """这份文件里所有模块级 `NAME = re.compile(...)` 的名字 → 它编译出来的那把尺子。"""
    source = Path(__file__).resolve().read_text(encoding="utf-8")
    out = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "compile"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = globals()[target.id]
    return out


def _selector_branches() -> list[str]:
    """所有顶层规则的选择器，逗号拆开、空白压平。"""
    out = []
    for selector, _ in _top_level_rules():
        for part in selector.split(","):
            part = re.sub(r"\s+", " ", part).strip()
            if part:
                out.append(part)
    return out


def _css_class_names() -> set[str]:
    """CSS 顶层选择器里真的出现过的类名。"""
    names: set[str] = set()
    for part in _selector_branches():
        names.update(re.findall(r"\.([\w-]+)", part))
    return names


def test_only_one_ruler_says_what_a_state_token_is():
    """「什么算一个状态记号」这个概念，这份文件里只许有一把尺子（§7.23）。

    v1.37 写下这条法的同一轮留着 `_covers(..., strip)` 那个参数，而参数让调用方挑尺子——
    于是这个概念在四个地方长成四个样子，没有一条断言会红：两份逐字节相同的名单各抄了
    一遍；`_MATERIAL_STATE` 缺 15 个今天真在用的名字又带 2 个死名字；`STATE_SELECTOR`
    的 `\\b` 咬进 `.show-delete` 的中间，把那两条规则**答对了、理由是错的**。所以这条法
    自己需要一把尺子。

    认尺子用的是**行为**，不是名字、也不是正则原文：一把尺子认状态记号
    （`:hover` / `.done` / `[hidden]` 这一类），同时不认普通选择器（`div` / `.card`）。
    这样第五把尺子换成什么拼法都躲不过去，而按「正则原文里有没有 `:hover`」去找就躲得过。
    """
    rulers = _module_level_regexes()
    assert len(rulers) >= 20, sorted(rulers)  # 仪器自校验：扫瞎了只会报一个空集合
    state = ("button:hover", ".card.done", "a:focus", "div[hidden]", "li:checked")
    plain = ("div", ".card", "#panel", '[data-p="red"]', "p>span")
    speaks = sorted(
        name
        for name, ruler in rulers.items()
        if any(ruler.search(s) for s in state) and not any(ruler.search(p) for p in plain)
    )
    assert speaks == ["_HAND", "_STATE_MARK", "_STATE_TOKEN"], speaks

    # 另外两把答的是更窄的问题（`_HAND` 是「哪里有一只手」，`_STATE_MARK` 是 §7.16 认的
    # 那一档），所以它们必须是这一把的**真**子集：越出去一条，就是第二个答案而不是子问题。
    branches = _selector_branches()
    assert len(branches) > 800, len(branches)
    for name in ("_HAND", "_STATE_MARK"):
        narrow = rulers[name]
        outside = [p for p in branches if narrow.search(p) and not _STATE_TOKEN.search(p)]
        witness = [p for p in branches if _STATE_TOKEN.search(p) and not narrow.search(p)]
        assert outside == [], (name, outside)
        assert len(witness) > 50, (name, len(witness))

    # 反方向：尺子宽了会把一条选择器整个吃光，于是它的「主体」是空串——而空串覆盖一切，
    # §7.23 判据一会静静变成一句空话。这条守卫是那个方向上的唯一对手。
    naked = [p for p in branches if not _STATE_TOKEN.sub("", p).strip()]
    assert naked == [], naked


def test_the_state_list_carries_no_name_the_css_never_uses():
    """名单上的每个名字都得在 CSS 里真的出现——类名那一半是数据，会腐。

    `.off` / `.closed` / `.selected` / `.loading` / `.error` 是这么删掉的：四把旧尺子各自
    带着几个，而 CSS 里一处都没有。一个死名字不会让任何断言变红，它只是让读名单的人
    以为那个状态还在被管着。
    """
    classes = _css_class_names()
    assert len(classes) > 300, len(classes)  # 仪器自校验
    dead = sorted(name for name in STATE_CLASSES if name not in classes)
    assert dead == [], dead

    line = _doc_line("### 7.23", "名单上今天有")
    assert [len(STATE_CLASSES)] == [int(n) for n in re.findall(r"\*\*(\d+)\*\* 个类名", line)], (
        len(STATE_CLASSES),
        line,
    )


def test_a_class_the_script_mounts_and_the_css_styles_is_on_the_state_list():
    """反方向：能机械查出来的状态不许漏——漏报比误报危险（§7.15）。

    一个 class 是状态，最硬的证据是脚本在运行时装卸它。这条守卫补上了 19 个漏报
    （`.show` 在 CSS 里 26 处、`.media-unavailable` 14 处，而四把旧尺子一把都不认）。

    它只能守住 `classList` 那条通道：另有 8 个名字是靠 `className='…'` 拼串上身的，
    这条守卫看不见它们，所以名单还得手写、还得靠上面那条守卫防腐（→ 另立一账）。
    """
    mounted: set[str] = set()
    for call in re.finditer(
        r"classList\s*\.\s*(?:add|remove|toggle|replace)\s*\(([^;]*?)\)", SCRIPT_NO_COMMENTS
    ):
        for literal in re.finditer(r"""['"]([^'"]+)['"]""", call.group(1)):
            mounted.update(literal.group(1).split())
    assert len(mounted) >= 30, sorted(mounted)  # 仪器自校验：解析瞎了只会报一个空集合

    styled = _css_class_names()
    missing = sorted(c for c in mounted & styled if c not in STATE_CLASSES)
    assert missing == [], missing

    line = _doc_line("### 7.23", "名单上今天有")
    assert [len(mounted)] == [int(n) for n in re.findall(r"装卸的类名有 \*\*(\d+)\*\*", line)], (
        len(mounted),
        line,
    )


# 行内 style 里写 color 的 alpha，图形标签除外：`<svg style="color:rgba(...)">` 的
# color 是喂 currentColor 给 stroke 的，那是图标不是字。按标签判，不按名单判。
_GRAPHIC_TAGS = {
    "svg", "path", "circle", "line", "rect", "polyline", "polygon", "ellipse", "g", "use", "defs",
}


def _static_opacities(body: str) -> list[float]:
    """规则体里写死的中间 opacity。0 和 1 不算：那是开关，不是旋钮。"""
    values = [float(m.group(1)) for m in _OPACITY_VALUE.finditer(body)]
    return [v for v in values if 0 < v < 1]


def test_the_opacity_instrument_can_actually_see_the_file():
    """先证明尺子在量东西，再相信它报的零。

    G1–G6 都是「不存在」型断言，而一个把 CSS 读成空串的解析器会把六条全报成绿。
    所以这里先校验仪器：规则数、认出的文字规则数、认出的状态规则数都必须够多。
    （一个没被校验过的仪器给出的数字，和一个没被测试守着的规则是同一类东西。）
    """
    rules = _top_level_rules()
    assert len(rules) > 600, len(rules)
    text_rules = [s for s, b in rules if TEXT_PROPERTY.search(b)]
    assert len(text_rules) > 200, len(text_rules)
    state_rules = [s for s, b in rules if _STATE_TOKEN.search(s)]
    assert len(state_rules) > 80, len(state_rules)
    # 而且它确实能在字上读出 opacity —— 拿关键帧作正对照：淡入淡出是合法用法，
    # 也证明 `_static_opacities` 的正则不是一直返回空表。
    assert _static_opacities("opacity:.35;font-size:9px") == [0.35]
    assert _static_opacities("opacity:1;opacity:0") == []


def test_text_brightness_comes_only_from_the_colour_tier():
    """没有一条规则同时说「字怎么长」和「把字压暗多少」。

    先前 39 处文字带着静态 opacity：`.comp-boundary` 是 faint×0.72 = 2.43:1，
    `.comp-input textarea::placeholder` 是 dim×0.50 = 2.80:1（还在聚焦时再降到
    0.35，恰好在提示语是屏幕上唯一一行字的那一刻把它藏起来）。两条声明各自看起来
    都合理，乘完才是地板之下——所以守卫必须问「有没有人在一条规则里同时拧两样」。
    """
    bad = [
        (re.sub(r"\s+", " ", sel).strip(), _static_opacities(body))
        for sel, body in _top_level_rules()
        if _static_opacities(body) and TEXT_PROPERTY.search(body) and not _STATE_TOKEN.search(sel)
    ]
    assert bad == [], bad


def test_a_rule_that_names_a_text_colour_has_no_second_knob():
    """写着三档文字色或身份色的规则，旁边不许再有第二个旋钮。

    G1 认的是「这条规则在排字」，这一条认的是「这条规则在给字上色」——同一条律的
    另一半覆盖面：`.dv-place{color:var(--life-green);opacity:.55}` 里一个字体属性
    都没有，G1 看不见它，但它照样是一档色乘一个旋钮。
    """
    tier = re.compile(
        r"var\(--text(?:-dim|-faint)?\)|"
        r"var\(--(?:life-green|memo-blue|river-rose|comp-warm|me-violet|refuse)\)"
    )
    bad = []
    for sel, body in _top_level_rules():
        colour = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        if not colour or not tier.search(colour.group(1)):
            continue
        values = _static_opacities(body)
        if values and not _STATE_TOKEN.search(sel):
            bad.append((re.sub(r"\s+", " ", sel).strip(), colour.group(1).strip(), values))
    assert bad == [], bad


def _dimming_keyframes() -> dict[str, float]:
    """每个把 opacity 压到 1 以下的 @keyframes → 它的最低那一帧。"""
    css = CSS_NO_COMMENTS
    out = {}
    for m in re.finditer(r"@keyframes\s+([\w-]+)\s*\{", css):
        depth, i = 1, m.end()
        while i < len(css) and depth:
            depth += (css[i] == "{") - (css[i] == "}")
            i += 1
        values = [float(x) for x in _OPACITY_VALUE.findall(css[m.end() : i - 1])]
        if values and min(values) < 1:
            out[m.group(1)] = min(values)
    return out


def test_a_rule_that_names_a_text_colour_is_not_also_animated_dimmer():
    """@keyframes 是这条通道的**第三个**写法，而它和前两个一样不许碰字的亮度。

    淡入淡出是合法的：一段淡入**结束在 1**，它是过渡，不是静息态。缺陷形状是
    `infinite` —— 一条无限循环的动画把 opacity 压在 1 以下，意味着这段字的静息
    亮度永远不是它的色档，而是循环里的某一帧。`.dv-hint` 就是这么被压到
    **1.17–1.64:1** 的：`dvHintPulse` 在 `.2–.45` 之间脉动，而它写着 `--text-faint`，
    两个驱动者答同一个问题，最终亮度谁都没选过。

    判据落在「这条规则有没有指定色档」上，而不是一张名单上。因为反例是**emoji**：
    `.dv-card-emoji`（64px 的 `card.mood`）与 `.empty-tip .big`（36px 的 🌿/🔍/📭）
    也在无限脉动，但它们是**图形不是字**——`color` 到不了一个彩色 emoji 字形，
    所以那里没有色档在被改写，`opacity` 是它们仅有的一条通道（归 #29 的图形簇）。
    两者都不写 `color`，于是这条判据自动放它们过去，而不需要我记住它们的名字。
    """
    tier = re.compile(
        r"var\(--text(?:-dim|-faint)?\)|"
        r"var\(--(?:life-green|memo-blue|river-rose|comp-warm|me-violet|refuse)\)"
    )
    dimming = _dimming_keyframes()
    assert dimming, "一个压暗的关键帧都没认出来 —— 先确认 @keyframes 解析还在工作"

    bad, graphic = [], []
    for sel, body in _top_level_rules():
        infinite = [
            m.group(1)
            for m in re.finditer(r"(?<![-\w])animation\s*:\s*([^;}]+)", body)
            if "infinite" in m.group(1)
        ]
        hits = [
            (name, lo)
            for decl in infinite
            for name, lo in dimming.items()
            if re.search(r"(?<![-\w])" + re.escape(name) + r"(?![\w-])", decl)
        ]
        if not hits:
            continue
        colour = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        flat = re.sub(r"\s+", " ", sel).strip()
        if colour and tier.search(colour.group(1)):
            bad.append((flat, colour.group(1).strip(), hits))
        else:
            graphic.append((flat, hits))
    assert bad == [], bad
    # 正对照：那两处 emoji 必须真的被这条守卫看见过、再被判为图形放过去。
    # 一处都没看见，说明 `infinite` 或关键帧匹配已经坏了，上面那个空表不算数。
    assert len(graphic) >= 2, graphic


# --- #32：状态通道自己是几档（§7.16）-----------------------------------------

# 「这是一个状态」的写法：伪类，或者一个说状态的类名。状态通道的判据只对这些生效，
# 材料与图形上的 opacity 归 #29/#49，它们答的不是同一个问题。
#
# 这是 `_STATE_TOKEN` 的**真子集**，而且是一笔明账（§7.23，v1.39）：它今天看不见 7 条状态
# 减光声明，那 7 条带进来 5 个新值（0.32 / 0.55 / 0.7 / 0.72 / 0.9），而「它们算不算状态
# 减光、各落哪一档」是 #32 的问法，不是「几把尺子」这一问 → #110。子集关系与那 7 条
# 由守卫钉着，所以它既不能自己长出名字，也不能静静多漏一条。
# `.selected` / `.disabled` / `.loading` / `.error` / `.closed` 是这么删掉的：CSS 里一处都没有。
_STATE_MARK = re.compile(
    r":disabled|\[disabled\]|:hover|:active|:focus|:checked|"
    r"\.(?:done|faded|open|show|pending|recording|dragging|"
    r"failed|collapsed|has-open)(?![\w-])"
)


def _compounds(selector: str) -> list[tuple[set[str], set[str], str]]:
    """一条选择器按后代/子组合符切成若干段，每段给出 (正类, 被 :not() 否掉的类, 原文)。

    最后一段是 key（真正被上样式的那个元素），前面的都在描述祖先。
    """
    out = []
    for part in re.split(r"\s*>\s*|\s+", re.sub(r"\s+", " ", selector).strip()):
        if not part:
            continue
        negated = set(re.findall(r":not\(\s*\.([\w-]+)\s*\)", part))
        positive = set(re.findall(r"\.([\w-]+)", re.sub(r":not\([^)]*\)", "", part)))
        out.append((positive, negated, part))
    return out


def _opacity_rules() -> list[tuple[str, float, str]]:
    """(单条选择器, 声明的 opacity, 规则体)，只要顶层写了 opacity 的。逗号已拆开。"""
    out = []
    for selector, body in _top_level_rules():
        m = _OPACITY_VALUE.search(body)
        if not m:
            continue
        for one in selector.split(","):
            one = re.sub(r"\s+", " ", one).strip()
            if one:
                out.append((one, float(m.group(1)), body))
    return out


def test_the_narrower_state_mark_is_blind_to_exactly_these_declarations():
    """`_STATE_MARK` 比 `_STATE_TOKEN` 窄，差额是一笔钉住的账（§7.23，v1.39 → #110）。

    并到第四把就得先答另一个问题：这 7 条声明算不算状态减光、各落哪一档。那是 #32 的
    问法（其中四条已经是 #62/#63 的地界，`.scene-canvas` 那两条正是 #63 点名的两块氛围
    画布），不是「同一个概念有几把尺子」这一问。所以这一轮不并，改成把差额钉住：子集
    关系由上面那条守卫守着，差额由这条守着——它既不能自己长出名字，也不能静静多漏
    一条，于是 §7.16 那把四档阶梯「今天只读到 4 个值」也就有了对手。
    """
    ledger = {
        "#screen-companion.canvas-ready::before": 0.32,
        ".le-agent-copy.rewritten::after": 0.55,
        "body.companion-expanded #screen-companion.canvas-ready::before": 0.55,
        ".memo-group-title.overdue .bar": 0.7,
        ".life-entry.long-pressing": 0.72,
        "#screen-me.active .scene-canvas": 0.85,
        "#screen-river.active .scene-canvas": 0.9,
    }
    blind = {
        sel: value
        for sel, value, _ in _opacity_rules()
        if 0 < value < 1 and _STATE_TOKEN.search(sel) and not _STATE_MARK.search(sel)
    }
    assert blind == ledger, sorted(set(blind.items()) ^ set(ledger.items()))
    # 而它们带进来的确实是 §7.16 那四档之外的新值——这才是「得先答哪一档」的理由。
    newcomers = sorted(set(blind.values()) - {0.28, 0.34, 0.5, 0.85})
    assert newcomers == [0.32, 0.55, 0.7, 0.72, 0.9], newcomers

    line = _doc_line("### 7.23", "§7.16 那把尺子今天看不见")
    written = [int(n) for n in _DOC_BOLD_NUM.findall(line)]
    assert written == [len(ledger), len(newcomers)], (written, line)


def test_two_state_opacities_never_sit_on_one_ancestor_chain():
    """§7.16 判据二：α 会相乘，所以合法性只能在最终渲染值上判。

    这是那条判据里**唯一结构可判定**的部分——不需要知道底色，只看嵌套：一处状态
    opacity 压在另一处状态 opacity 的祖先上时，屏幕上出现的是两者的乘积，而那个
    乘积没有人说过它在说哪句话。10 个声明值两两相乘会造出 52 个新值，其中只有
    `.35` 和 `.45` 落回名单上——也就是说相乘几乎总是在造第 11 个值。

    实测的那个形状：`.tr-lane.faded{opacity:.28}` 是
    `.tr-lanes.has-open .tr-lane:not(.open) .tr-band{opacity:.34}` 的祖先，于是别的
    地貌被展开时，沉寂的泳道真正渲染出的是 **.28×.34=.0952** —— growing 带最浓处
    从 4.22:1 掉到 1.08:1（对底 ΔE 2.86，刚过可辨阈 2.3），几乎读不出它还在。修法
    是互斥（`:not(.faded)`），不是换值：换值只会把乘积挪到另一个没人选过的数上。

    `opacity:0` 不参与：0 乘任何数还是 0，「不在」乘「不在」还是「不在」。
    """
    rules = [
        (sel, value, _compounds(sel))
        for sel, value, _ in _opacity_rules()
        if 0 < value < 1 and _STATE_MARK.search(sel)
    ]
    assert len(rules) >= 8, f"状态 opacity 只认出 {len(rules)} 条 —— 先确认选择器解析还在工作"

    bad = []
    for a_sel, a_value, a_compounds in rules:
        a_positive, a_negated, _ = a_compounds[-1]
        if not a_positive:
            continue
        for b_sel, b_value, b_compounds in rules:
            if b_sel == a_sel or len(b_compounds) < 2:
                continue
            for positive, negated, raw in b_compounds[:-1]:
                if not (a_positive & positive):
                    continue  # 说的不是同一个元素
                if (a_positive & negated) or (positive & a_negated):
                    continue  # 互斥，两个 α 不会同时出现
                bad.append(
                    f"{a_sel}({a_value}) 是 {b_sel}({b_value}) 的祖先段 `{raw}`，"
                    f"屏幕上是 {a_value * b_value:.4f}"
                )
    assert bad == [], bad


def test_cannot_be_clicked_is_one_rung_written_once():
    """§7.16 判据一：档定在句子上，不定在数值上——所以「点不动」只能有一个值。

    先前它有三个（`.5` / `.45` / `.35`），而它们不是三档：在 `--text-dim` 上
    `.50↔.45` 只差 ΔE 3.23、`.45↔.35` 差 6.31，都远在「够得上一档」的 ΔE 15 之下
    （§7.11）。三个值说同一句话，就是同一档被写了三遍。

    这条守卫盯的是**句子数必须严格少于站点数**：`:disabled` 的站点会继续变多，
    值不许跟着变多。
    """
    values = {
        value
        for sel, value, _ in _opacity_rules()
        if re.search(r":disabled|\[disabled\]", sel)
    }
    sites = [sel for sel, _, _ in _opacity_rules() if re.search(r":disabled|\[disabled\]", sel)]
    assert len(sites) >= 3, f"`:disabled` 上的 opacity 只认出 {len(sites)} 处"
    assert values == {0.5}, f"「点不动」应当只有 0.5 一个值，读到 {sorted(values)}：{sites}"


# 状态通道减光的全部档，以及每一档在说哪句话。0（不在）与 1（在）不在其内——那两个
# 是这条通道的两个端点，不是档。
STATE_DIMMING_RUNGS = {
    0.85: "不是一档：`stateBreath` 的静止替身（见下一条守卫）",
    0.50: "「点不动」（`:disabled`）与「按下去了」（`:active`）共用；两句由光标与时长分开，不由 α 分开",
    0.34: "「你正在看别的一条」——瞬时的退让",
    0.28: "「这条地形沉寂了」——长驻的沉寂",
}


def test_the_state_channel_has_exactly_these_dimming_rungs():
    """§7.16 判据一：档数 = 句子数，而句子必须严格少于站点。

    审计起点是 17 处说 10 句、用 10 个值——1:1 就不是阶梯，只是一堆数字。收敛之后
    减光档剩 3 个（外加一个不是档的静止替身），铺在 22 处上。

    这条守卫不判「这个值对不对」，它判**有没有第 5 个值悄悄长出来**。新值必须先在
    这张表里领到一句话，才能进 CSS。

    实测里还剩一个没解决的：`.28`（沉寂）与 `.34`（退让）在屏幕上分不开——玫瑰带
    最浓处两者 ΔE **2.35**、最淡处 **1.15**，而可辨阈是 2.3。两句话共用一档，而它们
    恰好会同时出现在屏幕上（展开一条时，别的既有沉寂的也有只是没被展开的）。这是
    待办「沉寂与退让分不开」的题，不在这一轮里。
    """
    seen = {}
    for sel, value, _ in _opacity_rules():
        if 0 < value < 1 and _STATE_MARK.search(sel):
            seen.setdefault(value, []).append(sel)
    assert set(seen) == set(STATE_DIMMING_RUNGS), (
        f"状态减光档应当是 {sorted(STATE_DIMMING_RUNGS)}，读到 {sorted(seen)}"
    )
    sites = sum(len(v) for v in seen.values())
    assert sites > len(seen), f"{sites} 处只说 {len(seen)} 句 —— 站点数必须多于档数，否则不是阶梯"


def _cubic_bezier_y(x: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """解 x(t)=x 再取 y(t)。CSS 的 `ease-in-out` 是 cubic-bezier(.42,0,.58,1)。"""
    low, high = 0.0, 1.0
    for _ in range(60):
        t = (low + high) / 2
        if 3 * x1 * t * (1 - t) ** 2 + 3 * x2 * t**2 * (1 - t) + t**3 < x:
            low = t
        else:
            high = t
    t = (low + high) / 2
    return 3 * y1 * t * (1 - t) ** 2 + 3 * y2 * t**2 * (1 - t) + t**3


def _keyframe_opacity_mean(name: str) -> float:
    """一段 `@keyframes` 里 opacity 的**时间加权均值**（逐段按 ease-in-out 插值）。

    不是区间中点：呼吸在两端停留的时间和在中间不一样，所以中点通常偏亮。
    """
    stops = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", _keyframe_bodies()[name]):
        o = _OPACITY_VALUE.search(m.group(2))
        if not o:
            continue
        for part in m.group(1).split(","):
            part = part.strip()
            if part == "from":
                stops.append((0.0, float(o.group(1))))
            elif part == "to":
                stops.append((1.0, float(o.group(1))))
            elif part.endswith("%"):
                stops.append((float(part[:-1]) / 100, float(o.group(1))))
    stops.sort()
    assert len(stops) >= 2, f"@keyframes {name} 里读不到两个以上的 opacity 停点"
    if stops[0][0] > 0:
        stops.insert(0, (0.0, stops[0][1]))
    if stops[-1][0] < 1:
        stops.append((1.0, stops[-1][1]))

    total = 0.0
    for (t0, v0), (t1, v1) in zip(stops, stops[1:]):
        span = t1 - t0
        if span <= 0:
            continue
        steps = max(400, int(40_000 * span))
        acc = sum(
            v0 + (v1 - v0) * _cubic_bezier_y((i + 0.5) / steps, 0.42, 0.0, 0.58, 1.0)
            for i in range(steps)
        )
        total += acc / steps * span
    return total


# 已判定的「静止替身」：减弱动效时那条兜底把迭代压到 1，而这些动画都没有 `forwards`，
# 于是动画一结束元素就回到规则里声明的那个值。它必须是这段动画自己的时间加权均值——
# 静止的那一帧要和动的那一段带同样多的光。
# 全 app 13 条无限压暗动画里现在只有这一条对得上；其余归待办「减弱动效的静息值」。
RESTING_STAND_INS = {
    ".comp-state.show": "stateBreath",
}


@pytest.mark.parametrize("selector", sorted(RESTING_STAND_INS))
def test_a_resting_stand_in_carries_the_mean_of_the_breath_it_replaces(selector):
    """§7.16 判据一的延伸：静止的那一帧要和动的那一段带同样多的光。

    `.comp-state.show{opacity:0.85}` 看着像一档静息亮度，其实不是：`stateBreath` 的
    0% 与 100% 都写着 `.78`，呼吸把它整个盖住了。它只在**减弱动效**时露出来（§3 那条
    兜底把 `animation-iteration-count` 压到 1，而这条动画的 `animation-fill-mode`
    是默认的 `none`，所以动画结束后不停在末帧、而是回到声明值——浏览器实测
    computed opacity = 0.85）。

    所以这个数不该是手写的：它等于 `stateBreath` 的时间加权均值 0.8491。自然的变异
    是区间中点 (0.78+0.96)/2 = 0.870，这条守卫必须能把那个中点判红。
    """
    declared = [value for sel, value, _ in _opacity_rules() if sel == selector]
    assert len(declared) == 1, f"{selector} 上的 opacity 声明读到 {len(declared)} 条"
    mean = _keyframe_opacity_mean(RESTING_STAND_INS[selector])
    assert abs(declared[0] - mean) <= 0.005, (
        f"{selector} 声明 {declared[0]}，而 {RESTING_STAND_INS[selector]} 的时间加权"
        f"均值是 {mean:.4f}（区间中点不算——那是另一个数）"
    )


def _selector_chain(selector: str) -> list[tuple[frozenset, frozenset, str]]:
    """一条选择器切成 [(类, 伪类, 伪元素)]，最后一项是 key。

    只比 key 那一段是不够的：`.msg-user .bubble::before` 与 `.msg-ai .bubble` 的 key
    段看着像同一个元素，祖先段却是两个互斥的说话人。所以必须全链逐段比。
    """
    out = []
    for part in re.split(r"\s*>\s*|\s+", re.sub(r"\s+", " ", selector).strip()):
        if not part:
            continue
        bare = re.sub(r":not\([^)]*\)", "", part)
        element = re.findall(r"::([\w-]+)", bare)
        out.append(
            (
                frozenset(re.findall(r"\.([\w-]+)", bare)),
                frozenset(re.findall(r"(?<!:):([\w-]+)", bare)),
                element[0] if element else "",
            )
        )
    return out


def _refines(broad: str, narrow: str) -> bool:
    """broad 是不是 narrow 的**更宽**版本：同一条链、每一段的类与伪类都是子集。"""
    a, b = _selector_chain(broad), _selector_chain(narrow)
    if len(a) != len(b) or a == b:
        return False
    return all(x[0] <= y[0] and x[1] <= y[1] and x[2] == y[2] for x, y in zip(a, b))


def _contains(outer: str, inner: str) -> bool:
    """inner 是不是长在 outer 下面（容器被压暗，里面的字自己上过色）。"""
    a, b = _selector_chain(outer), _selector_chain(inner)
    if len(b) <= len(a):
        return False
    return any(
        all(x[0] and x[0] <= y[0] and x[1] <= y[1] and x[2] == y[2] for x, y in zip(a, seg))
        for seg in (b[i : i + len(a)] for i in range(len(b) - len(a) + 1))
    )


# 已知缺口：这三处按下去时把一整句字压到读不出来，而 `:active` **没有** WCAG 的豁免。
# 值倒是一致的（一句话一个值 .5，符合判据一），问题是这句话该不该由减光来说。
# 归待办「按下去了这一句该不该由减光说」，不是这一轮的题。名单在这里而不是在
# 守卫的判据里：判据不许为了让今天变绿而放宽，缺口要带着名字和归属躺着。
TEXT_DIMMED_BY_PRESS = {
    ".leb-dismiss:active": "生活回声那个 × ，15px 的字形",
    ".le-agent-fb:active": "AI 回应右侧的反馈钮，11px 一句英文",
    ".mtp-fb:active": "整句话就是按钮（另见待办：给它补可点性线索）",
}


def test_dimmed_text_that_already_has_a_colour_tier_has_a_named_reason():
    """§7.16 判据三 + §7.11 那个跨选择器的洞。

    §7.11 的两条守卫只在 `color` 与 `opacity` 落在**同一条规则体**里时才开火。可是
    色档常常写在**另一条**规则上：`.comp-state{color:var(--text-faint)}` 与
    `.comp-state.show{opacity:0.85}` 压的是同一段字，两条守卫都看不见它。这一条把
    范围扩到两个方向——色档在更宽的选择器上（继承下来），和色档在后代上（容器被
    整块压暗，`.memo-item.done{opacity:.55}` 就是这个形状）。

    被色档压过的字，它的亮度只有两个正当的第二驱动者：

    1. `:disabled` —— WCAG 2.x SC 1.4.3 明文豁免 `inactive user interface component`
       里的文字。豁免只到这一类为止：`:hover` 不在其内（光标不盖着字），
       「按压时手指盖着字所以可以不可读」是我的判断、不是 WCAG 的。
    2. 它是那段无限动画的**静止替身**（见上一条守卫）。

    其余都红：那时最终亮度是色档 × α，而这两个驱动者谁都没选过那个乘积。

    伪元素不在这条守卫里：它们绝大多数是 `content:""` 的装饰面，`color` 到不了一个
    没有字的盒子，归 #29/#49 的图形簇。
    """
    tier = re.compile(r"var\(--text(?:-dim|-faint|-ghost)?\)")
    tiers, dimmers = [], []
    for selector, body in _top_level_rules():
        colour_decl = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        opacity = _OPACITY_VALUE.search(body)
        for one in selector.split(","):
            one = re.sub(r"\s+", " ", one).strip()
            if not one:
                continue
            if colour_decl and tier.search(colour_decl.group(1)):
                tiers.append(one)
            # 自己写了 color 的不算：那时祖先的档已经被覆盖，这条规则是唯一的驱动者。
            if (
                opacity
                and 0 < float(opacity.group(1)) < 1
                and not colour_decl
                and not any(c[2] for c in _selector_chain(one))
            ):
                dimmers.append((one, float(opacity.group(1)), body))
    assert len(tiers) >= 100, f"写文字色档的规则只认出 {len(tiers)} 条"
    assert len(dimmers) >= 20, f"候选压暗规则只认出 {len(dimmers)} 条"

    bad, exempt, known = [], [], []
    for sel, value, body in dimmers:
        if not _selector_chain(sel)[-1][0]:
            continue
        found = [t for t in tiers if t != sel and _selector_chain(t)[-1][0] and _refines(t, sel)]
        found += [t for t in tiers if t != sel and _contains(sel, t)]
        if not found:
            continue
        if re.search(r":disabled|\[disabled\]", sel):
            exempt.append((sel, "WCAG 1.4.3 豁免 inactive component"))
        elif sel in RESTING_STAND_INS and re.search(r"(?<![-\w])animation\s*:", body):
            exempt.append((sel, f"静止替身 {RESTING_STAND_INS[sel]}"))
        elif sel in TEXT_DIMMED_BY_PRESS:
            known.append(sel)
        else:
            bad.append(f"{sel}(opacity {value}) 的色档写在 {found} 上，第二个驱动者没有理由")
    assert bad == [], bad
    # 正对照一：两条豁免都必须真的被走到过，否则上面那个空表是「一条都没看见」。
    assert len(exempt) >= 2, exempt
    # 正对照二：缺口名单不许烂成一串已经不存在的名字。修掉一处就要从名单里删掉它。
    assert sorted(known) == sorted(TEXT_DIMMED_BY_PRESS), (sorted(known), sorted(TEXT_DIMMED_BY_PRESS))
    # 正对照三：容器方向那半边今天没人踩到，所以拿一对合成选择器验它还认得出来。
    assert _contains(".memo-item.done", ".memo-item.done .memo-text")
    assert not _contains(".memo-item::before", ".memo-item.done .memo-text")


def test_a_colour_alpha_on_text_is_only_ever_written_by_a_state():
    """`color` 上的 alpha 只许由状态限定的选择器写。

    基线声明上的 α 是在三档之下发明第四档；而状态上的 α 是在回答「此刻发生了什么」。
    最后那处合法的状态 α（`.msg-ai.typing .bubble` 的 `rgba(var(--text-rgb),.80)`）
    在 v1.48 收进 `--text-dim`——「那口气还没成为一句话」正是 dim 那一档的语义。
    """
    bad, legal = [], []
    for sel, body in _top_level_rules():
        colour = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        if not colour:
            continue
        faded = [a for a in _rgba_alphas(colour.group(1)) if a < 1]
        if not faded:
            continue
        target = legal if _STATE_TOKEN.search(sel) else bad
        target.append((re.sub(r"\s+", " ", sel).strip(), colour.group(1).strip(), faded))
    assert bad == [], bad
    # 正对照：app 里已经一处状态 alpha 都不剩（v1.48），「解析器还认得这种拼法」
    # 不能再靠现存的实例证明。拿一句合成的 rgba(var(--x-rgb),.80) 喂给同一个
    # 解析器，它必须读出 0.80——这是 §7.7 的教训：尺子要先证明自己量得到。
    assert _rgba_alphas("rgba(var(--text-rgb),.80)") == [0.80]


def test_the_two_dimming_channels_never_multiply_in_one_rule():
    """色的 alpha 和元素的 opacity 是两条通道，乘在一起没人算得过来（§7.10）。

    上面那条 `..._by_an_opacity_animation` 管的是「α × 关键帧」，这一条管的是
    「α × 同一条规则里写死的 opacity」——同一个病的静态形态。
    """
    bad = []
    for sel, body in _top_level_rules():
        colour = re.search(r"(?<![-\w])color\s*:\s*([^;}]+)", body)
        if not colour or not [a for a in _rgba_alphas(colour.group(1)) if a < 1]:
            continue
        if _static_opacities(body):
            bad.append((re.sub(r"\s+", " ", sel).strip(), colour.group(1).strip()))
    assert bad == [], bad


def test_dimming_never_hides_in_an_inline_style():
    """减光不许躲进行内 `style=`。

    CSS-only 的审计报了绿，而详情页的分隔点是 `style="opacity:0.3"`、清空数据按钮
    是 `style="opacity:.45"` —— 都写在 HTML 里，扫 CSS 的守卫一处也看不见（§7.7）。
    """
    in_markup = [
        m.group(0)[:120]
        for m in re.finditer(r"""style\s*=\s*["'][^"']*opacity[^"']*["']""", MARKUP_NO_COMMENTS)
    ]
    from_js = [
        m.group(0)[:120]
        for m in re.finditer(r"""style\s*=\\?["'][^"'`]*opacity[^"'`]*""", SCRIPT_NO_COMMENTS)
    ]
    assert in_markup == [], in_markup
    assert from_js == [], from_js


def test_dimming_never_hides_in_a_hand_picked_js_decimal():
    """JS 写 opacity，只许写 0/1 这种开关，或从一个具名常量算出来的位置函数。

    `confirmBtn.style.opacity = ok ? '1' : '0.45'` 和 `lapImgAdd.style.opacity = '.35'`
    都是在一条本来就有 `disabled` 属性在切的通道上，又手挑了一个小数——三个驱动者
    同时说一个 opacity，最终亮度就没人算得出来。合法的只剩照片叠层那一批：
    `1 - layer * STACK_OPACITY`，一个常量生成的位置函数，不是手挑的阶梯。
    """
    bad = []
    for m in re.finditer(r"style\.opacity\s*=\s*([^;,\n)]+)", SCRIPT_NO_COMMENTS):
        rhs = m.group(1).strip()
        if re.fullmatch(r"""1|'1'|"1"|0|'0'|"0"|''|""|`1`|`0`""", rhs):
            continue
        if re.search(r"[A-Za-z_$]", rhs):  # 具名常量/变量参与运算 —— 有名有姓，可追
            continue
        bad.append(rhs)
    assert bad == [], bad


def test_an_inline_style_never_writes_a_colour_alpha_on_text():
    """行内 `style=` 也不许写 `color` 的 alpha。

    图形标签除外：`<svg style="color:rgba(var(--life-green-rgb),.5)">` 的 color 是
    喂 currentColor 给 `stroke` 的，它是图标不是字（那一处归 #29 的图标簇）。
    这个例外按标签判，不按 class 名单判——名单会跟不上，标签不会。
    """
    bad = []
    for m in re.finditer(r"""<([A-Za-z][\w-]*)\b[^>]*?style\s*=\s*"([^"]*)\"""", MARKUP_NO_COMMENTS):
        tag, style = m.group(1).lower(), m.group(2)
        if tag in _GRAPHIC_TAGS:
            continue
        colour = re.search(r"""(?<![-\w])color\s*:\s*([^;"]+)""", style)
        if colour and [a for a in _rgba_alphas(colour.group(1)) if a < 1]:
            bad.append((tag, colour.group(1).strip()))
    assert bad == [], bad


def test_one_selector_declares_opacity_at_most_once():
    """一条通道上只允许一个驱动者。

    同一条规则里写两遍 opacity，后一遍会静静吃掉前一遍：读代码的人看到的是第一个
    数，浏览器算的是第二个。这就是「最终亮度算不出来」最便宜的一种形态。
    """
    bad = []
    for sel, body in _top_level_rules():
        declared = _OPACITY_VALUE.findall(body)
        if len(declared) > 1:
            bad.append((re.sub(r"\s+", " ", sel).strip(), declared))
    assert bad == [], bad


# 状态限定本身（把它从选择器上剥下来，剩下的就是「主体」）。尺子只有一把，见 `_STATE_TOKEN`。


def _subject(part: str) -> str:
    """一条选择器分支的「主体」：剥掉状态限定，取最后那一个复合选择器。

    `.life-entry:hover .le-delete` → `.le-delete`
    `.le-img-grid .gi:active::after` → `.gi::after`（伪元素留着，它是另一个主体）
    """
    return _STATE_TOKEN.sub("", re.sub(r"\s+", " ", part).strip()).strip().split(" ")[-1]


def test_a_state_rule_that_restores_full_opacity_has_something_to_restore():
    """`:active{opacity:1}` 只有在基线真的把它压暗过时才是一句话。

    这一轮把基线上的静态减光删干净之后，那些「按下去恢复满强度」的状态规则集体
    变成了空话——而它们从 G1–G6 旁边走过去了：opacity 归状态是合法的，1 又不是
    一个减光值，于是没有一条守卫看得见「这条声明一天都不会生效」（§7.8 的形状，
    这次出现在状态通道上）。

    代价不只是死文档。`.leb-dismiss` 上写着 `-webkit-tap-highlight-color:transparent`，
    平台默认的按压反馈被关掉了，自己那条又不生效，于是在手机上按那个 × 一点回应
    都没有——而 `cursor:pointer` 在触屏上不存在。
    """
    rules = _top_level_rules()
    # 谁在基线上真的声明过 opacity（不分值，0 和 1 都算：0 也是一个对手）
    dimmed = set()
    for sel, body in rules:
        if _STATE_TOKEN.search(sel):
            continue
        if not re.search(r"(?<![-\w])opacity\s*:", body):
            continue
        dimmed.update(_subject(p) for p in sel.split(","))
    assert len(dimmed) > 20, sorted(dimmed)  # 仪器校验：索引不能是空的

    bad, legal = [], []
    for sel, body in rules:
        if not _STATE_TOKEN.search(sel):
            continue
        if not any(float(v) == 1.0 for v in _OPACITY_VALUE.findall(body)):
            continue
        flat = re.sub(r"\s+", " ", sel).strip()
        if all(_subject(p) not in dimmed for p in sel.split(",")):
            bad.append(flat)
        else:
            legal.append(flat)
    assert bad == [], bad
    # 正对照：合法的那一族必须真的在（`.le-delete`/`.lap-mask`/`.screen` 这些基线 0
    # → 状态 1 的开关）。一条都认不出来，说明主体解析已经坏了，上面那个空表没意义。
    assert len(legal) > 8, legal


# --- #28 / §7.12：`!important` 是在说「我打赢了一个作者对手」-------------------
#
# 判据三级，缺一级就不合格：
#   1. 对手必须是作者来源的。UA 样式表永远不是对手 —— 级联的**来源优先级**里，
#      普通作者声明本来就压过普通 UA 声明。「为了压掉按钮的 ButtonFace 而写
#      `!important`」是把来源优先级和特异度搞混了：那 14 处里有 11 处是这个病。
#   2. 对手存在，也不一定该打赢它。若对手来自**另一个互斥的状态同时挂在同一个
#      元素上**，正确的动作是让对手不存在（修状态机）。而且 `!important` 只压得住
#      它列举到的那几条声明 —— 同一条对手规则里没被列举的会继续生效。
#   3. 真的该打赢的，用特异度说，不用 `!important` 说。
#
# 全文只有一处过得了三级：`prefers-reduced-motion` 里的 `*`。它特异度 0，要压住的是
# 散在两百多条规则里的每一条动画声明，特异度赢不了；对手也不是某一个状态，是全体。

_REDUCED_MOTION_TRIO = {
    "animation-duration:.001ms",
    "animation-iteration-count:1",
    "transition-duration:.001ms",
}


def _important_sites(src: str = APP_HTML) -> list[tuple[int, str]]:
    """(行号, 声明) —— 全文每一处 `!important`。

    三条通道都要看（§7.7 守卫的覆盖面必须和它守的律一样宽）：CSS 规则体、行内
    `style=`、以及 JS 的 `setProperty(prop, value, 'important')`。只扫 CSS 的正则
    会把后两条整片报成绿。

    注释先剥掉 —— 注释正是解释「为什么把它删了」的地方，那里必然会写出被删掉的
    原文。但剥的时候要保留行数，否则报出来的位置回查不到。
    """

    blanked = re.sub(
        r"/\*.*?\*/|<!--.*?-->",
        lambda m: re.sub(r"[^\n]", "", m.group(0)),
        src,
        flags=re.S,
    )
    # 只剥整行的 `//` 注释：`https://` 里的双斜杠前面有非空白字符，不会被咬到。
    blanked = re.sub(r"^([ \t]*)//.*$", lambda m: m.group(1), blanked, flags=re.M)
    out = []
    for m in re.finditer(r"!\s*important", blanked, re.I):
        head = blanked[max(0, m.start() - 90) : m.start()]
        decl = re.sub(r"\s+", "", head.rsplit(";", 1)[-1].rsplit("{", 1)[-1])
        out.append((blanked[: m.start()].count("\n") + 1, decl))
    return out


def test_the_important_instrument_can_see_all_three_channels():
    """先证明尺子量得到，再相信它报的零。

    下面那条律是「不存在」型断言，而一个只认 CSS 规则体的正则会把行内 `style=`
    与 JS 的 `setProperty` 两条通道整片报成绿。所以先拿三份合成样本喂它。
    """
    css_like = "  .x{color:red !important}\n"
    markup_like = '  <div style="color:red!important"></div>\n'
    for sample in (css_like, markup_like):
        assert len(_important_sites(sample)) == 1, sample
    # 第三条通道长得不一样：`setProperty` 的第三参数不带感叹号，上面那把尺子看不见
    # 它，所以它必须有自己的正则 —— 一条律漏掉一条通道，等于这条律没写（§7.7）。
    js_like = "  el.style.setProperty('color','red','important');\n"
    assert _important_sites(js_like) == [], "感叹号那把尺子不该认得 setProperty"
    assert re.search(r"setProperty\([^)]*,\s*['\"]important['\"]\s*\)", js_like)
    # 而且注释必须真的被剥掉：app.html 里现在就有几处注释在讲被删掉的 `!important`。
    assert "!important" in APP_HTML
    assert APP_HTML.count("!important") > len(_important_sites())


def test_the_only_important_left_is_the_one_that_cannot_win_by_specificity():
    """全文只剩 `prefers-reduced-motion` 里那三条，而且必须在那个查询里面。

    这一条同时是这台尺子的**正对照**：它钉的不是「有没有缺陷」，而是「仪器今天
    还看得见东西」。三条一起消失（比如有人把整个查询删了），断言会说话；
    多出第四处，断言也会说话。
    """
    sites = _important_sites()
    assert {_by_quantity(decl) for _, decl in sites} == {
        _by_quantity(decl) for decl in _REDUCED_MOTION_TRIO
    }, sites

    # 位置也要对：同样三条声明写在顶层，是另一件事（它会把所有人的动画都停掉）。
    block = re.search(r"@media[^{]*prefers-reduced-motion[^{]*\{", CSS)
    assert block, "prefers-reduced-motion 查询不见了"
    depth, i = 1, block.end()
    while depth and i < len(CSS):
        depth += (CSS[i] == "{") - (CSS[i] == "}")
        i += 1
    inside = CSS[block.start() : i]
    for _, decl in sites:
        assert re.sub(r"\s+", "", inside).count(decl + "!important") == 1, decl

    # JS 也不许绕过去：`setProperty(...,'important')` 一处都不许有。
    assert re.findall(r"setProperty\([^)]*,\s*['\"]important['\"]\s*\)", SCRIPT_NO_COMMENTS) == []


# 一条选择器分支里，最后那一段复合选择器的简单选择器集合。
_SIMPLE_SELECTOR = re.compile(
    r"#[\w-]+|\.[\w-]+|\[[^\]]+\]|^[a-zA-Z][\w-]*|(?<=[\s>+~])[a-zA-Z][\w-]*"
)


def _pseudo_branches() -> list[tuple[str, str, frozenset[str], bool]]:
    """每一条点到 `::before`/`::after` 的选择器分支 → (原文, 伪元素, 主体集合, 写了 content)。"""

    out = []
    for sel, body in _top_level_rules():
        writes_content = bool(re.search(r"(?<![-\w])content\s*:", body))
        for part in sel.split(","):
            part = re.sub(r"\s+", " ", part).strip()
            pseudo = re.search(r"::(before|after)", part)
            if not pseudo:
                continue
            last = re.split(r"[\s>+~]+", part)[-1].split("::")[0]
            out.append(
                (part, pseudo.group(1), frozenset(_SIMPLE_SELECTOR.findall(last)), writes_content)
            )
    return out


def test_a_pseudo_element_rule_only_exists_if_someone_wrote_content():
    """`content` 是伪元素的开关。没人写过它，那个盒子从来没被生成过。

    `.tab::before, .tab::after` 上曾经有五条 `!important`，封杀「任何伪元素产生的
    方块底」—— 而全文没有第二条规则给 `.tab::before/::after` 写过 `content`。
    对一个不存在的盒子立五条法，是对空气立法。（浏览器实测：整条规则删掉后，
    宿主 `.tab` 的几何 78x54 一点没动。）

    判据不是一张名单，是一条可判定的关系：一条分支合法，当且仅当存在另一条**主体
    更宽松**的同名伪元素分支写了 `content`。状态与语境限定（`.gi.live-off::after`、
    `body.companion-expanded #screen-companion::before`）因此自动被基线那条放过去，
    而不需要我逐个记住它们的名字。
    """
    branches = _pseudo_branches()
    givers = [(ps, subj) for _, ps, subj, writes in branches if writes]
    assert len(branches) > 40, len(branches)  # 仪器校验：选择器解析不能是空的
    assert len(givers) > 20, len(givers)

    orphans = sorted(
        {
            part
            for part, ps, subj, _ in branches
            if not any(ps2 == ps and subj2 <= subj for ps2, subj2 in givers)
        }
    )
    assert orphans == [], orphans


def _navbar_rule(selector: str) -> str:
    hits = [b for s, b in _top_level_rules() if re.sub(r"\s+", "", s) == selector]
    assert len(hits) == 1, f"{selector}: 期望恰好一条顶层规则，实际 {len(hits)}"
    return hits[0]


def test_the_navbar_cancels_only_what_is_actually_there():
    """否定式声明（`0`/`none`/`transparent`）的资格判据：它取消的东西在渲染上存在吗。

    这条律有两个方向，都要守，否则下一轮清理会从一边跌到另一边：

    **不许写空话。** `.tabbar` 是个 `<div>`，没有原生外观 —— `border-style` 的初始值
    本来就是 `none`。它那条 `border:0 !important` 的对手（附录里一条重复画顶边线的
    `border-top`）在上一轮被删掉之后，它就成了空话。

    **不许把承重的一起删掉。** `.tab` 是个 `<button>`，浏览器里量出来裸按钮是
    `background-color:rgb(240,240,240)` 的浅灰 ButtonFace、`border:2px outset`。
    这两条不写，深色玻璃条上会浮出四个系统按钮。而 `appearance:none` **不**会把
    ButtonFace 拿掉 —— 这一点是实测的，不是推的。

    （这一轮差点在这里出事：CSSOM 把声明 `removeProperty` 之后，**已经在页面上的
    元素不重算 UA 那一层的值**，探针于是把这两条报成了死声明。换成「在同一个父节点
    里新建一个同类元素再问」，答案立刻反过来。仪器的假阴性比缺陷贵。）

    §7.14 之后这条守卫换了个方向：那两条承重声明仍然必须存在，但**不在 `.tab` 上**。
    ButtonFace 与 outset 是所有 button 的问题，写在 tab 上就是把一条通律
    存放在四个按钮的名字里——正是名字表会烂的那个机制。所以现在要求的是
    「`.tab` 上没有它们，而归零层有」。
    """
    tabbar = re.sub(r"\s+", "", _navbar_rule(".tabbar"))
    assert "border:0" not in tabbar, tabbar
    assert "border-style:none" not in tabbar, tabbar

    tab = re.sub(r"\s+", "", _navbar_rule(".tab"))
    assert "background:transparent" not in tab, "归零层已经说过了，别在 tab 上再说一遍"
    assert "border:0" not in tab, tab
    floor = "".join(re.sub(r"\s+", "", b) for _s, b in _floor_rules())
    assert "background:transparent" in floor and "border:0" in floor, floor
    # 而空话不许回来：裸 `<button>` 这三个值本来就是它的初始值。
    for empty in ("border-radius:0", "box-shadow:none", "background-image:none"):
        assert empty not in tab, empty


def _says_unavailable(sel: str) -> bool:
    """这条规则是不是**在失败态上**说话。

    `:not(.media-unavailable)` 说的是它的**反面**——正常态那一条（`.dv-voice` 的
    hover 就写成这样，因为一行取不回来的语音不该再有 hover 反馈）。用 `in sel`
    去认，会把正常态那一条当成失败态那一条，于是「失败态不许画底」这条守卫会咬住
    一条其实只画在**没失败**时的底。
    """

    return "media-unavailable" in re.sub(r":not\([^()]*\)", "", sel)


def test_a_media_node_is_never_loading_and_unavailable_at_once():
    """加载态与失败态互斥。这件事要在状态机上成立，不在级联上摆平（判据第 2 级）。

    先前失败那条路忘了摘 `media-loading`，两个类同挂在一个元素上，CSS 只好用
    `!important` 去压加载态的底色 —— 而它只压得住被列举的 `background-image` 与
    `background-color`，同一条对手规则里的 `background-size:200% 200%` 与
    `animation:mediaShimmer 1.8s ease-in-out infinite` 压不住。于是一张**取不回来**
    的照片，永远在放**正在加载**的微光。这不是「红色不够显眼」，是状态说错了。
    """
    # CSS 侧：不许有任何一条规则去补偿「两个互斥类同挂」这件事。
    both = [
        re.sub(r"\s+", " ", sel).strip()
        for sel, _ in _top_level_rules()
        if "media-loading" in sel and "media-unavailable" in sel
    ]
    assert both == [], both

    # JS 侧：每一处挂上失败态之前，必须先摘掉加载态。这个类现在只由说出失败的
    # 那个函数挂（它和那句话说的是同一件事，所以挂在同一个元素上），于是要看的
    # 不再是 `classList.add` 那一行的上文，而是**它的调用点**的上文。
    speak = re.search(r"function speakMediaUnavailable\(node\)\{(.*?)\n\}", SCRIPT_NO_COMMENTS, re.S)
    assert speak, "speakMediaUnavailable 不在了"
    adds = list(re.finditer(r"classList\.add\(\s*['\"]media-unavailable['\"]", SCRIPT_NO_COMMENTS))
    assert len(adds) == 1, [m.start() for m in adds]  # 挂上失败态的地方只许有一处
    assert speak.start() < adds[0].start() < speak.end(), "挂它的那一行不在说出失败的那个函数里"
    calls = [m.start() for m in re.finditer(r"(?<!function )speakMediaUnavailable\(", SCRIPT_NO_COMMENTS)]
    assert calls, "一处都没找到 —— 先确认失败态还在被说出来"
    for start in calls:
        window = SCRIPT_NO_COMMENTS[max(0, start - 600) : start]
        assert re.search(r"classList\.remove\(\s*['\"]media-loading['\"]", window), (
            "说出失败之前没摘加载态：" + re.sub(r"\s+", " ", window[-160:])
        )


def test_the_failure_reaches_every_host_the_shimmer_reaches():
    """覆盖面这条法留着，尺子换了：失败态现在不是一片底，是一个由 JS 挂上去的身体。

    原先这里守的是**特异度**。加载态是宿主限定的（`.le-img-grid .gi` / `.lap-img-item`
    / `.dv-card`），而失败态是个裸 `.media-unavailable`——(0,1,0) 打不过 (0,2,0)，所以
    当年补了两个 `!important`。#36 把那片洗色整个删了（§7.20：七档里没有一档能用），
    于是「靠特异度赢」这个问题连同它的对手一起不存在了。

    **但那条法不是为洗色立的**：一个能亮起微光的宿主就一定能取不回来，于是它必须也能
    说出取不回来。所以覆盖面照旧要比，只是右边那一半从「哪些选择器画了这片底」换成
    「哪些宿主会长出那个身体」。#37 把身体铺到八个宿主、#38 把微光铺到四个，于是今天
    是 **9 ⊇ 4**（左边九行折成八个宿主，气泡两种身份色算两行）。这两个数不必相等，那个
    差由 §7.22 判据一算出来：失败是终局，八个宿主都到得了；等待是过程，只有「这块地上
    没有属于这份媒体的第二个身体」的宿主才需要说它。
    """
    def hosts(cls: str) -> set[str]:
        found = set()
        for sel, body in _top_level_rules():
            if "background" not in body:
                continue
            for part in sel.split(","):
                part = re.sub(r"\s+", " ", part).strip()
                if f".{cls}" not in re.sub(r":not\([^()]*\)", "", part):
                    continue
                found.add(part.replace(f".{cls}", ""))
        return found

    loading = hosts("media-loading")
    assert loading, "加载态一个宿主都没认出来"
    speaking = {host for host, _ in _MEDIA_FAILURE_HOSTS}
    assert loading <= speaking, sorted(loading - speaking)
    # 而且那片洗色不许悄悄回来——它一回来，上面这个比较就换了对象。
    assert hosts("media-unavailable") == set(), sorted(hosts("media-unavailable"))


# --- #29：面与线的不透明度七档（§7.13）------------------------------------

LADDER = {
    "--o-1": ".04",
    "--o-2": ".08",
    "--o-3": ".16",
    "--o-4": ".30",
    "--o-5": ".45",
    "--o-6": ".72",
    "--o-7": ".95",
}

_TINT = re.compile(r"^(background|background-color|background-image)$")
_EDGE = re.compile(r"^(border|border-[a-z]+|border-[a-z]+-color|outline)$")


def _strip_gradients(value: str) -> str:
    """去掉所有 `*-gradient(...)` 的平衡片段，剩下的是这条声明里的平面层。

    渐变**底下**那一层平面仍然是一档。先前 3 处正是靠躲在渐变后面绕过了收敛
    （`.comp-state` 的 .03、`.life-capture-cta` base/active 的 .58/.62）——
    一条只看「值里有没有 gradient(」就整条放行的规则，又是 §7.7 那个形状。
    """
    out, i = "", 0
    while i < len(value):
        m = re.compile(r"[-a-z]*gradient\(").match(value, i)
        if not m:
            out += value[i]
            i += 1
            continue
        depth, j = 0, m.end() - 1
        while j < len(value):
            if value[j] == "(":
                depth += 1
            elif value[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return out


def _rgba_last_args(value: str) -> list[str]:
    """每个 `rgba(...)` 的最后一个顶层参数，**原样**返回。

    `_rgba_alphas` 返回 float，于是 `var(--o-5)` 会被静静丢掉——档位化之后，
    那个按数字白名单查纱的守卫就变成了一条恒真的规则（本轮改完基线一个都没红，
    正是这个原因）。这里保留字面量，才能分辨「写了一档」和「写了一个数」。
    """
    out: list[str] = []
    for m in re.finditer(r"rgba\(", value):
        depth, i = 1, m.end()
        while i < len(value) and depth:
            depth += (value[i] == "(") - (value[i] == ")")
            i += 1
        args, depth, cur = [], 0, ""
        for ch in value[m.end() : i - 1]:
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                args.append(cur)
                cur = ""
            else:
                cur += ch
        args.append(cur)
        out.append(args[-1].strip())
    return out


def _face_and_line_declarations() -> list[tuple[str, str, str]]:
    """(选择器, 属性, 值)，面与线，跳过 @keyframes。

    @media 里的也要收——`@media(min-width:480px)` 里 `.phone` 的边框一样是一条线，
    而 `_top_level_rules()` 按定义只走顶层。@keyframes 的 0%/50%/100% 是时间上
    的斜坡，和渐变的色标同一类东西，归 #32。
    """
    css = CSS_NO_COMMENTS
    out: list[tuple[str, str, str]] = []
    stack: list[str] = []
    buf, depth, quote, i = "", 0, "", 0
    while i < len(css):
        ch = css[i]
        if quote:
            buf += ch
            if ch == quote and css[i - 1] != "\\":
                quote = ""
            i += 1
            continue
        if ch in "\"'":
            quote, buf = ch, buf + ch
            i += 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if depth == 0 and ch == "{":
            stack.append(re.sub(r"\s+", " ", buf).strip())
            buf = ""
        elif depth == 0 and ch in "};":
            prop, sep, val = buf.partition(":")
            if sep and buf.strip():
                prop = prop.strip()
                inside_keyframes = any(s.startswith("@keyframes") for s in stack)
                if (_TINT.match(prop) or _EDGE.match(prop)) and not inside_keyframes:
                    out.append((stack[-1] if stack else "", prop, val.strip()))
            if ch == "}" and stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
        i += 1
    return out


def _rungs_on(selector: str, prop_pattern: str) -> set[str]:
    """某个选择器上，某类属性用到的档位名。"""
    want = re.compile(prop_pattern)
    found: set[str] = set()
    for sel, prop, val in _face_and_line_declarations():
        if sel != selector or not want.match(prop):
            continue
        found.update(_O_TOKEN_REF.findall(val))
    return found


def test_the_ladder_has_exactly_seven_rungs_with_the_documented_values():
    """七档，值逐字写死，不许长出第八档。

    档数不是口味：相邻档在各自真正被用的底上都必须在可辨阈之上（ΔE2000 白压抬面
    3.00 / 6.08 / 11.25 / 13.68 / 19.95 / 12.64，JND 2.3）。收敛前这两个几何上
    写了 51 个值 / 278 处，其中 27 个值离最近的档在阈下——它们从来就不是独立的档。

    第一版梯子的第 5 档取 .55，在 .30 与 .55 之间留了个洞，49 处（.38/.40/.42/
    .45/.48）掉在洞里、代价 ΔE 7–11；把第 5 档挪到 .45，洞就没了。
    这条测试守的是「别再挪回去」。
    """
    root = re.search(r":root\s*\{(.*?)\n\s*\}", CSS_NO_COMMENTS, re.S)
    assert root, ":root 没找到"
    got = {k: _by_quantity(v.strip()) for k, v in re.findall(r"(--o-\d+)\s*:\s*([^;]+);", root.group(1))}
    assert got == {k: _by_quantity(v) for k, v in LADDER.items()}, got


def test_a_face_or_a_line_names_a_rung_instead_of_a_number():
    """面与线上的单值 alpha 必须写成 `var(--o-N)`。

    判据（§7.13）：一条声明里**只有一个** alpha 才是「一档」；有两个以上的是同一块
    材料内部的位置（渐变的色标、多层阴影的层），交换它们画面在物理上就不成立了。
    这个检验是可判定的，所以渐变和多层阴影不需要一张要维护的例外名单。

    渐变**底下**那层平面另算：它是平面，仍然是一档（`_strip_gradients` 之后再查）。

    纯黑的面另算（判据七，§6.1）：`--o-N` 是**白**的价钱，黑不配——黑纱压上近黑
    地色几乎无处可去（整条 alpha 通道只值 4 档 / 2 档）。所以黑面的 alpha 手写
    （三个删除圆键 `.le-delete` / `.memo-delete` / `.lap-img-item .mi-remove` 的
    `rgba(0,0,0,.72)`），不许冒充白纱的档；「黑的面用 `var(--o-N)`」由另一条守卫
    直接禁。
    """
    offenders = []
    for sel, prop, val in _face_and_line_declarations():
        if "rgba(0,0,0" in val:
            continue
        has_gradient = "gradient(" in val
        args = _rgba_last_args(_strip_gradients(val) if has_gradient else val)
        if len(args) != 1:
            continue
        if _PLAIN_NUMBER.fullmatch(args[0]) and float(args[0]) not in (0.0, 1.0):
            offenders.append((sel, prop, args[0]))
    assert offenders == [], offenders


def test_black_never_names_a_rung():
    """黑的 alpha 不许写 `var(--o-N)`（判据七：`--o-N` 是白的价钱）。

    判据七的禁令原先只覆盖「光的 alpha」，而 `--o-N` 只承诺 alpha、不承诺一档这句
    话对**黑的面与线**同样成立——黑纱压近黑地色整条通道只值 4 档 / 2 档，把
    `rgba(0,0,0,var(--o-6))` 写成「第 6 档」是把黑的 alpha 冒充白纱的档。三个删除
    圆键是第一实例（v1.45 改手写 `.72`）。这一条守「第二处不再出现」。
    """

    assert not re.findall(r"rgba\(0,\s*0,\s*0,\s*var\(--o-\d+\)", CSS_NO_COMMENTS)


def test_two_or_more_alphas_in_one_declaration_always_means_a_gradient():
    """「多个 alpha = 斜坡」不是断言，是这个文件里的事实。

    上一条放行了「一条声明里有 ≥2 个 alpha」的情形。如果哪天出现一条**没有渐变**却
    写了两个 alpha 的面/线声明，那条放行就成了漏洞——多层平面色叠在一起，每一层
    都是一档，一个都没被查。所以把这个前提本身钉住。
    """
    leaks = [
        (sel, prop, val[:70])
        for sel, prop, val in _face_and_line_declarations()
        if len(_rgba_last_args(val)) >= 2 and "gradient(" not in val
    ]
    assert leaks == [], leaks


def test_the_glass_tokens_come_from_the_ladder():
    """两个玻璃 token 自己也走梯子，否则它们的引用整片脱轨。

    `--glass-border` 原先是 .12，正好卡在第 2、3 档中间，作者自己的值问不出答案；
    用文件自己的事实定：72 条同时写了面与线的规则，线/面比值中位数 3.46，
    而这块面（`--bg-card`）是第 1 档，3.46 × .04 = .138 → 第 3 档。
    """
    expect = {"--bg-card": "--o-1", "--glass-border": "--o-3"}
    for token, rung in expect.items():
        m = re.search(rf"{token}\s*:\s*([^;]+);", CSS_NO_COMMENTS)
        assert m, token
        assert m.group(1).strip() == f"rgba(255,255,255,var({rung}))", (token, m.group(1))


def test_a_seam_is_always_the_first_rung():
    """缝——同一块材料内部换段了——是第 1 档，十处一个值。

    原先写了 .04/.05/.055/.06/.07 五个值，而这五个值彼此**全部**在可辨阈之下
    （.02↔.05 ΔE 2.26、.06↔.08 ΔE 1.50），从来就是同一档写了五遍。
    """
    seams = [
        ".topbar", ".we-control-list", ".we-control-row", ".mtp-source",
        ".lap-advanced", ".dv-boundary", ".dv-boundary-row", "[data-degraded]",
        ".lap-private-toggle", ".mtp-compose",
    ]
    for sel in seams:
        got = _rungs_on(sel, r"^border-(top|bottom)$")
        assert got == {"--o-1"}, (sel, got)


def test_the_three_views_of_one_recording_share_one_rung():
    """一段录音在长卷、采集、详情里是同一段录音，条也就是同一个条。

    原先条是 .3/.38/.5、点亮是 .72/.72/.78。§7.5 早就判过三个视图是同一段录音
    （#21 把随机 `--h` 换成了固定图案，三处共用一份形状），可没有守卫管到 alpha，
    于是同一个条在三个视图里亮度差 ΔE 18.8。

    落在第 5 档而不是第 4 档：三个值里有两个（.38/.5）离第 5 档更近——作者自己写的
    多数意见指向那里；而且第 5 档的最坏代价（13.68）比第 4 档的（18.84）小。
    """
    for view in ("le", "lap", "dv"):
        assert _rungs_on(f".{view}-voice-wave span", r"^background$") == {"--o-5"}, view
        assert _rungs_on(f".{view}-voice-wave span.lit", r"^background$") == {"--o-6"}, view


def test_a_scrim_that_carries_text_over_a_photo_is_the_sixth_rung():
    """压在照片上的幕，只要上面还要放字，就是第 6 档——这是能过 4.5:1 的最低一档。

    亮照片（230 灰）上：第 6 档 `--text` 8.18 / `--text-dim` 4.55；
    第 5 档只有 3.31 / 1.84。作者原先写的 .4/.5/.55 里有三个本来就不合格
    （纯黑 .4 → 2.96、.5 → 4.07；纱 .55 → 4.55 只够 `--text` 一档用）。

    七处分两族：四个标签幕用纱，三个圆键用纯黑。**圆键不是幕**（v1.45 判定：
    它们是黑材料，不是压照片的纱——`--o-N` 是白的价钱，黑不配（判据七）；而且
    白纱需要深字，而深字在文字阶梯外，--ink 只给那张纸）。所以圆键从这张名单
    拆出去，alpha 手写（黑只值 4 档 / 2 档，值本身归黑材料的档那一笔账）。
    """
    scrims = {
        ".le-img-grid .gi.live-photo::before,.le-img-band .gi.live-photo::before",
        ".le-img-grid .gi.live-photo::after,.le-img-band .gi.live-photo::after",
        ".lap-img-item.live-part::after",
        ".dv-card.live-off::before",
    }
    black_keys = {".le-delete", ".memo-delete", ".lap-img-item .mi-remove"}
    seen = set()
    for sel, prop, val in _face_and_line_declarations():
        if prop != "background":
            continue
        if sel in scrims:
            seen.add(sel)
            assert _O_TOKEN_REF.findall(val) == ["--o-6"], (sel, val)
        elif sel in black_keys:
            seen.add(sel)
            assert re.fullmatch(r"rgba\(0,0,0,\d*\.\d+\)", val), (sel, val)
    assert seen == scrims | black_keys, sorted((scrims | black_keys) - seen)


def test_every_page_veil_is_the_sixth_rung_except_the_one_you_walked_into():
    """七个页幕在第 6 档，只有 `.dv-mask` 在第 7 档。

    原先以为纱有三档（.55 贴在内容上 / .7 整页退到后面 / .95 整页走了），
    可 .55 那一档**一个页幕都没有**——它的全部成员是标签幕（上一条）。
    纱真正回答的问题只有两个：你还会回来（第 6 档），你已经进去了（第 7 档）。

    这条同时替掉了先前那个按数字白名单查纱的守卫：档位化之后它一个 alpha 都读不到，
    变成了恒真——一个守着空气的守卫比没有守卫更坏，因为它让人以为这里查过了。
    """
    veils: dict[str, list[str]] = {}
    for sel, prop, val in _face_and_line_declarations():
        if "--bg-veil-rgb" not in val or not _TINT.match(prop) or "gradient(" in val:
            continue
        if "mask" not in sel:
            continue
        veils[sel] = _O_TOKEN_REF.findall(val)
    assert len(veils) == 7, sorted(veils)
    assert veils.pop(".dv-mask", None) == ["--o-7"], "详情页那层幕是唯一一个「你已经进去了」"
    for sel, rungs in veils.items():
        assert rungs == ["--o-6"], (sel, rungs)



# ============ §7.14 UA 默认值不是一个人选过的值 ============
# 一个原生控件在屏幕上的样子有六条通道：color / background / border / font /
# appearance / color-scheme。任何一条没被这个文件说过，说的就是浏览器——
# 而浏览器的默认值是替**所有**网页选的，不是替这一个近黑的房间选的。
# 审计时的事实：39 个元素带着 UA 痕迹，其中 2 个按钮渲染出 rgb(240,240,240) 的
# buttonface，2 个圆键带着 2px outset 的环（吃在 30px/24px 的圆里），16 个控件
# 渲染成 Arial，未勾选的 checkbox 是一个纯白方块——19.28:1，比这个 app 最亮的
# 文字（--text 16.54:1）还响。
#
# 归零按标签走，不按名字走。先前那份名字表列了 14 个类，漏掉 26 个按钮；
# 漏掉没被看见，只因为每个新按钮恰好把文字包在一个有颜色的子元素里（§7.2）。

# UA 给了自己一套画法或调色板的标签。option/optgroup/meter/progress/fieldset/
# legend 这个文件里一个都没有——但它们在这张表里，因为下一个人写下第一个时，
# 下面那条覆盖面检查要立刻红，逼他做一次判断，而不是静静继承浏览器的选择。
_UA_DRAWN_TAGS = {
    "button", "input", "textarea", "select", "summary",
    "option", "optgroup", "meter", "progress", "fieldset", "legend",
}
# 归零值的全部词汇：回到一张白纸。这里不许出现 var()/十六进制/rgb()——
# 一旦出现，归零层就变成了「又一个人在这里选了一次」，而它的位置在所有
# 组件规则之前、特异度之下，谁都覆盖不到它的选择，那就是第二个答案。
_BLANK_SHEET = {"inherit", "transparent", "0", "0px", "none", "currentcolor"}


def _native_control_sites() -> dict[str, int]:
    """标记里和 JS 模板串里，每个原生标签各出现几次。"""
    sites: dict[str, int] = {}
    for src in (MARKUP_NO_COMMENTS, SCRIPT_NO_COMMENTS):
        for tag in re.findall(r"<([a-z]+)(?=[\s/>])", src):
            if tag in _UA_DRAWN_TAGS:
                sites[tag] = sites.get(tag, 0) + 1
    return sites


def _floor_rules() -> list[tuple[str, str]]:
    """归零层那几条：选择器里**只有**原生标签，没有类名也没有 id。"""
    out: list[tuple[str, str]] = []
    for sel_text, body in _top_level_rules():
        parts = [re.sub(r"\s+", " ", s).strip() for s in sel_text.split(",")]
        if not parts or any(not p for p in parts):
            continue
        bare = [re.sub(r"(:{1,2}[a-z-]+|\[[^\]]*\]|\([^)]*\))", "", p).strip() for p in parts]
        if all(b in _UA_DRAWN_TAGS for b in bare):
            out.append((sel_text, body))
    return out


def _decls(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in body.split(";"):
        prop, sep, val = line.partition(":")
        if sep and prop.strip():
            out[prop.strip()] = val.strip()
    return out


def test_the_floor_instrument_can_see_every_native_control_site():
    """先证明尺子不是瞎的：它必须在标记和 JS 两处都数到控件。

    这个文件有 77 个 button（51 在标记里、26 在 JS 模板串里）、12 个 input、
    5 个 textarea、2 个 summary。JS 那 26 个是关键：只扫标记的尺子会以为
    按钮只有 51 个，而恰恰是动态那批最容易漏掉归零。
    """
    sites = _native_control_sites()
    assert set(sites) == {"button", "input", "textarea", "summary"}, sites
    assert sites["button"] >= 70, sites
    assert sites["input"] >= 10 and sites["textarea"] >= 5 and sites["summary"] == 2, sites
    assert re.search(r"<button[^>]*id=\"momentThreadClose\"", MARKUP_NO_COMMENTS)
    assert "<button" in SCRIPT_NO_COMMENTS, "JS 模板串里那批按钮必须被数到"


def test_the_zeroing_floor_is_keyed_to_tags_not_to_a_list_of_names():
    """归零层必须按标签走。

    名字表会烂，而且烂得没有声音：先前 `.icon-btn,…,.tab` 那 14 个名字漏掉了
    26 个按钮，屏幕上看不出来，因为漏掉的那些恰好把文字包在有颜色的子元素里。
    按标签走的归零层没有「漏掉」这个状态——它的覆盖面就是标签本身。
    """
    floor = _floor_rules()
    assert floor, "归零层不见了"
    for sel_text, _body in floor:
        assert "." not in sel_text and "#" not in sel_text, sel_text
    # 判据不是「归零层长什么样」，是「谁在做归零层的活」：一条规则如果整条都在
    # 那六条 UA 通道上说「回到白纸」，它就是一层归零，而归零必须按标签走。
    # 这样写才拦得住把 `button,input,textarea,select` 换回 `.icon-btn,.tab` ——
    # 只检查「归零层里没有类名」的守卫，遇到这种替换会安静地放行（它眼里归零层
    # 直接消失了，剩下的都干净）。那正是 §7.7 那个形状。
    channels = {"font", "color", "background", "background-color",
                "border", "appearance", "-webkit-appearance"}
    # appearance 这一条通道只有一个合法的落点。别的通道上「取消」有时是局部的
    # 判断（一张卡不要边），而 appearance 只回答一个问题：这个控件由谁来画。
    # 所以它出现在归零层之外，一定是有人在补一个已经被接住的洞。
    for sel_text, body in _top_level_rules():
        if {"appearance", "-webkit-appearance"} & set(_decls(body)):
            assert (sel_text, body) in floor, f"{sel_text} 又自己管了一次 appearance"
    for sel_text, body in _top_level_rules():
        # 伪元素不在这条律里：`::-webkit-scrollbar-track` 那层是滚动条自己的皮，
        # 每个宿主都要单独决定给不给它一层面，不是一个能被归零的通道。
        if "::" in sel_text:
            continue
        decls = _decls(body)
        if not decls or set(decls) - channels:
            continue
        if any(v.lower() not in _BLANK_SHEET for v in decls.values()):
            continue
        assert (sel_text, body) in floor, f"{sel_text} 在归零层之外又归零了一次"


def test_the_floor_declares_every_channel_the_ua_has_an_opinion_about():
    """六条通道一条都不能少：font / color / background / border / appearance / color-scheme。

    少一条就等于那一条仍然是浏览器在替这个房间选。border 是最容易忘的一条——
    `button` 的 UA 值是 `2px outset ButtonBorder`，而 box-sizing:border-box
    会把那 2px 从 30px 的圆里吃掉，于是「忘了归零」表现为几何变小。
    """
    declared: set[str] = set()
    for _sel, body in _floor_rules():
        declared |= set(_decls(body))
    for prop in ("font", "color", "background", "border"):
        assert prop in declared, f"归零层没说 {prop}"
    assert {"appearance", "-webkit-appearance"} <= declared, declared
    assert re.search(r"color-scheme\s*:\s*dark", ROOT), ":root 必须自己说清楚这个房间是暗的"


def test_the_floor_values_are_a_blank_sheet_not_a_choice():
    """归零值只能是「回到一张白纸」的那几个词。

    这条守的是归零层的**位置**：它坐在所有组件规则之前、特异度之下。
    如果它开始写 var(--bg-card) 这种选过的值，那就是一个谁都覆盖不掉的选择，
    而且是对同一个问题的第二个答案（§7.9）。
    """
    for sel, body in _floor_rules():
        for prop, val in _decls(body).items():
            if prop in ("list-style", "display"):
                continue
            assert val.lower() in _BLANK_SHEET, (sel, prop, val)


def test_appearance_is_zeroed_everywhere_except_the_two_widgets_the_browser_draws():
    """checkbox 和 radio 必须被排除在 appearance:none 之外。

    别的原生控件，UA 画的那层下面还有一个盒子；checkbox 的方框就是 UA 画的那层，
    `appearance:none` 会把它整个擦掉，剩下一个 13×13 的空位。
    所以这里的判据不是「全都归零」，是「归零到还剩一个能看的控件为止」——
    未勾选那格的亮度交给 color-scheme:dark（纯白 19.28:1 → rgb(59,59,59) 1.72:1），
    勾上那格交给 accent-color（正好是 --life-green）。
    """
    rules = [(s, b) for s, b in _floor_rules() if {"appearance", "-webkit-appearance"} & set(_decls(b))]
    assert len(rules) == 1, [s for s, _ in rules]
    sel_text = rules[0][0]
    inputs = [p for p in sel_text.split(",") if "input" in p]
    assert len(inputs) == 1, sel_text
    assert "[type=checkbox]" in inputs[0].replace(" ", ""), inputs[0]
    assert "[type=radio]" in inputs[0].replace(" ", ""), inputs[0]
    assert inputs[0].count(":not(") == 2, inputs[0]


def test_every_native_tag_in_the_file_is_reached_by_the_floor():
    """§7.7：守卫的覆盖面必须和它守的律一样宽。

    律说的是「所有原生控件」，所以文件里出现的每个原生标签都得在归零层的
    选择器里。`select` 一个都没有却列着，是因为归零层的语义边界是「原生控件」
    这整个类，不是「文件里现在有哪些控件」——它的职责恰恰是接住下一个。
    """
    reached: set[str] = set()
    for sel_text, _body in _floor_rules():
        for part in sel_text.split(","):
            reached.add(re.sub(r"(:{1,2}[a-z-]+|\[[^\]]*\]|\([^)]*\))", "", part).strip())
    missing = set(_native_control_sites()) - reached
    assert not missing, missing


def test_the_summary_marker_is_suppressed_once_at_the_tag_level():
    """三角也是 UA 画的一个控件，而它先前被压过两遍、两遍都挂在类名上。

    `.dv-boundary summary` 和 `.lap-advanced summary` 各写了一次 list-style:none
    加一次 ::-webkit-details-marker——覆盖面刚好等于「现在有两个 details」，
    下一个 details 就会长出三角。同一个病，同一个药：搬到标签层，写一遍。
    """
    marker_rules = [s for s, b in _top_level_rules() if "details-marker" in s]
    assert marker_rules == ["summary::-webkit-details-marker"], marker_rules
    for sel_text, body in _top_level_rules():
        if "list-style" not in body:
            continue
        assert "summary" not in sel_text or sel_text == "summary", sel_text


def test_the_three_boundary_questions_look_the_same_in_both_views():
    """§7.5：同三个边界问题的两个视图，除了光学对齐之外不许有第二处差别。

    `.lap-adv-item input`（采集抽屉）与 `.dv-boundary-row input`（详情页）问的是
    同三句话。先前抽屉那边多了 `opacity:.55` 和 `transform:scale(.92)`——那是
    有人在局部按下「这个白方块太响了」，而响的根因是 color-scheme（§7.11：
    opacity 归状态，不归亮度）。margin-top 可以不同：12.5px 与 10px 两种字号
    要各自对齐第一行的视觉中线。
    """
    a = _decls(_rule_body(".lap-adv-item input"))
    b = _decls(_rule_body(".dv-boundary-row input"))
    assert set(a) == set(b) == {"margin-top", "accent-color"}, (sorted(a), sorted(b))
    assert a["accent-color"] == b["accent-color"] == "var(--life-green)", (a, b)


# --- §7.15：文档里的数字必须能被今天的文件重算出来 ----------------------------

DOC = (Path(__file__).resolve().parents[2] / "内在地形-美学基线-v1.md").read_text(encoding="utf-8")
# §7.13 第二条锚假设的那张亮照片。**它是一个假设，不是一个测量**，所以它必须
# 同时出现在文档里（下面那条守卫会检查），否则「压在照片上过不过 4.5」这个结论
# 就少了一个说不出来的输入。
PHOTO_GREY = 230


def _doc_section(heading: str) -> str:
    """从某个标题切到下一个同级或更浅的标题。

    不按节切，「这个数出现在文档里」就不是一句有内容的话——`3.73` 这种四个字符
    在 1400 行里几乎必然撞上别的东西，于是守卫变成恒真的。
    """

    lines = DOC.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith(heading)), None)
    assert start is not None, f"文档里找不到这一节：{heading}"
    depth = len(lines[start]) - len(lines[start].lstrip("#"))
    for j in range(start + 1, len(lines)):
        head = lines[j]
        if head.startswith("#") and len(head) - len(head.lstrip("#")) <= depth:
            return "\n".join(lines[start:j])
    return "\n".join(lines[start:])


def _doc_line(heading: str, key: str) -> str:
    hits = [ln for ln in _doc_section(heading).splitlines() if key in ln]
    assert len(hits) == 1, f"{heading} 里含「{key}」的行有 {len(hits)} 条，认不准"
    return hits[0]


def _cell(text: str) -> str:
    return text.replace("*", "").replace("`", "").strip()


def _doc_rows(heading: str) -> list[list[str]]:
    """一节里所有表格行，去掉强调标记。"""

    return [
        [_cell(c) for c in ln.strip().strip("|").split("|")]
        for ln in _doc_section(heading).splitlines()
        if ln.startswith("|")
    ]


def _root_value(name: str) -> str:
    m = re.search(rf"{name}\s*:\s*([^;]+);", ROOT)
    assert m, name
    return m.group(1).strip()


def _steps() -> list[float]:
    return [float(_root_value(f"--o-{i}")) for i in range(1, 8)]


def test_the_ruler_is_calibrated_before_anything_is_measured_with_it():
    """§7.15：先证明尺子对，再用它量产品。

    这条把校准数据和下面每一条守卫**显式**接起来。没有它，`test_colour_ruler.py`
    被删掉之后所有守卫照样全绿——它们还在量，只是没人再知道尺子准不准。
    §7.10 那一列错了整整一版就是这么发生的：算它的脚本算完就删了。
    """

    from tests import test_colour_ruler as ruler

    assert ruler.colour is colour, "校准的和用来量产品的必须是同一把尺子"
    assert len(ruler.SHARMA) == 34, len(ruler.SHARMA)
    worst = max(abs(colour.ciede2000(a, b) - e) for a, b, e in ruler.SHARMA)
    assert worst < 6e-5, f"尺子漂到了 {worst:.3e}"


def test_one_step_of_text_colour_costs_the_delta_e_the_doc_quotes():
    """§7.10：「一档 ΔE 15 起」这句话的三个数字必须是算出来的。"""

    text, dim, faint = _tier("--text"), _tier("--text-dim"), _tier("--text-faint")
    steps = [
        colour.delta_e(text, dim),
        colour.delta_e(dim, faint),
        colour.delta_e(text, faint),
    ]
    line = _doc_line("### 7.10", "这三档两两相差")
    written = re.search(r"ΔE ([\d.]+) / ([\d.]+) / ([\d.]+)", line)
    assert written, line
    assert list(written.groups()) == [f"{v:.2f}" for v in steps], (
        f"§7.10 该写 {[f'{v:.2f}' for v in steps]}，文档写的是 {list(written.groups())}"
    )
    # 「15 起」这句话本身：最小的一档不许掉到 14.5 以下，否则那句话就该改。
    assert min(steps) >= 14.5, steps


def test_the_text_floor_is_already_below_aa_on_the_page_ground():
    """§7.11 整条律的前提：阶梯自己的地板在 `--bg-deep` 上就已经过不了 AA。

    这个数不是我挑的门槛，是阶梯自己定的——它一旦被改高到 4.5 之上，
    §7.11「opacity 这条通道留给状态」的全部论证都要重做。
    """

    deep = _tier("--bg-deep")
    floor = colour.contrast(_tier("--text-faint"), deep)
    second = colour.contrast(_tier("--text-dim"), deep)
    assert floor < 4.5, floor
    row = _doc_line("### 7.11", "| 对照：")
    assert f"{floor:.2f}" in row and f"{second:.2f}" in row, (
        f"该写 {floor:.2f} / {second:.2f}，文档那一行是：{row}"
    )


def test_the_opacity_knob_table_recomputes_cell_by_cell():
    """§7.11 那张 8 行表逐格重算——包括合成 RGB 那一列。

    这一条是这一轮买来的：表里「合成 RGB」是按浏览器逐层取整写的，而它右边
    「对比度」「ΔE」两列原先是在**连续空间**算的。于是同一行里的三个数描述了
    两个不同的颜色，表读起来自洽，但右边两列算的那个底屏幕上不存在。
    """

    deep, faint = _tier("--bg-deep"), _tier("--text-faint")
    rows = [r for r in _doc_rows("### 7.11") if re.fullmatch(_NUM, r[0])]
    assert len(rows) == 8, f"§7.11 的旋钮表应有 8 行，读到 {len(rows)}"
    for alpha_text, ground, contrast_text, delta_text, _why in rows:
        composited = colour.over(faint, deep, float(alpha_text))
        assert tuple(int(x) for x in ground.strip("()").split(",")) == composited, (
            f"α={alpha_text} 的合成 RGB 该是 {composited}，文档写 {ground}"
        )
        assert _by_quantity(f"{colour.contrast(composited, deep):.2f}:1") == _by_quantity(
            contrast_text
        ), (
            f"α={alpha_text} 的对比度该是 {colour.contrast(composited, deep):.2f}:1，"
            f"文档写 {contrast_text}"
        )
        assert _by_quantity(f"{colour.delta_e(faint, composited):.2f}") == _by_quantity(
            delta_text
        ), (
            f"α={alpha_text} 的 ΔE 该是 {colour.delta_e(faint, composited):.2f}，"
            f"文档写 {delta_text}"
        )


def test_no_setting_of_the_opacity_knob_is_both_a_step_and_readable():
    """§7.11 的判据本身，写成一个不受 8 位取整抖动影响的形式。

    原先文档给的是「分界点 α=0.583」。那个三位小数是假的：逐层取整让 ΔE 在
    α≈0.58 附近上下跳——α 从 0.571 抬到 0.575，合成色只动了蓝一个刻度
    ((71,68,63)→(71,68,64))，ΔE 反而从 15.28 升到 15.42：α 变大、离满强度更近，
    距离却变远了。所以 ΔE 穿过 15 这件事在这一带发生了不止一次，「分界点」不是单值。
    **可判定的说法是一个上确界**：够得上一档（ΔE ≥ 15）的那些格里，
    对比度最高的一格是多少——它必须仍然读不出字。
    """

    deep, faint = _tier("--bg-deep"), _tier("--text-faint")
    readable = [
        colour.contrast(colour.over(faint, deep, a / 1000), deep)
        for a in range(1, 1001)
        if colour.delta_e(faint, colour.over(faint, deep, a / 1000)) >= 15
    ]
    assert readable, "一格都够不上一档，那 §7.11 的论证要重写"
    ceiling = max(readable)
    assert ceiling < 4.5, f"竟然有一格同时够得上一档又读得出字：{ceiling:.2f}:1"
    # 必须钉在**论证那句话**上，不能只是「出现在这一节里」——上确界同时也是表里的
    # 一个格子，所以「在这一节里」这个条件被那张表恒真地满足了，抹掉结论也不会红。
    # 变异测试抓出来的。
    conclusion = _doc_line("### 7.11", "对比度最高的一格")
    assert f"{ceiling:.2f}:1" in conclusion, (
        f"§7.11 的结论那句该写上确界 {ceiling:.2f}:1，现在是：{conclusion}"
    )


def test_the_three_ground_tiers_are_two_steps_of_the_same_height():
    """§7.9：三档近黑是两个一样高的台阶，而「一样高」是算出来的。"""

    veil, deep, lift = _tier("--bg-veil"), _tier("--bg-deep"), _tier("--bg-lift")
    steps = [
        colour.delta_e(veil, deep),
        colour.delta_e(deep, lift),
        colour.delta_e(veil, lift),
    ]
    assert min(steps) >= colour.JND, steps
    assert abs(steps[0] - steps[1]) < 0.15, f"两个台阶不一样高：{steps}"
    line = _doc_line("### 7.9", "三档相互")
    written = re.search(r"三档相互 \*\*([\d.]+) / ([\d.]+) / ([\d.]+)\*\*", line)
    assert written, line
    assert list(written.groups()) == [f"{v:.2f}" for v in steps], (
        f"§7.9 该写 {[f'{v:.2f}' for v in steps]}，文档写 {list(written.groups())}"
    )


def test_the_five_identity_lights_are_further_apart_than_the_jnd():
    """§7.9：四个入口 + 共处那颗光球，两两必须分得出——否则「你在哪」没被说。"""

    lights = [
        _tier(f"--{name}")
        for name in ("life-green", "memo-blue", "river-rose", "me-violet", "comp-warm")
    ]
    gaps = [
        colour.delta_e(lights[i], lights[j])
        for i in range(len(lights))
        for j in range(i + 1, len(lights))
    ]
    assert min(gaps) >= colour.JND, gaps
    line = _doc_line("### 7.9", "五盏灯两两")
    written = re.search(r"五盏灯两两 ΔE \*\*([\d.]+)–([\d.]+)\*\*", line)
    assert written, line
    assert list(written.groups()) == [f"{min(gaps):.2f}", f"{max(gaps):.2f}"], (
        f"§7.9 该写 {min(gaps):.2f}–{max(gaps):.2f}，文档写 {'–'.join(written.groups())}"
    )


def test_each_of_the_seven_alpha_steps_is_visible_as_a_step():
    """§7.13：七档之所以是七档，全靠相邻档在可辨阈之上。

    档数是这条梯子唯一被证明过的东西（「哪一档」还欠着，见 §10）。所以这张
    相邻档 ΔE 表就是「七」这个数字的全部依据，它必须能被重算。
    """

    lift = _tier("--bg-lift")
    rungs = [colour.over(colour.WHITE, lift, a) for a in _steps()]
    gaps = [colour.delta_e(rungs[i], rungs[i + 1]) for i in range(6)]
    assert min(gaps) >= colour.JND, gaps
    row = _doc_line("#### 七档不是七个写法", "| ΔE2000 |")
    written = [_cell(c) for c in row.strip().strip("|").split("|")][1:]
    assert written == [f"{g:.2f}" for g in gaps], (
        f"§7.13 该写 {[f'{g:.2f}' for g in gaps]}，文档写 {written}"
    )


def test_the_sixth_step_is_the_lowest_veil_a_caption_survives():
    """§7.13 第二条锚：第 6 档是**能过 4.5:1 的最低一档**，这是算出来的。

    那张照片有多亮是一个**假设**，所以它必须写在文档里——否则这个结论少了一个
    说不出来的输入，而换一张更亮的照片答案就变了。
    """

    veil, text, dim = _tier("--bg-veil"), _tier("--text"), _tier("--text-dim")
    photo = (PHOTO_GREY,) * 3
    section = "#### 四条锚"
    assert f"{PHOTO_GREY} 灰" in _doc_section(section), "那张照片有多亮，必须写出来"

    def pair(alpha: float) -> tuple[float, float]:
        ground = colour.over(veil, photo, alpha)
        return colour.contrast(text, ground), colour.contrast(dim, ground)

    rows = [r for r in _doc_rows(section) if re.fullmatch(r"第 \d 档 " + _NUM, r[0])]
    assert len(rows) == 2, f"那张对比度表应有两行（第 5、6 档），读到 {len(rows)}"
    for first, *cells in rows:
        alpha = float(first.split()[-1])
        assert [f"{v:.2f}" for v in pair(alpha)] == cells, (
            f"{first} 该写 {[f'{v:.2f}' for v in pair(alpha)]}，文档写 {cells}"
        )

    alphas = _steps()
    assert pair(alphas[5])[1] >= 4.5, "第 6 档已经托不住 dim 了"
    assert pair(alphas[4])[0] < 4.5, "第 5 档竟然够用了，那「最低一档」要重定"


def test_an_unchecked_native_box_would_outshine_the_brightest_text():
    """§7.14：`color-scheme` 那句话的代价，是两个可重算的数字。

    一个等着被勾的原生 checkbox 是纯白；它比全 app 最亮的字还亮。这两个数就是
    「必须在 `:root` 上跟浏览器说这个房间是暗的」这条判据的全部内容。
    """

    deep = _tier("--bg-deep")
    empty_box = colour.contrast(colour.WHITE, deep)
    brightest_text = colour.contrast(_tier("--text"), deep)
    assert empty_box > brightest_text, (empty_box, brightest_text)
    line = _doc_line("#### 第六条通道写在", "最响的东西")
    for value in (empty_box, brightest_text):
        assert f"{value:.2f} : 1" in line, f"§7.14 该写 {value:.2f} : 1，那一行是：{line}"


# --- #33 / §7.17：三档是三种墨，对比度是「墨与底」这一对的属性 ----------------
#
# §7.11 给了三档三个数（16.55 / 9.21 / 3.73:1），而在这之前没有一处说出过那三个数
# 是在哪个底上算的。三档全是**不透明**的 hex，墨搬到哪个底上都还是这一个值——所以
# 「档」并没有漏掉一个参数，漏掉参数的是「一档 = 某个对比度」这句话本身。

_ROOT_HEX = {
    m.group(1): tuple(int(m.group(2)[i : i + 2], 16) for i in (0, 2, 4))
    for m in re.finditer(r"(--[\w-]+)\s*:\s*#([0-9a-fA-F]{6})\s*;", ROOT)
}
_ROOT_RGB = dict(_ROOT_HEX)
_ROOT_RGB.update(
    {
        m.group(1): tuple(int(x) for x in m.group(2).split(","))
        for m in re.finditer(r"(--[\w-]+)\s*:\s*(\d+\s*,\s*\d+\s*,\s*\d+)\s*;", ROOT)
    }
)

_BG_DECL = re.compile(r"(?<![-\w])background(?:-color|-image)?\s*:\s*([^;}]+)")
_TIER_DECL = re.compile(r"(?<![-\w])color\s*:\s*var\((--text(?:-dim|-faint)?)\)")


def _gate(lo_tier: str, hi_tier: str) -> float:
    """两种墨在哪个底上打平：contrast=(L_hi+.05)/(L_lo+.05)，于是解析解是几何均值。"""
    a = colour.luminance(_tier(lo_tier)) + 0.05
    b = colour.luminance(_tier(hi_tier)) + 0.05
    return math.sqrt(a * b) - 0.05


def _veil_crossing(base: tuple[int, int, int]) -> float:
    """门槛换算成压在 base 上的白纱 α。"""
    gate = _gate("--text-dim", "--text-faint")
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if colour.luminance(colour.over(colour.WHITE, base, mid)) < gate:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _grey(value: float) -> tuple[int, int, int]:
    """给定相对亮度反解一个灰——用来在门槛两侧各站一只脚。"""
    lo, hi = 0.0, 255.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if colour.luminance((mid, mid, mid)) < value:
            lo = mid
        else:
            hi = mid
    return ((lo + hi) / 2,) * 3


def _layers_in(value: str, base: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """一条 background 声明里读得出的每一层颜色，各自压在 base 上。

    渐变里的每一个色标都单独算：一层渐变的最亮处就是坐在它上面那个字的最坏情况。

    alpha 那一段必须允许它自己带一层括号。原先写的是 `([^)]+?)`，于是
    `rgba(var(--life-green-rgb),var(--o-2))`——**全 app 写一层纱最标准的那个拼法**——
    的 alpha 被截成 `var(--o-2`，两个 `fullmatch` 都不认，这一层底就被静默丢掉了。
    §7.7 那个形状的又一次：一个换了拼法就失效的尺子，报出来的是空集而不是错误。

    hex 也必须两种长度都认。原先只认 6 位，于是 `.photo-viewer{background:#000}`——
    一块**占满整屏**的底——在这把尺子眼里根本不存在（#36 量 `.pv-slide` 时撞上的）。
    同一个形状的第二次：换个拼法就静默返回空集。
    """

    out: list[tuple[int, int, int]] = []
    pattern = (
        r"rgba?\(\s*(?:var\((--[\w-]+)\)|(\d+)\s*,\s*(\d+)\s*,\s*(\d+))\s*"
        r"(?:,\s*((?:[^()]|\([^()]*\))+?))?\s*\)"
    )
    for m in re.finditer(pattern, value):
        if m.group(1):
            rgb = _ROOT_RGB.get(m.group(1))
            if rgb is None:
                continue
        else:
            rgb = tuple(int(m.group(i)) for i in (2, 3, 4))
        raw = (m.group(5) or "").strip()
        if not raw:
            out.append(rgb)
            continue
        rung = re.fullmatch(r"var\((--o-\d)\)", raw)
        if rung:
            out.append(colour.over(rgb, base, float(_root_value(rung.group(1)))))
        elif _PLAIN_NUMBER.fullmatch(raw):
            out.append(colour.over(rgb, base, float(raw)))
    for m in re.finditer(r"var\((--[\w-]+)\)", value):
        if m.group(1) in _ROOT_HEX:
            out.append(_ROOT_HEX[m.group(1)])
    for m in re.finditer(r"#([0-9a-fA-F]{6})(?![0-9a-fA-F])", value):
        out.append(tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4)))
    for m in re.finditer(r"#([0-9a-fA-F]{3})(?![0-9a-fA-F])", value):
        out.append(tuple(int(c * 2, 16) for c in m.group(1)))
    return out


def _floors_and_words():
    """静态枚举：声明出来的每一层底，以及声明了文字档的每一个选择器。

    **必须是静态的。** 量过：抬面那一族 162 个选择器里有 68 个（42%）在一个空 app 里
    根本没有元素——面板没开、列表没数据、状态没触发。拿浏览器去列「底 × 档」这张表，
    结构上就漏掉将近一半，而且会随内容无声地变化，也就是一张会自己变绿的表。
    """

    lift = _tier("--bg-lift")
    floors = []
    words = []
    for sel_text, body in _top_level_rules():
        sel = re.sub(r"\s+", " ", sel_text).strip()
        if sel == ":root":
            continue
        parts = [p.strip() for p in sel.split(",") if p.strip()]
        for m in _BG_DECL.finditer(body):
            for layer in _layers_in(m.group(1), lift):
                floors.extend((p, layer) for p in parts)
        tier = _TIER_DECL.search(body)
        if tier:
            words.extend((p, tier.group(1)) for p in parts)
    return floors, words


def test_the_three_text_tiers_are_opaque_inks_not_three_contrast_numbers():
    """三档是三种不透明的墨。所以一个对比度数字必须连底一起说，否则它不指向任何东西。

    这条守卫钉的是**那句话**，不是那三个数：三档若哪天写成 `rgba(...)`，「墨搬到哪个
    底上都还是这一个值」就不再成立，而 §7.11 与 §7.17 的全部推导都建立在它上面。
    """

    for name in ("--text", "--text-dim", "--text-faint"):
        assert re.search(rf"{name}\s*:\s*#[0-9a-fA-F]{{6}}\s*;", ROOT), f"{name} 不再是不透明的墨"
    # §7.11 那三个数是「三档 × --bg-deep」这三对的属性，文档必须把底说出来。
    deep = _tier("--bg-deep")
    pairs = [colour.contrast(_tier(t), deep) for t in ("--text", "--text-dim", "--text-faint")]
    line = _doc_line("### 7.17", "对比度是一对")
    assert "--bg-deep" in line, f"§7.17 该说出那三个数是在哪个底上算的：{line}"
    for value in pairs:
        assert f"{value:.2f}:1" in line, f"§7.17 该写 {value:.2f}:1，那一行是：{line}"


def test_the_ladder_is_only_a_ladder_below_a_floor_that_can_be_solved_for():
    """三档只在底暗于某个亮度时才是一条梯子，而那个亮度有解析解。

    contrast 是 (L_hi+.05)/(L_lo+.05)，所以两种墨打平的条件是
    (L_bg+.05)² = (L_1+.05)(L_2+.05)——门槛是两档「加了 .05 之后」的**几何均值**。
    这里同时用暴力扫一遍验解析解：一个只有解析解的数字，等于只有一个证人。
    """

    gate = _gate("--text-dim", "--text-faint")
    dim, faint = _tier("--text-dim"), _tier("--text-faint")
    brute = next(
        v / 2000
        for v in range(2001)
        if colour.contrast(dim, _grey(v / 2000)) <= colour.contrast(faint, _grey(v / 2000))
    )
    assert abs(brute - gate) < 0.002, (brute, gate)
    # 越过门槛，`--text-dim` 是三档里**最不可读**的一档：「更安静」变成「看不见」。
    beyond = _grey(min(gate + 0.05, 1.0))
    tiers = {t: colour.contrast(_tier(t), beyond) for t in ("--text", "--text-dim", "--text-faint")}
    assert min(tiers, key=tiers.get) == "--text-dim", tiers
    # 门槛以下它还是原来的序，否则「越过」这个词没有对照。
    below = _grey(max(gate - 0.05, 0.0))
    under = [colour.contrast(_tier(t), below) for t in ("--text", "--text-dim", "--text-faint")]
    assert under == sorted(under, reverse=True), under
    # 三个门槛各自也得能被重算：dim/faint 只是最低的那一个。
    gates = [
        _gate("--text-dim", "--text-faint"),
        _gate("--text", "--text-faint"),
        _gate("--text", "--text-dim"),
    ]
    assert gates == sorted(gates), gates
    line = _doc_line("### 7.17", "三个门槛")
    for value in gates:
        assert _by_quantity(f"{value:.4f}") in _by_quantity(line), (
            f"§7.17 该写 L={value:.4f}，那一行是：{line}"
        )


def test_the_fifth_veil_is_the_last_rung_the_ladder_survives():
    """把门槛换算成白纱，它必须落在第 5 与第 6 档之间——这条法与 §7.13 是同一条法。

    §7.11（字有三档）与 §7.13（纱有七档）此前从未碰面：一条说字，一条说纱，而纱
    恰恰就是字底下那个底。两条法在这里交出同一个数。
    """

    rungs = _steps()
    for base in ("--bg-deep", "--bg-lift"):
        alpha = _veil_crossing(_tier(base))
        assert rungs[4] < alpha <= rungs[5], (base, alpha, rungs[4], rungs[5])
    line = _doc_line("### 7.17", "换算成白纱")
    for base in ("--bg-lift", "--bg-deep"):
        alpha = _veil_crossing(_tier(base))
        assert _by_quantity(f"{alpha:.4f}") in _by_quantity(line), (
            f"§7.17 该写压在 {base} 上的交界 α={alpha:.4f}，那一行是：{line}"
        )


def test_no_floor_past_the_gate_carries_a_word():
    """越过门槛的底上不许有字。今天有 20 层越过了门槛，其中零层承字。

    伪元素不算底——那 20 层里有 8 层是伪元素，抽查过的两个是假阳性：`.memo-item::before`
    是左缘一道 3px 色条，`.life-capture-cta::before` 是一条 `height:1px` 的顶边高光线。
    （`content` 里真写了字的那一类归 #62，判完之后这条守卫要跟着放宽；「整类排除 `::`」
    这个做法本身也归那一轮重判。）
    """

    gate = _gate("--text-dim", "--text-faint")
    floors, words = _floors_and_words()
    past = {
        (sel, tuple(round(v) for v in layer))
        for sel, layer in floors
        if colour.luminance(layer) >= gate and "::" not in sel
    }
    # 这条守卫不许恒真：越过门槛的底必须真的存在，否则它什么都没在守。
    # 门槛定在 12 而不是 10 有具体理由——`_layers_in` 的 alpha 组原先读不了
    # `rgba(var(--x-rgb),var(--o-n))`，那个瞎掉的尺子在这里数出 11 个。
    assert len(past) >= 12, f"越过门槛的底只找到 {len(past)} 个，这条守卫多半瞎了"
    carried = [
        (sel, word_sel, tier)
        for sel, _ in past
        for word_sel, tier in words
        if word_sel == sel or re.search(re.escape(sel) + r"(?=[\s>])", word_sel)
    ]
    assert carried == [], carried


def test_the_paper_is_past_the_gate_and_that_is_exactly_why_it_has_its_own_ink():
    """正对照：门槛之外确实有一块承字的底，而那里的答案不是换一档，是换一套墨。

    这条是上面那条的反面。只证「没有底越过门槛且承字」是不够的——那句话在一个
    全暗的 app 里恒真。必须同时指出：越过门槛的那块底**存在**、它**承字**、
    而它承的字用的是另一套墨（`--ink`），不是三档里的任何一档。
    """

    gate = _gate("--text-dim", "--text-faint")
    paper = _ROOT_RGB["--paper"]
    assert colour.luminance(paper) >= gate, (colour.luminance(paper), gate)
    # 三档搬到纸上会完全倒过来：最亮的一档反而最不可读。
    on_paper = [colour.contrast(_tier(t), paper) for t in ("--text", "--text-dim", "--text-faint")]
    assert on_paper == sorted(on_paper), f"纸上的序竟然没倒：{on_paper}"
    # 第四种墨买回来的是这个差距。
    ink = colour.contrast(_ROOT_RGB["--ink"], paper)
    assert ink >= 4.5 and ink / max(on_paper) > 2, (ink, on_paper)
    body = _rule_body(".msg-user .bubble")
    assert "var(--ink)" in body, body
    for tier in ("var(--text)", "var(--text-dim)", "var(--text-faint)"):
        assert tier not in body, f"纸上又用回了三档：{tier}"
    line = _doc_line("### 7.17", "那张纸")
    for value in (colour.luminance(paper), gate):
        assert _by_quantity(f"{value:.4f}") in _by_quantity(line), (
            f"§7.17 该写 {value:.4f}，那一行是：{line}"
        )


# --- #34 / §7.18：可点性线索必须长在这个东西自己的身体上 ----------------------
#
# 待办把这一项记成「`.mtp-fb` 缺一条可点性线索」。审计的结论比它大：全 app 51 个可点类
# 按「线索组合」归组有 9 组——也就是「怎么让人知道我能点」在同一个文件里有 9 个答案。
# 而一个东西只有三种身体能说这句话：**盒**（自己的底 / 边 / 圆角 / 光）、**形**（一个
# 字形、一个图标、一个自绘控件）、**一段字**（盒和形都没有，只剩字自己）。所以判据不是
# 「补一条下划线」，是把这 51 个类按身体归位，并要求每一类用它那个身体说话。
#
# 尺子的边界要说清楚：它认 `<button>` / `<a>` / `<summary>` / `<label>` 和写了 `onclick`
# 的元素。靠事件委托才能点的（`.le-agent` 整片可点去开详情）它看不见——那一片归 #60。

_CLICKABLE = re.compile(
    r"<(button|a|summary|label)\b([^>]*)>|<(\w+)([^>]*\bonclick\s*=[^>]*)>", re.I
)
_CLASS_ATTR = re.compile(r"""class\s*=\s*["']([^"']*)""")

# 形：身体是一个形状。每一处都记下**那个形状是什么**——一张名字清单会烂掉，一张
# 「名字 → 证据」表不会：证据一旦消失，守卫就红，而不是默默放行。
_SHAPE_SPEAKS = {
    "lap-adv-item": "<input",          # label 裹着一个原生 checkbox
    "we-switch": "<input",             # 自绘开关：一条轨道加一个滑块
    "le-voice": "<svg",                # 声波
    "tab": "<svg",                     # 导航图标
    "le-agent-fb": "···",              # 三个点本身就是「还有更多」
    "leb-dismiss": "×",
    "pv-close": "×",
    "tr-weather": ".tr-weather-glow",  # 12px 的一团光，由 JS 挂进去
}

# 容器说话（判据二）：容器里**每一样**都能点，于是「这一片能点」由容器说一次就够。
_CONTAINER_SPEAKS = {"tr-more-item": "tr-more-menu"}

# 一段字就是按钮：三处，全 app 同一条线。
_TEXT_IS_THE_BUTTON = ("le-retry-inline", "mtp-fb", "tr-src-toggle")

_UNDERLINE_INK = "var(--text-faint)"
_UNDERLINE_HOVER_INK = "var(--text-dim)"
# 一条线是图形而不是字，所以它自己那一关是 WCAG 1.4.11 的非文本对比 3:1。
_NON_TEXT_MIN = 3.0

# 名字 → 画底的那条规则；None = 它到页底之间没有一层画底。
_TEXT_BUTTON_GROUND = {
    "le-retry-inline": None,
    "tr-src-toggle": None,
    "mtp-fb": ".mtp-message.assistant",
}
# 那两个 None 不是断言，是可查的：这些祖先规则里一层底都没有。
_GROUNDLESS_ANCESTORS = {
    "le-retry-inline": (".le-agent", ".le-agent-copy"),
    "tr-src-toggle": (".tr-card",),
}

_BOX_CUES = (
    ("底", re.compile(r"background(?:-color|-image)?\s*:\s*(?!transparent|none)")),
    ("边", re.compile(r"border(?:-(?:top|right|bottom|left|color|style|width))?\s*:\s*(?!0|none)")),
    ("光", re.compile(r"box-shadow\s*:\s*(?!none)")),
    ("圆角", re.compile(r"border-radius\s*:\s*(?!0)")),
)

_EMPTY_CLASS = re.match(r"()", "")  # 没写 class 的元素：当作「一个没有名字的孩子」


def _clickable_class_names() -> set[str]:
    """标记与 JS 模板串里每一个可点元素的类名。

    两处都得扫：这个文件里一多半按钮活在 JS 模板串里，只扫标记的尺子看不见它们
    （§10 归零层那一族踩过同一个坑）。
    """

    src = _comment_blanked_app()
    names: set[str] = set()
    for m in _CLICKABLE.finditer(src):
        cls = _CLASS_ATTR.search(m.group(2) or m.group(4) or "")
        if cls:
            names.update(n for n in cls.group(1).split() if not n.startswith("${"))
    return names


def _base_bodies(name: str) -> list[str]:
    """这个类**基态**规则的规则体：不带伪类、不带属性选择器，且它是选择器的末段。"""

    out = []
    for sel_text, body in _top_level_rules():
        for part in (p.strip() for p in re.sub(r"\s+", " ", sel_text).split(",")):
            if part and not re.search(r"[:\[]", part) and part.split()[-1] == f".{name}":
                out.append(body)
    return out


def _box_cues(name: str) -> set[str]:
    bodies = _base_bodies(name)
    return {label for label, pat in _BOX_CUES for body in bodies if pat.search(body)}


def _clickable_bodies() -> tuple[set[str], set[str]]:
    """(有基态规则的可点类, 其中靠自己那个盒子说话的)"""

    named = {n for n in _clickable_class_names() if _base_bodies(n)}
    return named, {n for n in named if _box_cues(n)}


def _inner_of(name: str, src: str) -> str | None:
    """这个类第一处元素的内部（标记或 JS 模板串都算）。"""

    for m in re.finditer(rf"""class\s*=\s*["'][^"']*\b{re.escape(name)}\b[^"']*["']""", src):
        start = src.rfind("<", 0, m.start())
        tag = re.match(r"<(\w+)", src[start:])
        if not tag:
            continue
        gt = src.find(">", m.end())
        pat = re.compile(rf"</?{tag.group(1)}\b", re.I)
        depth, i = 1, gt + 1
        while depth and i < len(src):
            hit = pat.search(src, i)
            if not hit:
                return None
            depth += -1 if hit.group(0).startswith("</") else 1
            if not depth:
                return src[gt + 1 : hit.start()]
            i = hit.end()
        return None
    return None


def _visible_text(html: str) -> str:
    return re.sub(r"\s+", "", re.sub(r"\$\{[^}]*\}", "", re.sub(r"<[^>]*>", "", html)))


def _paints_a_floor(body: str) -> bool:
    return any(
        re.sub(r"\s+", "", m.group(1)) not in ("none", "transparent")
        for m in _BG_DECL.finditer(body)
    )


def _ground_under(name: str) -> tuple[int, int, int]:
    """这一段字真正坐在什么上面——静态合成，不问浏览器。

    静态是必须的：量过一次，抬面那一族 42% 的选择器在一个空 app 里根本没有元素，
    拿浏览器列这张表结构上就会漏掉一半（§7.17 那条守卫的同一个理由）。
    """

    rule = _TEXT_BUTTON_GROUND[name]
    if rule is None:
        for ancestor in _GROUNDLESS_ANCESTORS[name]:
            body = _rule_body(ancestor)
            assert not _paints_a_floor(body), f"{ancestor} 开始画底了，.{name} 的底得重算：{body}"
        return _tier("--bg-deep")
    m = _BG_DECL.search(_rule_body(rule))
    assert m, rule
    layers = _layers_in(m.group(1), _tier("--bg-lift"))
    assert len(layers) == 1, (rule, layers)
    return layers[0]


def _ink_of(name: str) -> tuple[str, tuple[int, int, int]]:
    value = _decls(re.sub(r"\s+", " ", " ".join(_base_bodies(name)))).get("color", "")
    m = re.fullmatch(r"var\((--[\w-]+)\)", value)
    assert m, f".{name} 的字色不是一个 token：{value!r}"
    return m.group(1), _ROOT_RGB[m.group(1)]


def test_every_clickable_thing_speaks_with_a_box_a_shape_or_its_own_words():
    """判据一：51 个可点类**恰好**分成三种身体，没有第四种，也没有一个没归位的。

    这条守的不是「下划线在不在」，是「这个东西凭什么让人知道它能点」在全 app 只有
    三个合法答案。往文件里新加一个既没有盒也没有形的按钮，这里就红——它必须先被
    判成「容器替它说话」或者「一段字就是按钮」，而那两条各自还有自己的守卫。
    """

    named, boxes = _clickable_bodies()
    # 尺子不许瞎：这一族必须真的很大，否则下面那个相等是两个空集相等。
    assert len(named) >= 50, f"只数到 {len(named)} 个有规则的可点类，尺子多半瞎了"
    assert len(boxes) >= 30, f"只数到 {len(boxes)} 个盒，尺子多半瞎了"
    naked = named - boxes
    classified = set(_SHAPE_SPEAKS) | set(_CONTAINER_SPEAKS) | set(_TEXT_IS_THE_BUTTON)
    assert naked == classified, {
        "没归位": sorted(naked - classified),
        "已经不裸了或已消失": sorted(classified - naked),
    }


def test_each_shape_that_speaks_for_itself_really_has_a_shape():
    """「形」不是一句托词：每一处都要指得出那个形状，而三种证据各自可查。

    一个标签（`<svg` / `<input`）必须在它内部；一个字形必须**就是**它的全部内容；
    一个由 JS 挂上去的形状必须有一条真的把形状画出来的规则。
    """

    src = _comment_blanked_app()
    for name, evidence in _SHAPE_SPEAKS.items():
        if evidence.startswith("<"):
            inner = _inner_of(name, src)
            assert inner is not None, f".{name} 在标记里找不到"
            assert evidence in inner.lower(), f".{name} 内部没有 {evidence}：{inner[:90]!r}"
        elif evidence.startswith("."):
            body = _rule_body(evidence)
            assert "border-radius" in body and "background" in body, f"{evidence} 不画形状：{body}"
            assert re.search(rf"['\"]{evidence[1:]}['\"]", src), f"{evidence} 没有任何一处挂上去"
        else:
            inner = _inner_of(name, src)
            assert _visible_text(inner or "") == evidence, (
                f".{name} 的内容不是 {evidence!r}：{inner!r}"
            )


def test_a_container_speaks_for_its_children_only_if_every_child_is_clickable():
    """判据二：一段字要不要自己说话，由**容器里有没有不能点的东西**决定。

    `.tr-more-menu` 里三项全是 `.tr-more-item`，所以「这一片能点」由容器说一次就够，
    每一项不必各自再说一遍。反面就在同一张卡上：`.tr-src-toggle` 上面四行全是不能点
    的字（说法 / 状态 / 跨度 / 为什么是现在），容器在那里什么也说不了。
    """

    src = _comment_blanked_app()
    for child, container in _CONTAINER_SPEAKS.items():
        opening = src.index(f'<div class="{container}">')
        block = src[opening + len(f'<div class="{container}">') : src.index("</div>", opening)]
        # 数的是**每一个**元素，不只是能点的那些：一句不能点的说明混进来，
        # 「容器替它们说话」就当场失效，而只数按钮的尺子恰恰看不见那句说明。
        children = [
            (_CLASS_ATTR.search(m.group(0)) or _EMPTY_CLASS).group(1).split()
            for m in re.finditer(r"<\w+[^>]*>", block)
        ]
        assert children, f"读不出 .{container} 里有什么"
        assert all(child in c for c in children), f".{container} 里混进了别的：{children}"
        # `.danger` 是修饰语（永久删除那一项），不是第二种东西。
        assert all(set(c) - {child, "danger"} == set() for c in children), children

    # 反面：同一张卡上，`.tr-src-toggle` 那四个不能点的邻居必须还在。
    card = src[src.index("function _trCardHtml") :]
    card = card[: card.index("\n}")]
    clickable = _clickable_class_names()
    assert "tr-src-toggle" in card
    for quiet in ("tr-card-expr", "tr-card-state", "tr-card-span", "tr-card-why"):
        assert quiet in card, quiet
        assert quiet not in clickable, f"{quiet} 变成能点的了，判据二要重算"


def test_a_button_that_is_only_words_wears_the_same_line_in_all_three_places():
    """判据一的落点：三处「一段字就是按钮」必须是同一条线，hover 也同一档。

    这一族原先有两个答案：`.le-retry-inline` 有线（而且是 α 折扣出来的），
    `.mtp-fb` 与 `.tr-src-toggle` 什么都没有。同一种身体上不许有两个答案——
    #34 的审计里，全 app 对这个问题一共给了 9 个答案，这是其中最贵的一处。
    """

    for name in _TEXT_IS_THE_BUTTON:
        decls = _decls(re.sub(r"\s+", " ", " ".join(_base_bodies(name))))
        assert decls.get("text-decoration") == "underline", (name, decls.get("text-decoration"))
        assert decls.get("text-decoration-color") == _UNDERLINE_INK, (name, decls)
        assert decls.get("text-underline-offset") == "3px", (name, decls)
        hover = _decls(re.sub(r"\s+", " ", _rule_body(f".{name}:hover")))
        assert hover.get("text-decoration-color") == _UNDERLINE_HOVER_INK, (name, hover)


def test_a_box_never_borrows_the_line_that_belongs_to_a_run_of_words():
    """正对照：下划线不是「按钮的标配」，是盒和形都没有时唯一剩下的那条线索。

    39 个靠盒子说话的可点类里一条下划线都没有——否则上面那条守卫就退化成
    「每个按钮都得有下划线」，而那是另一个缺陷：同一句话被说两遍。
    """

    _, boxes = _clickable_bodies()
    assert len(boxes) >= 30, len(boxes)
    for family, names in (("盒", boxes), ("形", set(_SHAPE_SPEAKS))):
        borrowed = sorted(n for n in names if any("underline" in b for b in _base_bodies(n)))
        assert borrowed == [], f"{family} 借了字的线索：{borrowed}"


def test_the_line_under_a_word_is_ink_from_the_ladder_not_an_alpha_discount():
    """判据三：`text-decoration-color` 是墨的第四个属性名，§7.10 那条法照样管它。

    §7.10 判过：`color` 上的 α 只许由状态限定的选择器写，基线声明上的 α 是在三档之下
    发明第四档。而 §7.7 的教训是「只盖住 `color:` 的审计会给 `stroke:` 发通行证」——
    这里是同一个形状的第四次。原先 `.le-retry-inline` 的线正是 `rgba(--refuse-rgb,.4)`。
    """

    sites = [
        (re.sub(r"\s+", " ", sel).strip(), re.sub(r"\s+", " ", m.group(1)).strip())
        for sel, body in _top_level_rules()
        for m in re.finditer(r"text-decoration-color\s*:\s*([^;}]+)", body)
    ]
    assert len(sites) >= 6, f"全 app 只有 {len(sites)} 处 text-decoration-color，尺子多半瞎了"
    assert [s for s in sites if [a for a in _rgba_alphas(s[1]) if a < 1]] == [], sites
    assert [s for s in sites if "var(--" not in s[1]] == [], sites


def test_the_copied_alpha_would_not_have_bought_the_same_thing():
    """同一个 α 不是同一句话：α 是「乘」，档是「位置」，照搬一个乘数不落在同一个位置。

    三档全是暖墨（`--text-faint` 的 r−b = +24）。α=.4 压到冷底上，暖度塌到 +3 以内、
    对比度掉到 2:1 以下——那不是「淡一档」，那是换了一种材料。换档买回来的是：三条线
    全部过 WCAG 1.4.11 的 3:1，而且离字还有一档（§7.10 的 ΔE 15）。
    """

    faint = _ROOT_RGB["--text-faint"]
    for tier in ("--text", "--text-dim", "--text-faint"):
        rgb = _tier(tier)
        assert rgb[0] > rgb[1] > rgb[2], f"{tier} 不再是暖墨了：{rgb}"
    assert faint[0] - faint[2] >= 20, faint

    for name in _TEXT_IS_THE_BUTTON:
        ground = _ground_under(name)
        _, ink = _ink_of(name)
        assert colour.contrast(faint, ground) >= _NON_TEXT_MIN, (
            name, colour.contrast(faint, ground)
        )
        assert colour.delta_e(ink, faint) >= 15, (name, colour.delta_e(ink, faint))
        # 反面：照搬先例那个 .4
        discounted = colour.over(faint, ground, 0.4)
        assert colour.contrast(discounted, ground) < 2.0, (name, discounted)
        assert discounted[0] - discounted[2] <= 3, (name, discounted)


def test_a_button_that_is_only_words_clears_aa_on_the_floor_it_actually_sits_on():
    """#34 真正的缺陷比「缺一条线索」重：那两段字原先根本读不出来。

    `.mtp-fb` 坐的底是 `rgba(--life-green,--o-2)` 压在抬面上，`--text-faint` 在那里
    只有 3.14:1；`.tr-src-toggle` 在页底上 3.73:1。两处都过不了 AA 的 4.5:1——一个
    **必须被读到的动作**读不到。升到 `--text-dim` 之后是 7.75:1 与 9.21:1。
    """

    faint = _ROOT_RGB["--text-faint"]
    for name in _TEXT_IS_THE_BUTTON:
        ground = _ground_under(name)
        token, ink = _ink_of(name)
        assert colour.contrast(ink, ground) >= 4.5, (name, token, colour.contrast(ink, ground))
    # 反面：原先那一档在这两处确实过不了，否则「升一档」买回来的是零。
    for name in ("mtp-fb", "tr-src-toggle"):
        assert colour.contrast(faint, _ground_under(name)) < 4.5, name


# --- #35 / §7.19：一个手势要不要教，取决于它是不是唯一的路 --------------------
#
# 待办把这一项记成「那句提示不再指向任何方向，该不该改成一个方向性的动作」。判完的
# 结论是**那句提示整个不该存在**：手势没有身体（§7.18 的盒 / 形 / 一段字它一个都不是），
# 它只能借别人的身体说话；而借不借，先问它是不是唯一的路。详情页翻卡有五条通道，
# 其中三条不需要手势（圆点点击、左右方向键、单击卡片左右两侧），所以拖拽是**加速器**——
# 不知道它的人到得了每一张卡，于是它不需要一段字来教。
#
# 主证人是**卡堆自己的几何**，不是圆点。这一点是浏览器逼出来的：非激活圆点坐在
# `--o-3` 上，对底只有 1.28:1（WCAG 1.4.11 要 3:1），所以那三个点其实没在说
# 「还有三张」——只有激活那一个在说「你在这里」。而卡堆的位移 / 缩放 / 旋转是实的
# （实测三张卡 top 15.6 / 47.5 / 80.1，宽 340 / 328.5 / 315.6）。圆点那 1.28:1 与
# 「`.fly-in` 的 `both` 压掉行内 opacity」都是这一轮翻出来的新缺陷，各自另立任务。

_DETAIL_HINT_GONE = ("dv-hint", "dvHint", "轻拨卡片")

# 「把一个元素打开 / 关掉 / 往它里面写字」在 DOM 里的六种写法。
_TURNS_ON = (
    r"style\.display\s*=",
    r"style\.visibility\s*=",
    r"hidden\s*=",
    r"classList\.(?:add|remove|toggle)\s*\(",
    r"(?:set|remove)Attribute\s*\(\s*['\"]hidden",
    r"(?:innerHTML|textContent)\s*=",
)



@pytest.mark.parametrize("token", _DETAIL_HINT_GONE)
def test_the_detail_page_does_not_grow_a_sentence_to_teach_a_gesture(token):
    """那句「轻拨卡片 · 翻阅记忆」整个删掉了：规则、标记、id、显示条件，四处都不在。

    删它不是「少一句提示」，是**取消一处重复**：`detailCards.length>1` 这一个条件
    原先同时打开三样东西，而其中两样说得更准（`.dv-counter` 把数量说全，`.dv-dots`
    的激活点说位置）。这段字说得最少、离它描述的那个东西最远（隔着 meta 行、正文、
    语音条），而且它自己在 3.89:1 上根本读不到——修它等于把一句重复的话说得更响。

    查的是**去注释后的源**：那条解释「这里曾经有什么」的注释里正写着这三个词，
    和 §7.3 那几个退役色号踩的是同一个陷阱。
    """

    assert token not in CSS_NO_COMMENTS, token
    assert token not in SCRIPT_NO_COMMENTS, token
    assert token not in _comment_blanked_app(), token


def test_the_card_stack_still_shows_it_is_a_stack():
    """删掉那句字的**理由**必须留在文件里，否则下一个人只看到「少了个提示」。

    卡堆的三个常量就是那个理由：后面的卡向下偏、缩小、转一点，于是「这是一叠、
    不止一张」由内容自己的边说出来（§7.19 判据一的第三类）。谁把它们改成 0，
    删提示的理由就没了——这条守卫红的时候说的是**「你刚把证人抽走了」**。

    这里只钉位移 / 缩放 / 旋转，**故意不钉 opacity**：`STACK_OPACITY` 那一条今天
    是死的（`.dv-card.fly-in` 的 `animation … both` 在层叠里压过行内 style，实测
    inline `1 / 0.75 / 0.5` 全部 computed 成 `1`），另立任务。**把一条死通道写进
    守卫，等于让守卫替一个不存在的证据作证。**
    """

    consts = dict(
        re.findall(r"const (STACK_OFFSET|STACK_SCALE|STACK_OPACITY)\s*=\s*([\d.]+)", SCRIPT)
    )
    assert set(consts) == {"STACK_OFFSET", "STACK_SCALE", "STACK_OPACITY"}, consts
    assert float(consts["STACK_OFFSET"]) >= 8, consts
    assert float(consts["STACK_SCALE"]) > 0, consts
    # 旋转也在写这句话（注释写着「像扑克牌扇形」），它是硬编码的每层度数，而**两处**
    # 都要算：一处首次渲染、一处翻页后重排。只断言「存在一个非零的」等于让改坏其中
    # 一处的人过关——同一句话写了两遍，守卫就得数两遍。
    rots = re.findall(r"const rot\s*=\s*layer\s*\*\s*([\d.]+)", SCRIPT)
    assert len(rots) == 2, rots
    assert all(float(v) > 0 for v in rots), rots


def test_the_gesture_is_not_the_only_road_to_another_card():
    """判据一的前提：拖拽是加速器而不是唯一的路。前提没了，结论也就没了。

    三条不用手势的通道各留一个锚：圆点点击跳转、左右方向键、单击卡片左右两侧。
    钉的是**通道还在**，不是某一行怎么写的——所以取语义上不可替换的那一小段。

    方向键那一条必须钉在**详情页那个 handler** 上：照片查看器另有一对
    `ArrowLeft`/`ArrowRight`（走 `pvGoTo`），只查这两个词的话，把详情页的方向键
    整段删掉守卫照样绿。**一个词在两个地方出现，它就不能当某一处的锚点。**
    """

    assert "dvDots.querySelectorAll('.dv-dot')" in SCRIPT, "圆点的点击绑定没了"
    assert "jumpToCard(idx)" in SCRIPT, "圆点点击跳转没了"
    arrows = re.search(
        r"if\s*\(\s*detailOpen\s*&&\s*detailCards\.length\s*>\s*1\s*\)\s*\{(.*?)\n  \}",
        SCRIPT,
        re.S,
    )
    assert arrows, "详情页的方向键 handler 没了"
    assert "ArrowLeft" in arrows.group(1) and "ArrowRight" in arrows.group(1), arrows.group(1)
    assert "triggerSwipe" in arrows.group(1), "方向键没接到翻页上"
    assert re.search(r"relX\s*<\s*rect\.width\s*\*\s*0?\.\d+", SCRIPT), "单击左侧翻页没了"
    assert re.search(r"relX\s*>\s*rect\.width\s*\*\s*0?\.\d+", SCRIPT), "单击右侧翻页没了"



def test_only_two_things_open_on_the_more_than_one_card_condition():
    """判据三：同一个显示条件底下不许有两个身体说同一句话。

    `detailCards.length` 这个数是详情页唯一的那个门。原先它开三样东西，现在开两样：
    顶栏的 `.dv-counter`（`2 / 4`）与卡堆下沿的 `.dv-dots`。**第三个只为说话而存在的
    元素回来，这条就红。**

    只数「拿这个数去开关一个元素、或者往它里面写字」的门，不数用它做别的判断的地方
    （能不能拖、单击要不要翻页、跳到第几张都在读同一个数，那些不是在说话）。

    `_TURNS_ON` 这张单子是变异测试逼出来的：第一版只认 `style.display` / `innerHTML` /
    `textContent`，于是一个用 `hidden=false` 开出来的第三个身体**从守卫旁边走过去了**。
    「把一个元素打开」在 DOM 里有六种写法，只认一种等于只守六分之一（§7.7）。
    """

    body = "|".join(_TURNS_ON)
    gates = re.findall(
        rf"(\w+)\.(?:{body})[^;\n]*?(?:detailCards\.length|\btotal\b)\s*(?:>|<=)\s*1",
        SCRIPT_NO_COMMENTS,
    )
    blocks = re.findall(
        r"if\s*\(\s*(?:detailCards\.length|\btotal\b)\s*(?:>|<=)\s*1\s*\)\s*\{(.*?)\n  \}",
        SCRIPT_NO_COMMENTS,
        re.S,
    )
    assert len(blocks) == 2, len(blocks)  # 空集会让下面那条断言恒真
    for blk in blocks:
        gates += re.findall(rf"(\w+)\.(?:{body})", blk)
    assert set(gates) == {"dvCounter", "dvDots"}, sorted(set(gates))


# --- #36 / §7.20：一个取不回来的东西，得有一个能被读到的身体 --------------------
#
# 待办把这一项记成「失败态与正常态只差 ΔE 4.74」，也就是当成一个**色差不够**的问题。
# 审计的结论比它大两层：
#
#   一、「失败」在全 app 有 5 个答案，其中 4 个不会说话。回声那一处是对的（一个形 +
#      一句字 + 一个动作，ΔE(失败环,等待环)=30.04）；4 个图片宿主只有一片洗色；
#      占满整屏的 `.pv-img` 连洗色都没有，只有一句 `console.warn`——一个铺满屏幕的
#      失败，屏幕上什么都没有；3 个 audio 宿主什么都没有（归 #37，待办把它记成两个）。
#
#   二、那片洗色不是「差得不够」，是**根本不该存在**。七档全试过（见下面那条反面守卫）：
#      能让它和最像的邻居——正常态那片底——差到 §7.10 一档的只有 --o-4 起，而从 --o-4
#      起「再试」就掉到 AA 4.5 之下。**一片为了让人看见失败而加深的底，会把那句说出
#      失败的话压掉。** 而一句话和一片色是两个身体，同一个显示条件下不许都说「没成」
#      （§7.19 判据三）。所以洗色归零，留下能被读的那一个。
#
# 判据一 失败是一个结构不是一个色：必须答三句话（哪一样没成 / 现在什么状态 / 接下来
#        能做什么），而身体只许用 §7.18 的三种。
# 判据二 一个色只有当它是**唯一的区别**时才必须差一档；不是唯一的区别，它就得是零
#        （§7.2）。而「一档」要量最像的那个邻居，不是裸底。
# 判据三 一个会自己消失的身体，不能承担一件用户还有事要做的事。

# 八个宿主，以及失败态那个身体在每一处坐在什么上面。语音那个气泡有两种身份色
# （它说的话 / 你说的话），所以八个宿主九个底。
# (宿主选择器, 它底下画底的那条规则；None = 直接坐在页底那道斜坡上)
_MEDIA_FAILURE_HOSTS = (
    (".le-img-grid .gi", None),
    (".le-img-band .gi", None),
    (".lap-img-item", ".lap-card"),
    (".dv-card", ".dv-mask"),
    (".pv-slide", ".photo-viewer"),
    (".le-voice", None),
    (".dv-voice", ".dv-mask"),
    (".mtp-message.assistant", ".mtp-card"),
    (".mtp-message.user", ".mtp-card"),
)

# 那个身体上的三种墨，各自要过的那一关。三个 token 都必须**从文件里读**：
# 第一版把它们硬写成 `--text-dim` / `--refuse` / `--text-faint`，于是变异测试第 11 项
# ——「把这句话连同回声一起降到 --text-faint」——从守卫旁边走过去了。抄写那条守卫看不出
# 差别（两边一起降的），而这条守卫压根没在看 CSS，它量的是自己写下的答案。§7.7 那个
# 形状的又一次。
def _media_failure_inks() -> tuple[tuple[str, str, float], ...]:
    """(名字, 墨的 token, 它要过的那个门槛)"""

    copy = _decls(re.sub(r"\s+", " ", _rule_body(".gi-unavailable-copy")))
    sentence = re.fullmatch(r"var\((--[\w-]+)\)", copy.get("color", ""))
    assert sentence, f".gi-unavailable-copy 的字色不是一个 token：{copy.get('color')!r}"
    retry = _decls(re.sub(r"\s+", " ", " ".join(_base_bodies("le-retry-inline"))))
    action = re.fullmatch(r"var\((--[\w-]+)\)", retry.get("color", ""))
    line = re.fullmatch(r"var\((--[\w-]+)\)", retry.get("text-decoration-color", ""))
    assert action and line, retry
    return (
        ("说明字", sentence.group(1), 4.5),            # 一句要被读的话：AA
        ("再试", action.group(1), 4.5),                # 一个要被读的动作：AA
        ("下划线", line.group(1), _NON_TEXT_MIN),       # 一条线是图形：WCAG 1.4.11
    )


def _slope() -> list[tuple[int, int, int]]:
    """页底不是一个值，是一道斜坡（抬面 → 深底），所以压在它上面的每一个合成结果
    都是**一段区间**。#36 第一遍只拿 `--bg-deep` 算，整片对比度都偏乐观——斜坡这件事
    本身归 #69，这里先把两端都量。"""

    stops = _layers_in(_BG_DECL.search(_rule_body("#screen-life")).group(1), _tier("--bg-deep"))
    assert len(stops) == 2, stops
    return stops


def _media_failure_floors(host: str, under: str | None) -> list[tuple[int, int, int]]:
    """这个宿主里，那句话真正坐在什么上面——静态合成，两端都算。"""

    if under is None:
        bases = _slope()
    else:
        decl = _BG_DECL.search(_rule_body(under))
        assert decl, f"{under} 不再画底了，{host} 的底得重算"
        bases = [layer for stop in _slope() for layer in _layers_in(decl.group(1), stop)]
    assert bases, under
    own = _BG_DECL.search(_rule_body(host))
    if not own or re.sub(r"\s+", "", own.group(1)) in ("none", "transparent"):
        return bases  # 宿主自己不画底：那句话坐在下面那一层上
    floors = [layer for stop in bases for layer in _layers_in(own.group(1), stop)]
    assert floors, (host, own.group(1))
    return floors


def test_a_lost_picture_is_not_answered_with_a_wash_of_colour():
    """判据二的落点：那片 `rgba(--refuse-rgb,--o-2)` 整个删掉了，一层都不许再画。

    删它的理由不是「洗色太淡」——淡是可以调的。理由是**七档里没有一档能用**，
    而那正是下面那条反面守卫在证的事。这里先钉住结果：`media-unavailable` 这个词
    出现在哪条规则里都不许画底，否则「失败靠一片色说话」就悄悄回来了。
    """

    painted = [
        (re.sub(r"\s+", " ", sel).strip(), body.strip())
        for sel, body in _top_level_rules()
        if _says_unavailable(sel) and _paints_a_floor(body)
    ]
    assert painted == [], painted
    # 尺子不许瞎：这条规则必须真的存在，否则上面那个空集是「没有规则」而不是「没有底」。
    rules = [sel for sel, _ in _top_level_rules() if _says_unavailable(sel)]
    assert rules, "media-unavailable 这条规则整个不在了，失败态的光标还在不在？"
    # 那两个 !important 也跟着走了：它们打的官司对手（加载态）现在在 JS 里被摘掉。
    for sel, body in _top_level_rules():
        if _says_unavailable(sel):
            assert "!important" not in body, (sel, body)
    # 于是取不回来的那一幅，脚下和正常那一幅是同一片底——区别整个搬到字上去了。
    for host, under in _MEDIA_FAILURE_HOSTS:
        assert _media_failure_floors(host, under) == _media_failure_floors(host, under)


def test_no_rung_of_the_ladder_could_have_carried_that_wash():
    """反面：这条路是**被量死的**，不是被口味否掉的。七档，一档都不成立。

    量的是最像的那个邻居——正常态那片底。（待办原先记的 5.44 量的是**裸底**，而裸底
    是这片色唯一不会被误认的东西：照片一回来就没有裸底了。拿最不像的邻居当尺子，
    量出来的差永远偏大。）

    两条腿必须都真的被踩到：低档输在 ΔE，高档输在对比度。只断言「每一档至少输一条」
    是不够的——如果七档全输在同一条腿上，那说明的是另一件事（比如尺子只有一条腿在动）。
    """

    normal = _media_failure_floors(".le-img-grid .gi", None)
    inks = _media_failure_inks()
    lost_on_delta_e, lost_on_contrast = [], []
    for rung in range(1, 8):
        alpha = float(_root_value(f"--o-{rung}"))
        for floor in normal:
            washed = colour.over(_ROOT_RGB["--refuse-rgb"], floor, alpha)
            unreadable = [
                (label, colour.contrast(_ROOT_RGB[token], washed))
                for label, token, floor_min in inks
                if colour.contrast(_ROOT_RGB[token], washed) < floor_min
            ]
            if colour.delta_e(washed, floor) < 15:
                lost_on_delta_e.append(rung)
            elif unreadable:
                lost_on_contrast.append(rung)
            else:
                raise AssertionError(
                    f"--o-{rung} 在 {floor} 上竟然两关都过了："
                    f"ΔE {colour.delta_e(washed, floor):.2f}、"
                    + "、".join(f"{label} {colour.contrast(_ROOT_RGB[t], washed):.2f}:1" for label, t, _ in inks)
                    + "——那这一项的结论得重判"
                )
    assert set(lost_on_delta_e) == {1, 2, 3}, sorted(set(lost_on_delta_e))
    assert set(lost_on_contrast) == {4, 5, 6, 7}, sorted(set(lost_on_contrast))


def test_the_body_that_says_it_borrows_the_echo_and_invents_nothing():
    """判据一：失败态的身体照回声那一处写——零个新值、零个新词汇。

    回声那一处（`.le-agent.le-status.failed`）是全 app 唯一一个把失败说对了的地方，
    所以这里不该有第二种写法：一句直立的降级说明（§8.2）+ 一个「一段字就是按钮」
    （§7.18）。六个属性逐个对着它抄，抄的是**那条规则**而不是六个字面值——回声改了
    字号，这里就得跟着改，否则同一句话在两个地方是两个样子。

    「直立」在这里是白拿的：回声那一处必须写 `font-style:normal`，因为 `.le-agent-copy`
    本身是斜的；`.gi-unavailable-copy` 没有斜体祖先，什么都不用写。这条守卫钉住那个
    **区别**还在——`.le-agent-copy` 斜、它的失败态正，否则 §8.2「直立 = 降级说明」
    在文件里就没有证人了。
    """

    echo = _decls(re.sub(r"\s+", " ", _rule_body(".le-agent-copy")))
    failed = _decls(re.sub(r"\s+", " ", _rule_body(".le-agent.le-status.failed .le-agent-copy")))
    assert echo.get("font-style") == "italic", echo
    assert failed.get("font-style") == "normal", failed
    expected = dict(echo)
    expected.update(failed)
    mine = _decls(re.sub(r"\s+", " ", _rule_body(".gi-unavailable-copy")))
    for prop in ("font-family", "font-weight", "font-size", "line-height", "letter-spacing", "color"):
        assert mine.get(prop) == expected.get(prop), (prop, mine.get(prop), expected.get(prop))

    # 判据一的第二半：这个身体是「一段字」，不是第四种身体。它一旦长出盒子的线索，
    # §7.18 把 `.le-retry-inline` 归进「一段字就是按钮」的那个分类就当场失效。
    # 这两行同时是上一条守卫的**前提**：`.gi-unavailable` 自己一画底，那句话真正坐的
    # 那片底就不再是 `_media_failure_floors` 算出来的那一片，而那条守卫不会知道。
    for label, pat in _BOX_CUES:
        assert not pat.search(_rule_body(".gi-unavailable")), f".gi-unavailable 长出了{label}"
    for label, pat in _BOX_CUES:
        assert not pat.search(_rule_body(".gi-unavailable-copy")), f".gi-unavailable-copy 长出了{label}"

    # 三句话都得在：哪一样没成（这一张 / 这段语音）、现在什么状态（取不回来）、
    # 接下来能做什么（再试）。#37 之后量词有两个，因为一张照片和一段语音不能共用一个
    # 量词；除了量词以外这句话只许有**一份**，否则同一件事在两处会漂成两句话。
    src = _comment_blanked_app()
    assert len(re.findall(r"\+\s*'取不回来。'", src)) == 1, re.findall(r"'[^']*取不回来[^']*'", src)
    assert sorted(re.findall(r"'(这段语音|这一张)'", src)) == ["这一张", "这段语音"], re.findall(
        r"'(这段语音|这一张)'", src
    )
    assert re.search(r"className\s*=\s*'le-retry-inline'", src), "「再试」这个动作不在了"
    assert re.search(r"textContent\s*=\s*'再试'", src), "「再试」这两个字不在了"


def test_the_words_that_say_it_can_be_read_on_every_floor_they_land_on():
    """五个宿主的底各不相同，而这句话在**每一个**上面都得能读。

    这是 #36 真正买回来的东西：洗色归零之后，唯一说出失败的东西就是这句话，于是它
    读不到就等于失败没被说出来。三种墨各自那一关：一句要被读的话与一个要被读的动作
    过 AA 4.5:1，一条线是图形、过 WCAG 1.4.11 的 3:1。

    每个宿主都量两端——页底是一道斜坡，抬面那一端最亮，也就是最难读的那一端。
    """

    seen: list[tuple[int, int, int]] = []
    inks = _media_failure_inks()
    for host, under in _MEDIA_FAILURE_HOSTS:
        floors = _media_failure_floors(host, under)
        assert floors, host
        seen.extend(floors)
        for floor in floors:
            for label, token, floor_min in inks:
                got = colour.contrast(_ROOT_RGB[token], floor)
                assert got >= floor_min, (host, label, round(got, 2), floor_min, floor)
    # 尺子不许瞎：五个宿主的底必须真的是几种不同的底，否则这一圈等于只量了一处。
    assert len({tuple(round(v) for v in f) for f in seen}) >= 4, sorted(
        {tuple(round(v) for v in f) for f in seen}
    )
    # §7.18 判据三：字与线之间也得隔一档，否则那条线不是线索，是同一句话说了两遍。
    line_ink = _ROOT_RGB[inks[2][1]]
    for label, token, _ in inks[:2]:
        assert colour.delta_e(_ROOT_RGB[token], line_ink) >= 15, (
            label, colour.delta_e(_ROOT_RGB[token], line_ink)
        )


def test_every_host_that_can_lose_a_picture_walks_the_same_one_road():
    """一处失败一种写法，就会有一处失败没有写法——`.pv-img` 原先就是那一处。

    它自己写了一份取回逻辑，`catch` 里只有一句 `console.warn`，于是一个**占满整屏**的
    失败在屏幕上什么都没有。并回 `hydrateProtectedMedia` 之后，八个宿主走同一条路，
    说同一句话。

    生活流那个播放键原先是第二处：它自己 `await loadProtectedMedia`，失败只有一句
    1800ms 的提示。#37 把它也并了回来（`await hydrateOneMedia(audio)`，然后看
    `audio.src` 有没有），于是那一行里既然会长出那句话，就不该再有第二个说法。

    剩下三处自己写的取回路径全都不是「一个装在页面上的媒体节点」：Live Photo 的
    两处 video 是当场造出来的、相册抽屉那一处是 `new Audio(objectUrl)`（草稿区试听，
    页面上根本没有它的宿主）。这个数字钉在这里：再冒出一条**挂在页面上的**取回路径，
    它就红。
    """

    src = _comment_blanked_app()
    calls = [m.start() for m in re.finditer(r"(?<!function )loadProtectedMedia\s*\(", src)]
    assert len(calls) == 4, len(calls)
    outside = [src[max(0, i - 260) : i] for i in calls if "hydrateOneMedia" not in src[max(0, i - 900) : i]]
    assert len(outside) == 3, len(outside)
    for ctx in outside:
        assert re.search(r"\b(video|audio|voice|motion|Audio)\b", ctx), ctx[-200:]
    # 那条并回去的路必须真的接上了，而那句只会写进控制台的失败必须不在了。
    assert "hydrateProtectedMedia(pvTrack)" in src, "照片查看器没接回同一条路"
    assert "[photo] load failed" not in src, "那句只对控制台说的失败还在"
    assert "[life-voice] load failed" not in src, "生活流那个播放键又自己写了一份取回"
    # 八个宿主由一个函数分派：替换型元素装不了孩子，所以挂到它外面那一层。
    host_fn = re.search(r"function mediaFailureHost\(node\)\{(.*?)\n\}", src, re.S)
    assert host_fn, "mediaFailureHost 不在了"
    assert "return null" not in host_fn.group(1), "又有一支返回 null——那一支等于一处没有身体的失败"
    for tag in ("IMG", "AUDIO", "VIDEO"):
        assert tag in host_fn.group(1), f"{tag} 那一支不再往父节点挂"
    assert "parentElement" in host_fn.group(1), "替换型元素那一支不再往父节点挂"


def test_the_body_that_says_it_stays_until_the_thing_comes_back():
    """判据三：说「取不回来」的那个身体不许自己消失，因为用户还有一件事要做。

    全 app 82 处提示走同一个身体——一个 `#toast` 元素 + `.show` + 一个 **1800ms** 的
    `setTimeout`，成功与失败共用它。失败态**不能**是其中之一：一个 1800ms 后就没了的
    东西，没法承担「再试」。所以这个身体只由一件事拿走——那一张真的回来了。

    「再试」这一下必须走**捕获阶段**：它坐在五个宿主里，而生活流、相册抽屉、详情页
    三处各有一个祖先在冒泡阶段监听点击，三处都会把它解释成「打开这张照片」。三条
    拖拽/长按通道也各要一个让路——它坐在卡片正中，而那张卡的单击命中区是左 40% /
    右 60%，两边都够不到它，可 `pointerup` 仍会先跑一遍翻页判定。
    """

    src = _comment_blanked_app()
    speak = re.search(r"function speakMediaUnavailable\(node\)\{(.*?)\n\}", src, re.S)
    assert speak, "speakMediaUnavailable 不在了"
    for vanishing in ("setTimeout", "toast(", "showToast"):
        assert vanishing not in speak.group(1), f"失败态借了会自己消失的身体：{vanishing}"
    # 唯一拿走它的地方是成功那一支——外加「这一行换成另一段语音了」那一处。
    clears = re.findall(r"clearMediaUnavailable\(", src)
    assert len(clears) == 3, len(clears)  # 一处定义、成功那一支、详情页换语音那一处
    hydrate = re.search(r"async function hydrateOneMedia\(node\)\{(.*?)\n\}", src, re.S)
    assert hydrate, "hydrateOneMedia 不在了"
    body = hydrate.group(1)
    ok, _, failed = body.partition("}catch(")
    assert "clearMediaUnavailable(node)" in ok, "失败态的身体不是在照片回来时才被拿走的"
    assert "clearMediaUnavailable" not in failed, failed
    # 加载态与失败态互斥：这一句原先只写在成功与回退两条路上，失败那条没写。
    assert "classList.remove('media-loading')" in failed, failed

    # 捕获阶段：`true` 那个参数就是这一条能不能到达 handler 的全部。
    # 不许用 `addEventListener\('click',\(e\)=>\{(.*?)\n\},\s*true\)` 去找它——变异测试
    # 第 8 项（把 `},true);` 改成 `});`）正是这么漏网的：`.*?` 跨过被改坏的那一处，
    # 一直吃到文件后面**另一个**以 `},true)` 收尾的监听，于是守卫在别人的括号上打了绿灯。
    # 改成先按那个独一无二的选择器定位，再看它自己那一对括号怎么收尾。
    anchor = src.index(".gi-unavailable .le-retry-inline")
    start = src.rindex("addEventListener(", 0, anchor)
    end = src.index("\n}", anchor)
    listener = src[start:end]
    assert re.match(r"\n\},\s*true\)", src[end : end + 16]), (
        "「再试」那一下退回冒泡阶段了：" + src[end : end + 16].strip()
    )
    assert "'click'" in listener, listener[:80]
    assert "stopPropagation" in listener, "没拦住冒泡阶段那三个祖先"
    assert "retryProtectedMedia" in listener, listener
    # 三条手势通道各让一次路，加上「取不回来的那一幅打不开」。
    assert len(re.findall(r"closest\('\.gi-unavailable'\)\)return", src)) == 3, re.findall(
        r"closest\('\.gi-unavailable'\)\)return", src
    )
    assert re.search(
        r"classList\.contains\('media-unavailable'\)\)\s*return", src
    ), "取不回来的那一幅还在假装自己能打开"


_PRESSABLE_CURSORS = {"pointer", "grab"}
_SAYS_NOTHING = {"none", "0", "0px", "transparent", "auto", "initial"}


def _last_compound_classes(selector: str) -> set[str]:
    """选择器里**主语**那一节的类名——`A B` 说的是 B，不是 A。"""

    out: set[str] = set()
    for part in selector.split(","):
        chunks = part.strip().split()
        if chunks:
            out.update(re.findall(r"\.([\w-]+)", chunks[-1]))
    return out


def test_a_host_that_says_it_cannot_be_fetched_stops_saying_it_can_be_pressed():
    """判据三（§7.21）：「取不回来」与「按我就能听」不许同时在场。

    「能按」不是一件事，是**四条各自独立的通道**：看得见的零件、指针、读屏器、
    以及按下去真正发生的事。留着任何一条，那句话就变成一个骗人的按钮。语音这三处
    原先四条全开着——因为它们连那句话都没有（`mediaFailureHost` 对 AUDIO 返回 null）。

    第一条通道的正面问法不是「哪些类名要闭上」（清单会漏掉下一个被加进去的零件），
    而是**这个宿主是不是那段语音本身**：`.le-voice` / `.dv-voice` 的名字是它那个
    `<audio>` 类名的前缀，说明整行都只为这段语音存在，于是它的每一个零件都得闭上；
    `.mtp-message` 不是（它是一条消息，还要接着说话），于是只有代表那段语音的那个
    `<audio>` 要闭上。

    第二条通道里**继承**来的那一份读不出来——`.le-voice` 自己没写 `cursor`，它的
    手指是从 `.life-entry` 继承下来的，而继承走 DOM、不走这份 CSS 文本。那一份由
    §7.21 那张表按浏览器实测记账（一次就抓到了：改前 `.le-voice` 算出 `pointer`）。

    第四条通道也不是名单：只有「宿主自己就是那个可按的东西」才需要一条早退，而这件事
    由 HTML 自己说——它写了 `role="button"`。零件被第一条通道拿走的地方，那一下连落点
    都没有。
    """

    rules = _top_level_rules()
    src = _comment_blanked_app()

    # ── 通道一：看得见的零件 ───────────────────────────────────────────
    shut: set[str] = set()
    for sel, body in rules:
        if _says_unavailable(sel) and _decls(body).get("display") == "none":
            shut |= _last_compound_classes(sel)
    assert shut, "失败态里一条把零件闭上的规则都没有——通道一整条不设防"

    media = _page_media()
    assert len(media) == 3, media  # 页面上装着的那三段语音；其余是当场造的
    hosts = _failure_hosts()
    normally_hidden = {
        cls
        for sel, body in rules
        if _decls(body).get("display") == "none" and not _says_unavailable(sel)
        for cls in _last_compound_classes(sel)
    }
    for medium in media:
        row = next((h for h in hosts if medium.startswith(h + "-")), None)
        if row is None:
            # 宿主不是这段语音本身：只有它要闭上，别的零件是另一件事的零件。
            assert medium in shut, f"{medium} 在失败态还占着像素"
            continue
        parts = {c for c in re.findall(r"\.({}-[\w-]+)".format(re.escape(row)), CSS_NO_COMMENTS)}
        assert len(parts) >= 3, (row, parts)
        for part in parts:
            assert part in shut or part in normally_hidden, f"{part} 在失败态还占着像素"

    # 通道一还有另一半：hover 与 active 是「按我就能听」说得最响的两种——手一放上去
    # 底色就深一档、按下去就暗一下，而这一下什么都不会发生。所以一条主语是失败宿主的
    # hover/active 规则，要么点名 `:not(.media-unavailable)`，要么它一句正面的话都
    # 没说（`.life-entry:hover` 整条只有 `none` 与 `0`，它是在拆玻璃拟态，不是在回应手）。
    for sel, body in rules:
        if not re.search(r":(hover|active)\b", sel) or not (_last_compound_classes(sel) & hosts):
            continue
        positive = {k: v for k, v in _decls(body).items() if v not in _SAYS_NOTHING}
        if positive:
            assert ":not(.media-unavailable)" in sel, f"{sel} 在失败态还回应手：{sorted(positive)}"

    # ── 通道二：指针 ──────────────────────────────────────────────────
    for sel, body in rules:
        if _says_unavailable(sel):
            assert _decls(body).get("cursor") not in _PRESSABLE_CURSORS, sel
    calmed = {
        cls
        for sel, body in rules
        if _says_unavailable(sel) and _decls(body).get("cursor") == "default"
        for cls in _last_compound_classes(sel)
    } - {"media-unavailable"}
    assert calmed <= hosts, calmed - hosts  # 名单里不许出现第九个名字
    for sel, body in rules:
        if _decls(body).get("cursor") in _PRESSABLE_CURSORS and not _says_unavailable(sel):
            claiming = _last_compound_classes(sel) & hosts
            assert claiming <= calmed, f"{sel} 让宿主 {claiming - calmed} 在失败态还留着手指"

    # ── 通道三：读屏器 ────────────────────────────────────────────────
    speak = re.search(r"function speakMediaUnavailable\(node\)\{(.*?)\n\}", src, re.S).group(1)
    clear = re.search(r"function clearMediaUnavailable\(node\)\{(.*?)\n\}", src, re.S).group(1)
    for attr in ("role", "tabindex", "aria-label"):
        assert f"removeAttribute('{attr}')" in speak, f"读屏器那边还听得见 {attr}"
        assert f"setAttribute('{attr}'" in clear, f"{attr} 回来之后不再被交还"
    stash = re.search(r"dataset\.(\w+)=", speak).group(1)
    assert f"dataset.{stash}" in clear, "存起来的那把钥匙和取出来的不是同一把"

    # ── 通道四：按下去真正发生的事 ────────────────────────────────────
    # 只有「宿主自己就是那个可按的东西」才需要一条早退——零件被通道一拿走的地方，
    # 那一下根本没有落点可言。判据不是一张名单，是它自己在 HTML 里写了 role="button"
    # （这也正是通道三那个 `getAttribute('role')==='button'` 分支的入口条件）。
    self_pressable: list[tuple[str, str]] = []
    for tag in re.findall(r"<\w+\b[^>]*\brole=\"button\"[^>]*>", src):
        cls = re.search(r'class="([^"]*)"', tag)
        node_id = re.search(r'id="(\w+)"', tag)
        if cls and node_id and set(cls.group(1).split()) & hosts:
            self_pressable.append((node_id.group(1), cls.group(1)))
    assert self_pressable, "一个自己就写了 role=button 的失败宿主都没有——通道四整条不设防"
    for node_id, cls in self_pressable:
        assert re.search(
            rf"{node_id}\.classList\.contains\('media-unavailable'\)\)\s*return", src
        ), f"{cls}（#{node_id}）按下去还会真的去播"


def _failure_hosts() -> set[str]:
    """九行宿主折成七个类名：`.gi` 两处同名，气泡两种身份色是同一个类。"""

    return {re.findall(r"\.([\w-]+)", h.split()[-1])[0] for h, _ in _MEDIA_FAILURE_HOSTS}


def _fold_identity(sel: str) -> str:
    """把主语上的身份色折掉：`.mtp-message.assistant` → `.mtp-message`。

    容器那一节留着——`.le-img-grid .gi` 与 `.le-img-band .gi` 是两个宿主，不是一个。
    """

    chunks = sel.split()
    chunks[-1] = "." + re.findall(r"\.([\w-]+)", chunks[-1])[0]
    return " ".join(chunks)


def _page_media() -> list[str]:
    """页面上真的装着的那几段媒体（其余是当场造出来的，没有宿主）。"""

    return [m.split()[0] for m in re.findall(r"<audio\b[^>]*?class=\"([\w -]+)\"", _comment_blanked_app())]


def _host_of(medium: str, src: str) -> str:
    """一段媒体装在哪个宿主里：往前找最后一次被提到的宿主类名。"""

    i = src.index(f'class="{medium}"')
    seen = [c for m in re.finditer(r'class="([^"]*)"', src[:i]) for c in m.group(1).split() if c in _failure_hosts()]
    assert seen, medium
    return seen[-1]


def _anything_after(medium: str, src: str) -> bool:
    """那段媒体后面还有没有别的孩子——`</audio>` 之后第一个标签是不是闭合标签。

    这就是「宿主整块是不是只属于这一个媒体」那个判据的可计算形式，也正是那个盒子
    插在 `node.after(box)` 而不是 `host.appendChild(box)` 的理由。
    """

    j = src.index("</audio>", src.index(f'class="{medium}"')) + len("</audio>")
    return src[src.index("<", j) + 1] != "/"


def _keeps_a_place(cls: str) -> bool:
    """这个宿主在媒体没来的时候还撑不撑着一块地——它自己声明过高度。"""

    for sel, body in _top_level_rules():
        if cls not in _last_compound_classes(sel):
            continue
        for prop in ("aspect-ratio", "height", "min-height"):
            if _decls(body).get(prop, "none") not in _SAYS_NOTHING:
                return True
    return False


def test_the_sentence_only_covers_a_host_that_kept_a_place_for_it():
    """判据二（§7.21）：在流里占一行是默认，覆盖是特例。

    覆盖要成立得同时满足两件事，而三个语音宿主各缺一件：

    （一）**宿主整块都只属于这一个媒体。** `.mtp-message` 不是——它是一条消息，那段
    语音下面还有「为什么是现在」「来自…」「听它说」，覆盖会把它们一起盖掉。可计算的
    形式就是「那段媒体后面还有没有别的孩子」。

    （二）**宿主在这个媒体没来的时候仍然撑着一块地。** 五个图片宿主全都自己声明了高度
    （`aspect-ratio` / `height`），照片没来它们照样占着那块地；两条语音行**一个都没有**
    ——它们的高度就是内容的高度，而通道一刚刚把内容全 `display:none` 了。于是一个
    `position:absolute;inset:0` 的覆盖层会被压成 **0 高**：一句被静音的话比一句没写的话
    更危险，因为仪器会报「已经说了」。

    所以对语音行来说「覆盖」根本不是一个选项——它没有可覆盖的地方。这条守卫两个方向
    都钉：覆盖名单里多一个不撑地的、或者少一个撑着地的，都红。
    """

    rules = _top_level_rules()
    src = _comment_blanked_app()
    hosts = _failure_hosts()

    covering = [
        sel
        for sel, body in rules
        if ">.gi-unavailable" in re.sub(r"\s+", "", sel) and _decls(body).get("position") == "absolute"
    ]
    assert len(covering) == 1, covering
    covered = {
        re.findall(r"\.([\w-]+)", part.split(">")[0].strip().split()[-1])[0]
        for part in covering[0].split(",")
    }
    assert covered == {c for c in hosts if _keeps_a_place(c)}, covered
    assert covered < hosts, "全部宿主都在覆盖名单里，判据二没有对手"

    # 文档里那张表是同一件事的账本（§7.15）：三列逐行重算。这张表一行一个**宿主**
    # （八个），而 §7.20 那张一行一片**底**（九片，气泡的两种身份色各算一片）。
    doc = [r for r in _doc_rows("### 7.21") if len(r) == 4 and r[3] in ("覆盖", "在流里")]
    assert {r[0] for r in doc} == {_fold_identity(h) for h, _ in _MEDIA_FAILURE_HOSTS}, [r[0] for r in doc]
    assert len(doc) == len({r[0] for r in doc}), [r[0] for r in doc]
    media = _page_media()
    for row in doc:
        cls = re.findall(r"\.([\w-]+)", row[0].split()[-1])[0]
        mine = [m for m in media if _host_of(m, src) == cls]
        sole = not any(_anything_after(m, src) for m in mine)
        assert row[1].startswith("是") == sole, f"{row[0]}：整块是不是只属于这一个媒体，文档写 {row[1]!r}"
        assert row[2].startswith("是") == _keeps_a_place(cls), f"{row[0]}：撑不撑着一块地，文档写 {row[2]!r}"
        assert (row[3] == "覆盖") == (cls in covered), f"{row[0]}：几何，文档写 {row[3]!r}"


def test_the_cursor_ledger_in_the_doc_is_what_the_browser_saw():
    """§7.21 那张指针表：第二条通道有一半在 CSS 文本里读不出来，所以它有一本账。

    `cursor` 会**继承**，而继承走 DOM、不走这份 CSS 文本。`.le-voice` 自己一个字的
    `cursor` 都没写，它的手指是从 `.life-entry` 继承下来的——浏览器实测一次就抓到了
    改前那个 `pointer`，而任何一条读 CSS 文本的守卫都抓不到。

    于是 `.le-voice.media-unavailable{cursor:default}` 是全 app 唯一一条**对手不在这份
    文本里**的声明。按「没有对手就删掉」把它清掉（#54 要做的正是这件事），屏幕上就会
    回到一个取不回来的语音顶着一根手指，而没有一条守卫会红。这条守卫因此从那张表
    **反过来**钉 CSS：第二列写了 `pointer`/`grab` 的必须在名单里，写「没有人给」的必须
    不在。这不是一张要维护的名单，是一张证据表——那一列一旦和实测不符，它就得重测。
    """

    rules = _top_level_rules()
    hosts = _failure_hosts()
    calmed = {
        cls
        for sel, body in rules
        if _says_unavailable(sel) and _decls(body).get("cursor") == "default"
        for cls in _last_compound_classes(sel)
    } - {"media-unavailable"}

    doc = [r for r in _doc_rows("### 7.21") if len(r) == 4 and r[3] in ("default", "auto")]
    assert len(doc) == len(_MEDIA_FAILURE_HOSTS), [r[0] for r in doc]
    assert {r[0] for r in doc} == {h for h, _ in _MEDIA_FAILURE_HOSTS}, [r[0] for r in doc]
    wanted, spared = set(), set()
    for row in doc:
        cls = re.findall(r"\.([\w-]+)", row[0].split()[-1])[0]
        hand = re.search(r"\b(pointer|grab)\b", row[1])
        assert bool(hand) == (row[2] == "要"), f"{row[0]}：有没有手 / 要不要 default 对不上：{row[1]!r} {row[2]!r}"
        assert row[3] == ("default" if hand else "auto"), f"{row[0]}：实测那一列写 {row[3]!r}"
        (wanted if hand else spared).add(cls)
        own = {
            _decls(body).get("cursor")
            for sel, body in rules
            if cls in _last_compound_classes(sel) and not _says_unavailable(sel)
        } & _PRESSABLE_CURSORS
        if "继承" in row[1]:
            assert not own, f"{row[0]} 自己写了 {own}，那一格不该记成继承"
        elif hand:
            assert hand.group(1) in own, f"{row[0]}：文档说自己写 {hand.group(1)}，CSS 里是 {own}"
        else:
            assert not own, f"{row[0]}：文档说没有人给手，CSS 里是 {own}"
    assert wanted and spared, (wanted, spared)  # 两边都得有人，否则这张表没在分辨什么
    assert wanted | spared == hosts, (wanted | spared) ^ hosts
    assert calmed == wanted, f"CSS 那份名单是 {sorted(calmed)}，表说该是 {sorted(wanted)}"


def test_the_ground_under_a_run_of_words_is_a_table_of_homes_not_one_home():
    """§7.18 那把尺子是**一个类一个底**，而 `.le-retry-inline` 现在有十个家。

    这是 #36 翻出来的结构缺陷，形状和 §7.7 一模一样：`_TEXT_BUTTON_GROUND` 给
    `le-retry-inline` 记的是一个答案（页底），于是它只替**第一个**家作证，另外九个底
    一个都没被量到——而尺子不会报错，它会报绿。

    这两张表因此是同一把尺子的两半：§7.18 那一张管回声那个家，`_MEDIA_FAILURE_HOSTS`
    管另外九个（八个宿主，语音那个气泡有两种身份色）。1 + 9 = 10，而 10 必须等于文件里
    真的有多少个底会托着这个按钮。

    这个缺陷今天**还没变成错**，但它的方向是坏的：§7.18 答的那个底（页底）比九个
    真实的家里最亮的那一个还暗，也就是它报出来的数字**偏乐观**。洗色一旦回来（那正是
    上面两条守卫在防的事），偏乐观就会变成放行一句读不到的话。
    """

    src = _comment_blanked_app()
    births = re.findall(r"""(?:class\s*=\s*["'][^"']*\ble-retry-inline\b|className\s*=\s*'le-retry-inline')""", src)
    assert len(births) == 2, births  # 回声那一处模板串 + speakMediaUnavailable 里那一个
    assert _TEXT_BUTTON_GROUND["le-retry-inline"] is None, _TEXT_BUTTON_GROUND
    assert len(_MEDIA_FAILURE_HOSTS) == 9, _MEDIA_FAILURE_HOSTS

    echo_floor = _ground_under("le-retry-inline")
    real = [f for host, under in _MEDIA_FAILURE_HOSTS for f in _media_failure_floors(host, under)]
    inks = _media_failure_inks()
    for label, token, _ in inks:
        ink = _ROOT_RGB[token]
        one_answer = colour.contrast(ink, echo_floor)
        worst = min(colour.contrast(ink, f) for f in real)
        assert one_answer > worst, (
            f"{label}：§7.18 那一个答案（{one_answer:.2f}:1）不再比九个家里最坏的"
            f"（{worst:.2f}:1）乐观了，这条守卫的说法得重写"
        )
    # 而今天这份乐观还没有放行任何读不到的东西——上面那条守卫量的是真的那九个底。
    for label, token, floor_min in inks:
        assert min(colour.contrast(_ROOT_RGB[token], f) for f in real) >= floor_min, label


def _doc_ends(cell: str) -> list[str]:
    """一格里的区间两端。写成一个数的格子（斜坡两端四舍五入后同值）返回一个。"""

    txt = cell.replace("✓", "").replace("✗", "").strip()
    return [p for p in txt.split("–") if p]


def _doc_says(cell: str, values: list[float], what: str) -> None:
    """这一格必须逐位等于今天重算出来的那段区间——精度由格子自己写的小数位数定。"""

    ends = _doc_ends(cell)
    assert 1 <= len(ends) <= 2, (what, cell)
    dec = len(ends[0].partition(".")[2])
    got = [f"{min(values):.{dec}f}", f"{max(values):.{dec}f}"]
    want = ends if len(ends) == 2 else [ends[0], ends[0]]
    assert got == want, f"{what}：文档写 {cell!r}，今天算出来是 {got}"


def test_the_two_tables_in_the_doc_are_recomputed_from_todays_files():
    """§7.15 落到 §7.20 上：这两张表里的每一个数都得能从今天的文件里重算出来。

    七档那张表是这一项的**论证**——「读得下去的只有前两档，而前两档最多到 ΔE 5.80」
    这句话整个立在它上面。五个宿主那张表是这一项**买回来的东西**。两张表写旧了就是
    红的，而失败信息里印着今天该写成什么。
    """

    rows = _doc_rows("### 7.20")
    inks = _media_failure_inks()

    # 七档：α 从 `:root` 读，四列各自重算。
    ladder = {r[0]: r for r in rows if re.fullmatch(r"--o-[1-7]", r[0])}
    assert len(ladder) == 7, sorted(ladder)
    normal = _media_failure_floors(".le-img-grid .gi", None)
    for rung in range(1, 8):
        row = ladder[f"--o-{rung}"]
        assert len(row) == 6, row
        alpha = float(_root_value(f"--o-{rung}"))
        assert float(row[1]) == alpha, (row[1], alpha)
        washed = [colour.over(_ROOT_RGB["--refuse-rgb"], f, alpha) for f in normal]
        _doc_says(row[2], [colour.delta_e(w, f) for w, f in zip(washed, normal)], f"--o-{rung} 的 ΔE")
        for i, (label, token, _) in enumerate(inks):
            _doc_says(row[3 + i], [colour.contrast(_ROOT_RGB[token], w) for w in washed], f"--o-{rung} 的{label}")

    # 五个宿主：靠「第二格是一串合成色」认行——第一张表里那一行的宿主名长得一样，
    # 只按名字认会把它一起认进来。
    floor_rows = [r for r in rows if len(r) == 5 and re.match(r"^\(\d+,\d+,\d+\)", r[1])]
    assert len(floor_rows) == len(_MEDIA_FAILURE_HOSTS), [r[0] for r in floor_rows]
    everything: list[tuple[int, int, int]] = []
    for host, under in _MEDIA_FAILURE_HOSTS:
        hits = [r for r in floor_rows if r[0].startswith(host)]
        assert len(hits) == 1, (host, [r[0] for r in floor_rows])
        floors = _media_failure_floors(host, under)
        everything.extend(floors)
        said = [tuple(int(v) for v in t.split(",")) for t in re.findall(r"\((\d+,\d+,\d+)\)", hits[0][1])]
        assert said == [tuple(round(v) for v in f) for f in dict.fromkeys(floors)], (host, said, floors)
        for i, (label, token, _) in enumerate(inks):
            _doc_says(hits[0][2 + i], [colour.contrast(_ROOT_RGB[token], f) for f in floors], f"{host} 的{label}")

    worst = {r[0]: r for r in rows if r[0] in ("全体最坏", "门槛")}
    assert len(worst) == 2, sorted(worst)
    for i, (label, token, floor_min) in enumerate(inks):
        _doc_says(worst["全体最坏"][2 + i], [min(colour.contrast(_ROOT_RGB[token], f) for f in everything)], label)
        assert float(worst["门槛"][2 + i]) == floor_min, (label, worst["门槛"][2 + i], floor_min)

    # 字与线之间那一档，也写在散文里。
    line = _doc_line("### 7.20", "ΔE(说明字,线)")
    for label, token, _ in inks[:2]:
        got = colour.delta_e(_ROOT_RGB[token], _ROOT_RGB[inks[2][1]])
        assert f"**{got:.2f}**" in line, f"{label}↔线 今天是 {got:.2f}：{line}"


# --- #38：等待说给谁听（§7.22）----------------------------------------------


def _enclosing_fn(src: str, pos: int) -> str:
    """包着这个位置的那个顶层函数。

    「同一个主语」这件事只在一个函数体内部才有意义。用固定字符窗口去问会把
    `hydrateOneMedia` 成功那条路和失败那条路切开——它们相隔 900 多个字符，而那个
    `data-media-asset-id` 只在函数第一行出现一次。
    """

    starts = [m.start() for m in re.finditer(r"^(?:async )?function \w+\(", src, re.M)]
    before = [s for s in starts if s <= pos]
    assert before, pos
    after = [s for s in starts if s > pos]
    return src[before[-1] : after[0] if after else len(src)]


def _shimmer_rule() -> tuple[str, str]:
    """全文唯一一条画「在来」的规则。

    「唯一」这件事只有 `..._one_material...` 那条守卫在断言。覆盖面那条走下面那个函数，
    它对**所有**提到 `mediaShimmer` 的规则取并集——否则给某个宿主偷偷加写一条自己的
    规则，会让两条守卫同时红，而那两件事（覆盖面错了 / 材质分了两份）不是一件事。
    """

    hits = [(sel, body) for sel, body in _top_level_rules() if "mediaShimmer" in body]
    assert len(hits) == 1, [h[0] for h in hits]
    return hits[0]


def _shimmer_hosts() -> set[str]:
    """名单里那几个宿主。剥 `.media-loading` 是 `_fold_identity` 的活（它只留末节的第一个
    类），这里再剥一遍是空话——变异时把那一句删掉，四条守卫一条都没红。"""

    return {
        _fold_identity(re.sub(r"\s+", " ", part).strip())
        for sel, body in _top_level_rules()
        if "mediaShimmer" in body
        for part in sel.split(",")
    }


def _mount_hosts(src: str) -> set[str]:
    """JS 里真的会被挂上 `media-loading` 的那几个宿主类名。

    问的是「这个类落在了谁身上」，不是「哪个函数里出现过这个类」。前者要顺着接受者往上找
    它是怎么被起名的（`X.className='dv-card'…`），后者只要函数里有那个字符串就算——而
    `renderLifeEntry` 同时渲染 `.gi` 和 `.le-voice`，于是「函数里有」会把语音行也算成一个
    落点，明明没有一行代码给它挂过这个类。
    """

    found: set[str] = set()
    for m in re.finditer(r"media-loading", src):
        pre = src[: m.start()]
        add = re.search(r"(\w+)\.classList\.add\(\s*['\"]$", pre)
        if add:
            fn = _enclosing_fn(src, m.start())
            for c in re.finditer(rf"\b{add.group(1)}\.className\s*=\s*['\"`]([\w-]+)", fn):
                found.add(c.group(1))
            continue
        tag = re.search(r"class=\"([\w-]+)\$\{[^\"]*$", pre)
        if tag:
            found.add(tag.group(1))
    return found


def test_the_shimmer_speaks_exactly_where_no_second_body_is_already_there():
    """判据一（§7.22）：等待说给谁听 = 「这块地上没有属于这份媒体的第二个身体」的宿主。

    失败态的覆盖面是八个宿主一个不少（§7.21）——失败是终局，任何一个宿主都到得了。
    等待是过程，只有「这段时间里这块地上什么都没有」的宿主才需要一个身体去说它。所以
    两个覆盖面**不必**相等，但那个差必须能被一个正面问法算出来，而不是记成一张要维护
    的名单：这份媒体在等待期间，屏幕上有没有另一个属于它自己的、已经画好的东西？
    `.pv-slide` 有（用户刚点开的那张源瓦片），三个语音行有（播放键 + 20 根柱子 +
    时长）。这条守卫从文档那张表**反过来**钉 CSS，两个方向都钉。
    """

    doc = [r for r in _doc_rows("### 7.22") if len(r) == 3 and r[2] in ("要", "不要")]
    assert {r[0] for r in doc} == {_fold_identity(h) for h, _ in _MEDIA_FAILURE_HOSTS}, [r[0] for r in doc]
    assert len(doc) == len({r[0] for r in doc}), [r[0] for r in doc]
    # 表自己先要自洽：说「没有第二个身体」的那几行，必须正好是说「要」的那几行。
    for host, second, want in doc:
        assert second.startswith("没有") == (want == "要"), (host, second, want)
    want = {r[0] for r in doc if r[2] == "要"}
    assert _shimmer_hosts() == want, sorted(_shimmer_hosts() ^ want)
    assert want, "一个宿主都不说等待——先确认这层微光还在"
    assert want < {r[0] for r in doc}, "八个宿主全都说等待，判据一没有对手"


def test_the_waiting_is_one_material_with_one_definition():
    """判据二（§7.22）：这层微光只有一份定义，四个宿主共用同一条选择器。

    长卷是**横向**滚的，所以「微光要不要跟着滚动方向转」是一个真问题。答案是不转，而
    这个答案不靠约定维持，靠结构：长卷那一格是加进**同一条**选择器名单的，不是自己写
    一条规则。于是「同一句话只有一份定义」（§7.5）在结构上成立——两格算出来的每一个值
    都必然逐字相同，浏览器实测同一帧的 `background-position` 与 `opacity` 一模一样。
    **它是一层材质，不是一个方向。**
    """

    sel, body = _shimmer_rule()
    decls = _decls(body)
    assert "135deg" in decls.get("background-image", ""), decls.get("background-image")
    assert decls.get("background-size") == "200% 200%", decls.get("background-size")
    assert re.match(r"^mediaShimmer\b", decls.get("animation", "")), decls.get("animation")
    assert len(re.findall(r"@keyframes\s+mediaShimmer\b", CSS_NO_COMMENTS)) == 1
    # 整份 CSS 里提到这个类的次数，必须正好等于这条名单的节数——多出来的那一次就是
    # 「按宿主改写这层材质」的入口，而它可以躲在 `@media` 里（那里 `_top_level_rules()`
    # 看不见）。数的是节数而不是 4：名单该有几个宿主是判据一的事，不是这条守卫的事，
    # 否则从名单里删掉一格会让两条守卫同时红，而那是两件不同的病。
    assert CSS_NO_COMMENTS.count("media-loading") == len(sel.split(",")), CSS_NO_COMMENTS.count(
        "media-loading"
    )
    # 文档里那张表逐格重算：这条规则说的每一句话都得在表里，表里也不许有第四句。认行
    # 不按名字认——首格是「一个纯小写的 CSS 属性名」这个形状，判据一那张表的首格是
    # `.xxx` 宿主（带点带空格），形状对不上，不会被认进来。
    props = [
        r
        for r in _doc_rows("### 7.22")
        if len(r) == 3 and re.fullmatch(r"[a-z]+(?:-[a-z]+)*", r[0])
    ]
    assert {r[0] for r in props} == set(decls), ([r[0] for r in props], sorted(decls))
    # 这里比的是**字节**，不是量——全文件唯一一处（§7.15）。因为这张表的身份是这条 CSS
    # 规则的**抄本**：抄本的判据就是「逐字相同」，换一种拼法的抄本已经不是抄本了。
    # 判别法是机械的：抄本类改**任一侧**都会红（这一条在 app.html 改与在文档改都红），
    # 而按量比的那一类只有改被抄的那一侧才红。
    for prop, value, _ in props:
        assert value == decls[prop], (prop, value, decls[prop])


def test_the_loading_class_lands_on_the_element_that_carries_the_asset_id():
    """判据三（§7.22）：等待挂在带 `data-media-asset-id` 的那个元素上。

    §7.21 判到失败态的落点是 `mediaFailureHost(node)`——失败是一句**要被读**的话，得挂
    到一个装得下孩子的元素上。等待不是话，是宿主自己那块地的底色，所以它挂在带
    `data-media-asset-id` 的那个元素上。**两个落点不同，因为一个是话、一个是底。**

    这个落点在四个要说等待的宿主上**恰好就是宿主本身**。哪天某个 `data-media-asset-id`
    从宿主挪到宿主里面的一个元素上（`.pv-slide` 与三个语音行今天就是这样），等待就会
    **静音**——而没有任何一条只读 CSS 的守卫会红。
    """

    src = SCRIPT_NO_COMMENTS
    hits = list(re.finditer(r"media-loading", src))
    assert len(hits) == 6, [src.count("\n", 0, h.start()) + 1 for h in hits]
    for h in hits:
        fn = _enclosing_fn(src, h.start())
        where = re.sub(r"\s+", " ", src[max(0, h.start() - 90) : h.start() + 20])
        assert "data-media-asset-id" in fn, where
        m = re.search(r"(\w+)\.classList\.(?:add|remove)\(\s*['\"]$", src[: h.start()])
        if m:
            recv = m.group(1)
            assert re.search(rf"\b{recv}\.(?:get|set)Attribute\(\s*['\"]data-media-asset-id", fn), (recv, where)
        else:
            # 模板那一处：类和属性写在同一个标签上，并且由同一个 `assetId` 三元门控。
            assert "${assetId?' media-loading':''}" in fn, where
            tag = fn[fn.index("class=\"gi${assetId") :]
            tag = tag[: tag.index(">")]
            assert "${mediaAttrs}" in tag, tag
            assert re.search(r"mediaAttrs\s*=\s*assetId\?[^;]*data-media-asset-id", fn), "类和属性不是同一个条件"

    # CSS 名单里的每一个宿主类名，都要能在 JS 里找到「同一个接受者身上既挂这个类、又写
    # 这个属性」；反过来也要钉：一个在 JS 里被挂上、而 CSS 里没有人画的类名，就是 #38
    # 本身那个缺陷（挂了没人听）。这里比的是**类名**而不是完整选择器——两处 `.gi` 折成
    # 一个，因为同一个模板服务网格和长卷；容器那一层的差由判据一那条守卫钉。
    assert _mount_hosts(src) == {h.split()[-1].lstrip(".").split(".")[0] for h in _shimmer_hosts()}, (
        sorted(_mount_hosts(src)),
        sorted(_shimmer_hosts()),
    )


# --- #48 / §7.23：一个状态在面与线上落在哪一档 --------------------------------

# 「一个落在梯子上的取值」：`rgba(色, var(--o-N))`。色相原样留着字面量，因为
# 「换没换色」是一个字面事实，不需要解析成数字。
_RUNG_PICK = re.compile(r"rgba\(\s*(var\(--[\w-]+\)|[\d\s,]+?)\s*,\s*var\(--o-(\d)\)\s*\)")
_NEGATION = re.compile(r":not\([^()]*\)")
# 几何：`1px`、`solid`、空白。剥掉它们，剩下的是这条声明**画出来的颜色**本身——
# `border:1px solid rgba(X,var(--o-2))` 与 `border-color:rgba(X,var(--o-2))` 画的是
# 同一条线，不剥就永远比不出「状态逐字节重述了基线」。
_GEOMETRY = re.compile(r"\b\d+(?:\.\d+)?px\b|\b(?:solid|dashed|dotted|none)\b|\s+")
_HAND = re.compile(r":hover|:active")


def _tokens_that_are_a_rung() -> dict[str, str]:
    """自定义属性里，整条值就**是**「某个色 + 某一档」的那些。

    这不是一张要维护的名单：判据是**形状**（一条 `rgba(…, var(--o-N))` 占满整条值），
    所以以后再有人把一档包成 token，它自动被认进来；反过来，把 `--glass-border`
    改成手写 alpha，它也自动掉出去。

    展开时必须换成**整条** `rgba(...)`，不能只换档号：`border:1px solid
    var(--glass-border)` 只换档号会变成 `1px solid var(--o-3)`，不再是 rgba 的形状，
    于是基线被读成「这一族没有取值」——而那正是把死声明藏起来的形状（#48 审计的
    第五刀就是这样漏报了 `.map-btn.cancel:hover` 和 `.mtp-close:hover` 两处）。
    """

    out: dict[str, str] = {}
    for name, val in re.findall(r"(--[\w-]+)\s*:\s*([^;{}]+)", CSS_NO_COMMENTS):
        val = val.strip()
        if _RUNG_PICK.fullmatch(val):
            out[f"var({name})"] = val
    return out


def _expand_rung_tokens(value: str) -> str:
    for token, rgba in _tokens_that_are_a_rung().items():
        value = value.replace(token, rgba)
    return value


def _rung_picks(value: str) -> list[tuple[str, int]]:
    """这条声明里每一个落在梯子上的取值：(色相字面, 档号)。"""

    return [(re.sub(r"\s+", "", m.group(1)), int(m.group(2))) for m in _RUNG_PICK.finditer(value)]


def _is_state(part: str) -> bool:
    """这一条选择器分支说的是「现在不一样了」，还是「平时」。

    先把 `:not(...)` 整段抹掉再判：一条选择器里只剩否定态时，它说的是**平时**
    （`#inputField:not(.expanded) .voice-ico-btn:not(.recording)` 是基线的另一种
    写法），而 `_STATE_TOKEN` 把 `:not(` 也算作状态。

    变异测试里这一抹**今天没有对手**（拆掉它全绿，§7.23）：误分类要凑第二个条件
    才变成误报，而那条规则的基线在表里查不到东西。留着是因为它编码的是 CSS 语义。
    """

    return bool(_STATE_TOKEN.search(_NEGATION.sub("", part)))


def _painted(value: str) -> str:
    return _GEOMETRY.sub("", value)


def _faces_by_subject() -> tuple[dict[tuple[str, str], dict[str, str]], list[tuple[str, str, str, str]]]:
    """把面与线的每一条声明分成「平时」和「现在不一样了」两堆。

    键是 (选择器分支, 面/线)：一块面和一条线是两条通道，`background` 与
    `background-color` 是同一块面的两种写法，得放进同一格。
    """

    base: dict[tuple[str, str], dict[str, str]] = {}
    state: list[tuple[str, str, str, str]] = []
    for sel, prop, value in _face_and_line_declarations():
        if _TINT.match(prop):
            fam = "面"
        elif _EDGE.match(prop):
            fam = "线"
        else:
            continue
        value = _expand_rung_tokens(value)
        for part in sel.split(","):
            part = re.sub(r"\s+", " ", part).strip()
            if _is_state(part):
                state.append((part, fam, prop, value))
            else:
                base.setdefault((part, fam), {})[prop] = value
    return base, state


def _ruling_baselines(owners: list[str]) -> list[str]:
    """一组都覆盖同一条状态的基线里，层叠中赢的那些（>1 条 ⇒ 判不出谁赢，得出声）。"""

    return [x for x in owners if not any(y != x and _covers(x, y) for y in owners)]


def _baseline_under(
    base: dict[tuple[str, str], dict[str, str]], part: str, fam: str
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """这条状态声明脚下的基线：**逐个属性**取层叠里赢的那一条。

    返回 ({属性: (值, 那条基线的选择器)}, 判不出谁赢的那些属性)。

    先前这里拿「剥掉状态记号后的选择器字符串」当 dict 键，那把尺子对不上层叠的三处：
      · 层叠是**有方向**的。`.icon-btn:hover` 脚下压着的是 `.topbar .icon-btn`，字符串
        相等看不出来（今天 18 条状态因此一条基线都查不到）。方向也不能换成对称的「同
        一块材料」：基线更特化时（今天 21 对），那条状态只在一部分宿主上重述基线，它
        在别的宿主上仍然是一句话，不是死声明。
      · 一条状态脚下可以压着**几条**基线（今天 8 处），而它们的值全都不同。
      · 层叠按**属性**分胜负，不按规则：`.cm-btn{background}` 与
        `.cm-btn.danger{border-color}` 同时覆盖 `.cm-btn.danger:hover`，按规则取赢家
        会把 `background` 那一条整条丢掉——漏报（§7.12）。
    """

    cover = [b for (b, f) in base if f == fam and _covers(b, part)]
    won: dict[str, tuple[str, str]] = {}
    tie: list[str] = []
    for prop in sorted({k for b in cover for k in base[(b, fam)]}):
        tops = _ruling_baselines([b for b in cover if prop in base[(b, fam)]])
        if len(tops) == 1:
            won[prop] = (base[(tops[0], fam)][prop], tops[0])
        else:
            tie.append(f"{fam} {prop} 脚下比不出高低 {sorted(tops)}  {part}")
    return won, tie


def test_a_state_that_repaints_the_baseline_byte_for_byte_never_happened():
    """判据一（§7.23）：状态在面/线上画出来的颜色与基线逐字节相同 ⇒ 一句没发生的话。

    这条不看梯子。「重述基线」与档位无关——`border:0` 重述 `border:0` 和
    `border-color:rgba(X,var(--o-2))` 重述 `border:1px solid rgba(X,var(--o-2))`
    是同一种病，只是一个在梯子上一个不在。第五刀那版尺子只扫落在梯子上的取值，
    于是 `.life-entry:hover{border:0}` 这类从它旁边走过去了。

    这是 §7.2 / §7.8 那个形状第三次出现（第一次在减光、第二次在 `opacity:1`）：
    一条一天都不会生效的声明，读代码的人会以为这里有一句话。
    """

    base, state = _faces_by_subject()
    dead, tie, standing = [], [], 0
    for part, fam, prop, value in state:
        won, part_tie = _baseline_under(base, part, fam)
        tie += part_tie
        standing += bool(won)
        for bprop, (bval, owner) in won.items():
            if _painted(bval) == _painted(value):
                dead.append(f"{fam} {prop} = {owner} 的 {bprop}  {part}  ({value[:60]})")
                break
    assert not dead, dead
    assert not tie, tie

    # 这两个数是这条判据的覆盖面：脚下一条基线都查不到的那些状态，判据一对它们无话可说。
    # 换尺子那一轮它是 95/118，新尺子 113/118（§7.15：文档里的数要能被今天的文件重算）。
    line = _doc_line("### 7.23", "面与线这条通道上今天有")
    assert (len(state), standing) == tuple(
        int(n) for n in _DOC_BOLD_COUNT.findall(line)
    ), (len(state), standing, line)


_SHADOW_PROP = re.compile(r"^(box-shadow|text-shadow|filter)$")


def _one_spelling_per_number(value: str) -> str:
    """把一条光的值里每个数写成规范形，再比。

    这是光这条通道要摘的那副面具：同一个 alpha 在这个文件里有两种拼法（`.11` 式 39
    次、`0.11` 式 146 次），同一个零可以写成 `0` 也可以写成 `0px`。两种拼法画出来的
    是同一层光，逐字节比会让重述从旁边走过去——而漏报不会让任何人动手（§7.12）。
    """

    def one(m: re.Match[str]) -> str:
        return ("%f" % float(m.group(0))).rstrip("0").rstrip(".") or "0"

    v = re.sub(r"\s+", " ", value).strip().lower()
    v = re.sub(r"\d*\.?\d+", one, v)
    v = re.sub(r"(?<![\w.])0px\b", "0", v)
    return re.sub(r"\s*,\s*", ",", v)


def _light_declarations_by_selector() -> tuple[dict[tuple[str, str], str], list[tuple[str, str, str]]]:
    """光的三条通道，按选择器分成「平时」和「现在不一样了」两堆，跳过 @keyframes。

    @keyframes 不是状态，是时间——而且把它当状态会当场造出误报：`fieldBreathe` 的 0%
    与 100% **逐字节等于** `.comp-input .field` 的静态 `box-shadow`，因为一个循环必须
    回到自己的起点，否则每一圈结束的那一帧会跳。
    """

    base: dict[tuple[str, str], str] = {}
    state: list[tuple[str, str, str]] = []
    for stack, decl in _declarations_everywhere():
        if any(s.startswith("@keyframes") for s in stack):
            continue
        prop, sep, value = (p.strip() for p in decl.partition(":"))
        if not sep or not _SHADOW_PROP.match(prop):
            continue
        if prop == "filter" and "drop-shadow" not in value:
            continue
        value = _expand_rung_tokens(value)
        for part in (stack[-1] if stack else "").split(","):
            part = re.sub(r"\s+", " ", part).strip()
            if not part:
                continue
            if _is_state(part):
                state.append((part, prop, value))
            else:
                base[(part, prop)] = value
    return base, state


def test_a_state_that_repaints_the_light_it_already_had_never_happened():
    """判据一（§7.23）的第三条通道：状态在光上重述基线 ⇒ 一句没发生的话。

    判据一原先只覆盖面与线。不是因为光上没有这个病，是因为 #48 那一节的题目是
    「哪一档」，而光归 §6——整条通道从判据旁边走过去了（§7.7 那个形状，§7.23 自己
    已经记过一次「让一节的题目决定判据的覆盖面」，这是第二次）。

    三条通道是同一条法，但**每条通道要摘掉的面具不一样**，因为「这条声明画出来的是
    什么」在两处是两件事：
      面与线：剥掉几何。`1px solid` 不画颜色，`border:1px solid X` 与 `border-color:X`
        画的是同一条线。
      光：**几何一个字都不许剥。** 偏移与模糊就是这层光在说的那句话（判据五定 y 的
        符号、判据六定 blur = 4y）。套用面与线那把剥子，`0 1px 0 X` 和 `0 2px 0 X`
        会被剥成同一条——一个「浮 1px」和一个「浮 2px」在它眼里没有区别。
    光这边要摘的是数字的拼法（`_one_spelling_per_number`）。

    今天 0 处，所以这条守卫是纯防漏报的：它承不承重由变异测试证明，不由今天的绿色
    证明（§7.9「一条没有对手的声明是死声明」的镜像——一条没有对手的**守卫**是死守卫）。
    """

    base, state = _light_declarations_by_selector()
    dead, tie, standing = [], [], 0
    for part, prop, value in state:
        tops = _ruling_baselines(
            [b for (b, pr) in base if pr == prop and _covers(b, part)]
        )
        if len(tops) > 1:
            tie.append(f"{prop} 脚下比不出高低 {sorted(tops)}  {part}")
            continue
        if not tops:
            # `box-shadow` 的初始值就是 `none`：基线什么都没写，等于基线写了 none。
            # 这是 `border:0` 重述 `border:0` 那一种病，只是换了一条通道。
            if _one_spelling_per_number(value) == "none":
                dead.append(f"{prop}:none 而基线这条通道上什么都没有  {part}")
            continue
        standing += 1
        if _one_spelling_per_number(base[(tops[0], prop)]) == _one_spelling_per_number(value):
            dead.append(f"{prop} = {tops[0]} 上同一条  {part}  ({value[:60]})")
    assert not dead, dead
    assert not tie, tie

    # 正对照：这两个数写进文档就得能被今天的文件重算出来（§7.15）。
    line = _doc_line("### 7.23", "光这条通道上")
    assert (len(base), len(state)) == tuple(
        int(n) for n in _DOC_BOLD_COUNT.findall(line)
    ), (len(base), len(state), line)

    # 这条通道的覆盖面分两半：脚下查得到基线的那些走「逐字节相同」，脚下什么都没有的
    # 那些走 `none` 那个入口（初始值就是 none，所以基线不写等于基线写了 none）。
    line = _doc_line("### 7.23", "光上脚下")
    assert (standing, len(state) - standing) == tuple(
        int(n) for n in _DOC_BOLD_COUNT.findall(line)
    ), (standing, len(state) - standing, line)


def test_the_hand_moves_exactly_one_rung_up_and_never_changes_the_hue():
    """判据二（§7.23）：`:hover` / `:active` 只准跨一档、方向恒为上、色相不变。

    理由不是「一档好看」，是**这句话说给谁听**：hover 在触屏上根本不存在，active
    只存在于手指按住的那一瞬（§7.3 驳回 `title` 用的是同一条理由）。一句只在这两
    种时刻出现的话不能承担任何必须被读到的信息，所以它只有资格说「不方便」级别的
    话。色相是身份（§7.9），而你的手指不改变这块内容是什么。

    三条推论，都在这条守卫里：
      · 基线在这条通道上没有任何声明 ⇒ 状态只能落第 1 档（从无到有）。
      · 基线在这条通道上是一道斜坡（渐变，或者没有一个取值落在梯子上）⇒
        「跨一档」在那里没有定义，手不该在这条通道上说话。
      · 一条声明里有 ≥2 个落在梯子上的取值 ⇒ 那是同一块材料内部的位置（§7.13），
        不走梯子，整条不判；斜坡内部改色标是合法的，所以两边都含渐变时准许平面
        底那一档不变。
    """

    base, state = _faces_by_subject()
    bad, tie, spoken = [], [], 0
    for part, fam, prop, value in state:
        if not _HAND.search(part):
            continue
        picks = _rung_picks(value)
        if len(picks) != 1:
            continue
        hue, rung = picks[0]
        where = f"{fam} {prop}  {part}"
        baseline, part_tie = _baseline_under(base, part, fam)
        tie += part_tie
        if not baseline:
            if rung == 1:
                spoken += 1
            else:
                bad.append(f"从无到有却落在 o-{rung} 而不是第一档  {where}")
            continue
        under = [pick for v, _owner in baseline.values() for pick in _rung_picks(v)]
        if not under:
            bad.append(f"基线在这条通道上不是一档（斜坡或手写），手不该在这里说话  {where}")
            continue
        same_hue = [r for h, r in under if h == hue]
        if not same_hue:
            bad.append(f"换了色相 基线 {sorted(set(under))} → {picks}  {where}")
            continue
        slope = "gradient(" in value or any("gradient(" in v for v, _owner in baseline.values())
        step = rung - max(same_hue)
        if step == 1 or (step == 0 and slope):
            spoken += 1
        else:
            bad.append(f"跨 {step:+d} 档 基线 o-{max(same_hue)} → o-{rung}  {where}")
    assert not bad, bad
    assert not tie, tie
    # 一个空的判据也是绿的。手在面与线上说话的处数得和文档里那个数对上，否则「全 app
    # 都合格」这句话可能只是因为没有一处被认出来（§7.15：文档里的数必须能被今天的
    # 文件重算出来）。
    assert spoken == int(re.search(r"(\d+)\s*处", _doc_line("### 7.23", "手在面与线上说话")).group(1)), spoken


# ---------------------------------------------------------------------------
# §6.1 光：一个光源，六个位置
#
# 光归 §6（玻璃）而不是 §7（颜色）：§6 已经定了这套材料的底（`--bg-card`）、线
# （`--glass-border`）与模糊（三档），光是第四样，先前没定。理由不是归类偏好，是
# 判据四量出来的——全 app 每一层光的色相都是 §7 已经定过的入口，或者纯黑纯白，
# 一个新入口都没有。光不开颜色的口，它只是把已有的色摆到别的位置上。


def _split_top(value: str) -> list[str]:
    """按**顶层**逗号切：`rgba(...)` 里的逗号不算。"""

    parts, depth, buf = [], 0, ""
    for ch in value:
        depth += (ch == "(") - (ch == ")")
        if ch == "," and depth == 0:
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


def _drop_shadows(value: str) -> list[str]:
    """`filter` 里每一段 `drop-shadow(...)` 的内容，按**配平括号**取。

    不能用正则：`drop-shadow\\(([^()]*(?:\\([^()]*\\)[^()]*)*)\\)` 只允许一层嵌套，
    而 `rgba(var(--x-rgb),.5)` 是两层，于是这一族整段匹配不上——审计时它静默漏掉了
    8 层光，全都是用 token 写色的那些。漏报比误报危险：它报出来的是「合格」。
    """

    out, i = [], 0
    while True:
        i = value.find("drop-shadow(", i)
        if i < 0:
            return out
        j = i + len("drop-shadow(")
        depth = 1
        while j < len(value) and depth:
            depth += (value[j] == "(") - (value[j] == ")")
            j += 1
        out.append(value[i + len("drop-shadow(") : j - 1].strip())
        i = j


def _declarations_everywhere() -> list[tuple[tuple[str, ...], str]]:
    """(上下文栈, 声明)。和 `_top_level_rules()` 不同：`@media` 与 `@keyframes` 里的也要。

    177 层光里有 22 层长在 `@keyframes` 里、1 层长在 `@media` 里。一把只看顶层的尺子
    会把它们判成不存在，而「不存在」和「合格」在断言里长得一模一样。
    """

    out: list[tuple[tuple[str, ...], str]] = []
    stack: list[str] = []
    buf = ""
    for ch in CSS_NO_COMMENTS:
        if ch == "{":
            stack.append(re.sub(r"\s+", " ", buf).strip())
            buf = ""
        elif ch in "};":
            out.extend(
                (tuple(stack), re.sub(r"\s+", " ", d).strip())
                for d in buf.split(";")
                if d.strip()
            )
            buf = ""
            if ch == "}" and stack:
                stack.pop()
        else:
            buf += ch
    return out


def _light_layers() -> list[tuple[str, str, str]]:
    """每一层光：(通道, 它长在哪, 层原文)。

    三条通道都算。光不是 `box-shadow` 的专属——同一句「它在发光」在文字上写作
    `text-shadow`、在 svg 上写作 `filter:drop-shadow(...)`。只扫一条通道的尺子会给
    另外两条发通行证（§7.7 那个形状）。
    """

    out: list[tuple[str, str, str]] = []
    for stack, decl in _declarations_everywhere():
        prop, _, value = (part.strip() for part in decl.partition(":"))
        where = " > ".join(stack)
        if prop in ("box-shadow", "text-shadow"):
            if value != "none":
                out.extend((prop, where, lay) for lay in _split_top(value))
        elif prop == "filter":
            out.extend(("drop-shadow", where, lay) for lay in _drop_shadows(value))
    return out


_LIGHT_SHAPE = re.compile(
    r"^(?P<inset>inset\s+)?(?P<x>-?[\d.]+px|0)\s+(?P<y>-?[\d.]+px|0)"
    r"(?:\s+(?P<blur>-?[\d.]+px|0))?(?:\s+(?P<spread>-?[\d.]+px|0))?\s+(?P<colour>\S.*)$"
)

# 名字 → 这个形状成立吗。几何唯一决定一层光在说哪句话，不看它长在谁身上：一张按
# 名字写的表迟早会漏掉下一个元素，而形状不会（§9 那条「例外写成判据的一部分」）。
_POSITIONS = (
    ("投影", lambda inset, y, blur, spread: not inset and y != 0 and blur > 0 and spread == 0),
    ("光晕", lambda inset, y, blur, spread: not inset and y == 0 and blur > 0 and spread == 0),
    ("环", lambda inset, y, blur, spread: not inset and y == 0 and blur == 0 and spread > 0),
    ("厚度线", lambda inset, y, blur, spread: inset and y != 0 and blur == 0 and spread == 0),
    ("内光", lambda inset, y, blur, spread: inset and y == 0 and blur > 0),
    ("内描边", lambda inset, y, blur, spread: inset and y == 0 and blur == 0 and spread > 0),
)


def _px(raw: str | None) -> float:
    if not raw:
        return 0.0
    return float(raw[:-2]) if raw.endswith("px") else float(raw)


def _parsed_light() -> list[tuple[str, str, str, dict[str, str | None]]]:
    out = []
    for channel, where, layer in _light_layers():
        m = _LIGHT_SHAPE.match(layer)
        assert m, f"这一层光的形状读不出来：{channel} @ {where}  {layer!r}"
        out.append((channel, where, layer, m.groupdict()))
    return out


def _alpha_of(colour: str) -> float | None:
    """`rgba(...)` 的最后一个顶层参数。`rgba(var(--x-rgb),a)` 只有两个参数。"""

    m = re.fullmatch(r"rgba\((.*)\)", colour.strip())
    if not m:
        return None
    args = _split_top(m.group(1))
    if len(args) not in (2, 4):
        return None
    try:
        return float(args[-1])
    except ValueError:
        return None


def _doc_positions() -> dict[str, int]:
    """§6.1 那张表里写下的「每个位置几层」。"""

    rows: dict[str, int] = {}
    for line in _doc_section("### 6.1").splitlines():
        cells = [_cell(c) for c in line.split("|")[1:-1]]
        if len(cells) == 4 and cells[3].isdigit():
            rows[cells[0]] = int(cells[3])
    return rows


def test_light_comes_from_straight_above():
    """一个光源，在正上方：每一层光的 x 偏移必须是 0。

    这不是一条审美偏好，是这个 app 唯一的光源假设。177 层光今天全是 0——也就是说
    它一直被遵守着，只是从来没被写下来，所以下一个人写一层斜光不会有任何东西反对他。
    一层斜光会让它那块材料看起来来自另一个房间。
    """

    bad = [
        f"{ch} @ {where}  {layer}"
        for ch, where, layer, g in _parsed_light()
        if _px(g["x"]) != 0
    ]
    assert not bad, bad


def test_every_layer_of_light_sits_in_one_of_six_positions():
    """一层光在说哪句话，由它的几何形状唯一决定；第七种形状即缺陷。

    例外写成判据的一部分，不写成名单：四个几何量全为 0 的那一层是**动画的起点**
    （`recRing` 的 0%，光从无到有得有个 from），所以它只在 `@keyframes` 里合法；
    顶层规则里的一层全 0 什么都画不出来，那是空话。
    """

    counts = {name: 0 for name, _ok in _POSITIONS}
    starts, bad = 0, []
    for ch, where, layer, g in _parsed_light():
        inset = bool(g["inset"])
        y, blur, spread = _px(g["y"]), _px(g["blur"]), _px(g["spread"])
        if (_px(g["x"]), y, blur, spread) == (0.0, 0.0, 0.0, 0.0):
            if where.startswith("@keyframes"):
                starts += 1
            else:
                bad.append(f"顶层规则里一层全 0 的光，什么也没画  {ch} @ {where}  {layer}")
            continue
        hit = [name for name, ok in _POSITIONS if ok(inset, y, blur, spread)]
        if len(hit) != 1:
            bad.append(f"落在 {hit or '六个位置之外'}  {ch} @ {where}  {layer}")
        else:
            counts[hit[0]] += 1
    assert not bad, bad

    # 这把尺子只认三个属性名。要是有人换个名字写光（`-webkit-box-shadow`、
    # `-webkit-filter`），上面的循环会一层都看不见，然后报绿。
    props = {
        decl.partition(":")[0].strip()
        for _stack, decl in _declarations_everywhere()
        if "shadow" in decl.partition(":")[0]
    }
    assert props == {"box-shadow", "text-shadow"}, props
    hosts = {
        decl.partition(":")[0].strip()
        for _stack, decl in _declarations_everywhere()
        if "drop-shadow(" in decl
    }
    assert hosts == {"filter"}, hosts

    # 文档里那张表的每一格都得能被今天的文件重算出来（§7.15）。少了这一句，
    # 「六个位置都合格」可能只是因为哪一族一层都没被认出来。
    counts["起点"] = starts
    assert _doc_positions() == counts, (_doc_positions(), counts)
    assert sum(counts.values()) == int(
        re.search(r"(\d+)\s*层光，分布在", _doc_line("### 6.1", "层光，分布在")).group(1)
    ), sum(counts.values())


def test_the_glass_tokens_reference_counts_are_todays():
    """§6 那张表里的「N 处引用」必须是今天数出来的。

    三个数原先写着 11 / 30 / 3——那是 v1.1 数的。此后 §7.8 并回重复声明、§7.9 把近黑
    收成三档、§7.13 让面与线走梯子，每一轮都在动这些引用点，而**没有一条守卫在看这
    张表**，于是三个数一起过期了二十版（§7.15）。
    """

    doc: dict[str, int] = {}
    for cells in _doc_rows("## 6"):
        if len(cells) != 3:
            continue
        m = re.fullmatch(r"(\d+)\s*处引用", cells[1])
        if m:
            doc[cells[0]] = int(m.group(1))
    assert set(doc) == {"--bg-card", "--glass-border"}, doc

    today = {
        token: len(re.findall(rf"var\({token}\)", CSS_NO_COMMENTS)) for token in doc
    }
    assert doc == today, (doc, today)


def test_light_is_never_opaque():
    """光必须是半透明的：每一层的颜色都得带 alpha，而且 alpha < 1。

    理由是物理的，不是审美的。光是叠在底上的，blur 中心处的不透明度就是它自己的
    alpha。α=1 时那一层把底完全盖住，于是它渲染出来不是光，是一块糊掉的实心——
    `.comp-state.*::before` 先前写着 `0 0 8px var(--comp-warm)`，浏览器算出的光色
    `rgb(212,165,116)` 和那颗 3px 点自己的底**逐字节相同**，点因此没有边缘。
    """

    bad = []
    for ch, where, layer, g in _parsed_light():
        alpha = _alpha_of(g["colour"] or "")
        if alpha is None:
            bad.append(f"光的颜色不带 alpha  {ch} @ {where}  {layer}")
        elif alpha >= 1:
            bad.append(f"光是不透明的 α={alpha}  {ch} @ {where}  {layer}")
    assert not bad, bad


def test_light_opens_no_new_colour_entrance():
    """光不开新的颜色入口——这就是它归 §6 而不是 §7 的理由。

    每一层光的色相要么是纯黑纯白（影与纱），要么是 `:root` 里已经声明过的某个入口。
    只要这一条成立，光就不是第 N 个颜色入口，而是玻璃这套材料的一个位置。
    """

    hues: dict[str, list[str]] = {}
    for ch, where, _layer, g in _parsed_light():
        colour = (g["colour"] or "").strip()
        inner = re.fullmatch(r"rgba\((.*)\)", colour)
        args = _split_top(inner.group(1)) if inner else []
        hue = ",".join(a.strip() for a in args[:3]) if len(args) == 4 else (args[0].strip() if args else colour)
        hues.setdefault(hue, []).append(f"{ch} @ {where}")

    bad = []
    for hue, uses in hues.items():
        if hue in ("0,0,0", "255,255,255"):
            continue
        token = re.fullmatch(r"var\((--[\w-]+)\)", hue)
        if not token or token.group(1) not in DECLARED:
            bad.append(f"{hue} 不是任何已声明的入口，用在 {uses}")
    assert not bad, bad

    assert len(hues) == int(
        re.search(r"(\d+)\s*个色相", _doc_line("### 6.1", "个色相")).group(1)
    ), sorted(hues)

    # 逐色相的层数账也是文档里的数，也得有对手（§7.15）。先前只有「12 个色相」这一个
    # 数被守着，于是 #90 把 5 层顶亮线从 `--warm-hi` 换成纯白之后，「纯白 31 层、
    # `--warm-hi` 10」在文档里静静地错着，全套 484 条守卫一条都没红。
    line = _doc_line("### 6.1", "个色相")
    doc = {
        "0,0,0": int(re.search(r"纯黑 (\d+) 层", line).group(1)),
        "255,255,255": int(re.search(r"纯白 (\d+) 层", line).group(1)),
    }
    doc.update({
        f"var({tok})": int(n) for tok, n in re.findall(r"`(--[\w-]+)`\s*(\d+)(?![\d.])", line)
    })
    assert doc == {h: len(v) for h, v in hues.items()}, (doc, {h: len(v) for h, v in hues.items()})
    rest = int(re.search(r"其余 (\d+) 层", line).group(1))
    assert rest == sum(n for h, n in doc.items() if h not in ("0,0,0", "255,255,255")), rest


def _hue_rgb_of_light(colour_text: str) -> colour.Rgb:
    """一层光的色相（丢掉 alpha），`var(--x)` 就地求值成 r,g,b。

    `rgba(var(--warm-hi),0.35)` 顶层只有两个参数，`rgba(255,255,255,.12)` 有四个。
    只认一种拼法的尺子会静默跳过另一整族——§7.11 结尾那条病在这一族里已经犯过两次。
    """

    inner = re.fullmatch(r"rgba?\((.*)\)", colour_text.strip())
    assert inner, colour_text
    args = [a.strip() for a in _split_top(inner.group(1))]
    if len(args) >= 3:
        return colour.parse(",".join(args[:3]))
    token = re.fullmatch(r"var\((--[\w-]+)\)", args[0])
    assert token, colour_text
    return colour.parse(_root_value(token.group(1)))


def test_the_sign_of_y_is_decided_by_the_light_not_by_the_author():
    """§6.1 判据五：光在正上方，于是 y 的符号不再由作者自由选择。

    判据一只钉了 x=0，而它还有另一半没被写下来。光在正上方 ⟹ 影子落在材料下方，
    顶面朝光、底面背光。先前有 4 层往上的投影（`.lap-card` / `.map-card` /
    `.tabbar` / `.mtp-card`），全部是贴底面板，它们说的是「光从下面来」。

    浏览器实测（幕底下压一块纯白，取最坏情况）：那层雾把顶缘线↔上方的对比度从
    1.381:1 抬到 1.735:1，两个数都远低于 1.4.11 的 3:1；而在这一屏本来的深内容上
    只从 1.926 抬到 1.979。更要紧的是**同一层光在两种底上作用方向相反**——面与面
    之比反而从 1.296:1 掉到 1.032:1。一句话不会随底反向，一个旋钮才会。

    y 的**大小**不属于这条判据：y 在两族里量的根本不是同一件事。投影的 y 是「这块
    材料浮多高」（与 blur 同向），厚度线的 y 是「这块玻璃有多厚」（blur 恒为 0）。
    """

    upward = [
        f"{ch} @ {where}  {layer}"
        for ch, where, layer, g in _parsed_light()
        if not g["inset"] and _px(g["y"]) < 0 and _px(g["blur"]) > 0
    ]
    assert not upward, upward

    top: list[tuple[float, str]] = []
    bottom: list[tuple[float, str]] = []
    for ch, where, layer, g in _parsed_light():
        y = _px(g["y"])
        if not (g["inset"] and y != 0 and _px(g["blur"]) == 0 and _px(g["spread"]) == 0):
            continue
        lum = colour.luminance(_hue_rgb_of_light(g["colour"] or ""))
        (top if y > 0 else bottom).append((lum, f"{ch} @ {where}  {layer}"))

    # 正对照：两族都得看得见东西。这两句**没有独立的对手**——变异测试里把分族条件
    # 弄瞎之后，下面那个 min() 在空集上抛 ValueError 同样是红。留着它只为把一个
    # ValueError 换成一句说得清「哪一族空了」的话；真正防住「只认出一半」的是末尾
    # 那两个文档数字。这件事写在这里，好过让下一个人以为它在承重。
    assert top and bottom, (len(top), len(bottom))

    # 顶面朝光、底面背光，所以两族的亮度区间不许有交集。这条判据是纯内部的，
    # 不需要知道宿主的底——而「逐层相对自己宿主的底是加光还是减光」是更强的那一条，
    # 它要浏览器才能量（登记为独立任务，不在这里放宽成一张色名单）。
    floor = min(top)
    ceiling = max(bottom)
    assert floor[0] > ceiling[0], (floor, ceiling)

    # §7.15：文档里那两个数今天重算。没有它，上面那条断言在「顶面只剩一层」时照样绿。
    doc = re.search(
        r"顶面\s*(\d+)\s*层、底面\s*(\d+)\s*层", _doc_line("### 6.1", "层、底面")
    )
    assert (int(doc.group(1)), int(doc.group(2))) == (len(top), len(bottom)), (
        doc.groups(),
        (len(top), len(bottom)),
    )

    # 两族的**地板与天花板**也是两个数，而它们原先只在散文里（写着 0.87 / 0.05，实测
    # 0.8844 / 0.0403，错了六个版本）。上面 `floor[0] > ceiling[0]` 那条断言对这两个
    # 数字写错免疫——它只比大小。§7.15：一个只出现在人眼里的数，不会被任何断言接住。
    written = [
        float(n) for n in _DOC_L_VALUE.findall(_doc_line("### 6.1", "层、底面"))
    ]
    assert written == [round(floor[0], 4), round(ceiling[0], 4)], (written, floor, ceiling)

    # 被驳回的那个前提（§9 有它的抄本）：底面这一族最亮的墨比**最亮的底**还亮，所以
    # 「相对中性灰的方向」这个外部门槛在这套颜色里对每一层都成立，也就什么都没说。
    lift = colour.luminance(_tier("--bg-lift"))
    assert ceiling[0] > lift, (ceiling, lift)
    premise = _doc_line("### 6.1", "相对 50% 中性灰")
    assert float(_DOC_L_VALUE.search(premise).group(1)) == round(lift, 4), (
        premise[:80],
        lift,
    )
    assert _by_quantity(f"{round(ceiling[0], 4)}") in _by_quantity(premise), premise[:80]


def test_a_drop_shadow_has_only_one_degree_of_freedom():
    """§6.1 判据六：投影的 blur 不是第二个旋钮，它是「浮多高」的第二个读数。

    屏幕是一块竖直的舞台，材料浮在背景前方 z 处，全 app 只有一个光源（判据一）。
    于是投影的位移 y 与它的半影宽度 blur **都正比于 z**，比值 `blur/y` 只由光源的
    两个角度决定，与 z 无关——也就是说它是光源的属性，不是这块材料的属性。61 层
    投影先前有 **17 个不同的比值**（2.0–5.0），等于说这个 app 有 17 个光源。

    「模糊有几档」这个问法因此是错的：投影的 blur 没有自己的档。而光晕与内光的
    blur 量的是另外两件事（发光半径、光透进材料多深），那是两笔独立的账。

    这条判据不覆盖 y=0 的位置（光晕 / 内光 / 环 / 内描边 / 起点），因为它们的 blur
    根本不是「浮多高」；也不覆盖厚度线（blur 恒为 0，它的 y 量的是玻璃有多厚）。
    """

    ratio = int(re.search(r"blur\s*=\s*(\d+)\s*×\s*y", _doc_line("### 6.1", "× y")).group(1))

    drops: list[tuple[float, float, str]] = []
    for ch, where, layer, g in _parsed_light():
        y, blur, spread = (_px(g[k]) for k in ("y", "blur", "spread"))
        if not g["inset"] and y != 0 and blur > 0 and spread == 0:
            drops.append((abs(y), blur, f"{ch} @ {where}  {layer}"))

    bad = [
        f"{who}  blur/y={blur / y:g}，这一档 y 该配 {ratio * y:g}px"
        for y, blur, who in drops
        if blur != ratio * y
    ]
    assert not bad, bad

    # §7.15：两个数今天重算。没有这一句，上面那条断言在「投影一层不剩」时照样绿——
    # 这与判据五那个正对照不同，那一句真的没有对手，这一句有：它是这条守卫唯一的
    # 「我确实看见了东西」。第二个数（blur 的取值数）钉的是**有几个不同的浮起高度**，
    # 那正是下一轮要判的东西，钉住它，下一轮就不能悄悄漂移。
    doc = re.search(
        r"今天 \*\*(\d+) 层\*\*投影全部合格[^。]*?收到 \*\*(\d+) 个\*\*",
        _doc_line("### 6.1", "投影全部合格"),
    )
    assert (int(doc.group(1)), int(doc.group(2))) == (len(drops), len({b for _y, b, _w in drops})), (
        doc.groups(),
        (len(drops), len({b for _y, b, _w in drops})),
    )


# --- §6.1 判据十：长度的档由比值判 --------------------------------------------
#
# 判据六把投影压成一维（blur = 4y）之后，「浮多高」仍有 15 个自由取值；光晕的
# 「发光半径」是另一本账的 14 个。这两维的档不由 alpha 那套 ΔE 判（判据七：档数
# 是「色 × 底」发的额度），由**比值**判——而界线的位置是文件自己裂开的：两族全部
# 27 对相邻比值里，没有一对落在 1.2 与 1.25 之间。≤ 1.2 的 12 对全是「同一句话
# 写了两遍」（24 与 25、5 与 6、8 与 9、10 与 12……），≥ 1.25 的 15 对全是跨语义
# 的边界（静止↔hover ×2、呼吸两端 ×1.5、键↔卡↔全屏面板）。心理物理只给得出
# 锐边长度的 Weber 分数（约 5–10%），软掉的边缘更粗、但说不出「几」——1.25 不是
# 校出来的，是从那条缝里读出来的。


def _doc_ladder(key: str) -> dict[float, int]:
    """§6.1 判据十那两行梯子：{刻度: 层数}。

    刻度、总层数、分档负载全从文档读，梯子自身的相邻比也不低于判据写下的那个
    数——与 `k 从文档里读`守的是同一个方向：文档不能单方面漂。
    """

    line = _doc_line("### 6.1", key)
    m = re.search(r"：\s*([\d\s·]+?)\s*——\s*(\d+) 层分档\s*([\d\s/]+?)\s*$", line)
    assert m, f"梯子这行读不出刻度与分档：{line[:80]}"
    rungs = [float(v) for v in m.group(1).split("·")]
    loads = [int(v) for v in m.group(3).split("/")]
    assert len(rungs) == len(loads) and sum(loads) == int(m.group(2)), (rungs, loads)

    floor = float(
        re.search(r"比值不低于\s*\*\*(\d\.\d+)\*\*", _doc_line("### 6.1", "比值不低于")).group(1)
    )
    gaps = [b / a for a, b in zip(rungs, rungs[1:])]
    assert min(gaps) >= floor, ([f"{g:g}" for g in gaps], floor)
    return dict(zip(rungs, loads))


def test_how_high_it_floats_is_a_ladder_not_a_knob():
    """§6.1 判据十前一半：投影的 y（浮多高）只许站在浮高梯子的刻度上。

    15 个高度收成 11 档靠的是并入（比值 ≤ 1.2 并入近邻、多数表决）：10→12、
    14→12、16→18、25→24。blur 跟着 y 走（判据六），于是 blur 的取值数与高度的
    取值数一起从 15 到 11——那半个对账在判据六自己的守卫里。
    """

    ladder = _doc_ladder("浮多高的档")
    seen: dict[float, int] = {}
    bad = []
    for ch, where, layer, g in _parsed_light():
        y, blur, spread = (_px(g[k]) for k in ("y", "blur", "spread"))
        if not g["inset"] and y != 0 and blur > 0 and spread == 0:
            seen[abs(y)] = seen.get(abs(y), 0) + 1
            if abs(y) not in ladder:
                bad.append(f"{ch} @ {where}  {layer}")
    assert seen and not bad, (bad or "一把只看投影的尺子今天一层都没读到")
    assert seen == ladder, (seen, ladder)


def test_the_glow_radius_is_a_ladder_not_a_knob():
    """§6.1 判据十后一半：光晕的 blur（发光半径）只许站在自己的梯子上。

    半径的九档今天全部落在浮高梯子的刻度上（只多一个 50）——这是观察到的事实，
    不是这条判据的一部分：两维量的不是同一件事（判据二），守卫也各是各的。
    """

    ladder = _doc_ladder("发光半径的档")
    seen: dict[float, int] = {}
    bad = []
    for ch, where, layer, g in _parsed_light():
        y, blur, spread = (_px(g[k]) for k in ("y", "blur", "spread"))
        if not g["inset"] and y == 0 and blur > 0 and spread == 0:
            seen[blur] = seen.get(blur, 0) + 1
            if blur not in ladder:
                bad.append(f"{ch} @ {where}  {layer}")
    assert seen and not bad, (bad or "一把只看光晕的尺子今天一层都没读到")
    assert seen == ladder, (seen, ladder)


def test_how_deep_the_light_reaches_is_a_ladder_not_a_knob():
    """§6.1 判据十续：内光的 blur（透进多深）只许站在自己的梯子上。

    第一档 16 坐着七层——两枚 44px 玻璃键的全部状态（rest / hover / recording /
    recPulse 峰）共享同一个深度，状态只用亮度说话（alpha 0.03→0.10）；100 那一档
    坐着呼吸的峰与聚焦态（并档前 100 与 120，比值 1.2）。深度是材料的，亮度是
    状态的——判据八「厚度是材料的属性」在内光上的那句话。
    """

    ladder = _doc_ladder("内光深度的档")
    seen: dict[float, int] = {}
    bad = []
    for ch, where, layer, g in _parsed_light():
        y, blur = _px(g["y"]), _px(g["blur"])
        if g["inset"] and y == 0 and blur > 0:
            seen[blur] = seen.get(blur, 0) + 1
            if blur not in ladder:
                bad.append(f"{ch} @ {where}  {layer}")
    assert seen and not bad, (bad or "一把只看内光的尺子今天一层都没读到")
    assert seen == ladder, (seen, ladder)


# --- §6.1 判据八：厚度是材料的属性，不是状态的读数 ---------------------------
#
# 这条判据的仪器不能建在「谁有厚度线」上，得建在「谁声明了 `box-shadow`」上。
# `box-shadow` 不叠加，后面那条整条替换前面那条：一个状态重写了 box-shadow 却没写
# 厚度线，等于让这块玻璃在那个状态薄到 0——和改它的颜色是同一个错，而只看「有厚度线
# 的宿主」的仪器会把这一整族看成不存在。
#
# 「同一块材料」不是一张名单，是一个结构问题：**两条规则能不能同时命中同一个元素**。
# `.voice-ico-btn` 与 `#inputField:not(.expanded) .voice-ico-btn:not(.recording)` 能
# （前者是后者去掉限定），`.msg-user .bubble` 与 `.msg-ai .bubble` 不能（一个元素不会
# 同时在两个祖先里）。先前按「剥掉状态记号后的选择器字符串」认材料的写法漏了 `:not()`
# 那一族两层，而它漏掉的恰好就是不合格的那两层。剥状态记号用的是全文唯一那把
# `_STATE_TOKEN`——这一节先前自带一把 `_MATERIAL_STATE`，它缺 15 个今天真在用的名字
# （`.show` 26 处）又带 2 个 CSS 里根本没有的死名字，v1.39 并掉了。


# 一个复合选择器要求的那些「简单选择器」。属性选择器整段留着（`[data-screen="terrain"]`
# 是一个条件），伪元素单独认：`::before` 是宿主生出来的**另一个盒**，它有自己的厚度线，
# 和宿主不是同一块材料。不复用 §7.11 的 `_compounds`——那个只抽类名，于是 `.tab` 与
# `.tab[data-screen="x"]` 在它眼里相等、`div.x` 与 `span.x` 也相等，那是往多认的方向错。
_MATERIAL_SIMPLE = re.compile(r"\[[^\]]*\]|::[\w-]+|:[\w-]+(?:\([^)]*\))?|[.#][\w-]+|\*|^[\w-]+")
_MATERIAL_LEGACY_PE = re.compile(r"(?<!:):(before|after|first-line|first-letter|placeholder|selection)\b")


def _simple_selectors_of(compound: str) -> frozenset[str]:
    """一个复合选择器 → 它要求的简单选择器集合。读不出的字符留声，不静默丢。"""

    rest, got = _MATERIAL_LEGACY_PE.sub(r"::\1", compound), []
    while rest:
        m = _MATERIAL_SIMPLE.search(rest)
        if not m:
            got.append("??" + rest)
            break
        if m.start():
            got.append("??" + rest[: m.start()])
        got.append(m.group(0))
        rest = rest[m.end():]
    return frozenset(got)


def _compound_covers(general: str, specific: str) -> bool:
    """凡是命中 specific 的元素，一定也命中 general 吗（同一层，且画的是同一个盒）。"""

    g, s = (_simple_selectors_of(c) for c in (general, specific))
    return {x for x in g if x.startswith("::")} == {x for x in s if x.startswith("::")} and g <= s


_COMBINATOR = re.compile(r"\s*([>+~])\s*")


def _links(sel: str) -> list[tuple[str, str]]:
    """选择器 → [(与左邻的关系, 复合选择器)]，第一段的关系是空串。

    组合子必须自己成一层。先前靠 `sel.split()` 按空白切层，而 `>` / `+` / `~` 不是空白，
    于是 `.dv-card>.gi-unavailable` 整条算成一层，`.dv-card` 被判成覆盖它——可它画的是
    `.gi-unavailable` 那个盒，**主体换了**。
    """

    got: list[tuple[str, str]] = []
    comb = ""
    for tok in _COMBINATOR.sub(r" \1 ", sel).split():
        if tok in ">+~":
            comb = tok
        else:
            got.append((comb or " ", _STATE_TOKEN.sub("", tok)))
            comb = ""
    if got:
        got[0] = ("", got[0][1])
    return got


def _covers(general: str, specific: str) -> bool:
    """凡是命中 specific 的元素，一定也命中 general 吗（**有方向**，整条选择器）。

    祖先那一层只认**能证明是祖先**的：`+` / `~` 左边那一段是主体的兄弟，不是祖先——但
    兄弟与主体同父，所以跳过那一段继续往左走仍然是共同祖先，于是 `.x` 覆盖 `.w + .x`
    （主体同为那个 `.x` 元素，兄弟那一段只是多加一个条件）。
    一般那条自己用了强组合子时只认结构完全一致的：`.a .b` 覆盖 `.a>.b`，反过来不行。

    先前这里还有第三个参数 `strip`，让调用方指定用哪把「状态记号」尺子。**那个参数就是
    分叉的入口**：v1.37 一边写下「同一个概念不许留两把尺子」，一边留着它，于是那个概念在
    四个地方长成了四个样子而没有一条断言会红（v1.39 并成一把，见 `_STATE_TOKEN`）。
    """

    lg, ls = _links(general), _links(specific)
    if not lg or not ls or not _compound_covers(lg[-1][1], ls[-1][1]):
        return False
    if any(c in ">+~" for c, _ in lg[1:]):
        return len(lg) == len(ls) and all(
            a[0] == b[0] and _compound_covers(a[1], b[1]) for a, b in zip(lg, ls)
        )
    walk = iter([ls[i - 1][1] for i in range(len(ls) - 1, 0, -1) if ls[i][0] in (" ", ">")])
    return all(any(_compound_covers(x, y) for y in walk) for _rel, x in reversed(lg[:-1]))


def _same_material(a: str, b: str) -> bool:
    """a、b 两条规则说的是同一块材料吗（文本上的近似）。

    判据是**一方处处更一般**：凡是命中特化那条的元素，一定也命中一般那条 ⇒ 两条规则
    一定同时命中同一个元素。逐层用「简单选择器集合包含」比而不是字符串相等——
    `.tab.active svg` 与 `.tab[data-screen="terrain"].active svg` 是同一块材料，字符串
    相等看不出来（#104：那一版**两层都**用 `==`，不只是祖先那一层）。

    这里取**两个方向的并**，而 §7.23 判据一/二取的是有方向的那一个：这条问的是「两条
    规则能不能同时命中同一个元素」，谁更特化不影响答案；那条问的是「这条状态脚下压着
    的是哪条基线」，反着来就不是同一句话（#105）。

    不取「有可能同时命中」（`.a` 与 `.b` 可以长在同一个元素上）：那不是同一块材料的两个
    状态，而同一组的成员会被要求读数相等，所以多认在这里造的是**误报**，不是多比一对。
    方向也必须整条链一致——一边在目标上更一般、另一边在祖先上更一般，两条规则并不保证
    同时命中。
    """

    return _covers(a, b) or _covers(b, a)


def test_the_same_material_ruler_reads_a_specialisation_as_one_material():
    """`_same_material` 这把尺子自己的对手。

    判据八的分组全靠它，而它在今天的 `app.html` 上只多认出**一对**（`.we-ops-led` 与
    `.we-ops-led.warn`，两边都没有厚度线）——**今天的绿色证明不了它读对了什么**。所以
    这八对是构造的：头两对是 #104 修掉的洞（那一版两层都用字符串相等，看不出属性限定
    是一种特化），中间三对是不许多认的方向（互斥的祖先、伪元素是另一个盒），接着两对是
    它本来就得认出来的，末一对盯的是方向必须整条链一致（见那一行自己的注释）。
    """

    cases = [
        (True, ".tab", '.tab[data-screen="terrain"]'),
        (True, ".tab.active svg", '.tab[data-screen="terrain"].active svg'),
        (False, ".msg-user .bubble", ".msg-ai .bubble"),
        (False, ".comp-echo", ".comp-echo::before"),
        (False, ".comp-echo::before", ".comp-echo::after"),
        (True, "#inputField:not(.expanded) .voice-ico-btn:not(.recording)", ".voice-ico-btn"),
        (True, ".x .y", ".y"),
        # 第八对盯的是「方向整链一致」，前七对都盯不住它：这两条一个在目标上更一般、
        # 一个在祖先上更一般，于是谁都不保证另一个也命中（`.wrap .btn.extra` 里的按钮
        # 可以不在 `.wrap.extra` 里）。只按「每一层各自任一方向」比会把它认成一对。
        (False, ".wrap.extra .btn", ".wrap .btn.extra"),
        # 第九对是 #105：按空白切层时 `>` 不成一层，于是 `.dv-card` 与它认成一块材料
        # ——可后者画的是 `.gi-unavailable` 那个盒，两条规则命中的根本不是同一个元素。
        (False, ".dv-card", ".dv-card>.gi-unavailable"),
    ]
    wrong = [(a, b, want) for want, a, b in cases if _same_material(a, b) is not want]
    assert not wrong, wrong

    # `_same_material` 是对称的，它盯不住方向；而 §7.23 判据一/二靠的正是方向。
    # 这四对里有三对是我第一次写原型时期望写错的地方（尺子读对了）：`.a .b` 覆盖
    # `.a>.b`（凡命中子代的都命中后代），反过来不行；`.x` 覆盖 `.w + .x`（主体是同一个
    # `.x`，兄弟那一段只是多一个条件），反过来不行。
    directed = [
        (True, ".a .b", ".a>.b"),
        (False, ".a>.b", ".a .b"),
        (True, ".x", ".w + .x"),
        (False, ".w + .x", ".x"),
        # 兄弟那一段不是祖先：`.w + .x` 里的 `.w` 是 `.x` 的哥哥，不是它的父辈。
        (False, ".w .x", ".w + .x"),
        # 但兄弟与主体同父，所以跳过那一段继续往左走，`.p` 仍然是共同祖先。
        (True, ".p .x", ".p .w + .x"),
    ]
    off = [(g, s, want) for want, g, s in directed if _covers(g, s) is not want]
    assert not off, off


def _thickness_lines(value: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """一条 box-shadow 的值 → (顶面那几条线的颜色, 底面那几条线的颜色)。"""

    top, bot = [], []
    for layer in _split_top(value):
        m = _LIGHT_SHAPE.match(layer)
        if not m:
            assert layer.strip() == "none", layer   # 读不出的层不许静默跳过
            continue
        y, blur, spread = _px(m["y"]), _px(m["blur"]), _px(m["spread"])
        if m["inset"] and y != 0 and blur == 0 and spread == 0:
            (top if y > 0 else bot).append(m["colour"].strip())
    return tuple(top), tuple(bot)


def test_thickness_is_a_property_of_the_material_not_a_reading_of_the_state():
    """§6.1 判据八：同一块材料的厚度线，在它所有状态下逐字节相同。

    顶面 35 层的**几何**是 `y=+1px`，零例外、跨状态一层没动过。而先前它的**颜色**
    在 7 组材料、13 层上跨状态在动（`.voice-ico-btn` 白 .40 → `:hover` .50 →
    `.recording` 暖 .35 → `:not(.expanded)` 白 .45）。同一条线的两个读数互相矛盾，
    而几何那一个已经封闭。

    「这一状态下它变亮了」在物理上只有两个指称：材料变厚（那 y 该变，可 y 没变），
    或者光变强（那所有材料的顶面都该变亮，可只有这一个元素变）。两个都不成立，所以
    那 13 层说的是一句没有指称的话——「它抬起来了」早已由投影说（判据六）。

    关键帧不是一块独立的材料，是宿主的时间维度，所以它折进宿主所在的那一组比。
    """

    shadow: dict[str, str] = {}
    animated: dict[str, list[str]] = {}
    for stack, decl in _declarations_everywhere():
        where = " > ".join(stack)
        prop, _, val = decl.partition(":")
        if prop.strip() == "box-shadow":
            shadow[where] = val
        elif prop.strip() in ("animation", "animation-name"):
            for name in re.findall(r"[A-Za-z][\w-]*", val):
                animated.setdefault(name, []).append(where)

    # 关键帧 → 它的宿主。接不上宿主的关键帧会被这一组比较整个漏掉，所以先拦住。
    hosts_of: dict[str, list[str]] = {}
    for where in shadow:
        if not where.startswith("@keyframes"):
            continue
        name = where.split(" > ")[0].removeprefix("@keyframes").strip()
        hosts_of[where] = [h for h in animated.get(name, []) if not h.startswith("@")]
    assert all(hosts_of.values()), [w for w, h in hosts_of.items() if not h]

    # 分组。`@media` 里的规则用它内层的选择器参加分组——那是同一块材料在另一个视口下。
    def selector_of(where: str) -> str:
        return where.split(" > ")[-1]

    named = {w for w in shadow if not w.startswith("@")} | {
        h for hs in hosts_of.values() for h in hs
    }
    groups: list[list[str]] = []
    for where in sorted(named) + sorted(w for w in shadow if w.startswith("@media")):
        for g in groups:
            if any(_same_material(selector_of(where), selector_of(o)) for o in g):
                g.append(where)
                break
        else:
            groups.append([where])

    bad, compared = [], []
    for g in groups:
        members = [w for w in g if w in shadow]
        members += [k for k, hs in hosts_of.items() if any(h in g for h in hs)]
        reading = {w: _thickness_lines(shadow[w]) for w in members}
        if not any(any(v) for v in reading.values()):
            continue
        if len(members) > 1:
            compared.append(members)
        for face, i in (("顶面", 0), ("底面", 1)):
            seen = {v[i] for v in reading.values()}
            if len(seen) > 1:
                bad.append(
                    f"{face}的厚度线跨状态动了：" + "；".join(
                        f"{w} → {' '.join(reading[w][i]) or '（这个状态整条不见了）'}"
                        for w in members
                    )
                )
    assert not bad, bad

    # 这条守卫最危险的失败方式不是误报，是仪器一对都没认出来然后报绿。所以要两个
    # 正对照：它数出来的厚度线总层数必须等于判据二那张位置表里的 55，而能跨状态
    # 比较的材料组数必须等于文档写下的数（§7.15）。
    census = sum(
        1
        for _ch, _where, _layer, g in _parsed_light()
        if g["inset"] and _px(g["y"]) != 0 and _px(g["blur"]) == 0 and _px(g["spread"]) == 0
    )
    counted = sum(
        len(t) + len(b) for w in shadow for t, b in [_thickness_lines(shadow[w])]
    )
    assert counted == census, (counted, census)

    doc = re.search(
        r"(\d+)\s*组材料能跨状态比较[^0-9]*(\d+)\s*条",
        _doc_line("### 6.1", "组材料能跨状态比较"),
    )
    today = (len(compared), sum(len(m) for m in compared))
    assert (int(doc.group(1)), int(doc.group(2))) == today, (doc.groups(), today, compared)


def _lifted_layers(value: str) -> list[str]:
    """一条 `box-shadow` 里落在盒子**外面**的那些层——「它离地」的唯一说法。

    名字不叫 `_drop_shadows`：那个名字在这个文件里已经有主（`filter` 里的
    `drop-shadow(...)`），而重名的模块级赋值 Python 一声不响——判据八那一轮的
    `_STATE_MARK` 就是这么把 §7.16 的尺子悄悄换掉的。
    """

    out = []
    for layer in _split_top(value):
        m = _LIGHT_SHAPE.match(layer)
        if not m:
            assert layer.strip() == "none", layer   # 读不出的层不许静默跳过
            continue
        if not m["inset"]:
            out.append(layer.strip())
    return out


def test_the_lower_thickness_line_is_a_consequence_of_lying_above_the_surface():
    """§6.1 判据九：下缘那道暗线是「它离地」与「上缘受光」的推论，不是可选的装饰。

    #90 留下的问法是「厚度线必须成对吗」，而那个问法自带的答案——贴屏幕边缘的下缘不在
    画面里，所以只有顶面的那些是合法例外——量一遍就倒了：15 条只有顶面的里，只有
    `.lap-card` 与 `.tabbar` 是 `bottom:0`，其余 13 条的下缘都在画面里（`.memo-fab` 是
    `bottom:150px` 的圆键、`.we-avatar` 84px、`.dv-live-mode` 38px 药丸）。第二个假设
    （黑暗边压在深底上本来看不见，所以只有浅底才画）也倒了：两组底的明度区间完全重叠，
    `.lap-btn.send` 合成出 0.076、`.memo-fab` 0.072，都比画了暗边的 `.comp-state`
    0.015、`.voice-ico-btn` 0.020 更亮。

    今天零反例的那条线是**离地**：20 条顶底俱全的全部带外投影，而 4 条既没有外投影也
    没有下缘暗线的，恰好是两块贴屏幕边缘的面板（`.lap-card` / `.tabbar`）与两个嵌进面
    里的凹槽（`.lap-textarea` / `.we-choice.on`）——没有一个是浮在面上的板。于是判据按
    几何写：**下缘那道暗线一旦出现，同一条声明里就必须既有外投影（它离地），又有上缘
    那条受光线（判据一说光在正上方，上缘先受光，下缘才有背光可言）。**

    反方向今天不成立，也不写进判据：11 条声明离了地却只画上缘。它们该不该补齐，取决于
    「这道暗线有多厚是几档」与「它相对自己宿主的底是加光还是减光」，两笔都还没算。
    """

    bad, every, both, top_only, bot_only, lifted = [], [], [], [], [], []
    for stack, decl in _declarations_everywhere():
        where = " > ".join(stack)
        prop, _, val = decl.partition(":")
        if prop.strip() != "box-shadow":
            continue
        every.append(val)
        top, bot = _thickness_lines(val)
        if not (top or bot):
            continue
        drop = _lifted_layers(val)
        if bot and not top:
            bad.append(f"{where}：只画了下缘那道暗线，而上缘没有受光线")
        if bot and not drop:
            bad.append(f"{where}：贴在面上的一层膜画了下缘暗线——它没有离地")
        if top and bot:
            both.append(where)
        elif top:
            top_only.append(where)
            if drop:
                lifted.append(where)
        else:
            bot_only.append(where)
    assert not bad, bad

    # 两个正对照。这条守卫最危险的失败方式同样不是误报，是仪器一层外投影都没认出来
    # 然后报绿——那时上面那个 `bad` 会是空的，而空集也能过。
    census = sum(
        1 for ch, _w, _l, g in _parsed_light() if ch == "box-shadow" and not g["inset"]
    )
    assert sum(len(_lifted_layers(v)) for v in every) == census, census

    # 「只有底面 0 条」这一项的承重在上面那条禁令上，不在这个数上：走到这里它必然是 0。
    # 留着它是为了让文档里那个 0 也是数出来的，而不是我写在句子里的一个字面量（v1.35）。
    line = _doc_line("### 6.1", "条顶底俱全")
    today = {
        "条带厚度线": len(both) + len(top_only) + len(bot_only),
        "条顶底俱全": len(both),
        "条只有顶面": len(top_only),
        "条只有底面": len(bot_only),
        "条也离地": len(lifted),
        "条贴着": len(top_only) - len(lifted),
    }

    def written(label: str) -> int:
        # 每个数各自绑住自己后面那几个字，不靠它们在句子里的先后——一条按顺序读的
        # 正则，改一次措辞就会静静地把两个数读反。
        m = re.search(rf"(\d+)\D{{0,4}}{label}", line)
        assert m, (label, line)
        return int(m.group(1))

    assert {k: written(k) for k in today} == today, ({k: written(k) for k in today}, today)


# --- §6.1 判据七：「档」不是 alpha 的属性 -----------------------------------
#
# §7.13 那把七档梯子是**白压在地色上**算出来的价钱。把同一把梯子换成黑，相邻档
# 大面积掉到可辨阈之下——也就是说 `--o-N` 这个名字承诺的「一档」，只在白上成立。
# 光有 64 层是黑的，所以「把光的 alpha 贴上七档」这个最自然的动作是错的，而在这
# 一轮之前没有任何东西反对它。

_LIGHT_ALPHA_HUES = (("白", colour.WHITE), ("黑", colour.BLACK))


def _rung_gaps(base: tuple[int, int, int], hue: colour.Rgb) -> list[float]:
    rungs = [colour.over(hue, base, a) for a in _steps()]
    return [colour.delta_e(rungs[i], rungs[i + 1]) for i in range(6)]


def test_the_seven_rungs_are_a_white_price():
    """§6.1 判据七：七档是「白」的价钱，换成黑就不是七档。

    覆盖率取 1——也就是把每一层光都当成一片满覆盖的纱来算，这是对梯子**最有利**
    的极限（真实的一层投影在宿主边缘处的覆盖率由判据六定死，不到 0.7，算出来的档
    距只会更小）。取这个极限是为了让结论不依赖任何模糊模型：即使在最有利的假设下，
    黑那两行也已经塌了。
    """

    # 按结构认行，不按第一列的名字认：§6.1 里另有一张四格的位置表，而地色的名字
    # 在这一节里出现在好几处散文里。「九格、第二格是白或黑」只有这张表满足。
    rows = [r for r in _doc_rows("### 6.1") if len(r) == 9 and r[1] in ("白", "黑")]
    assert len(rows) == 4, f"判据七那张表应有 4 行，读到 {len(rows)}"

    for base_name, hue_name, *cells in rows:
        hue = dict(_LIGHT_ALPHA_HUES)[hue_name]
        gaps = _rung_gaps(_tier(base_name), hue)
        under = sum(1 for g in gaps if g < colour.JND)
        want = [f"{g:.2f}" for g in gaps] + [f"{under}/6"]
        assert [_by_quantity(c) for c in cells] == [_by_quantity(w) for w in want], (
            f"{base_name} {hue_name} 该写 {want}，文档写 {cells}"
        )

    # 断言这张表说的那件事，而不只是「表里的数没漂」：白全过，黑几乎全塌。
    for base_name in ("--bg-lift", "--bg-deep"):
        base = _tier(base_name)
        assert all(g >= colour.JND for g in _rung_gaps(base, colour.WHITE)), base_name
        assert sum(1 for g in _rung_gaps(base, colour.BLACK) if g < colour.JND) >= 5, base_name


def test_a_whole_alpha_channel_is_worth_less_in_black():
    """判据七的来处：黑在这块地上，一整条 alpha 通道装不下七档。

    白从 α=0 走到 α=1 跨过几十个可辨阈，黑只跨过几个——因为这个 app 的地色本来就
    是近黑，黑纱压上去几乎无处可去。**档数不是设计者能定的，是「色 × 底」发的额度。**
    """

    line = _doc_line("### 6.1", "一整条")
    nums = re.findall(r"\*\*(?:ΔE )?([\d.]+)", line)
    assert len(nums) == 6, f"那句话该有 6 个粗体数，读到 {nums}"

    def span(hue: colour.Rgb, base_name: str) -> float:
        base = _tier(base_name)
        return colour.delta_e(base, colour.over(hue, base, 1.0))

    white_lift = span(colour.WHITE, "--bg-lift")
    black_lift = span(colour.BLACK, "--bg-lift")
    black_deep = span(colour.BLACK, "--bg-deep")
    want = [
        f"{white_lift:.2f}",
        str(int(white_lift / colour.JND)),
        f"{black_lift:.2f}",
        f"{black_deep:.2f}",
        str(int(black_lift / colour.JND)),
        str(int(black_deep / colour.JND)),
    ]
    assert nums == want, (nums, want)

    # 这句话说的那件事：黑的额度比白小一个数量级。
    assert black_lift < white_lift / 5, (black_lift, white_lift)


def test_the_black_drop_shadows_were_never_twenty_steps():
    """判据七判出来的第一笔账：黑投影的 20 个值两两都分不开。

    这条守卫钉的不是「该收成几档」——那要浏览器逐层量每一层影子真正落在什么底上
    （地色 / 玻璃卡 / 照片，三个答案），已登记成新任务。它钉的是**今天这 20 个值
    彼此之间一对都过不了可辨阈**这件事：一个数写了 44 遍，看起来像 20 档。
    """

    alphas = sorted(
        {
            _alpha_of((g["colour"] or "").strip())
            for _ch, _where, _layer, g in _parsed_light()
            if not g["inset"]
            and _px(g["y"]) != 0
            and _px(g["blur"]) > 0
            and _px(g["spread"]) == 0
            and (g["colour"] or "").strip().startswith("rgba(0,0,0")
        }
    )
    layers = [
        1
        for _ch, _where, _layer, g in _parsed_light()
        if not g["inset"]
        and _px(g["y"]) != 0
        and _px(g["blur"]) > 0
        and _px(g["spread"]) == 0
        and (g["colour"] or "").strip().startswith("rgba(0,0,0")
    ]

    line = _doc_line("### 6.1", "两两之间")
    nums = re.findall(r"\*\*([\d.]+)", line)
    assert len(nums) == 5, f"那句话该有 5 个粗体数，读到 {nums}"

    spans = []
    for base_name in ("--bg-lift", "--bg-deep"):
        base = _tier(base_name)
        pairs = [
            colour.delta_e(
                colour.over(colour.BLACK, base, alphas[i]),
                colour.over(colour.BLACK, base, alphas[i + 1]),
            )
            for i in range(len(alphas) - 1)
        ]
        assert all(g < colour.JND for g in pairs), (base_name, [round(g, 2) for g in pairs])
        spans.append(
            colour.delta_e(
                colour.over(colour.BLACK, base, alphas[0]),
                colour.over(colour.BLACK, base, alphas[-1]),
            )
        )

    want = [str(len(layers)), str(len(alphas)), str(len(alphas) - 1), f"{spans[0]:.2f}", f"{spans[1]:.2f}"]
    assert nums == want, (nums, want)


def test_no_layer_of_light_walks_the_wall_and_line_ladder():
    """判据七的禁令：一层光的 alpha 不许写 `var(--o-N)`。

    今天 0 层写了它，所以这条断言本身守着空气——空气那一半由下面的正对照兜住：
    这把仪器必须在面与线上真的读到梯子，否则「光上没有」和「我看不见梯子」是同一
    个值。正对照钉的是「仪器还能看见梯子」，不是今天恰好存在的某一处。
    """

    bad = [
        f"{ch} @ {where}  {layer}"
        for ch, where, layer, g in _parsed_light()
        if re.search(r"var\(--o-\d\)", g["colour"] or "")
    ]
    assert not bad, bad

    on_walls = [
        decl
        for _stack, decl in _declarations_everywhere()
        if re.match(r"(background|border|outline)", decl) and "var(--o-" in decl
    ]
    assert on_walls, "仪器在面与线上一处梯子都没读到，上面那条断言不算数"


# --- §7.15 原件与抄本：§9 / §10 里的每一个数都必须指得准它的家 ------------------

# 摘掉**名字**再数量：节号、版本号、WCAG 条款号、验收行号都是标识符，不是测量值。
# 不列量词白名单——手写一张「处条层个次」的单子，本身就是 §9 禁的那个病（只摘活着
# 的那一副面具）：它会漏掉 `1.08:1`、`42%`、`1800ms`、`ΔE 5.80`、`0.19px`。
_DOC_NAME = re.compile(
    r"§\s*\d+(?:\.\d+)?|v\d+\.\d+|WCAG\s*[\d.]+|"
    r"\b(?:1\.4\.11|1\.4\.3|2\.4\.7|2\.5\.8|4\.1\.2)\b"
)
_DOC_ROWID = re.compile(r"^\|\s*\d+[a-z]*\s*\|")
# 省略前导零那一支必须自己写出来：`(?<![\w.])\d+…` 在 `.04` 上只能从 `0` 起匹配，而
# `0` 前面正是那个点号，于是这个量整个**读不到**——它不是「拼法没归一」，是漏报。
# 两节里今天有 51 处这么写（§9 30 + §10 21），一把读不到它们的尺子报出来的是合格。
_DOC_NUM = re.compile(r"(?<![\w.])((?:\d+(?:\.\d+)?|\.\d+)(?:e-?\d+)?)(?![\w.])")


def _doc_numbers(text: str) -> set[float]:
    """按**值**收数，不按拼法。`0.08` ≡ `.08`、`5` ≡ `5.0`——两副面具一起摘。"""

    return {float(m.group(1)) for m in _DOC_NUM.finditer(_DOC_NAME.sub(" ", text))}


def _doc_chapter(prefix: str) -> str:
    """按 §号取正文：`7.23` → `### 7.23 …`，`9` → `## 9. 禁止清单` 连同全部子节。

    标题写的是 `## 9. 禁止清单`，所以那个点号必须允许——不允许它，整章引用一律
    取到空串，于是点名它们的每一行都会被误判成「指错门」。
    """

    lines = DOC.splitlines()
    pat = re.compile(r"^#{2,5} " + re.escape(prefix) + r"\.? (?!\d)")
    i = next((k for k, ln in enumerate(lines) if pat.match(ln)), None)
    assert i is not None, f"文档里找不到 §{prefix}"
    depth = len(lines[i]) - len(lines[i].lstrip("#"))
    j = next(
        (k for k in range(i + 1, len(lines)) if re.match(r"#{1,%d} " % depth, lines[k])),
        len(lines),
    )
    return "\n".join(lines[i + 1 : j])


def test_the_two_checklists_hold_no_number_of_their_own():
    """§9 与 §10 里不许有原件，只许有抄本：每个数都要在它点名的那一节里查得到。

    「家在守卫文件」这一档很宽（六千行里的数几乎什么都撞得上），它被**三个数一起
    钉住**这件事收住：一个数从「点名的那一节」烂到只剩「守卫文件里碰巧有」，两个
    计数会同时移动，这条断言就红。所以宽的那一档不是一个逃逸口，是一个记账口。

    `silent` 那条断言今天守着空气（两处已补上出处），但它不需要正对照：认出处用的
    是同一个 `§` 正则，它一旦瞎掉，527 个数会一起掉进 `silent`，红得比谁都响。
    """

    guards = "\n".join(
        (Path(__file__).resolve().parent / f).read_text(encoding="utf-8")
        for f in ("test_aesthetic_baseline.py", "colour.py", "test_colour_ruler.py")
    )
    of_the_tool = _doc_numbers(guards)

    owned = tool = 0
    orphan, silent = [], []
    for heading, prefix in (("## 9.", "- "), ("## 10.", "| ")):
        for ln in _doc_section(heading).splitlines():
            if not ln.startswith(prefix) or set(ln) <= set("|-: "):
                continue
            here = _doc_numbers(_DOC_ROWID.sub(" ", ln))
            if not here:
                continue
            refs = list(dict.fromkeys(re.findall(r"§\s*(\d+(?:\.\d+)?)", ln)))
            if not refs:
                # 不写出处是这条判据唯一的逃逸口：一个抄本必须说出它抄的是哪一份。
                silent.append((sorted(here), re.sub(r"\s+", " ", ln)[:90]))
                continue
            home = set().union(*(_doc_numbers(_doc_chapter(r)) for r in refs))
            for n in sorted(here):
                if n in home:
                    owned += 1
                elif n in of_the_tool:
                    tool += 1
                else:
                    orphan.append((n, "§" + "/§".join(refs), re.sub(r"\s+", " ", ln)[:90]))
    assert not orphan, orphan
    assert not silent, silent

    counted = _doc_line("### 7.15", "个的家在它自己点名的那一节")
    written = [int(n) for n in _DOC_BOLD_NUM.findall(counted)]
    assert written == [owned + tool, owned, tool], (written, owned, tool)


_RE_CALLS = frozenset(
    ("compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split")
)

# 同一个量的三种合法拼法。三条都要在，因为它们各自暴露不同的偏食：只补前导零看不见
# 「只认 `0.NN`、`.NN` 一律漏掉」的尺子，只省前导零看不见反过来的那一把。
_RESPELLINGS = (
    ("补前导零", r"(?<![\w.])(\.\d)", r"0\1"),
    ("省前导零", r"(?<![\w.])0(\.\d)", r"\1"),
    ("补一位尾零", r"(?<![\w.])(\d*\.\d+)(?![\d.])", r"\g<1>0"),
)


def _numeric_rulers() -> dict[str, list[int]]:
    """本文件里所有写成字面量、并且认数字的正则：正则原文 → 它出现在哪几行。"""

    rulers: dict[str, list[int]] = {}
    for node in ast.walk(ast.parse(Path(__file__).resolve().read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _RE_CALLS or not node.args:
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "re"):
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
            continue
        if re.search(r"\\d|\[0-9", first.value):
            rulers.setdefault(first.value, []).append(node.lineno)
    return rulers


def test_no_ruler_changes_its_reading_when_a_quantity_is_spelt_the_other_legal_way():
    """一个量换一种合法拼法，这个文件里每一把认数的尺子读数都不许动。

    这一条不写成「换完之后测试不许红」，因为那样问是**朝一个方向瞎的**：一把过严的
    尺子会红得很响，而**真漏报的那一把恰恰保持绿**——它本来就没在看那些声明，少看见
    一处不会让任何断言不高兴。所以判据只能落在读数上：`len(findall)` 变了，就是这把
    尺子把「一个数」和「一个数的某一种写法」当成了一回事。

    这个病在这个文件里已经有三次病历：`TIME` 曾把 `.25s` 读成 25s（见开头那条注释）；
    §7.15 的计数曾读不到 `.04`；这一轮又抓到三处（§7.11 的 8 行、四条锚的 2 行、§6.1
    色相行把 `0.NN` 的 `0` 当成层数）。前两次都是改完一处就走，所以第三次还会来。

    代价大约 7s（尺子 × 两份文本 × 三种改写），是这个套件里最贵的一条。买的是「以后
    新加的认数尺子自动被查一遍」——这条法的对手是明天写的正则，不是今天的。
    """

    rulers = _numeric_rulers()
    assert len(rulers) >= 60, f"收集器该抓到几十把认数的尺子，只抓到 {len(rulers)}"

    moved = []
    for label, text in (("app.html", APP_HTML), ("文档", DOC)):
        for name, pattern, repl in _RESPELLINGS:
            other = re.sub(pattern, repl, text)
            assert other != text, f"{label} 里没有一处能{name}，这条改写在空跑"
            for ruler, lines in rulers.items():
                rx = re.compile(ruler, re.S)
                before, after = len(rx.findall(text)), len(rx.findall(other))
                if before != after:
                    moved.append((lines, label, name, before, after, ruler[:80]))
    assert not moved, moved


# 守字形的十三条 + 那张抄本表。动态守卫对它们跑「改写后的世界」：文件里凡是在拿
# 字面量比文字的断言，都要能通过 _by_quantity 或声明过的读法在另一种拼法下存活。
_GLYPH_GUARDED = (
    "recording_is_one_state_with_one_rhythm or "
    "no_hand_written_bezier_survives_outside_the_one_declaration or "
    "the_bounce_curve_stays_dead_until_something_is_lifted_out or "
    "reduced_motion_stops_everything_and_lands_on_the_end_state or "
    "the_companion_state_is_carried_by_rhythm_not_hue or "
    "the_only_important_left_is_the_one_that_cannot_win_by_specificity or "
    "the_ladder_has_exactly_seven_rungs_with_the_documented_values or "
    "the_opacity_knob_table_recomputes_cell_by_cell or "
    "the_ladder_is_only_a_ladder_below_a_floor_that_can_be_solved_for or "
    "the_fifth_veil_is_the_last_rung_the_ladder_survives or "
    "the_paper_is_past_the_gate_and_that_is_exactly_why_it_has_its_own_ink or "
    "the_sign_of_y_is_decided_by_the_light_not_by_the_author or "
    "the_seven_rungs_are_a_white_price or "
    "the_waiting_is_one_material_with_one_definition"
)


def _run_only(k_expression: str) -> set[str]:
    # 绝对路径锚定本文件：这条子进程从仓库根目录发起时（根目录 pytest.ini 同样收集
    # 本套件），相对路径 tests/... 会落空，pytest 以「file not found」退出且不输出
    # 任何 FAILED 行——红集期望便永远对不上，表现为一条无法解释的假红。
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(Path(__file__).resolve()), "-k", k_expression,
         "-p", "no:cacheprovider", "--tb=no", "-q"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return set(re.findall(r"^FAILED [^:]+::(\w+)", proc.stdout, re.M))


def test_the_by_quantity_guards_stay_green_under_every_legal_spelling():
    """把一个文件整份按一种合法拼法重写，这十四处断言不许跟着红。

    判据（§7.15）：十三条按量比的断言守的是量，`0.04` 换成 `.04` 它们必须全绿；
    抄本表那一条（the_waiting_is_one_material_with_one_definition）的判据就是**逐字**，
    它必须在**只改一侧**时红：app 写 `.10`、文档写 `0.10`，省掉任何一侧的前导零，两侧
    就不再逐字相同。两侧**一起**省零会把它们重新对齐，抄本反而绿——所以四个方向
    （app 补零 / app 省零 / 文档补零 / 文档省零）分开跑，红集写在表里而不是推导。

    只改两种拼法而不跑第三种（补一位尾零）：那一种会同时改掉 Python 侧 `repr(float)`
    的规范化输出（`0.4` → `0.40` 之类），把断言本身也改写掉。它属于 #117。
    """

    app = Path(__file__).resolve().parents[2] / "app.html"
    doc = Path(__file__).resolve().parents[2] / "内在地形-美学基线-v1.md"
    copy = {"test_the_waiting_is_one_material_with_one_definition"}
    cases = (
        (app, "补前导零", r"(?<![\w.])(\.\d)", r"0\1", set()),
        (app, "省前导零", r"(?<![\w.])0(\.\d)", r"\1", copy),
        (doc, "补前导零", r"(?<![\w.])(\.\d)", r"0\1", set()),
        (doc, "省前导零", r"(?<![\w.])0(\.\d)", r"\1", copy),
    )
    for target, label, pattern, repl, expect_red in cases:
        raw = target.read_bytes()
        try:
            target.write_bytes(re.sub(pattern, repl, raw.decode("utf-8")).encode("utf-8"))
            failed = _run_only(_GLYPH_GUARDED)
        finally:
            target.write_bytes(raw)
        assert hashlib.sha1(target.read_bytes()).digest() == hashlib.sha1(raw).digest(), (
            f"{label} 后还原失败：{target}"
        )
        assert failed == expect_red, (target.name, label, failed, expect_red)


def test_no_numeric_ruler_is_written_twice():
    """一把认数的尺子只许有一份定义（§7.23：一个概念只有一把尺子）。

    v1.40 的普查发现 6 组 20 处原文重复——同一个正则抄在好几个测试里，改一处漏
    六处，而两份抄本的行为一起变才算改对（§7.15 判据（一）的记账口拦不住它：
    抄本们一起错的时候没有任何读数会动）。这六组已提成模块级 `re.compile` 常量；
    这一条守「第二遍不再出现」：收集器里任何原文出现两次即红。

    盲区如实记：收集器只收「第一个参数是字面量」的调用，把正则拆成两段拼起来的
    （`r"..." + r"..."`）它看不见；compile 是唯一定义处，所以定义成 compile 而
    不是字符串。收集器自己瞎掉的保险在下面那条元守卫的「不少于六十把」自校验里，
    这一条也带同一句。
    """

    rulers = _numeric_rulers()
    assert len(rulers) >= 60, f"收集器该抓到几十把认数的尺子，只抓到 {len(rulers)}"
    dups = {pat: lines for pat, lines in rulers.items() if len(lines) > 1}
    assert not dups, dups

