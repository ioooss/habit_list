# 一次性审计脚本：投影的「浮多高」与光晕的「发光半径」全量数据
# 复用 test_aesthetic_baseline.py 的解析器，保证与守卫同一把尺子。
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import test_aesthetic_baseline as tab

layers = tab._parsed_light()

print("=== 投影（非 inset，y≠0，blur>0，spread=0）===")
drop_y = Counter()
drop_rows = []
for ch, where, layer, g in layers:
    y, blur, spread = (tab._px(g[k]) for k in ("y", "blur", "spread"))
    if not g["inset"] and y != 0 and blur > 0 and spread == 0:
        drop_y[abs(y)] += 1
        drop_rows.append((abs(y), blur, where, layer))
print(f"层数: {len(drop_rows)}  高度取值: {sorted(drop_y)}  ({len(drop_y)} 个)")
for y in sorted(drop_y):
    print(f"  y={y:g}px × {drop_y[y]} 层")

print()
print("=== 光晕（非 inset，y=0，blur>0，spread=0）===")
halo_b = Counter()
halo_rows = []
for ch, where, layer, g in layers:
    y, blur, spread = (tab._px(g[k]) for k in ("y", "blur", "spread"))
    if not g["inset"] and y == 0 and blur > 0 and spread == 0:
        halo_b[blur] += 1
        halo_rows.append((blur, where, layer))
print(f"层数: {len(halo_rows)}  半径取值: {sorted(halo_b)}  ({len(halo_b)} 个)")
for b in sorted(halo_b):
    print(f"  blur={b:g}px × {halo_b[b]} 层")

print()
print("=== 光晕逐层明细（半径 → 宿主）===")
for b, where, layer in sorted(halo_rows):
    print(f"  {b:>5g}  {where[:90]}  |  {layer[:70]}")

print()
print("=== 投影逐层明细（高度 → 宿主）===")
for y, blur, where, layer in sorted(drop_rows):
    print(f"  {y:>4g}/{blur:<4g}  {where[:90]}")

print()
print("=== 内光（inset，y=0，blur>0）参照 ===")
inner = Counter()
for ch, where, layer, g in layers:
    y, blur = tab._px(g["y"]), tab._px(g["blur"])
    if g["inset"] and y == 0 and blur > 0:
        inner[blur] += 1
print(f"层数: {sum(inner.values())}  深度取值: {sorted(inner)}  ({len(inner)} 个)")
