"""校准 `colour.py` 这把尺子——**全套里唯一一条测尺子、不测产品的测试。**

期望值全部来自外部权威，一个都不许由本仓库的实现生成：

- ΔE00 的 34 对来自 Sharma / Wu / Dalal, *Color Research & Application* 30(1):21-30
  (2005) 的 Table 1，那份表是专门为「实现对不对」造的：它挑的都是分支边界——
  跨 0° 的色相对、a'=b'=0 的中性对、`R_T` 起作用的蓝色区。所以 34 对全过
  几乎不可能是巧合，而**只错一两对**几乎一定是期望值抄错了而不是实现错了
  （实现里任何一个分支写错都会同时打挂一批）。
- 对比度的锚点来自 WCAG 2.x 自己：白对黑恰好 21，同色恰好 1，
  `#767676` 在白上恰好过 4.5 而 `#777777` 恰好不过。

阈值取 `1e-4`：上一轮量到的最大误差是 4.95e-05，所以一个「过了 1e-4 但明显
高于 5e-5」的结果意味着实现漂了，而不是精度不够。
"""
from __future__ import annotations

from tests import colour

# Sharma / Wu / Dalal (2005) Table 1：(Lab1, Lab2, 官方 ΔE00)
SHARMA = [
    ((50.0000, 2.6772, -79.7751), (50.0000, 0.0000, -82.7485), 2.0425),
    ((50.0000, 3.1571, -77.2803), (50.0000, 0.0000, -82.7485), 2.8615),
    ((50.0000, 2.8361, -74.0200), (50.0000, 0.0000, -82.7485), 3.4412),
    ((50.0000, -1.3802, -84.2814), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -1.1848, -84.8006), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, -0.9009, -85.5211), (50.0000, 0.0000, -82.7485), 1.0000),
    ((50.0000, 0.0000, 0.0000), (50.0000, -1.0000, 2.0000), 2.3669),
    ((50.0000, -1.0000, 2.0000), (50.0000, 0.0000, 0.0000), 2.3669),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0009), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0010), 7.1792),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0011), 7.2195),
    ((50.0000, 2.4900, -0.0010), (50.0000, -2.4900, 0.0012), 7.2195),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0009, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0010, -2.4900), 4.8045),
    ((50.0000, -0.0010, 2.4900), (50.0000, 0.0011, -2.4900), 4.7461),
    ((50.0000, 2.5000, 0.0000), (50.0000, 0.0000, -2.5000), 4.3065),
    ((50.0000, 2.5000, 0.0000), (73.0000, 25.0000, -18.0000), 27.1492),
    ((50.0000, 2.5000, 0.0000), (61.0000, -5.0000, 29.0000), 22.8977),
    ((50.0000, 2.5000, 0.0000), (56.0000, -27.0000, -3.0000), 31.9030),
    ((50.0000, 2.5000, 0.0000), (58.0000, 24.0000, 15.0000), 19.4535),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.1736, 0.5854), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2972, 0.0000), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 1.8634, 0.5757), 1.0000),
    ((50.0000, 2.5000, 0.0000), (50.0000, 3.2592, 0.3350), 1.0000),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((63.0109, -31.0961, -5.8663), (62.8187, -29.7946, -4.0864), 1.2630),
    ((61.2901, 3.7196, -5.3901), (61.4292, 2.2480, -4.9620), 1.8731),
    ((35.0831, -44.1164, 3.7933), (35.0232, -40.0716, 1.5901), 1.8645),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
    ((36.4612, 47.8580, 18.3852), (36.2715, 50.5065, 21.2231), 1.4146),
    ((90.8027, -2.0831, 1.4410), (91.1528, -1.6435, 0.0447), 1.4441),
    ((90.9257, -0.5406, -0.9208), (88.6381, -0.8985, -0.7239), 1.5381),
    ((6.7747, -0.2908, -2.4247), (5.8714, -0.0985, -2.2286), 0.6377),
    ((2.0776, 0.0795, -1.1350), (0.9033, -0.0636, -0.5514), 0.9082),
]


