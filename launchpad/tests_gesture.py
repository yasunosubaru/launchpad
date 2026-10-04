# -*- coding: utf-8 -*-
"""把 verify_gesture.py 固化为仓库回归测试 tests_gesture.py。

覆盖用户报的两个缺陷：
  ① 滑动力度太大，轻轻一划直接到底、中间全跳过去
  ② 地下标志（页码点）跟不上页面的节奏
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEventLoop, QPoint, QPointF, QTimer, Qt   # noqa: E402
from PyQt5.QtGui import QWheelEvent                                # noqa: E402
from PyQt5.QtWidgets import QApplication                           # noqa: E402

_APP = QApplication(sys.argv)

from launchpad.library import Library     # noqa: E402
from launchpad.settings import Settings   # noqa: E402
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


_st = Settings()
_st.load()
_lib = Library()
_lib.load()
win = Launchpad(_lib, _st.get("columns"), _st.get("rows"), settings=_st)
win.setGeometry(0, 0, 2560, 1600)
win.showFullScreen()
pump(2200)

g = win.grid
dots = win.dots
N = g.page_count
PH = g._page_height()

print("=" * 74)
print("tests_gesture —— 滚轮力度与页码点同步")
print(f"页数={N}  页高={PH}px  WHEEL_GESTURE_MS={g.WHEEL_GESTURE_MS}")
print("=" * 74)


def wheel(angle: int, pixel=(0, 0)) -> None:
    t = g._tiles[0]
    p = t.mapToGlobal(t.rect().center())
    # QWheelEvent 参数顺序：(pos, globalPos, pixelDelta, angleDelta, ...)
    # pixelDelta 在前 —— 写反会让 angle 恒为 0，事件掉进像素分支，
    # 症状与「滚轮完全没反应」一模一样（踩过，见 README）。
    ev = QWheelEvent(QPointF(p), QPointF(p), QPoint(*pixel),
                     QPoint(0, angle), Qt.NoButton, Qt.NoModifier,
                     Qt.NoScrollPhase, False)
    _APP.sendEvent(t, ev)


def scroll(n: int, gap_ms: int, angle: int = -120) -> None:
    for _ in range(n):
        wheel(angle)
        pump(gap_ms)


# ══════════════════════════════════════════════════════════
print()
print("=" * 74)
print("【1】一次手势内 N 个事件 = 一页 —— 用户报「轻轻一划直接到底了」")
print("=" * 74)
# 修复前：3 个事件（间隔 16ms）挤在 24 帧里连续滑过 3 页，
# 中间两页各只停留不到 1 帧，肉眼只看到「一滑到底」。
for gap in (8, 16, 50, 100, 200):
    g.goto(0, animate=False)
    pump(400)
    scroll(12, gap)
    pump(900)
    check(g.page == 1,
          f"间隔 {gap:>3}ms 连发 12 个事件 -> 只翻 1 页",
          f"终点第 {g.page + 1} 页")

print()
print("=" * 74)
print("【2】停手超过阈值能继续翻页（不能变成「划不动」）")
print("=" * 74)
g.goto(0, animate=False)
pump(400)
for _ in range(3):
    scroll(3, 16)
    pump(1000)
check(g.page == 3, "3 次「快划 + 停手」-> 翻了 3 页", f"终点第 {g.page + 1} 页")

g.goto(0, animate=False)
pump(400)
wheel(-120)
pump(300)
wheel(-120)
pump(900)
check(g.page == 2, "间隔 300ms 两次滚轮 -> 2 页（各自一次手势）",
      f"终点第 {g.page + 1} 页")

g.goto(0, animate=False)
pump(400)
wheel(-120)
pump(100)
wheel(-120)
pump(900)
check(g.page == 1, "间隔 100ms 两次滚轮 -> 1 页（同一次手势）",
      f"终点第 {g.page + 1} 页")

print()
print("=" * 74)
print("【3】动画时长正常 —— 不是被压扁的多页连滑")
print("=" * 74)
for n in (1, 3, 8):
    g.goto(0, animate=False)
    pump(400)
    cb = g._animator.on_frame
    frames: list[float] = []

    def spy(v, frames=frames):
        frames.append(-v / max(1, PH))
        return cb(v)

    g._animator.on_frame = spy
    scroll(n, 16)
    pump(900)
    g._animator.on_frame = cb
    seq = []
    for x in frames:
        pg = int(round(x))
        if not seq or seq[-1] != pg:
            seq.append(pg)
    # 修复前 3 个事件只给 24 帧却滑过 3 页 —— 帧数被摊薄到不足 8 帧/页。
    # 修复后 1/3/8 个事件的帧数必须**基本相同**（都只翻一页），
    # 且每页帧数要够密。不能用固定的帧数当阈值：自驱动按屏幕刷新率
    # （本机 144Hz、offscreen 下读不到屏）会给不同的总帧数，
    # 判据只要求「单页帧数够密 + 页序列只有一页」。
    per_page = len(frames) / max(1, len(seq) - 1)
    check(seq == [0, -1] and per_page >= 14,
          f"{n:>2} 个滚轮事件 -> 单页完整动画",
          f"{len(frames)} 帧（{per_page:.0f} 帧/页），页序列 {seq}")

# 「一次手势 = 一页」这条性质：**直接**用页序列测，不用帧数当代理。
#
# 为什么改：原来这里只比 1/3/8 个事件的**帧数**，门槛是 `极差 <= 3` 帧。
# 但帧数由 144Hz 定时器驱动，在这台机器（常态 11~22% 后台负载）上抖动
# 本来就有实测 2~15 帧：
#     HEAD 版本（本文件所在提交的 grid.py）跑 6 次 -> 2/5/10/7/8/4  （过 1/6）
#     加了 _wheel_gesture_ms() 之后跑 6 次      -> 3/9/6/15/11/3   （过 2/6）
# 两组统计上无差别，所以抖动是**预先存在**的，不是新改动引入的 ——
# 而这条门槛把「抖动」当成了失败信号，被测性质其实每次都成立。
#
# 帧数当代理的代价是它主要在测「这台机器当时有多空」，而不是
# 「力度有没有被节流」。所以改成两件事：
#   1. 直接断言页序列恰好是 [0, -1]（只翻了一页）—— 零抖动依赖，
#      这才是「力度被节流」的充要条件。
#   2. 帧数仍留一条，但容差按实测抖动定，且远小于它要抓的失效模式。
counts = []
seqs = []
for n in (1, 3, 8):
    g.goto(0, animate=False)
    pump(400)
    cb = g._animator.on_frame
    frames2: list[float] = []

    def spy_x(v, frames2=frames2):
        frames2.append(-v / max(1, PH))
        return cb(v)

    g._animator.on_frame = spy_x
    scroll(n, 16)
    pump(900)
    g._animator.on_frame = cb
    counts.append(len(frames2))
    seq: list[int] = []
    for x in frames2:
        pg = int(round(x))
        if not seq or seq[-1] != pg:
            seq.append(pg)
    seqs.append(seq)

check(all(s == [0, -1] for s in seqs),
      "1/3/8 个事件都只翻一页（页序列恒为 [0,-1]）",
      f"页序列 {seqs}  <- 这条直接测性质，不依赖帧数抖动")

# 代理指标：帧数不该随事件个数增长。它要抓的失效模式是「8 个事件
# 翻了三页」——那会给出约 3 倍帧数（实测 ~250 vs ~93，即 +170%）。
# 所以 30% 的容差既能容纳实测最大 16% 的抖动，又留了 5 倍余量去抓
# 真正的失效。下限 8 帧是为了让低帧数场景（比如屏读不到刷新率时
# 退到 60Hz）不会因为绝对值太小而误报。
spread = max(counts) - min(counts)
tol = max(8, counts[0] * 0.30) if counts else 0
check(spread <= tol and max(counts) < counts[0] * 1.5 if counts[0] else False,
      f"1/3/8 个事件的帧数同一量级 {counts}",
      f"极差 {spread} 帧 / 容差 {tol:.0f}（实测抖动 ≤15，"
      f"失效模式会是 +170%）")

print()
print("=" * 74)
print("【4】页码点实时跟随 —— 用户报「地下标志跟不上页面的节奏」")
print("=" * 74)
# 修复前：22 帧里 dots 恒为 0，而目标页已是 1、动画页位从 0 滑到 -1。
g.goto(0, animate=False)
pump(400)
dots.set_pages(N, 0)
samples: list[tuple[int, int]] = []
cb = g._animator.on_frame


def spy2(v):
    samples.append((dots._current, g._page_target))
    return cb(v)


g._animator.on_frame = spy2
scroll(3, 16)
pump(900)
g._animator.on_frame = cb
check(bool(samples) and samples[0][0] == 1,
      "动画第一帧圆点已在目标页 1",
      f"首帧 (dots={samples[0][0]}, target={samples[0][1]})" if samples else "无帧")
check(all(d == t for d, t in samples),
      f"全部 {len(samples)} 帧圆点都等于目标页",
      f"不一致 {sum(1 for d, t in samples if d != t)} 帧")
check(dots._current == g.page, "落定后圆点与网格一致",
      f"dots={dots._current} page={g.page}")

g.goto(0, animate=False)
pump(400)
dots.set_pages(N, 0)
g.goto(3)
first_dot = [None]
cb = g._animator.on_frame


def spy3(v):
    if first_dot[0] is None:
        first_dot[0] = dots._current
    return cb(v)


g._animator.on_frame = spy3
pump(30)
g._animator.on_frame = cb
check(first_dot[0] == 3, "goto(3) 后第一帧圆点就是 3（不等落定）",
      f"首帧 dots={first_dot[0]}")

print()
print("=" * 74)
print("【5】边界不抖：首/末页继续同向滚不越界")
print("=" * 74)
g.goto(0, animate=False)
pump(400)
scroll(10, 16, angle=120)
pump(900)
check(g.page == 0, "第 1 页继续往前滚 -> 仍在第 1 页", f"第 {g.page + 1} 页")

g.goto(N - 1, animate=False)
pump(400)
scroll(10, 16, angle=-120)
pump(900)
check(g.page == N - 1, "末页继续往后滚 -> 仍在末页", f"第 {g.page + 1} 页")

print()
print("=" * 74)
print("【6】其它入口的圆点同步不受影响")
print("=" * 74)
g.goto(0, animate=False)
pump(300)
g.next_page()
pump(900)
check(g.page == 1 and dots._current == 1, "next_page() 后圆点跟上",
      f"page={g.page} dots={dots._current}")

g.goto(0, animate=False)
pump(300)
g.goto(2)
pump(900)
check(g.page == 2 and dots._current == 2, "goto(2) 后圆点跟上",
      f"page={g.page} dots={dots._current}")

# 圆点点击：乐观更新语义仍成立（点即到位，不等动画）
g.goto(0, animate=False)
pump(300)
dots.set_pages(N, 0)
win._on_dot(3)
check(dots._current == 3, "点第 4 个圆点后圆点立即到位（乐观更新）",
      f"dots={dots._current}，grid.page 仍在 {g.page}（动画未落定）")
pump(900)
check(dots._current == 3 and g.page == 3, "落定后两者一致",
      f"dots={dots._current} page={g.page}")

print()
print("=" * 74)
print("【7】手势节流不能破坏触控板像素路径")
print("=" * 74)
# 注意：像素路径**不走**手势节流（wheelEvent 里 angle 分支才节流）。
# 触控板没有「刻度」，用户期望的是位移量正比于翻页数，所以连续累积是
# 正确的 —— 实测 2000px（阈值 105px）会翻到末页，与手势节流无关。
# 这里守的是「别让离散滚轮的节流逻辑误伤像素分支」。
step = max(20, g._page_height() // 4)
g.goto(0, animate=False)
pump(400)
for _ in range(200):
    wheel(0, pixel=(0, -10))
    pump(3)
pump(900)
check(g.page == N - 1,
      "触控板像素累积不受手势节流影响（2000px 翻到末页）",
      f"终点第 {g.page + 1} 页（阈值 {step}px）")

# 少量像素（不足一个阈值）不应翻页
g.goto(0, animate=False)
pump(400)
for _ in range(3):
    wheel(0, pixel=(0, -10))
    pump(20)
pump(600)
check(g.page == 0, "少量像素（30px < 阈值）不误触发", f"第 {g.page + 1} 页")

print()
print("=" * 74)
if FAILED:
    print(f"结果：{PASSED} 项通过，{len(FAILED)} 项失败")
    for f in FAILED:
        print(f"  - {f}")
    print("=" * 74)
    sys.exit(1)

print(f"结果：全部 {PASSED} 项通过")
print("=" * 74)
sys.exit(0)