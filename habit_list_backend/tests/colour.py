"""色度学尺子：sRGB / WCAG 对比度 / CIELAB / CIEDE2000 / alpha 合成。

**这个模块不认识这个产品。** 它只做数学，一个 token 名都不知道——这样它才能被
`test_colour_ruler.py` 用外部权威数据单独校准。产品那一侧（哪个 token、压在哪层底上）
全部留在 `test_aesthetic_baseline.py` 里，它已经在解析 `:root` 了。

为什么它必须存在：美学基线文档里有 52 处 ΔE 与 93 处对比度，而在这之前它们全部是
一次性脚本算出来的、脚本算完就删。§7.10 那张 ΔE 表的一整列因此错了整整一版
（4.03/3.52/3.33/3.00/2.72/2.73/1.17/0.94 → 实际 3.84/3.61/3.32/3.26/2.99/2.45/1.27/1.07），
而错误既发现不了也复现不了——尺子已经不在了。**一把没被校准过的尺子给出的数字，
和一条没有测试守着的规则是同一类东西：读起来像证据，实际只是碰巧没错到能被看出来。**

两个容易写错、而写错了不会有任何症状的地方，写在这里而不是只写在实现里：

1. **alpha 合成的口径不能靠推理，只能靠量。** 它有三个看起来都对的候选（连续空间不取整 /
   按精确 α 取整 / canvas 那种截断），三个答案互不相同，而**没有一个是 CSS 的**。
   实测口径见 `over()`。校准数据是浏览器截图逐像素读回来的，放在
   `test_colour_ruler.py` 的 `BROWSER_COMPOSITES` 里。
2. **WCAG 的线性化门槛写 0.03928，不是 sRGB 标准的 0.04045——而这个选择测不出来。**
   在 8 位**整数**通道上两个数逐值相同（v=10 时 10/255≈.0392 在两边都走线性支，
   v=11 时 ≈.0431 在两边都走幂支，中间没有整数），所以没有任何校准数据能分辨它们。
   这里按 WCAG 的原文写，并由 `test_the_two_linearisation_thresholds_are_indistinguishable_here`
   把「测不出来」这件事本身钉住。**先前这段声称 `#767676` 那个边界锁着这个选择，
   那是假的**（118/255≈.463，离两个门槛都远），而变异测试才把它抓出来。
"""
from __future__ import annotations

import math
import re

Rgb = tuple[float, float, float]
Lab = tuple[float, float, float]

# sRGB → XYZ (D65)，以及 D65 参考白。
_M = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_WHITE_D65 = (0.95047, 1.00000, 1.08883)


def parse(value: str) -> Rgb:
    """`#f2ede5` / `#fff` / `242,237,229` / `rgb(242,237,229)` → (r, g, b)，0–255。

    这个产品的 `:root` 里同一个颜色有两种拼法（hex 与 `-rgb` 孪生），两种都要认，
    否则测试就得自己挑一种抄下来——而抄下来的那一份正是 §7.4 要防的东西。
    """

    text = value.strip()
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", text)
    if hex_match:
        digits = hex_match.group(1)
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    triple = re.fullmatch(r"(?:rgba?\()?\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*\)?", text)
    if triple:
        return tuple(float(triple.group(i)) for i in (1, 2, 3))  # type: ignore[return-value]
    raise ValueError(f"认不出这个颜色：{value!r}")


def _round_half_up(value: float) -> int:
    """浏览器的取整：`.5` 一律向上，不是 Python 的「偶数优先」。

    整条梯子上只有一个值踩到这个区别，而它就在梯子中间：`.30 × 255 = 76.5`。
    Python 的 `round` 给 76，浏览器给 77，于是第 4 档整档差一个刻度。
    """

    return math.floor(value + 0.5)


def over(fg: Rgb, bg: Rgb, alpha: float) -> Rgb:
    """把 fg 以 alpha 压在 bg 上，返回浏览器**实测**会渲染出的那个 8 位结果。

    **这不是 `fg·α + bg·(1−α)` 再取整。** 那是最自然的猜法，也是这份文档先前用的，
    它和 Chromium 在 14 个测点里有 9 个差一个刻度。实测的三步是：

    1. **α 先量化到 8 位**（`round(α·255)/255`）。所以 `.08` 实际是 `20/255 = .078431`。
    2. **前景按量化后的 α 预乘、并在 8 位上取整**（`round(fg·α)`）。
    3. 再与底混合、取整。

    第 2 步是差别的主要来源，也是最不容易想到的一步：预乘是**存下来**的，不是算式里
    的一个中间量。校准数据见 `test_colour_ruler.py` 的 `BROWSER_COMPOSITES`。
    """

    aq = _round_half_up(alpha * 255) / 255
    return tuple(  # type: ignore[return-value]
        _round_half_up(_round_half_up(f * aq) + b * (1 - aq)) for f, b in zip(fg, bg)
    )


