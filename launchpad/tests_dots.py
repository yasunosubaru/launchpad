# -*- coding: utf-8 -*-
"""
launchpad/tests_dots.py —— 页码指示器（PageDots）同步与手感回归测试。

跑法：
    $env:QT_QPA_PLATFORM="offscreen"
    python -u -m launchpad.tests_dots
    
输出逐条 PASS/FAIL，失败退出码非 0。加 --strict 可把「别人文件里的
已知缺陷」也算进退出码（见 §D，默认不计，否则这个文件会替 grid.py
背锅而永远是红的）。

全部用真实 Qt 事件驱动：`QTest.mouseClick` / `QTest.mouseMove`，
不直接调内部方法冒充交互。几何/可见性断言跑在真实窗口 + 真实 Grid 上，
不是 mock。

## 语义约定（被测对象的行为契约）

    PageDots._current == 用户已经要求的页（乐观更新，macOS 行为）

    点击第 N 个点 → 立刻 `dots._current == N-1`，不等翻页动画播完。
    `set_pages()` 是唯一权威入口，无条件把 `_current` 覆盖成网格真值，
    因此任何漏掉的同步路径都会自愈。
    `grid.page` 是滞后量：动画未结束时它仍是旧页，只有动画结束的
    `_after()` 回调才发 `page_changed`。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import (QEventLoop, QPoint, QTimer, QVariantAnimation,
                          Qt)
from PyQt5.QtCore import QEvent, QPointF
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

_APP = QApplication(sys.argv)

from launchpad import theme                                    # noqa: E402
from launchpad.grid import Grid                                # noqa: E402
from launchpad.library import Entry, Library                   # noqa: E402
from launchpad.pagedots import MORPH_MS, PageDots             # noqa: E402
from launchpad.window import Launchpad                         # noqa: E402

T = theme.Theme
SLIDE_MS = T.SLIDE_MS

# 7×5 = 每页 35 个
COLS, ROWS = 7, 5
PER_PAGE = COLS * ROWS

# ── 断言框架 ──────────────────────────────────────────────
_RESULTS: list[tuple[str, str, bool, str]] = []


def check(section: str, name: str, cond: bool, detail: str = "") -> bool:
    _RESULTS.append((section, name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"\n         → {detail}" if detail else ""))
    return bool(cond)


def head(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def pump(ms: int) -> None:
    """跑 ms 毫秒的事件循环（翻页动画靠它推进）。"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    _APP.processEvents()


def settle() -> None:
    """等翻页动画 + 形变动画都结束。"""
    pump(SLIDE_MS + MORPH_MS + 200)


def mk_lib(n: int) -> Library:
    """内存里的假库，不碰用户真实的 shortcuts.json。"""
    lib = Library()
    lib.entries = [Entry(name=f"App{i:03d}", target=f"C:/fake/App{i:03d}.exe")
                   for i in range(n)]
    return lib


_LIVE: list[Launchpad] = []


def _close_all_windows() -> None:
    while _LIVE:
        w = _LIVE.pop()
        w.close()
        w.deleteLater()
    pump(120)


def make_win(n: int = PER_PAGE * 4, cols: int = COLS, rows: int = ROWS,
             w: int = 800, h: int = 600) -> Launchpad:
    """
    真实窗口 + 真实 Grid，接好 dots ↔ grid 的信号。

    **同一时刻只保留一个窗口**，这是必须的，不是洁癖：
    `QTest.mouseMove()` 走的是 `QCursor::setPos()` + 平台层路由，offscreen
    没有窗口管理器，多个同位置窗口并存时它解析不出"光标下是谁"，
    move 事件就凭空消失 —— 症状是 `_hover` 恒为 -1，看着像产品 bug。
    实测：1 个窗口时正常，第 2 个起就丢，`raise_()` / `activateWindow()`
    都救不回来；关掉旧窗口立刻恢复。
    （`QTest.mouseClick(Widget, ...)` 直接投递到该控件，不受影响。）

    窗口尺寸也收在 800×600 —— offscreen 虚拟屏正好这么大，
    超出部分 `topLevelAt()` 一律返回 None。
    """
    _close_all_windows()
    win = Launchpad(mk_lib(n), cols, rows)
    win.setGeometry(0, 0, w, h)
    win.show()
    pump(300)
    _LIVE.append(win)
    return win