# 浏览器实测：(fg, bg, α, 屏幕上真实的像素)
#
# 权威是 Chromium 本身，和 `SHARMA` 的权威是 Sharma/Wu/Dalal (2005) 一样——两半尺子
# 各有各的外部权威，一个都不许由 `colour.py` 生成。取法：在真实页面上叠一层 `__probe`
# 覆盖层，每档一条横带，`set viewport 400x800` 后截图，再用纯 Python 解 PNG 逐像素读回。
#
# 两条 α=0 的对照行不是凑数：它们证明这条取像素的链路（截图缩放、色彩管理、PNG 滤波）
# 本身不改颜色。如果对照行都读不回原色，上面 14 行一个都不能信。
BROWSER_COMPOSITES = [
    ((255, 255, 255), (14, 20, 35), 0.00, (14, 20, 35)),
    ((255, 255, 255), (14, 20, 35), 0.04, (23, 29, 44)),
    ((255, 255, 255), (14, 20, 35), 0.08, (33, 38, 52)),
    ((255, 255, 255), (14, 20, 35), 0.16, (53, 58, 70)),
    ((255, 255, 255), (14, 20, 35), 0.30, (87, 91, 101)),
    ((255, 255, 255), (14, 20, 35), 0.45, (123, 126, 134)),
    ((255, 255, 255), (14, 20, 35), 0.72, (188, 190, 194)),
    ((255, 255, 255), (14, 20, 35), 0.95, (243, 243, 244)),
    ((0, 0, 0), (230, 230, 230), 0.00, (230, 230, 230)),
    ((5, 7, 12), (230, 230, 230), 0.45, (128, 129, 131)),
    ((5, 7, 12), (230, 230, 230), 0.72, (68, 69, 73)),
    ((117, 108, 93), (10, 14, 24), 0.90, (107, 98, 86)),
    ((117, 108, 93), (10, 14, 24), 0.72, (87, 82, 74)),
    ((117, 108, 93), (10, 14, 24), 0.62, (76, 72, 67)),
    ((117, 108, 93), (10, 14, 24), 0.55, (69, 65, 62)),
    ((117, 108, 93), (10, 14, 24), 0.35, (48, 47, 48)),
]


def test_ciede2000_matches_sharma_reference():
    errors = {}
    for lab1, lab2, expected in SHARMA:
        got = colour.ciede2000(lab1, lab2)
        if abs(got - expected) >= 1e-4:
            errors[(lab1, lab2)] = (expected, round(got, 6))
    assert not errors, f"这些对和 Sharma 表对不上：{errors}"


def test_ciede2000_precision_has_not_drifted():
    """上一轮量到 4.95e-05。这条不是重复上面那条——它守的是「有没有漂」。"""

    worst = max(abs(colour.ciede2000(a, b) - e) for a, b, e in SHARMA)
    assert worst < 6e-5, f"最大误差 {worst:.3e} 超过上一轮的 4.95e-05，实现漂了"


def test_ciede2000_is_symmetric():
    """ΔE00 不对称的实现是常见错误：`H̄'` 的分支写反了只在一个方向上错。"""

    for lab1, lab2, _ in SHARMA:
        assert abs(colour.ciede2000(lab1, lab2) - colour.ciede2000(lab2, lab1)) < 1e-9


def test_wcag_contrast_anchors():
    assert colour.contrast(colour.WHITE, colour.BLACK) == 21.0
    assert colour.contrast(colour.WHITE, colour.WHITE) == 1.0
    assert colour.contrast(colour.BLACK, colour.BLACK) == 1.0
    # 谁在上无关。
    assert colour.contrast(colour.BLACK, colour.WHITE) == 21.0


def test_wcag_45_threshold_sits_between_767676_and_777777():
    """WCAG 自己发布的那个边界：`#767676` 在白上刚过 4.5，下一档灰就不过了。"""

    pass_grey = colour.contrast(colour.parse("#767676"), colour.WHITE)
    fail_grey = colour.contrast(colour.parse("#777777"), colour.WHITE)
    assert pass_grey >= 4.5, pass_grey
    assert fail_grey < 4.5, fail_grey


