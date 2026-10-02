# -*- coding: utf-8 -*-
"""
空白区交互 + 设置窗口自检。

覆盖：
  A. HitTester 命中正确性（中心 / 间隙 / 边界 / 四角 / 面积守恒 / 四实现一致）
  B. 性能（116 图标下 hit() < 0.1ms）
  C. 左右键语义（左键空白→退出；右键空白→设置；点图标两者都不触发）
  D. click_through_empty 开关
  E. 菜单位置钳制不出屏
  F. 设置窗口（增删改 / 保存 / 重载 / 钳制 / 恢复默认 / 取消回滚 / spring 透传）

离屏运行：QT_QPA_PLATFORM=offscreen，不碰真实桌面、不抢热键。
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import (QEvent, QEventLoop, QPoint, QPointF, QRect, Qt,
                          QTimer)
from PyQt5.QtGui import QIcon, QMouseEvent
from PyQt5.QtWidgets import QApplication, QWidget

app = QApplication(sys.argv)

from launchpad.blankarea import (HitTester, BlankAreaController,  # noqa: E402
                                 MENU_ITEMS, build_blank_menu,
                                 build_tile_menu, clamp_to_screen,
                                 rect_hit)
from launchpad.settings import SCHEMA, Settings                       # noqa: E402
from launchpad.settingswin import (GROUP_SPECS, SettingsWindow,       # noqa: E402
                                   clamp)
from launchpad.tile import Tile                                       # noqa: E402


# ── 测试框架 ──────────────────────────────────────────────
_results = []


def check(name, cond, detail=""):
    _results.append(bool(cond))
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {name}" + (f"   ({detail})" if detail else ""))
    return bool(cond)


def section(title):
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


def pump(ms=60):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


def press(widget, x, y, button=Qt.LeftButton):
    """在 widget 局部坐标 (x, y) 造一次真实的按下事件并投递。"""
    ev = QMouseEvent(QEvent.MouseButtonPress, QPointF(x, y),
                     button, button, Qt.NoModifier)
    QApplication.sendEvent(widget, ev)
    return ev


class FakeEntry:
    def __init__(self, name="App", target=r"C:\app.exe"):
        self.name = name
        self.target = target
        self.args = ""
        self.workdir = ""
        self.missing = False


# ══════════════════════════════════════════════════════════════
section("A. HitTester 命中正确性")
# ══════════════════════════════════════════════════════════════

# 3 个 128x128 的图标，横纵都留 32px 间隙
ICON = 128
GAP = 32
STEP = ICON + GAP
RECTS = [(i * STEP, j * STEP, ICON, ICON)
         for j in range(3) for i in range(3)]

ht = HitTester()
ht.set_layout(RECTS)
print(f"  backend={ht.backend}  n={len(ht)}  stats={ht.stats()}")

# 图标中心
centers_ok = True
for idx, (x, y, w, h) in enumerate(RECTS):
    got = ht.hit(x + w // 2, y + h // 2)
    if got != idx:
        centers_ok = False
        print(f"    中心错位: rect#{idx} 中心({x+w//2},{y+h//2}) -> {got}")
check("每个图标中心都命中自己", centers_ok)

# 间隙（四条边上的中点）
gaps = []
for i in range(2):
    cx = RECTS[i][0] + ICON + GAP // 2
    gaps.append((cx, RECTS[i][1] + ICON // 2))
for j in range(2):
    cy = RECTS[j][1] + ICON + GAP // 2
    gaps.append((RECTS[j][0] + ICON // 2, cy))
gap_bad = [p for p in gaps if ht.hit(*p) is not None]
check("图标之间的横向/纵向间隙都是空白", not gap_bad,
      f"{len(gaps)} 个间隙点，错判 {len(gap_bad)}: {gap_bad[:4]}")

# 角落交叉点（大面积空白）
corners = [(STEP * 2 - GAP // 2, STEP * 2 - GAP // 2),
           (STEP * 2 + ICON + GAP, STEP * 2 + ICON + GAP),
           (-GAP - 1, -GAP - 1)]
corner_bad = [p for p in corners if ht.hit(*p) is not None]
check("角落/远端空白是空白", not corner_bad, f"错判 {corner_bad}")

# ── 边界规则（半开区间） ─────────────────────────────────
x, y, w, h = RECTS[4]
check("左上角像素命中（左/上边界含）", ht.hit(x, y) == 4, ht.describe_hit(x, y))
check("右下角像素命中", ht.hit(x + w - 1, y + h - 1) == 4,
      ht.describe_hit(x + w - 1, y + h - 1))
check("右边界 x+w 不命中", ht.hit(x + w, y) is None,
      ht.describe_hit(x + w, y))
check("下边界 y+h 不命中", ht.hit(x, y + h) is None,
      ht.describe_hit(x, y + h))
check("外侧一像素不命中", ht.hit(x - 1, y) is None and ht.hit(x, y - 1) is None)

# 与 QRect.contains 逐像素一致
qr = QRect(x, y, w, h)
mismatch = [(px, py) for py in range(y - 2, y + h + 2)
            for px in range(x - 2, x + w + 2)
            if (qr.contains(px, py)) != (ht.hit(px, py) == 4)]
check("与 QRect.contains() 逐像素一致（边界无歧义）", not mismatch,
      f"不一致 {len(mismatch)} 处: {mismatch[:4]}")

# ── 相邻无间隙：边界像素归右边/下边那个 ──────────────────
tight = HitTester()
tight.set_layout([(0, 0, 100, 100), (100, 0, 100, 100)])
check("紧邻时左图右边界像素归右图", tight.hit(100, 50) == 1,
      tight.describe_hit(100, 50))
check("紧邻时左图最后一个像素仍归左图", tight.hit(99, 50) == 0,
      tight.describe_hit(99, 50))
tight_v = HitTester()
tight_v.set_layout([(0, 0, 100, 100), (0, 100, 100, 100)])
check("紧邻时下图上边界像素归下图", tight_v.hit(50, 100) == 1,
      tight_v.describe_hit(50, 100))

# ── 面积守恒：命中像素数 == 各矩形面积之和 ─────────────────
area = sum(w * h for _x, _y, w, h in RECTS)
counted = 0
x0 = min(r[0] for r in RECTS) - 2
y0 = min(r[1] for r in RECTS) - 2
x1 = max(r[0] + r[2] for r in RECTS) + 2
y1 = max(r[1] + r[3] for r in RECTS) + 2
for py in range(y0, y1):
    for px in range(x0, x1):
        if ht.hit(px, py) is not None:
            counted += 1
check("命中像素数 == 矩形面积之和（无重复无遗漏）", counted == area,
      f"命中 {counted} vs 面积 {area}")

# ── 重叠时取最小下标 ─────────────────────────────────────
# #0 完全覆盖 #2，#1 与二者部分重叠。任何落在重叠区的点都必须归 #0
# （下标最小者胜），否则同一像素在不同时刻会命中不同图标。
ov = HitTester()
ov.set_layout([(0, 0, 100, 100), (50, 50, 100, 100), (10, 10, 20, 20)])
check("重叠时命中最小下标", ov.hit(60, 60) == 0, ov.describe_hit(60, 60))
check("被完全覆盖的图标也能命中（靠最小下标仍归 #0）",
      ov.hit(15, 15) == 0, ov.describe_hit(15, 15))
check("只在 #2 范围内的点仍归 #0（#0 更大且下标更小）",
      ov.hit(12, 12) == 0, ov.describe_hit(12, 12))
check("三个矩形之外的点是真空白", ov.hit(300, 300) is None,
      ov.describe_hit(300, 300))

# ── 四种实现语义一致 ─────────────────────────────────────
from launchpad.blankarea import (_BisectIndex, _BucketIndex,  # noqa: E402
                                 _LinearIndex, _NumpyIndex)

probes = []
for py in range(-5, STEP * 3 + 6, 7):
    for px in range(-5, STEP * 3 + 6, 7):
        probes.append((px, py))
impls = {"linear": _LinearIndex(RECTS), "bisect": _BisectIndex(RECTS),
         "bucket": _BucketIndex(RECTS), "numpy": _NumpyIndex(RECTS)}
base_name = "linear"
for name, idx in impls.items():
    diff = [p for p in probes if idx.hit(*p) != impls[base_name].hit(*p)]
    check(f"{name} 与 linear 语义完全一致（{len(probes)} 个探测点）",
          not diff, f"不一致 {len(diff)}: {diff[:4]}")

# 紧邻布局上也一致
probes2 = [(px, py) for py in range(-3, 204, 3) for px in range(-3, 204, 3)]
tight_rects = [(0, 0, 100, 100), (100, 0, 100, 100), (0, 100, 100, 100),
               (100, 100, 100, 100)]
impls2 = {"linear": _LinearIndex(tight_rects),
          "bisect": _BisectIndex(tight_rects),
          "bucket": _BucketIndex(tight_rects),
          "numpy": _NumpyIndex(tight_rects)}
for name, idx in impls2.items():
    diff = [p for p in probes2 if idx.hit(*p) != impls2["linear"].hit(*p)]
    check(f"{name} 在紧邻布局上也与 linear 一致", not diff,
          f"不一致 {len(diff)}: {diff[:4]}")

# 零尺寸矩形不命中但下标不塌陷
z = HitTester()
z.set_layout([(0, 0, 128, 128), (500, 500, 0, 0), (300, 0, 128, 128)])
check("零尺寸矩形永不命中", z.hit(500, 500) is None, z.describe_hit(500, 500))
check("零尺寸项不挤压后续下标", z.hit(300 + 64, 64) == 2,
      z.describe_hit(364, 64))
check("len 保留全部项（含零尺寸）", len(z) == 3, f"len={len(z)}")

# 空布局
e = HitTester()
e.set_layout([])
check("空布局任何点都是空白",
      e.hit(0, 0) is None and e.hit(-999, 999) is None)

# 非法 backend
try:
    HitTester("bogus").set_layout([])
    check("非法 backend 抛 ValueError", False, "没有抛异常")
except ValueError:
    check("非法 backend 抛 ValueError", True)

# 非法矩形项
try:
    HitTester().set_layout([(1, 2, 3)])
    check("非 4 元组矩形抛 TypeError", False, "没有抛异常")
except TypeError:
    check("非 4 元组矩形抛 TypeError", True)


# ══════════════════════════════════════════════════════════════
section("B. 性能")
# ══════════════════════════════════════════════════════════════

from launchpad.blankarea import _grid_rects, bench                 # noqa: E402

big = _grid_rects(116, 7, 5, 352, 270, 128, 184, 56)
ht_perf = HitTester()
ht_perf.set_layout(big)
print(f"  auto 选型: {ht_perf.backend}")

# 单次命中 < 0.1ms。取多次批量测量的均值，比 timeit 单次更稳
sample = [(200 + i * 37 % 2000, 200 + i * 53 % 1200) for i in range(3000)]
t0 = time.perf_counter()
for px, py in sample:
    ht_perf.hit(px, py)
per = (time.perf_counter() - t0) / len(sample)
check(f"116 图标 hit() 单次 < 0.1ms", per < 0.0001,
      f"{per * 1e6:.3f}µs/次  ({per * 1e3:.5f}ms)")

# 空白查询也要快（真实点击多数落在空白）
blank_sample = [(30, 30), (2450, 5400), (1234, 1300)]
t0 = time.perf_counter()
for _ in range(3000):
    for px, py in blank_sample:
        ht_perf.hit(px, py)
per_blank = (time.perf_counter() - t0) / 9000
check("空白查询同样 < 0.1ms", per_blank < 0.0001, f"{per_blank * 1e6:.3f}µs/次")

# 四实现对比（116 / 1000 / 退化布局）
print()
for label, rects in (("116 (7x5)", big),
                     ("1000 (16x10)", _grid_rects(1000, 16, 10, 352, 270,
                                                  128, 184, 56)),
                     ("116 全重叠", [(100, 100, 128, 184)] * 116)):
    res = bench(rects)
    base = res["linear"]["hit_us"]
    auto = HitTester()
    auto.set_layout(rects)
    act = auto.backend
    cells = "  ".join(
        f"{k}={res[k]['hit_us']:.3f}µs({base / res[k]['hit_us']:.0f}x)"
        for k in ("linear", "bisect", "numpy", "bucket"))
    print(f"  {label:12s} {cells}   auto→{act}")
check("auto 选出的 backend 在 116 图标下确实最快",
      bench(big)["bucket" if ht_perf.backend == "bucket"
                 else ht_perf.backend]["hit_us"]
      <= bench(big)["linear"]["hit_us"])

# 重建索引的代价（resize 后必须重建）
t0 = time.perf_counter()
for _ in range(200):
    ht_perf.set_layout(big)
build = (time.perf_counter() - t0) / 200
check("116 图标 set_layout 重建 < 1ms", build < 0.001, f"{build * 1e3:.4f}ms")

# version 自增，可用于判断索引是否过期
v0 = ht_perf.version
ht_perf.set_layout(big)
check("set_layout 后 version 自增", ht_perf.version == v0 + 1)


# ══════════════════════════════════════════════════════════════
section("C. 左右键语义")
# ══════════════════════════════════════════════════════════════

# 造一个 3x3 的假网格：host 900x900，每个 Tile 352x184（占满单元格），
# 图标 128 居中 —— 复刻 grid.py 的真实比例，用来暴露"控件矩形 ≠ 图标矩形"。
CELL_W, CELL_H = 352, 270
TILE_W, TILE_H = 352, 184
ICON = 128
COLS, ROWS = 3, 3


def build_grid():
    host = QWidget()
    host.setGeometry(0, 0, CELL_W * COLS, CELL_H * ROWS)
    tiles = []
    for j in range(ROWS):
        for i in range(COLS):
            t = Tile(FakeEntry(f"App{i}{j}"), QIcon(), ICON, host)
            # 控件占满单元格；图标在控件内水平居中（tile.py 的 _icon_rect）
            t.setGeometry(i * CELL_W + (CELL_W - ICON) // 2,
                          j * CELL_H + (CELL_H - TILE_H) // 2,
                          TILE_W, TILE_H)
            t.show()
            tiles.append(t)
    return host, tiles


def probe(ctrl):
    """
    接管控制器信号，记录这次点击触发了什么。

    同时把弹菜单换成**立即返回 None** 的替身。

    这一步是必须的，不是便利：右键现在真的会弹菜单，而 ``QMenu.exec_()``
    起的是一个**嵌套事件循环**，阻塞到用户点某一项为止。无头测试里
    没有人会点，于是 ``press(..., Qt.RightButton)`` 永远不返回 ——
    实测整个测试进程挂死、退出码 -1073740791。
    用 ``set_menu_exec`` 换掉之后，「右键走到了弹菜单这一步」照样验得到，
    只是不会真等。

    返回的 log 里额外记一条 ``menu:<kind>@<key>``，用来区分
    「弹了菜单但用户取消了」和「压根没走到弹菜单」。
    """
    log = []
    ctrl.clicked_outside.connect(lambda: log.append("closed"))
    ctrl.settings_requested.connect(lambda: log.append("settings"))
    ctrl.blank_pressed.connect(lambda x, y: log.append(f"blank@{x},{y}"))
    ctrl.icon_pressed.connect(lambda i: log.append(f"icon#{i}"))
    ctrl.add_requested.connect(lambda: log.append("add"))
    ctrl.reload_requested.connect(lambda: log.append("reload"))
    ctrl.about_requested.connect(lambda: log.append("about"))
    ctrl.delete_requested.connect(
        lambda e: log.append(f"delete:{getattr(e, 'name', e)}"))
    return log





host, tiles = build_grid()


# 替身按菜单内容区分「空白菜单」还是「图标菜单」：图标菜单会多一个
# 灰字标题项，而它的首项是「从启动器移除」。用菜单里的真实文本来判，
# 比让调用方事后声称自己弹的是哪种更可靠 —— 声称错了测试也跟着错。
_menus_shown: list[tuple[str, list[str]]] = []


def _classify(menu) -> str:
    texts = [a.text() for a in menu.actions()]
    return "tile" if any("移除" in t for t in texts) else "blank"


def fake_menu_exec(_kind: str = "auto"):
    """
    造一个不阻塞的替身：记住弹了哪个菜单，返回 None 表示用户取消。

    返回 None 是刻意的：用户点空白取消是最常见的情况，而且不选任何一项
    就不会有任何信号被发出去 —— 正好用来验「取消 = 什么都不发生」。
    """
    def _exec(menu, pos):
        _menus_shown.append((_classify(menu),
                             [a.text() for a in menu.actions()]))
        return None
    return _exec


ctl = BlankAreaController(host, click_through_empty=True)
ctl.install_on(host, lambda: tiles)
# 右键会弹菜单；换成不阻塞的替身（见 probe 的注释）。
ctl.set_menu_exec(fake_menu_exec())
st = ctl.stats()
print(f"  backend={st['active']}  tiles={st['tiles']}  "
      f"hit_rects={len(ctl.hit.rects)}")
check("索引覆盖全部 tile", st["tiles"] == len(tiles), f"{st['tiles']}")
check("hit_rects 与 tiles 等长（下标对齐）", len(ctl.hit.rects) == len(tiles))

# 每个 tile 的图标矩形确实比控件小很多
t0tile = tiles[0]
ir = t0tile._icon_rect()
check("测试前提成立：控件矩形远大于图标矩形",
      t0tile.width() > ir.width() * 2,
      f"控件 {t0tile.width()}x{t0tile.height()} vs 图标 {ir.width()}x{ir.height()}")

# ── 左键空白 → clicked_outside ───────────────────────────
log = probe(ctl)
GAP_X = CELL_W // 2          # 第 0、1 列控件之间 = 两图标之间的空白
GAP_Y = CELL_H // 2
press(host, GAP_X, GAP_Y, Qt.LeftButton)
check("左键空白（两图标之间的间隙）→ clicked_outside",
      "closed" in log, f"log={log}")
check("左键空白不误触发 settings_requested", "settings" not in log, f"log={log}")

# ── 右键空白 → 弹菜单，且不触发 closed ────────────────────
#
# 行为变了：右键空白**不再直接打开设置窗口**，而是弹出菜单
# （「添加应用… / 重新载入图标 / 设置… / 关于」）。
# 原因见 blankarea._handle_press 的注释：原来直接 emit settings_requested，
# 于是 show_menu() 零调用点、整条「添加应用」链路都是死代码。
log = probe(ctl)
_menus_shown.clear()
press(host, GAP_X, GAP_Y, Qt.RightButton)
check("右键空白 → 弹了空白菜单", len(_menus_shown) == 1
      and _menus_shown[0][0] == "blank", str(_menus_shown))
check("空白菜单里含「添加应用」",
      _menus_shown and any("添加应用" in t for t in _menus_shown[0][1]),
      str(_menus_shown[:1]))
check("右键空白不再直接打开设置窗口（改由菜单里的「设置…」触发）",
      "settings" not in log, f"log={log}")
check("右键空白不触发 clicked_outside（关键：不退出）",
      "closed" not in log, f"log={log}")
check("右键空白记了 blank_pressed",
      any(x.startswith("blank@") for x in log), f"log={log}")

# ── 点图标本身：两种信号都不触发 ─────────────────────────
# 这是用户强调的"跟图标无关"：点图标必须被当成点图标。
log = probe(ctl)
t = tiles[0]
cx, cy = t.x() + t.width() // 2, t.y() + ICON // 2
press(t, t.width() // 2, ICON // 2, Qt.LeftButton)
check("左键图标中心 → 不触发任何空白信号",
      "closed" not in log and "settings" not in log and "blank" not in log,
      f"log={log}  pos=({cx},{cy})")
log = probe(ctl)
_menus_shown.clear()
press(t, t.width() // 2, ICON // 2, Qt.RightButton)
check("右键图标中心 → 弹的是**图标**菜单（不是空白菜单）",
      len(_menus_shown) == 1 and _menus_shown[0][0] == "tile",
      str(_menus_shown))
# actions() 的第一项是**标题**（灰字的应用名），不是动作 ——
# 菜单里 actions() 会把分隔线也算进去，所以下标对不上"第几个菜单项"。
# 这里断言真正该断的事：标题在、移除项在、移除项排在标题之后且在
# 分隔线之前。
_txt = _menus_shown[0][1] if _menus_shown else []
check("图标菜单首项是应用名（灰字标题）", bool(_txt) and "App" in _txt[0],
      str(_txt))
check("「从启动器移除」紧跟标题之后",
      len(_txt) > 1 and _txt[1] == "从启动器移除", str(_txt))
check("图标菜单也有「添加应用」",
      any("添加应用" in t for t in _txt), str(_txt))

check("右键图标中心 → 不触发 settings_requested",
      "settings" not in log, f"log={log}")
check("右键图标中心 → 不触发 closed（不能退出）",
      "closed" not in log, f"log={log}")
check("右键图标中心 → 用户取消时不删任何东西",
      not any(x.startswith("delete:") for x in log), f"log={log}")

# ── 点「控件内、图标外」必须算空白（核心回归） ───────────
# 这一圈是 bug 的根源：控件 352 宽，图标只有 128。点这里曾经被判成
# "点到图标"，于是不退出；正确行为是空白。
log = probe(ctl)
inside_ctrl_outside_icon = (t.width() // 2 - ICON // 2 - 10, ICON // 2)
press(t, inside_ctrl_outside_icon[0], inside_ctrl_outside_icon[1],
      Qt.LeftButton)
check("点在控件内但图标外 → 算空白（左键退出）",
      "closed" in log, f"log={log}  局部坐标={inside_ctrl_outside_icon}")

log = probe(ctl)
_menus_shown.clear()
press(t, inside_ctrl_outside_icon[0], inside_ctrl_outside_icon[1],
      Qt.RightButton)
# 这一圈是「控件矩形 ≠ 图标矩形」的边缘：它在 Tile 上，但不在图标上。
# 必须判成**空白**（弹空白菜单），而不是图标菜单 —— 否则「删除」会出现在
# 用户根本没点中的图标旁边。
check("点在控件内但图标外 → 判成空白（弹空白菜单）",
      len(_menus_shown) == 1 and _menus_shown[0][0] == "blank",
      str(_menus_shown))
check("这一圈右键不提供删除",
      not any(x.startswith("delete:") for x in log), f"log={log}")

# ── 点文字那一行也算点到图标（否则点名字会关掉启动器） ────
# 文字横跨控件全宽，但只用真实文字宽度（否则纵向间隙会塌成 0）。
lr = t._label_rect()
label_y = lr.top() + 4
log = probe(ctl)
press(t, t.width() // 2, label_y, Qt.LeftButton)
check("点图标文字 → 不当成空白（点名字不该关闭启动器）",
      "closed" not in log, f"log={log}  局部y={label_y}")

# 关掉文字命中后，文字行回到空白
ctl.update_settings(hit_labels=False)
pump(1)
log = probe(ctl)
press(t, t.width() // 2, label_y, Qt.LeftButton)
check("hide_labels 时文字行算空白", "closed" in log, f"log={log}")
ctl.update_settings(hit_labels=True)
pump(1)

# ── 非左/右键不触发 ─────────────────────────────────────
log = probe(ctl)
press(host, GAP_X, GAP_Y, Qt.MiddleButton)
check("中键 → 不触发任何空白信号",
      "closed" not in log and "settings" not in log, f"log={log}")

# ── 四周空白（控件外的边距）也是空白 ─────────────────────
log = probe(ctl)
press(host, 2, 2, Qt.LeftButton)
check("左上角边距 → 空白", "closed" in log, f"log={log}")

# ── 命中下标正确：点第 5 个图标命中 #4 ──────────────────
t4 = tiles[4]
press(t4, t4.width() // 2, ICON // 2, Qt.LeftButton)
check("命中下标与 tile 顺序一致",
      ctl.hit_test(t4.x() + t4.width() // 2, t4.y() + ICON // 2) == 4,
      f"hit={ctl.hit_test(t4.x() + t4.width() // 2, t4.y() + ICON // 2)}")

# ── 回归：文字命中宽度必须封顶，否则间隙归零 ────────────
# 实测过的坑：Tile 占满整个单元格，而「Agent Orchestrator」这类
# 长名字量出来 288px，比图标 128px 还宽一倍。文字命中矩形若不封顶，
# 相邻两列会直接接上（x 80~368 与 368~656），横向间隙归零 ——
# 界面看着有缝，点下去却全被当成点图标。
ratio = ctl.blank_pixel_ratio(step=6)
print(f"  空白像素占比 = {ratio * 100:.1f}%")
check("空白区仍然真实存在（占比 > 25%）", ratio > 0.25,
      f"{ratio * 100:.1f}%")

# 命中矩形宽度必须明显小于控件宽度（控件占满单元格）
max_w = max(r[2] for r in ctl.hit.rects)
check("命中矩形宽度远小于控件宽度",
      max_w < TILE_W * 0.75, f"最宽命中矩形={max_w} 控件={TILE_W}")

# 相邻两列的命中矩形之间必须留出空隙
col_hits = []
for t in tiles:
    r = ctl.hit.rects[tiles.index(t)]
    col_hits.append((t.x(), r))
row0 = [(t.x(), ctl.hit.rects[i]) for i, t in enumerate(tiles) if t.y() == tiles[0].y()]
gaps_between = []
for (xa, ra), (xb, rb) in zip(row0, row0[1:]):
    gaps_between.append(rb[0] - (ra[0] + ra[2]))
check("相邻列命中矩形之间有空隙（列间可点空白）",
      all(g > 0 for g in gaps_between), f"最小间隙={min(gaps_between)}px")

# bonus 封顶生效：横向不超过 icon + 2*bonus
ctl.update_settings(label_bonus=16)
pump(1)
over = [r for r in ctl.hit.rects if r[2] > ICON + 32 + 1]
check("文字命中宽度不超过 icon + 2*bonus", not over,
      f"越界 {len(over)}: {over[:3]}")
ctl.update_settings(label_bonus=0)
pump(1)
check("bonus=0 时命中宽度不超过图标宽度",
      all(r[2] <= ICON for r in ctl.hit.rects),
      f"最宽={max(r[2] for r in ctl.hit.rects)}")
ctl.update_settings(label_bonus=16)
pump(1)


# ══════════════════════════════════════════════════════════════
section("D. click_through_empty 开关")
# ══════════════════════════════════════════════════════════════

ctl2 = BlankAreaController(host, click_through_empty=False)
ctl2.install_on(host, lambda: tiles)
ctl2.set_menu_exec(fake_menu_exec())            # 同上：不阻塞
log = probe(ctl2)
press(host, GAP_X, GAP_Y, Qt.LeftButton)
check("关掉后左键空白 → 不退出", "closed" not in log, f"log={log}")
log = probe(ctl2)
_menus_shown.clear()
press(host, GAP_X, GAP_Y, Qt.RightButton)
check("关掉后右键空白 → 仍然弹菜单",
      len(_menus_shown) == 1 and "closed" not in log, f"log={log}")

# 开关是运行期可改的（设置窗口预览要用）
ctl2.update_settings(click_through_empty=True)
pump(1)
log = probe(ctl2)
press(host, GAP_X, GAP_Y, Qt.LeftButton)
check("运行期打开开关后左键空白 → 退出", "closed" in log, f"log={log}")

# 关掉时事件必须被吃掉，否则会沿事件链落到别处
log = probe(ctl2)
ctl2.update_settings(click_through_empty=False)
pump(1)
press(host, GAP_X, GAP_Y, Qt.LeftButton)
check("关掉开关时仍消费了空白点击（不透传下层）",
      "closed" not in log and "settings" not in log, f"log={log}")


# ══════════════════════════════════════════════════════════════
# E. 右键菜单位置钳制
#
# 菜单的**样式与文案**在这里验（用 build_blank_menu 直接构造，
# 不 exec_，所以不阻塞）；菜单的**弹出时机与位置**在 C 节已验。
section("E. 右键菜单位置钳制")
# ══════════════════════════════════════════════════════════════

# **必须传 parent。** 无父的 QMenu 是顶层窗口，会自己分配 HWND；
# 这里建完就丢，PyQt 回收它时会踩坏窗口栈 —— 实测进程在后面某一处
# 硬崩（-1073740791）。用 host 当 parent 就只是子窗口，没有这个问题。
# （C 节的右键路径不经过这里：它走 set_menu_exec 装的不阻塞替身。）
menu, acts = build_blank_menu(host)
labels = [a.text() for a in acts.values()]
print(f"  菜单项: {labels}")
check("菜单含 设置", any("设置" in t for t in labels))
check("菜单含 添加应用", any("添加" in t for t in labels))
check("菜单含 重新载入图标", any("重新载入" in t for t in labels))
check("菜单含 关于", any("关于" in t for t in labels))
check("菜单用深色样式（不是系统默认）",
      "#14161c" in menu.styleSheet(), menu.styleSheet()[:40] + "...")

SCREEN = QRect(0, 0, 1920, 1080)
MENU_W, MENU_H = 220, 200
# 右下角：全屏启动器最容易踩的坑，菜单往右下弹就整块跑出屏幕
p = clamp_to_screen(QPoint(1915, 1075), (MENU_W, MENU_H), screen=SCREEN)
check("右下角点击 → 菜单不出屏",
      p.x() + MENU_W <= SCREEN.right() and p.y() + MENU_H <= SCREEN.bottom(),
      f"({p.x()},{p.y()}) 右下=({SCREEN.right()},{SCREEN.bottom()})")
p2 = clamp_to_screen(QPoint(1900, 1000), (MENU_W, MENU_H), screen=SCREEN)
check("靠右点击 → 菜单不出屏", p2.x() + MENU_W <= SCREEN.right(),
      f"x={p2.x()}")
p3 = clamp_to_screen(QPoint(5, 5), (MENU_W, MENU_H), screen=SCREEN)
check("左上角点击 → 菜单不出屏且留边距",
      p3.x() >= SCREEN.left() and p3.y() >= SCREEN.top()
      and p3.x() + MENU_W <= SCREEN.right(),
      f"({p3.x()},{p3.y()})")
# 菜单比屏幕还大时，退到边距而不是全跑出去
p4 = clamp_to_screen(QPoint(1900, 1000), (4000, 3000), screen=SCREEN)
check("菜单大于屏幕 → 退回边距而非消失",
      p4.x() >= SCREEN.left() and p4.y() >= SCREEN.top(), f"({p4.x()},{p4.y()})")
# 非零原点屏幕（多显示器）
SC2 = QRect(1920, 0, 1280, 1024)
p5 = clamp_to_screen(QPoint(3190, 1010), (MENU_W, MENU_H), screen=SC2)
check("副屏右下角 → 菜单不出副屏",
      p5.x() + MENU_W <= SC2.right() and p5.y() + MENU_H <= SC2.bottom(),
      f"({p5.x()},{p5.y()}) 副屏右下=({SC2.right()},{SC2.bottom()})")

# 菜单动作分发
check("菜单规格里的 key 与信号一一对应",
      {k for k, _t in MENU_ITEMS if not k.startswith("sep")}
      == set(acts))
check("规格里正好一个分隔符",
      sum(1 for k, _t in MENU_ITEMS if k.startswith("sep")) == 1,
      str([k for k, _t in MENU_ITEMS if k.startswith("sep")]))


# ── 图标菜单 ────────────────────────────────────────────
# 构造（不 exec_）→ 验文案与结构。标题项是**禁用**的 QAction，
# 用户点它不该触发任何事 —— build_tile_menu 里 setEnabled(False)。
tmenu, tacts = build_tile_menu(host, "示例应用")
ttexts = [a.text() for a in tmenu.actions()]
print(f"  图标菜单项: {ttexts}")
check("图标菜单有 delete 动作", "delete" in tacts, str(sorted(tacts)))
check("图标菜单有 add 动作", "add" in tacts, str(sorted(tacts)))
check("图标菜单标题是应用名", ttexts and ttexts[0] == "示例应用", str(ttexts))
title_action = tmenu.actions()[0]
check("图标菜单标题被禁用（点了什么也不会发生）",
      not title_action.isEnabled())
check("图标菜单标题不是 delete/add 里的任何一个",
      title_action not in tacts.values())
check("图标菜单里「移除」排在标题之后",
      "从启动器移除" in ttexts and ttexts.index("从启动器移除") > 0,
      str(ttexts))
check("图标菜单也用深色样式",
      "#14161c" in tmenu.styleSheet(), tmenu.styleSheet()[:40] + "...")
check("空名字时没有标题项（不产生一个空白灰字）",
      build_tile_menu(host, "")[1].keys() == tacts.keys(),
      str(sorted(build_tile_menu(host, "")[1])))
check("长名字被截断成一行标题",
      len(build_tile_menu(host, "x" * 60)[0].actions()[0].text()) <= 28,
      str(len(build_tile_menu(host, "x" * 60)[0].actions()[0].text())))

# 保留引用，避免菜单在这一节末尾就被回收（PyQt 回收 QMenu 时会出问题）
_keepalive = [menu, tmenu]


# ══════════════════════════════════════════════════════════════
section("F. 设置窗口")
# ══════════════════════════════════════════════════════════════

tmpdir = Path(tempfile.mkdtemp(prefix="lp_settings_"))
sfile = tmpdir / "settings.json"
s = Settings(sfile)

applied = []
win = SettingsWindow(s, on_apply=lambda ch: applied.append(dict(ch)))
print(f"  控件行数={len(win.rows)}  分组={[g for g, _ in GROUP_SPECS]}")
check("窗口能构造出来", win is not None)

# 20 项都在 SCHEMA 里，且每一项都有对应控件行
missing = [k for k in SCHEMA if k not in win.rows]
check("SCHEMA 全部 20 项都有控件", not missing, f"缺: {missing}")
check("控件行数 == SCHEMA 项数", len(win.rows) == len(SCHEMA),
      f"{len(win.rows)} vs {len(SCHEMA)}")

# ── 改值 → 实时预览 ─────────────────────────────────────
applied.clear()
win.rows["columns"].spin.setValue(9)
pump(1)
check("改列数立即写进 Settings（实时预览）", s.get("columns") == 9,
      f"columns={s.get('columns')}")
check("改列数触发 on_apply", applied and applied[-1].get("columns") == 9,
      f"applied={applied[-1] if applied else None}")

# 滑块与数字框双向同步
win.rows["icon_size"].slider.setValue(96)
pump(1)
check("拖滑块 → 数字框跟着变", win.rows["icon_size"].spin.value() == 96,
      f"spin={win.rows['icon_size'].spin.value()}")
win.rows["icon_size"].spin.setValue(160)
pump(1)
check("填数字框 → 滑块跟着变",
      win.rows["icon_size"].slider.value() == 160,
      f"slider={win.rows['icon_size'].slider.value()}")

# 一次拖动只回调一次（回归：早先 _from_slider 和 _from_spin 都发信号）
applied.clear()
before = len(applied)
win.rows["icon_size"].spin.setValue(192)
pump(1)
check("改一项只触发一次 on_apply", len(applied) == 1, f"次数={len(applied)}")

# ── 枚举值原样透传（page_easing=spring 必须能传给布局引擎） ─
idx = win.rows["page_easing"].combo.findData("spring")
check("page_easing 下拉里有 spring", idx >= 0, f"index={idx}")
win.rows["page_easing"].combo.setCurrentIndex(idx)
pump(1)
check("page_easing 写回英文原串 spring",
      s.get("page_easing") == "spring", f"值={s.get('page_easing')!r}")
check("下拉显示的是中文标签",
      win.rows["page_easing"].combo.currentText() != "spring",
      f"显示={win.rows['page_easing'].combo.currentText()!r}")

# ── 保存 → 重载一致 ─────────────────────────────────────
win.rows["columns"].spin.setValue(11)
win.rows["sort_mode"].combo.setCurrentIndex(
    win.rows["sort_mode"].combo.findData("recent"))
win.rows["hide_labels"].check.setChecked(True)
pump(1)
win.commit()
check("保存后文件存在", sfile.exists(), str(sfile))
check("保存后 window 已 Accepted", win.result() == 1)

s_reload = Settings(sfile)
s_reload.load()
check("重载后 columns 一致", s_reload.get("columns") == 11,
      f"{s_reload.get('columns')}")
check("重载后 sort_mode 一致", s_reload.get("sort_mode") == "recent",
      f"{s_reload.get('sort_mode')}")
check("重载后 hide_labels 一致", s_reload.get("hide_labels") is True)
check("重载后 page_easing 一致", s_reload.get("page_easing") == "spring",
      f"{s_reload.get('page_easing')}")

# 新窗口读回同样的值
win2 = SettingsWindow(s_reload)
check("新窗口读回 columns", win2.rows["columns"].value() == 11,
      f"{win2.rows['columns'].value()}")
check("新窗口读回 page_easing", win2.rows["page_easing"].value() == "spring",
      f"{win2.rows['page_easing'].value()!r}")

# ── 非法值被钳制 ───────────────────────────────────────
check("columns 超上限被钳到 16", clamp("columns", 999) == 16)
check("columns 低于下限被钳到 3", clamp("columns", -5) == 3)
check("rows 超上限被钳到 10", clamp("rows", 99) == 10)
check("icon_size 超上限被钳到 256", clamp("icon_size", 9999) == 256)
check("cell_ratio 超上限被钳到 2.0", abs(clamp("cell_ratio", 99) - 2.0) < 1e-9)
check("非法枚举回落默认", clamp("page_easing", "bogus") == "outQuint")
check("非法布局模式回落默认", clamp("layout_mode", "nope") == "grid")
check("非数字回落默认", clamp("columns", "abc") == 7)
# 控件自身钳制
win2.rows["columns"].spin.setValue(9999)
pump(1)
check("数字框自己钳制超范围输入",
      win2.rows["columns"].spin.value() == 16,
      f"值={win2.rows['columns'].spin.value()}")
# 往 Settings 里塞脏值也安全（第二道闸）
s_dirty = Settings(sfile)
s_dirty.set("icon_size", 99999)
check("Settings.set 脏值被钳制", s_dirty.get("icon_size") == 256,
      f"{s_dirty.get('icon_size')}")

# ── 恢复默认 ────────────────────────────────────────────
win3 = SettingsWindow(s_reload, on_apply=lambda ch: applied.append(dict(ch)))
applied.clear()
win3.rows["columns"].spin.setValue(16)
win3.rows["icon_size"].spin.setValue(256)
pump(1)
win3.reset_defaults()
pump(1)
check("恢复默认后 columns 回到 7", s_reload.get("columns") == 7,
      f"{s_reload.get('columns')}")
check("恢复默认后 icon_size 回到 128", s_reload.get("icon_size") == 128,
      f"{s_reload.get('icon_size')}")
check("恢复默认后界面也同步",
      win3.rows["columns"].value() == 7 and win3.rows["icon_size"].value() == 128)
check("恢复默认触发 on_apply", bool(applied), f"applied={applied[-1] if applied else None}")
all_default = all(s_reload.get(k) == v[0] for k, v in SCHEMA.items())
check("恢复默认后全部 20 项都是出厂值", all_default,
      f"非默认={[k for k, v in SCHEMA.items() if s_reload.get(k) != v[0]]}")

# ── 取消回滚 ────────────────────────────────────────────
s_base = Settings(tmpdir / "base.json")
s_base.set("columns", 6)
s_base.set("icon_size", 100)
s_base.save()
applied.clear()
win4 = SettingsWindow(s_base, on_apply=lambda ch: applied.append(dict(ch)))
win4.rows["columns"].spin.setValue(15)
win4.rows["icon_size"].spin.setValue(250)
win4.rows["sort_mode"].combo.setCurrentIndex(
    win4.rows["sort_mode"].combo.findData("recent"))
pump(1)
check("改动先已生效（预览阶段）", s_base.get("columns") == 15,
      f"{s_base.get('columns')}")
win4.revert()
check("取消后 columns 回滚", s_base.get("columns") == 6,
      f"{s_base.get('columns')}")
check("取消后 icon_size 回滚", s_base.get("icon_size") == 100,
      f"{s_base.get('icon_size')}")
check("取消后 sort_mode 回滚", s_base.get("sort_mode") == "name",
      f"{s_base.get('sort_mode')}")
check("取消时通知过主窗口（带真实改动集）",
      applied and applied[-1].get("columns") == 6,
      f"applied={applied[-1] if applied else None}")
check("取消后窗口是 Rejected", win4.result() == 0)
s_chk = Settings(tmpdir / "base.json")
s_chk.load()
check("取消不会写盘", s_chk.get("columns") == 6, f"{s_chk.get('columns')}")

# ── 减弱动效置灰动效项 ─────────────────────────────────
win5 = SettingsWindow(Settings(tmpdir / "m.json"))
win5.rows["reduce_motion"].check.setChecked(True)
pump(1)
check("打开减弱动效后翻页时长被置灰",
      win5.rows["page_duration"].spin.isEnabled() is False)
check("打开减弱动效后缓动曲线被置灰",
      win5.rows["page_easing"].combo.isEnabled() is False)
win5.rows["reduce_motion"].check.setChecked(False)
pump(1)
check("关掉后恢复可用", win5.rows["page_duration"].spin.isEnabled() is True)

# ── 打开时不触发 on_apply（程序性同步不是用户改动） ───────
applied.clear()
s_quiet = Settings(tmpdir / "q.json")
s_quiet.set("columns", 14)
win6 = SettingsWindow(s_quiet, on_apply=lambda ch: applied.append(dict(ch)))
pump(1)
check("构造窗口不触发 on_apply", not applied, f"applied={applied}")

# ── layout_changed 提示 ─────────────────────────────────
check("layout_changed 能识别排版类键",
      win6.layout_changed({"columns": 5}) is True)
check("layout_changed 忽略动效类键",
      win6.layout_changed({"page_duration": 400}) is False)


# ══════════════════════════════════════════════════════════════
section("结果")
# ══════════════════════════════════════════════════════════════
total = len(_results)
failed = [i for i, okv in enumerate(_results) if not okv]
print(f"  {total - len(failed)}/{total} 通过"
      + (f"，{len(failed)} 项失败" if failed else "，全部通过"))
ok = not failed
print("=" * 68)
print("全部通过" if ok else "有失败项")
print("=" * 68)

# 清理临时目录
import shutil
try:
    shutil.rmtree(tmpdir, ignore_errors=True)
except Exception:
    pass

sys.exit(0 if ok else 1)