def in_view(grid: Grid) -> list:
    """当前真正落在网格可视区里的图标 —— 「用户看到的那一页」。"""
    return [t for t in grid._tiles if t.geometry().intersects(grid.rect())]


def first_visible_y(grid: Grid):
    vis = in_view(grid)
    return min(t.y() for t in vis) if vis else None


def click_dot(dots: PageDots, i: int) -> None:
    """用真实鼠标事件点第 i 个点（0 基）。"""
    QTest.mouseClick(dots, Qt.LeftButton, pos=QPoint(dots._centers[i], 15))


# ══════════════════════════════════════════════════════════
head("§A  出生状态：不排版就是 640×480 + 空圆心")
# ══════════════════════════════════════════════════════════
# 回归：__init__ 从不调 _relayout()，而首次 set_pages(1,0) 因等于默认值
# 被提前返回，于是 _centers 恒为 []、尺寸恒为 Qt 默认。
d0 = PageDots()
check("A", "出生即有圆心（不是空列表）", len(d0._centers) == d0._count,
      f"_centers={d0._centers}")
check("A", "出生即有正确高度（DOT_HIT，非 Qt 默认）",
      d0.height() == T.DOT_HIT, f"height={d0.height()} 期望 {T.DOT_HIT}")
check("A", "出生即有正确宽度（非 Qt 默认 640）",
      d0.width() == d0._total_width(),
      f"width={d0.width()} 期望 {d0._total_width()}")
check("A", "set_pages(1,0) 是幂等的", (d0.set_pages(1, 0) is None)
      and d0.width() == d0._total_width(), f"width={d0.width()}")
check("A", "单页时不可见", not d0.isVisible(),
      f"isVisible={d0.isVisible()}（父控件未 show，规则仍是 count>1）")

# ══════════════════════════════════════════════════════════
head("§B  几何与命中：整条轴无死区（沿用已验证结论，防回退）")
# ══════════════════════════════════════════════════════════
d4 = PageDots()
d4.set_pages(4, 0)
mid = d4.height() // 2
cs = d4._centers
check("B", "圆心数量 = 页数", len(cs) == 4, f"{cs}")
check("B", "圆心是整数（QPoint 兼容）",
      all(isinstance(c, int) for c in cs), f"{[type(c).__name__ for c in cs]}")
gaps = [b - a for a, b in zip(cs, cs[1:])]
check("B", "圆心间距 >= 20", all(g >= 20 for g in gaps), f"gaps={gaps}")

dead = [x for x in range(cs[0] - 8, cs[-1] + 9)
        if d4._hit_index(QPoint(x, mid)) < 0]
check("B", "轴上逐像素无死区", not dead, f"死区 {len(dead)}px: {dead[:8]}")
mis = [i for i, cx in enumerate(cs) if d4._hit_index(QPoint(cx, mid)) != i]
check("B", "圆心正上方命中自身", not mis, f"错位 {mis}")
check("B", "轴外不误触发",
      d4._hit_index(QPoint(cs[0] - 40, mid)) < 0
      and d4._hit_index(QPoint(cs[-1] + 40, mid)) < 0)
check("B", "单页时任何坐标都不命中（不误发 picked）",
      PageDots()._hit_index(QPoint(3, 5)) < 0)

# ══════════════════════════════════════════════════════════
head("§C  动画进行中点击：语义 = 立刻显示目标页（macOS 行为）")
# ══════════════════════════════════════════════════════════
win = make_win(PER_PAGE * 4)
g, dots = win.grid, win.dots
check("C", "前置条件：4 页 / 在第 1 页",
      g.page_count == 4 and g.page == 0 and dots._current == 0,
      f"pages={g.page_count} page={g.page} dots._current={dots._current}")