def test_the_two_linearisation_thresholds_are_indistinguishable_here():
    """WCAG 用 0.03928，sRGB 标准用 0.04045——**在 8 位整数通道上这两个数逐值相同**。

    上面那条守卫先前的文档串声称它「同时锁住门槛用的是 0.03928」。它锁不住：
    118/255 ≈ 0.463，离两个门槛都远得很。而且没有任何数据能锁住——v=10 时
    10/255 ≈ .0392 在两边都走线性支，v=11 时 ≈ .0431 在两边都走幂支，**中间没有整数**。

    所以这条守卫证的是「测不出来」这件事本身。留着它是因为：
    **一条自称在守某件事、而那件事它碰不到的断言，比没有断言更坏**——它让读的人
    以为那个选择被看着，于是不会再去看。这一条是变异测试抓出来的，不是我读出来的。
    """

    def channel(v: int, threshold: float) -> float:
        c = v / 255
        return c / 12.92 if c <= threshold else ((c + 0.055) / 1.055) ** 2.4

    disagree = [v for v in range(256) if channel(v, 0.03928) != channel(v, 0.04045)]
    assert not disagree, f"竟然分得出来，那门槛就该有守卫：{disagree}"
    # 实现按 WCAG 原文写。这一句是可判定的部分：它必须是那两条支，不是别的曲线。
    assert all(colour._wcag_channel(v) == channel(v, 0.03928) for v in range(256))


def test_lab_anchors():
    lightness, a, b = colour.to_lab(colour.WHITE)
    assert abs(lightness - 100.0) < 1e-4
    assert abs(a) < 1e-3 and abs(b) < 1e-3
    assert abs(colour.to_lab(colour.BLACK)[0]) < 1e-9
    assert colour.delta_e(colour.WHITE, colour.WHITE) == 0.0


def test_parse_accepts_every_spelling_this_product_uses():
    expected = (242, 237, 229)
    assert colour.parse("#f2ede5") == expected
    assert colour.parse("#F2EDE5") == expected
    assert colour.parse("242,237,229") == expected
    assert colour.parse("rgb(242, 237, 229)") == expected
    assert colour.parse("#fff") == colour.WHITE


def test_alpha_composite_matches_what_the_browser_actually_renders():
    """`over()` 的权威是浏览器本身，因为这个数最终由它算。

    这半把尺子先前根本没被校准过：它用的是 `fg·α + bg·(1−α)` 再取整，看起来天经地义，
    而它和实测在 14 个测点里有 9 个差一个刻度。一个刻度在对比度上是 0.01–0.05、
    在 ΔE 上是 0.1–0.3——**刚好是「读起来像证据」的量级**。
    """

    errors = {}
    for fg, bg, alpha, expected in BROWSER_COMPOSITES:
        got = colour.over(fg, bg, alpha)
        if got != expected:
            errors[(fg, bg, alpha)] = (expected, got)
    assert not errors, f"这些测点和浏览器对不上：{errors}"


def test_the_naive_composite_would_have_been_wrong_and_silently_so():
    """反面：把那个「天经地义」的算法放回来，14 个测点里有几个会错。

    留这条是因为**下一个人也会觉得 `fg·α + bg·(1−α)` 显然是对的**，然后顺手改回去。
    这条会告诉他错在哪、错多少。
    """

    def naive(fg, bg, a):
        return tuple(round(f * a + b * (1 - a)) for f, b in zip(fg, bg))
    wrong = [
        (fg, bg, alpha)
        for fg, bg, alpha, expected in BROWSER_COMPOSITES
        if naive(fg, bg, alpha) != expected
    ]
    assert len(wrong) == 9, [len(wrong), wrong]


def test_alpha_composite_edges_and_layering():
    """α=0 与 α=1 必须是恒等与替换；多层必须逐层落到 8 位。"""

    ground = (10, 14, 24)
    assert colour.over(colour.WHITE, ground, 0.0) == ground
    assert colour.over(colour.WHITE, ground, 1.0) == colour.WHITE
    assert colour.stack(ground) == ground
    # 逐层 vs 一次算完：合成等效 α 一次算会和浏览器差一个刻度。
    layered = colour.stack(ground, (colour.WHITE, 0.04), (colour.WHITE, 0.16))
    effective = 1 - (1 - 0.04) * (1 - 0.16)
    assert layered != colour.over(colour.WHITE, ground, effective)