def stack(bg: Rgb, *layers: tuple[Rgb, float]) -> Rgb:
    """依次压若干层：`stack(deep, (veil, .95), (white, .04))`。

    逐层取整，和浏览器一样——一次算完再取整会和实测像素差 1。
    """

    out = bg
    for colour, alpha in layers:
        out = over(colour, out, alpha)
    return out


def _wcag_channel(value: float) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: Rgb) -> float:
    """WCAG 相对亮度。"""

    r, g, b = (_wcag_channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: Rgb, b: Rgb) -> float:
    """WCAG 对比度，1.0–21.0，谁亮谁在上无关。"""

    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _srgb_channel(value: float) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def to_lab(rgb: Rgb) -> Lab:
    """sRGB (0–255) → CIELAB，D65。"""

    linear = [_srgb_channel(c) for c in rgb]
    xyz = [sum(row[i] * linear[i] for i in range(3)) for row in _M]

    def f(t: float) -> float:
        return t ** (1 / 3) if t > (6 / 29) ** 3 else t / (3 * (6 / 29) ** 2) + 4 / 29

    fx, fy, fz = (f(v / w) for v, w in zip(xyz, _WHITE_D65))
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1: Lab, lab2: Lab) -> float:
    """CIEDE2000，按 Sharma / Wu / Dalal (2005)。kL = kC = kH = 1。

    两个最容易写错、而写错了只在特定色相上差一点点的地方：
    - `H̄'` 的平均要按两角之差是否 > 180° 分支，否则跨 0° 的一对会算出反向的平均色相。
    - `R_T` 里的 `Δθ` 是 `30·exp(-((H̄'-275)/25)²)`，那个 275 是蓝色区的中心。

    规范里还有第三条「`h'` 在 a'、b' 同时为 0 时定义为 0」，这里**没有**写成分支：
    Python 的 `atan2(0, 0)` 已经给 0，写出来是死代码。变异测试把它删掉时一条都没红——
    **那正是「这段代码在守什么」的答案**，所以不留一段自称在守边界、实际没有对手的分支。
    """

    l1, a1, b1 = lab1
    l2, a2, b2 = lab2

    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    c_bar = (c1 + c2) / 2
    g = 0.5 * (1 - math.sqrt(c_bar**7 / (c_bar**7 + 25**7))) if c_bar else 0.5
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)

    def hue(ap: float, bp: float) -> float:
        return math.degrees(math.atan2(bp, ap)) % 360

    h1p, h2p = hue(a1p, b1), hue(a2p, b2)

    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dhp_big = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))

    lbp = (l1 + l2) / 2
    cbp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2

    t = (
        1
        - 0.17 * math.cos(math.radians(hbp - 30))
        + 0.24 * math.cos(math.radians(2 * hbp))
        + 0.32 * math.cos(math.radians(3 * hbp + 6))
        - 0.20 * math.cos(math.radians(4 * hbp - 63))
    )
    d_theta = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbp**7 / (cbp**7 + 25**7)) if cbp else 0.0
    sl = 1 + (0.015 * (lbp - 50) ** 2) / math.sqrt(20 + (lbp - 50) ** 2)
    sc = 1 + 0.045 * cbp
    sh = 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2 * d_theta)) * rc

    term_l = dlp / sl
    term_c = dcp / sc
    term_h = dhp_big / sh
    return math.sqrt(term_l**2 + term_c**2 + term_h**2 + rt * term_c * term_h)


def delta_e(a: Rgb, b: Rgb) -> float:
    """两个 sRGB 颜色之间的 CIEDE2000。"""

    return ciede2000(to_lab(a), to_lab(b))


WHITE: Rgb = (255, 255, 255)
BLACK: Rgb = (0, 0, 0)

# 可辨阈。§7.10 判过：ΔE 2.3 是「能不能看出两个颜色不一样」，而这条阶梯里
# 「够得上一档」的价钱是 ΔE 15 起——**两个数不是一回事，别混用。**
JND = 2.3