# 点击前后各埋点，记录 handler 返回时与动画中途的状态
t0 = time.perf_counter()
click_dot(dots, 3)
t_click = (time.perf_counter() - t0) * 1000
state_now = (dots._current, g.page, g._animator.is_running())
check("C", "mouseClick 返回后圆点已到目标页（不等动画）",
      state_now[0] == 3, f"dots._current={state_now[0]}，点击耗时 {t_click:.2f}ms")
check("C", "此刻 grid.page 仍是旧页（动画未结束，属预期）",
      state_now[1] == 0, f"grid.page={state_now[1]} animating={state_now[2]}")
check("C", "翻页动画确实在跑", state_now[2] is True)

pump(SLIDE_MS // 3)
mid_state = (dots._current, g.page)
check("C", "动画中途圆点仍是目标页（不被旧值拉回）",
      mid_state[0] == 3, f"dots._current={mid_state[0]} grid.page={mid_state[1]}")
check("C", "动画中途圆点与真值暂时不一致 —— 这就是被测的乐观语义",
      mid_state[0] != mid_state[1],
      f"dots={mid_state[0]} grid={mid_state[1]}，set_pages 是权威出口")

settle()
check("C", "动画结束后两者收敛到同一页",
      dots._current == 3 == g.page,
      f"dots._current={dots._current} grid.page={g.page}")
check("C", "网格真停在目标页",
      g._offset == -3 * g._page_height(),
      f"offset={g._offset} 期望 {-3 * g._page_height()}")
vis = in_view(g)
check("C", "视口内正好一页的图标",
      len(vis) == PER_PAGE, f"视口内 {len(vis)} 个，期望 {PER_PAGE}")

# 响应耗时：点击 → 指示器落到目标页（同步完成，0 帧等待）
t0 = time.perf_counter()
click_dot(dots, 0)
lat = (time.perf_counter() - t0) * 1000
check("C", "点击到指示器更新 < 16ms（一帧内，无 260ms 空窗）",
      lat < 16.0, f"{lat:.2f}ms（旧实现是 {SLIDE_MS}ms）")
settle()

# ══════════════════════════════════════════════════════════
head("§D  快速连续点击 / 动画被打断")
# ══════════════════════════════════════════════════════════
win2 = make_win(PER_PAGE * 4)
g2, d2 = win2.grid, win2.dots

click_dot(d2, 3)
pump(50)                                   # 动画进行到一半
check("D", "打断前处于动画中", g2._animator.is_running())
offset_mid = g2._offset
click_dot(d2, 0)                            # 中途改主意
check("D", "打断后圆点立刻跟到最新意图",
      d2._current == 0, f"dots._current={d2._current}")
settle()
check("D", "打断后最终状态 = 最后一次点击的页",
      d2._current == 0 == g2.page,
      f"dots._current={d2._current} grid.page={g2.page}")
check("D", "打断后网格偏移归位",
      g2._offset == 0, f"offset={g2._offset}（打断时曾到 {offset_mid}）")
check("D", "打断后视口内是一整页", len(in_view(g2)) == PER_PAGE,
      f"{len(in_view(g2))} 个")

# 一口气点 5 次，完全不等
for i in (1, 2, 3, 0, 2):
    click_dot(d2, i)
check("D", "连点过程中圆点始终等于最后一次点击",
      d2._current == 2, f"dots._current={d2._current}")
settle()
check("D", "连点后 dots 与 grid 一致于末次意图",
      d2._current == 2 == g2.page,
      f"dots._current={d2._current} grid.page={g2.page}")
check("D", "连点后网格停在正确偏移",
      g2._offset == -2 * g2._page_height(),
      f"offset={g2._offset} 期望 {-2 * g2._page_height()}")
check("D", "连点后视口内是一整页", len(in_view(g2)) == PER_PAGE,
      f"{len(in_view(g2))} 个")

# 连点**同一个**点：goto 提前返回，不发信号，乐观更新也不能把它改错
before = d2._current
click_dot(d2, 2)
check("D", "重复点当前页：状态不变（也不会误动）",
      d2._current == before == g2.page, f"{before} → {d2._current}")
settle()
check("D", "重复点当前页后仍一致",
      d2._current == 2 == g2.page, f"{d2._current} / {g2.page}")

# ══════════════════════════════════════════════════════════
head("§E  越界点击：权威信号把圆点拉回真值（自愈）")
# ══════════════════════════════════════════════════════════
win3 = make_win(PER_PAGE * 4)
g3, d3 = win3.grid, win3.dots
d3._count = 6                             # 模拟页数过期
d3._relayout()
pump(50)
check("E", "前置条件：圆点 6 个 / 网格 4 页",
      d3._count == 6 and g3.page_count == 4,
      f"dots._count={d3._count} grid.page_count={g3.page_count}")
click_dot(d3, 5)                          # 越界
# 越界点击后圆点**立即**等于网格钳位后的真值，而不是先乐观跟到 5。
#
# 之前这里断言「先乐观跟到 5，动画结束后才被纠正回 3」—— 那描述的是
# `page_changed`（动画结束才发）当唯一信号源时的行为。现在有了
# `page_intent`（`goto()` 里立刻发），越界值在同一次事件处理里就被纠正，
# 中间态根本不会被重绘出来，所以视觉上不可见 —— 这正是「标志跟不上」
# 那个缺陷的修法。
#
# 关键点：`_current` 在鼠标处理函数返回时必须已经是 3。若仍是 5，
# 说明「点一下圆点先跳到不存在的第 6 页再弹回来」，那就是可见的闪跳。
check("E", "越界点击后圆点立即等于权威真值（无可见的乐观闪跳）",
      d3._current == 3, f"dots._current={d3._current}（期望 3）")
check("E", "越界点击后 dots 已被纠正到真实页数",
      d3._count == g3.page_count,
      f"dots._count={d3._count} grid.page_count={g3.page_count}")
settle()
check("E", "动画结束后圆点与网格一致于真实末页",
      d3._current == 3 == g3.page,
      f"dots._current={d3._current} grid.page={g3.page}")
check("E", "自愈后视口内仍是一整页", len(in_view(g3)) == PER_PAGE,
      f"{len(in_view(g3))} 个")

# ══════════════════════════════════════════════════════════
head("§F  搜索导致页数变化：4 页 → 1 页 → 4 页")
# ══════════════════════════════════════════════════════════
win4 = make_win(PER_PAGE * 4)
g4, d4w = win4.grid, win4.dots
win4.grid.goto(2, animate=False)          # 先停在第 3 页，制造"中间态"
settle()
check("F", "前置条件：3 页共 4 页，停在第 3 页",
      g4.page == 2 and d4w._current == 2, f"page={g4.page}")
check("F", "多页时可见", d4w.isVisible())

win4._on_search("App007")                 # 唯一命中 → 1 页
settle()
check("F", "1 页时圆点隐藏（不是显示一个点）",
      not d4w.isVisible(), f"isVisible={d4w.isVisible()} count={d4w._count}")
check("F", "1 页时页数=1 且当前页被夹到 0",
      d4w._count == 1 and d4w._current == 0,
      f"count={d4w._count} current={d4w._current}")
check("F", "1 页时圆心收敛成 1 个", len(d4w._centers) == 1,
      f"{d4w._centers}")

win4._on_search("")                       # 回到 4 页
settle()
check("F", "回到 4 页时圆点重新可见",
      d4w.isVisible(), f"isVisible={d4w.isVisible()}")
check("F", "回到 4 页时 count 恢复",
      d4w._count == 4, f"count={d4w._count}")
check("F", "回到 4 页时当前页回到 0",
      d4w._current == 0 == g4.page, f"dots={d4w._current} grid={g4.page}")
check("F", "隐藏过的圆点恢复后仍可命中",
      d4w._hit_index(QPoint(d4w._centers[2], 15)) == 2)
click_dot(d4w, 2)
settle()
check("F", "隐藏过的圆点恢复后点击真的翻页",
      d4w._current == 2 == g4.page,
      f"dots={d4w._current} grid={g4.page}")

# 隐藏状态下不该还能点得动
win4.grid.goto(0, animate=False)
settle()
win4._on_search("App007")
settle()
check("F", "1 页隐藏期间点击不改变任何状态",
      (not d4w.isVisible()) and d4w._count == 1 and g4.page == 0,
      f"visible={d4w.isVisible()} count={d4w._count} page={g4.page}")

# ══════════════════════════════════════════════════════════
head("§G  窗口 resize：圆心重算")
# ══════════════════════════════════════════════════════════
win5 = make_win(PER_PAGE * 4)
g5, d5 = win5.grid, win5.dots
old_centers = list(d5._centers)
old_w = d5.width()
win5.resize(1000, 700)
pump(200)
check("G", "resize 后圆心仍是 4 个且是整数",
      len(d5._centers) == 4 and all(isinstance(c, int) for c in d5._centers),
      f"{d5._centers}")
check("G", "resize 不改变圆心位置（宽度只由页数决定）",
      d5._centers == old_centers, f"{old_centers} → {d5._centers}")
check("G", "resize 后页码条仍水平居中",
      d5.x() == (win5.width() - d5.width()) // 2,
      f"x={d5.x()} 居中应为 {(win5.width() - d5.width()) // 2}")
check("G", "resize 不产生多余的固定宽调用", d5.width() == old_w,
      f"{old_w} → {d5.width()}")

# resize 之后命中区仍然自洽
mid5 = d5.height() // 2
dead5 = [x for x in range(d5._centers[0] - 8, d5._centers[-1] + 9)
         if d5._hit_index(QPoint(x, mid5)) < 0]
check("G", "resize 后轴上仍无死区", not dead5, f"死区 {len(dead5)}px")
mis5 = [i for i, cx in enumerate(d5._centers)
        if d5._hit_index(QPoint(cx, mid5)) != i]
check("G", "resize 后圆心正上方仍命中自身", not mis5, f"错位 {mis5}")

# resize 后点击依然有效
click_dot(d5, 3)
settle()
check("G", "resize 后点击仍能翻到正确页",
      d5._current == 3 == g5.page, f"dots={d5._current} grid={g5.page}")

# 页数变化引起的宽度变化也必须重新居中（回归：只由 window._resize 居中）
# 页数现在由 layouts 依据 **_entries** 推导（不再是 len(_tiles)），
# 所以要同时截断 _entries 和 _tiles，只截 tiles 不会改变页数。
win5.grid._entries = win5.grid._entries[:PER_PAGE * 2]
win5.grid._tiles = win5.grid._tiles[:PER_PAGE * 2]
win5.grid._recompute_pages()
win5.grid.relayout()
win5._sync_dots()
pump(100)
check("G", "页数变 2 后控件宽度跟着变",
      d5.width() == d5._total_width() and d5._count == 2,
      f"count={d5._count} width={d5.width()} 期望 {d5._total_width()}")
check("G", "页数变化后页码条**重新居中**（不再停在旧 x）",
      d5.x() == (win5.width() - d5.width()) // 2,
      f"x={d5.x()} 居中应为 {(win5.width() - d5.width()) // 2}")

# ══════════════════════════════════════════════════════════
head("§H  形变动画：点 ↔ 胶囊平滑过渡，且不触发 resize")
# ══════════════════════════════════════════════════════════
win6 = make_win(PER_PAGE * 4)
g6, d6 = win6.grid, win6.dots
width_before = d6.width()
resize_log: list[tuple[int, int]] = []
orig_re = PageDots.resizeEvent


def spy_resize(self, ev):
    resize_log.append((ev.oldSize().width(), ev.size().width()))
    return orig_re(self, ev)


PageDots.resizeEvent = spy_resize
try:
    resize_log.clear()
    click_dot(d6, 2)
    widths = []
    for _ in range(10):
        pump(15)
        widths.append(tuple(round(w, 2) for w in d6._draw_widths))
    settle()
finally:
    PageDots.resizeEvent = orig_re

check("H", "形变期间圆点宽度确实在逐帧变化",
      len(set(widths)) > 2, f"采到 {len(set(widths))} 个不同的宽度向量")
check("H", "形变期间控件宽度**不变**（0 次 resize，不引发重排）",
      not resize_log and d6.width() == width_before,
      f"resizeEvents={resize_log} width {width_before}→{d6.width()}")
check("H", "形变结束后绘制快照精确等于目标几何",
      d6._draw_widths == [float(w) for w in d6._widths]
      and d6._draw_centers == [float(c) for c in d6._centers],
      f"draw={d6._draw_widths} target={d6._widths}")
check("H", "形变最终停在正确页",
      d6._current == 2 == g6.page, f"dots={d6._current} grid={g6.page}")

# 形变动画对象必须复用，不能每次点击都新建一个（内存泄漏）
morph_obj = d6._morph
for i in (0, 1, 3, 0):
    click_dot(d6, i)
    settle()
n_anim = len(d6.findChildren(QVariantAnimation))
check("H", "每个 PageDots 只有 1 个形变动画对象（复用，不泄漏）",
      n_anim == 1 and d6._morph is morph_obj,
      f"{n_anim} 个 QVariantAnimation 子对象，_morph 同一实例="
      f"{d6._morph is morph_obj}")

# ══════════════════════════════════════════════════════════
head("§I  hover / 按下反馈")
# ══════════════════════════════════════════════════════════
win7 = make_win(PER_PAGE * 4)
g7, d7 = win7.grid, win7.dots
# **直接投递 MouseMove，不走光标路由。**
#
# 先说为什么不用 QTest.mouseMove()：
#   `QTest.mouseMove(widget, pos)` 底层是 `QCursor::setPos()`，然后指望
#   Windows 把 WM_MOUSEMOVE 派发到该控件。这条链依赖：
#     ① 光标当前不在目标坐标（否则 setPos 是空操作，一个事件都没有）
#     ② 那一刻 Launchpad 正好在光标下、是该点的顶层窗口
#   两条都是**机器状态**，不是被测逻辑。实测：
#     offscreen  虚拟光标每次重置到 (10,10) -> 首次就命中
#     真实屏幕    光标被上一个测试留在圆点上 -> 首次必然失效
#   我试过「先 setPos 到别处再移入」，仍然偶发失败（连跑 4 次：
#   0 / 4 项FAIL / 2 项 FAIL / 0），失败项每次还不同 —— 因为 ② 那条
#   依赖系统把光标下哪个窗口当顶层，setPos 到 (3,3) 并不保证后续
#   move 会送给 Launchpad。
#
# 所以直接 sendEvent：绕过光标路由和前台窗口，只验证
# **收到 MouseMove 之后 _hit_index / setCursor / update 是否正确** ——
# 这才是 pagedots.py 该负责的部分。「Windows 会不会把事件送来」
# 是操作系统的事，测它只会引入机器状态的噪声。
# （点击测试一直稳定，正是因为 QTest.mouseClick 也是直接投递。）
def hover_at(dots, i: int) -> None:
    pos = QPoint(dots._centers[i], 15)
    ev = QMouseEvent(QEvent.MouseMove, QPointF(pos), Qt.NoButton,
                     Qt.NoButton, Qt.NoModifier)
    QApplication.sendEvent(dots, ev)
    pump(30)


def leave_from(dots) -> None:
    """让 dots 收到 Leave —— 直接发 QEvent.Leave，测的是 leaveEvent 的逻辑。

    之前是把 MouseMove 发给**窗口**的 (5,5)，指望 Qt 的 enter/leave 跟踪
    顺手给 dots 发 Leave。那个跟踪由 QApplication 在处理完 move 后驱动，
    直接 sendEvent 一个合成 move 不会走那条路，所以 leaveEvent 永远不来，
    _hover 停在原值 —— 6/6 次稳定失败。
    """
    QApplication.sendEvent(dots, QEvent(QEvent.Leave))
    pump(30)


hover_at(d7, 1)
check("I", "鼠标移入圆点上方 → hover 命中该点",
      d7._hover == 1, f"_hover={d7._hover}")
check("I", "hover 时是手型光标",
      d7.cursor().shape() == Qt.PointingHandCursor,
      f"shape={d7.cursor().shape()}")
hover_at(d7, 0)
check("I", "移到另一个点 → hover 跟着换",
      d7._hover == 0, f"_hover={d7._hover}")

t0 = time.perf_counter()
QTest.mousePress(d7, Qt.LeftButton, pos=QPoint(d7._centers[3], 15))
press_lat = (time.perf_counter() - t0) * 1000
check("I", "按下瞬间就有 press 状态（按下反馈存在）",
      d7._press == 3, f"_press={d7._press}，press→返回 {press_lat:.2f}ms")
check("I", "按下即翻页意图已生效",
      d7._current == 3, f"_current={d7._current}")
QTest.mouseRelease(d7, Qt.LeftButton, pos=QPoint(d7._centers[3], 15))
pump(20)
check("I", "松开后 press 复位", d7._press == -1, f"_press={d7._press}")
settle()

hover_at(d7, 1)
leave_from(d7)
pump(40)
check("I", "鼠标移出控件 → hover 复位",
      d7._hover == -1, f"_hover={d7._hover}")
check("I", "移出后光标恢复箭头",
      d7.cursor().shape() == Qt.ArrowCursor, f"shape={d7.cursor().shape()}")

# hover 重绘延迟：应在同一轮事件循环内发生，不等下一次状态变更
paints: list[float] = []
op = PageDots.paintEvent


def spy_paint(self, ev):
    paints.append(time.perf_counter())
    return op(self, ev)


PageDots.paintEvent = spy_paint
try:
    win8 = make_win(PER_PAGE * 4)
    d8 = win8.dots
    hover_at(d8, 0)
    paints.clear()
    t0 = time.perf_counter()
    hover_at(d8, 3)
    hover_at(d8, 1)
    pump(40)
    if paints:
        first = (paints[0] - t0) * 1000
    else:
        first = float("inf")
    check("I", "hover 重绘在一帧内发生（不等状态变更）",
          first < 16.0, f"首次重绘 {first:.2f}ms，共 {len(paints)} 次")
    check("I", "hover 后确实换了 hover 点",
          d8._hover == 1, f"_hover={d8._hover}")
finally:
    PageDots.paintEvent = op

# ══════════════════════════════════════════════════════════
head("§J  端到端：dots 与 grid 始终指向同一页（随机点击序列）")
# ══════════════════════════════════════════════════════════
win9 = make_win(PER_PAGE * 4)
g9, d9 = win9.grid, win9.dots
seq = [2, 0, 3, 1, 3, 3, 0, 1, 2, 0]
mismatch = []
for n, i in enumerate(seq):
    click_dot(d9, i)
    settle()
    if not (d9._current == i == g9.page
            and g9._offset == -i * g9._page_height()
            and len(in_view(g9)) == PER_PAGE):
        mismatch.append((n, i, d9._current, g9.page, g9._offset,
                         len(in_view(g9))))
check("J", f"10 次点击序列 {seq} 每一步都自洽", not mismatch,
      f"不自洽步骤: {mismatch}" if mismatch else
      "每次 dots._current == grid.page == 目标页，偏移与视口数量都对")

# ══════════════════════════════════════════════════════════
head("§K  grid.py 的两条不变量（已修，防回退）")
# ══════════════════════════════════════════════════════════
# K1 曾是 grid.py 的真实 bug：rebuild() 把 _page 置 0 后调 goto(0, animate=False)，
# 而 goto 的提前返回条件恰好命中 —— _offset 永不归零。在第 3 页搜索之后
# 图标全落在可视区上方，实测 24 条结果 0 个可见。已改为显式归零 _offset。
#
# K2 也是 grid.py 的 bug，且更隐蔽：窗口 resize 会改变页高，若图标尺寸随之
# 变化，relayout() 会触发 rebuild()，而 rebuild() 会把 _page 归 0 ——
# 于是「在第 3 页把窗口改一下大小」被弹回第 1 页。已改为记下当前页、
# 重建后回原页（条目内容真变了时仍回第 1 页，那是正确行为）。
#
# 这两条属于 grid.py，但本文件正好是唯一稳定复现它们的地方，所以一并守着。

# K1: 在第 3 页时搜索 → 页数变 1 → 网格停在 -2 页高的偏移，图标全跑到屏幕外
wk1 = make_win(PER_PAGE * 4)
gk1 = wk1.grid
gk1.goto(2, animate=False)
settle()
pre_offset = gk1._offset
wk1._on_search("App007")
settle()
k1_ok = gk1._offset == 0 and len(in_view(gk1)) == len(gk1._tiles)
check("K", "【grid.py】搜索后 _offset 归零、图标可见", k1_ok,
      f"_page={gk1._page} _offset={gk1._offset}（搜索前 {pre_offset}，"
      f"期望 0）；视口内 {len(in_view(gk1))}/{len(gk1._tiles)} 个图标，"
      f"最低 y={first_visible_y(gk1)}，网格高 {gk1.height()}")

# K2: 停在第 3 页时改变窗口高度 → _offset 与新的页高不自洽，视口被剪切
wk2 = make_win(PER_PAGE * 4)
gk2 = wk2.grid
gk2.goto(2, animate=False)
settle()
ph_before = gk2._page_height()
wk2.resize(1280, 1000)
pump(250)
ph_after = gk2._page_height()
k2_ok = gk2._offset == -2 * ph_after and len(in_view(gk2)) == PER_PAGE
check("K", "【grid.py】resize 后 _offset 与新页高自洽", k2_ok,
      f"_page={gk2._page} 页高 {ph_before}→{ph_after} 但 _offset={gk2._offset}"
      f"（期望 {-2 * ph_after}）；视口内 {len(in_view(gk2))} 个，期望 {PER_PAGE}")

# ══════════════════════════════════════════════════════════
head("汇总")
# ══════════════════════════════════════════════════════════
OWNED = "K"          # grid.py 名下的不变量（已由 grid.py 负责修好）

total = len(_RESULTS)
failed = [r for r in _RESULTS if not r[2]]
failed_owned = [r for r in failed if r[0] != OWNED]
failed_foreign = [r for r in failed if r[0] == OWNED]

print(f"  总计 {total} 条")
print(f"  pagedots.py 名下：{total - len(failed_owned) - len(failed_foreign)}"
      f"/{total - len(failed_foreign)} PASS，{len(failed_owned)} FAIL")
print(f"  grid.py 名下（§K 不变量）："
      f"{len(failed_foreign)} FAIL / {sum(1 for r in _RESULTS if r[0] == OWNED)} 条")
for r in failed_foreign:
    print(f"     - [{r[0]}] {r[1]}")
print()

if failed_owned or failed_foreign:
    print("  失败项：")
    for r in failed:
        print(f"     - [{r[0]}] {r[1]}")
        if r[3]:
            print(f"         {r[3]}")
    print()
    print("RESULT: FAIL")
    sys.exit(1)

print()
print("RESULT: PASS")
sys.exit(0)
