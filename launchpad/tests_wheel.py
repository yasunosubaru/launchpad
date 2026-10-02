# -*- coding: utf-8 -*-
"""
launchpad/tests_wheel.py —— 滚轮翻页回归。

跑法：
    $env:QT_QPA_PLATFORM="offscreen"
    python -u -m launchpad.tests_wheel

退出码非 0 表示有项不达标。

## 这套测试盯住的三个真 bug

用户报「用鼠标滚轮用力下滑，直接就卡了」→「好家伙直接卡的划不动了」。
查下来是**三个独立缺陷叠在一起**，每一个单独都足以让滚轮不可用：

### ① 滚轮落在图标上完全无效（这才是「划不动」）

Qt 5 **不把未处理的滚轮事件沿父链传播** —— 只投递给光标下的那个控件。
`Tile` 没实现 `wheelEvent`，事件到它就被默认实现 ignore 掉了。

而 Launchpad 整屏几乎都被图标铺满。实测 4 个抽样点（页面正中、图标之间、
顶部、底部）有 **3 个**落在 Tile 上。也就是说**用户最常见的滚轮位置
恰好是唯一失效的位置**。修法是 `Tile.wheelEvent` 显式转交父控件。

### ② 搜索框/页码点里藏着第二套滚轮实现，且用的是滞后页码

`Launchpad.eventFilter` 原本自己调 `self.grid.goto(self.grid.page ± 1)`。
`_page` 要等动画**结束**才更新，所以用力下滑时 20 次滚轮全部读到同一个
旧页码，全部朝同一页冲：滑一整屏只翻 1 页就停住 —— 最早的「卡」。
现在转交给 `Grid.wheelEvent`，全应用只有一套逻辑。

### ③ 绝不能按 angleDelta 的绝对值累加

曾经写死「攒够 120（Windows 经典 WHEEL_DELTA）翻一页」。但**一格的
angleDelta 因鼠标而异**：经典滚轮 120、高精度/自由滚轮可能只发 1~60、
某些设备发 240/480。按 120 累加，高精度轮要划十几下才翻一页 —— 这恰好
又把用户推进了「划不动」。所以 angleDelta **只看符号**，一个事件一页。
触控板的精细滚动走 pixelDelta（angleDelta 为 0），那边才用像素累积。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEventLoop, QTimer, QPoint, QPointF, Qt   # noqa: E402
from PyQt5.QtGui import QWheelEvent                                  # noqa: E402
from PyQt5.QtWidgets import QApplication                              # noqa: E402

_APP = QApplication(sys.argv)

from launchpad.grid import Grid            # noqa: E402
from launchpad.library import Library     # noqa: E402
from launchpad.settings import Settings   # noqa: E402
from launchpad.tile import Tile           # noqa: E402
from launchpad.window import Launchpad    # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  OK   {label}  {detail}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}  {detail}")


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def make_wheel(target, angle=120, pixel=(0, 0)):
    """构造滚轮事件。

    注意 QWheelEvent 的参数顺序是
        (pos, globalPos, pixelDelta, angleDelta, buttons, modifiers, phase, inverted)
    **pixelDelta 在 angleDelta 前面**。写反的话 angle 恒为 0，
    事件会掉进 pixelDelta 分支，看起来就像「滚轮完全没反应」——
    这个坑我自己踩过一次。
    """
    p = target.mapToGlobal(target.rect().center())
    return QWheelEvent(
        QPointF(p), QPointF(p),
        QPoint(pixel[0], pixel[1]),        # pixelDelta
        QPoint(0, angle),                  # angleDelta
        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)


def send_wheel(target, angle=120, pixel=(0, 0)) -> None:
    _APP.sendEvent(target, make_wheel(target, angle, pixel))


# ── 搭环境 ────────────────────────────────────────────────
_st = Settings()
_st.load()
_lib = Library()
_lib.load()
win = Launchpad(_lib, _st.get("columns"), _st.get("rows"), settings=_st)
win.setGeometry(0, 0, 2560, 1600)
win.showFullScreen()
pump(2200)

g = win.grid
PAGES = g.page_count
N_TILES = len(g._tiles)

print("=" * 72)
print(f"【环境】页数={PAGES}  图标数={N_TILES}  "
      f"grid={g.geometry().getRect()}  window={win.geometry().getRect()}")
print("=" * 72)


def at_last_page() -> None:
    """回到末页，并把滚轮手势状态清干净。

    手势节流靠两个字段：`_wheel_gesture`（本次手势已消费的方向）和
    `_wheel_last_ms`（上一个事件的时间戳）。它们由 wheelEvent 惰性维护，
    **goto() 不会碰** —— 所以跨用例时会残留：上一组留下的
    `_wheel_gesture` 非 0，下一组的头几个事件就被当成同一次手势而跳过，
    于是「3 个密集事件」只翻了 0 页。

    症状是偶发且看起来像产品 bug（同一份代码跑两次结果不同），
    实测只有把 WHEEL_GESTURE_MS 调小才必现。修法在这里清干净。
    """
    g.goto(PAGES - 1, animate=False)
    pump(350)
    g._wheel_gesture = 0
    g._wheel_last_ms = 0.0


print()
print("=" * 72)
print("【1】滚轮的目标是控件，光标下是什么控件就必须生效 —— bug ①")
print("=" * 72)
# 先确认「整屏几乎都是 Tile」这个前提，否则下面的测试没有说服力。
# 注意：抽样点必须按 grid 的**实际几何**算，不能写死 2560×1600 的坐标 ——
# offscreen 平台下窗口是 800×600，写死坐标会全部落在 None 上。
gr = g.rect()
pts = [("页面正中", (gr.width() // 2, gr.height() // 2)),
       ("左上四分之一", (gr.width() // 4, gr.height() // 4)),
       ("右上网格", (gr.width() * 3 // 4, gr.height() // 4)),
       ("左下网格", (gr.width() // 4, gr.height() * 3 // 4)),
       ("右下网格", (gr.width() * 3 // 4, gr.height() * 3 // 4)),
       ("中下方", (gr.width() // 2, gr.height() * 4 // 5)),
       ("中上方", (gr.width() // 2, gr.height() // 5))]
hits = []
for label, pt in pts:
    w = g.childAt(pt[0], pt[1])
    hits.append(type(w).__name__ if w is not None else "None")
n_tile = sum(1 for h in hits if h == "Tile")
print(f"  grid={gr.getRect()}  命中抽样：{hits}")
check(n_tile >= len(pts) - 2,
      "整屏大部分区域落在 Tile 上（这正是 bug 的土壤）",
      f"{n_tile}/{len(hits)} 个抽样点")

for name, target in (("Grid（图标之间的空隙）", g),
                     ("Tile（图标本身）", g._tiles[0]),
                     ("搜索框", win.search),
                     ("页码点", win.dots)):
    at_last_page()
    before = g.page
    send_wheel(target, angle=120)
    pump(650)
    check(g.page == before - 1,
          f"滚轮发给 {name} 能翻页",
          f"第 {before + 1} 页 -> 第 {g.page + 1} 页")

print()
print("=" * 72)
print("【2】搜索框/页码点 与 图标 必须行为一致 —— bug ②")
print("=" * 72)
# 同一串滚轮，分别打在图标上和搜索框上，落点必须完全相同
results = {}
for name, target in (("图标 Tile", g._tiles[0]), ("搜索框", win.search)):
    at_last_page()
    for _ in range(3):
        send_wheel(target, angle=120)
        pump(10)
    pump(700)
    results[name] = g.page
check(results["图标 Tile"] == results["搜索框"],
      "同一个滚轮序列在图标区和搜索框区落点一致",
      f"图标->第 {results['图标 Tile'] + 1} 页, "
      f"搜索框->第 {results['搜索框'] + 1} 页")
# 3 个事件间隔 10ms < WHEEL_GESTURE_MS(220)，属**同一次手势** -> 只翻 1 页。
# 这正是用户要的「轻轻一划 = 一页」（见本文件 docstring）。
check(results["图标 Tile"] == max(0, PAGES - 1 - 1),
      "3 个密集事件 = 同一次手势 = 1 页（不是翻 3 页）",
      f"期望第 {max(0, PAGES - 1 - 1) + 1} 页，"
      f"WHEEL_GESTURE_MS={Grid.WHEEL_GESTURE_MS}")

# 间隔超过阈值 -> 各自算一次手势 -> 翻 3 页（不能变成「划不动」）
spaced = {}
for name, target in (("图标 Tile", g._tiles[0]), ("搜索框", win.search)):
    at_last_page()
    for _ in range(3):
        send_wheel(target, angle=120)
        pump(Grid.WHEEL_GESTURE_MS + 120)     # 静默够久
    pump(700)
    spaced[name] = g.page
check(spaced["图标 Tile"] == max(0, PAGES - 1 - 3),
      "3 次「滚一下 + 停手」= 3 页（节流没有吞掉连续划动）",
      f"图标->第 {spaced['图标 Tile'] + 1} 页，"
      f"期望第 {max(0, PAGES - 1 - 3) + 1} 页")
check(spaced["搜索框"] == spaced["图标 Tile"],
      "间隔划动时搜索框区与图标区落点仍一致",
      f"搜索框->第 {spaced['搜索框'] + 1} 页")

print()
print("=" * 72)
print("【3】一格的 angleDelta 因鼠标而异，任何量级都必须能翻页 —— bug ③")
print("=" * 72)
for delta in (120, 240, 480, 60, 15, 5, 1):
    at_last_page()
    for _ in range(3):
        send_wheel(g._tiles[0], angle=delta)
        pump(10)
    pump(650)
    # 3 个密集事件 = 一次手势 = 1 页，与 delta 量级无关。
    # 正 delta 是「往前翻」，从末页退一页。
    check(g.page == PAGES - 2,
          f"angleDelta={delta} 在图标上划一下（3 个密集事件）",
          f"-> 第 {g.page + 1} 页（期望第 {PAGES - 1} 页）")

# 反向（往后翻）要从**首页**出发，方向断言才有意义。
# 之前写在同一个循环里、从末页起测 —— 末页本来就没有「下一页」，
# 断言却按第 3 页算，于是必然失败。这是测试写错，不是产品行为。
for delta in (-120, -60, -1):
    g.goto(0, animate=False)
    pump(400)
    for _ in range(3):
        send_wheel(g._tiles[0], angle=delta)
        pump(10)
    pump(650)
    check(g.page == 1,
          f"angleDelta={delta} 反向在图标上划一下",
          f"-> 第 {g.page + 1} 页（期望第 2 页）")

print()
print("=" * 72)
print("【4】用力猛滑 20 格 —— 原始场景")
print("=" * 72)
# 20 个事件间隔 8ms，全程属**一次手势** -> 只翻 1 页，且单帧不卡。
# 修复前这里是「翻 20 页到头」，而那正是用户报的「一滑到底、中间全跳过」。
for delta in (120, 15, 480):
    at_last_page()
    worst_ms = 0.0
    for i in range(20):
        t0 = time.perf_counter()
        send_wheel(g._tiles[0], angle=delta)
        worst_ms = max(worst_ms, (time.perf_counter() - t0) * 1000)
        pump(8)
    pump(900)
    check(g.page == PAGES - 2,
          f"angleDelta={delta} 猛滑 20 格 = 一次手势 = 1 页",
          f"-> 第 {g.page + 1} 页（期望第 {PAGES - 1} 页），"
          f"单次处理最慢 {worst_ms:.2f}ms")
    check(worst_ms < 5.0,
          f"angleDelta={delta} 单次滚轮处理无卡顿",
          f"最慢 {worst_ms:.2f}ms")

# 猛滑时每一格都必须基于**最新意图**继续往前，而不是都读同一个滞后页码。
# 这是最早那个「用力下滑直接卡住」的核心断言。
# goto() 内部会把越界的 index 钳到 [0, PAGES-1]，所以 spy 记录的是
# 传入的原始值：到头之后它会一直是负数，这是正常的，不该算失败。
at_last_page()
targets = []
_orig_goto = g.goto


def spy_goto(index, animate=True):
    targets.append(index)
    return _orig_goto(index, animate)


g.goto = spy_goto
for _ in range(6):
    send_wheel(g._tiles[0], angle=120)
    pump(8)
g.goto = _orig_goto
pump(700)
# 6 个密集事件 = 一次手势 -> 只应触发**一次** goto。
# 修复前是 6 次（每次都基于 _page_target 继续往前，于是「一滑到底」）。
check(len(targets) == 1 and targets[0] == PAGES - 2,
      "6 个密集事件只触发一次翻页（一次手势一页）",
      f"goto 调用 {len(targets)} 次，目标序列={targets}")

# 间隔超过阈值时，每次都要基于**最新意图**继续，而不是读滞后页码。
# 注意必须装一个**新的** spy：上一次的 spy_goto 闭包捕获的是 targets，
# 直接复用会让第二次的记录仍然写进 targets，targets2 永远是空的
# （这个坑踩过一次：断言看着像「意图没记账」，其实是探针坏了）。
targets2 = []


def spy_goto2(index, animate=True):
    targets2.append(index)
    return _orig_goto(index, animate)


g.goto = spy_goto2
for _ in range(3):
    send_wheel(g._tiles[0], angle=120)
    pump(Grid.WHEEL_GESTURE_MS + 150)
g.goto = _orig_goto
pump(700)
# 从末页往前翻 3 次：目标必须**每次都基于最新意图继续递减**，
# 即 [PAGES-2, PAGES-3, PAGES-4]；越界后 goto() 内部钳到 0，
# 所以传入值可以继续往下走负数 —— 那是正确的钳位行为，不是 bug。
# 之前这里额外要求「全都 >= 0」，于是第三次（已翻出首页）必然失败。
strict_desc = all(targets2[i] > targets2[i + 1]
                  for i in range(len(targets2) - 1))
check(strict_desc and len(set(targets2)) == len(targets2) == 3,
      "间隔划动时每格目标都不同（意图立刻记账）",
      f"目标序列={targets2}（越界后由 goto 内部钳位，"
      f"起点第 {PAGES} 页）")

print()
print("=" * 72)
print("【5】触控板精细滚动：angleDelta=0，只有 pixelDelta")
print("=" * 72)
step = max(20, g._page_height() // 4)
at_last_page()
for _ in range(200):
    send_wheel(g._tiles[0], angle=0, pixel=(0, 10))
    pump(3)
pump(800)
check(g.page == 0,
      "pixelDelta 累计 2000px 能翻到头",
      f"-> 第 {g.page + 1} 页（阈值={step}px）")

at_last_page()
for _ in range(200):
    send_wheel(g._tiles[0], angle=0, pixel=(0, 10))
    pump(3)
pump(800)
# pixel > 0 与 angle > 0 是**同一个手势方向**（Qt 约定：正值 = 往上滚），
# 所以必须翻到同一侧。修复前 turns 继承了 pixel 的符号，符号被算两次，
# 导致触控板往上一页、鼠标往下一页。
check(g.page == 0,
      "pixelDelta 正向与滚轮正向同向（都翻回上一页）",
      f"-> 第 {g.page + 1} 页")

# 反向也必须与滚轮反向一致
g.goto(0, animate=False)
pump(400)
for _ in range(200):
    send_wheel(g._tiles[0], angle=0, pixel=(0, -10))
    pump(3)
pump(800)
check(g.page == PAGES - 1,
      "pixelDelta 反向与滚轮反向同向（都翻到下一页）",
      f"-> 第 {g.page + 1} 页")

# 少量像素不该触发翻页（否则触控板轻碰就跳页）
at_last_page()
for _ in range(3):
    send_wheel(g._tiles[0], angle=0, pixel=(0, -10))
    pump(20)
pump(500)
check(g.page == PAGES - 1,
      "少量像素（30px < 阈值）不误触发翻页",
      f"-> 第 {g.page + 1} 页")

print()
print("=" * 72)
print("【6】边界钳位：到头后继续滚不应越界、抛错或卡住")
print("=" * 72)
for name, angle, want in (("第一页继续往下滚", 120, 0),
                          ("最后一页继续往上滚", -120, PAGES - 1)):
    at_last_page()
    g.goto(0, animate=False)
    pump(350)
    if want == PAGES - 1:
        at_last_page()
    for _ in range(10):
        send_wheel(g._tiles[0], angle=angle)
        pump(20)
    pump(600)
    check(g.page == want, name,
          f"-> 第 {g.page + 1} 页（应停在第 {want + 1} 页）")

print()
print("=" * 72)
print("【7】Tile 转交不能吞掉其它滚轮语义")
print("=" * 72)
# 完全没有滚轮量的事件：交给 Grid 的默认实现，不应翻页也不应崩
at_last_page()
before = g.page
send_wheel(g._tiles[0], angle=0, pixel=(0, 0))
pump(400)
check(g.page == before, "零滚轮事件不翻页也不报错",
      f"-> 第 {g.page + 1} 页")

# Tile 的父控件缺失时必须退回默认实现，不能因为 None 抛异常。
# 用真实的 Entry 构造 —— Tile.__init__ 会读 entry.name / entry.missing。
from launchpad.library import Entry   # noqa: E402

_stub = Entry(name="__probe__", target=r"C:\probe.exe", source_lnk="")
_orphaned = Tile(_stub, None, 64)
try:
    send_wheel(_orphaned, angle=120)
    pump(200)
    check(True, "父控件缺失时退回默认实现，不抛异常")
except Exception as exc:            # noqa: BLE001
    check(False, "父控件缺失时退回默认实现，不抛异常", repr(exc))

print()
print("=" * 72)
if FAILED:
    print(f"结果：{PASSED} 项通过，{len(FAILED)} 项失败")
    for f in FAILED:
        print(f"  - {f}")
    print("=" * 72)
    sys.exit(1)

print(f"结果：全部 {PASSED} 项通过")
print("=" * 72)
sys.exit(0)
