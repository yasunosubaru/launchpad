# -*- coding: utf-8 -*-
"""
页码指示器。

## 命中测试沿用的结论（已实测，不要推翻）

热区按「离点击位置最近的圆心」判定，而不是「必须在容差内」。
上一版按 `abs(dx) <= HIT/2` 逐个判定，但相邻圆心间距（32~38px）大于热区
容差（15px），两个点之间有约 8px 死区 —— 点在那儿什么也不发生。
取最近圆心保证整条横轴上任意位置都有归属（tests_dots.py 逐像素扫描验证）。

## 本版修掉的同步问题

### 1. 点击后 260ms 内指示器纹丝不动（用户报的「响应慢」）

`grid.goto()` 只在动画**结束**的回调 `_after()` 里发 `page_changed`
（grid.py:197），`window._sync_dots()` 又只订阅那个信号，于是
`PageDots.set_pages()` 在点击后整整 `Theme.SLIDE_MS = 260ms` 内不会被调用。
实测：`QTest.mouseClick` 返回时 `dots._current` 仍是旧页，+80ms 仍旧，
+480ms（动画结束后）才跳到目标页。这既是「慢」的全部来源，也是
「圆点和页面对不上」的直接成因 —— 网格已经在滑了，圆点还说自己在旧页。

**修法**：按 macOS 的语义，`mousePressEvent` 里立刻把 `_current` 挪到
**目标页**并重绘，然后才发 `picked`。语义定死为：

    `_current` == 用户已经要求的页（乐观更新）

配套的不变式：**权威信号永远能纠正乐观值**。`set_pages()` 是唯一入口，
它无条件把 `_current` 覆盖成网格的真值，所以任何漏掉的路径都会自愈。

为什么不需要「回滚定时器」：唯一不发信号就返回的路径是
`grid.goto()` 里 `index == grid._page and self._slide is None` 的提前返回
（grid.py:176），而那种情况下 `_current` 本来就等于目标页，乐观更新是个
空操作。已用 tests_dots.py 的 out-of-range 与打断场景验证。

### 2. 控件一出生就是 640×480、`_centers` 是空的

`__init__` 从不调 `_relayout()`，而 `Launchpad.__init__` 里第一次
`_sync_dots()` 发的是 `set_pages(1, 0)` —— 正好等于默认值，被
`set_pages` 的提前返回吃掉，于是 `_centers` 永远是 `[]`、
尺寸永远是 Qt 默认的 640×480（实测）。后果：这段时间里 `_hit_index()`
对任何坐标都返回 -1。现在出生即 `_relayout()`。

### 3. 页数变化后页码条不再居中

`_relayout()` 里的 `setFixedWidth()` 会改变控件宽度，但 `dots.move()`
只在 `Launchpad._resize()`（window.py:107）里做，而那只在**窗口** resize 时
才跑。实测强制切成 2 页后：`w=54 x=581`，居中应该是 613，偏 32px。
现在宽度一变就自己回父控件里重新居中。

### 4. 没有任何按下反馈

旧版只有 hover 三态。现在补 `_press`：按下的点立刻变色放大，
`mouseReleaseEvent` / `leaveEvent` 复位。

## 形变动画（点 ↔ 胶囊）

macOS 是平滑过渡的，瞬间切换显得很生硬。这里用 MORPH_MS=120ms 的
OutCubic 渐变，短于翻页的 260ms —— 指示器先落位，内容再滑到位。

**为什么动画期间控件宽度不变**：`_total_width()` 只取决于页数
（一个胶囊 + (n-1) 个圆点，挪哪个当激活点总宽都不变）。所以形变过程里
不需要 resize，也就避开了 resizeEvent + 重定位 —— 那正是要避免的卡顿。
`_target_widths/_target_centers`（整数、稳定）供命中测试用，
`_draw_widths/_draw_centers`（浮点、过渡帧）只供绘制用。
"""

from PyQt5.QtCore import (QEasingCurve, QRectF, Qt, QVariantAnimation,
                          pyqtSignal)
from PyQt5.QtGui import QPainter
from PyQt5.QtWidgets import QWidget

from . import theme

# 指示器形变时长。见模块 docstring。
MORPH_MS = 120


def _centers_for(widths, gap: float) -> list[float]:
    """由一串圆点宽度算出圆心（浮点）。用于形变中的过渡帧。"""
    out: list[float] = []
    n = len(widths)
    x = 0.0
    for i, w in enumerate(widths):
        out.append(x + w / 2.0)
        x += w + (gap if i + 1 < n else 0.0)
    return out


class PageDots(QWidget):
    picked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._count = 1
        self._current = 0
        self._hover = -1
        self._press = -1

        # 稳定几何（整数）。命中测试和外部断言只看这两个。
        self._centers: list[int] = []
        self._widths: list[int] = []

        # 绘制快照（浮点）。静止时与上面两个完全相等。
        self._draw_centers: list[float] = []
        self._draw_widths: list[float] = []

        self._morph: QVariantAnimation | None = None
        self._morph_from: list[float] = []
        self._morph_to: list[float] = []
        self._sizing = False

        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)

        # 出生即正确：不排版就意味着 _centers 为空、尺寸是 Qt 默认的
        # 640×480（见模块 docstring 第 2 条）。
        self._relayout()

    # ── 几何 ──────────────────────────────────────────────
    def _dot_gap(self) -> int:
        return theme.Theme.DOT_GAP

    def _dot_width(self, i: int) -> int:
        """激活点拉长成一个短条（macOS Launchpad 的做法）。"""
        return theme.Theme.DOT_BAR_W if i == self._current \
            else theme.Theme.DOT_R * 2

    def _total_width(self) -> int:
        """
        控件总宽。**只取决于页数，与当前页无关** —— 一个胶囊 + (n-1) 个圆点，
        挪哪个当激活点都不改总宽。正因如此形变动画全程不需要 resize。
        """
        T = theme.Theme
        if self._count <= 0:
            return 0
        dot = T.DOT_R * 2
        return self._count * dot + (self._count - 1) * T.DOT_GAP \
            + (T.DOT_BAR_W - dot)

    def _layout_targets(self) -> None:
        """算出整数的目标宽度与圆心。"""
        gap = self._dot_gap()
        widths = [self._dot_width(i) for i in range(self._count)]
        centers: list[int] = []
        x = 0
        n = len(widths)
        for i, w in enumerate(widths):
            centers.append(x + w // 2)
            x += w + (gap if i + 1 < n else 0)
        self._widths = widths
        self._centers = centers

    def _settle_draw(self) -> None:
        """绘制快照对齐到目标几何（形变结束 / 不形变时）。"""
        self._draw_widths = [float(w) for w in self._widths]
        self._draw_centers = _centers_for(self._draw_widths, self._dot_gap())

    def _center_in_parent(self) -> None:
        """
        宽度变了就自己回父控件里重新居中。

        `dots.move()` 只在 `Launchpad._resize()` 里做，而那只在**窗口**
        resize 时跑；`set_pages()` 改宽度不会触发窗口 resize，于是页码条
        会停在旧的 x 上（实测 2 页时偏 32px）。
        """
        p = self.parentWidget()
        if p is None or self._sizing:
            return
        x = (p.width() - self.width()) // 2
        if x != self.x():
            self.move(x, self.y())

    def _relayout(self, animate: bool = False) -> None:
        T = theme.Theme
        self._layout_targets()

        total = self._total_width()
        # 只在真的需要改尺寸时才调 setFixedWidth/setFixedHeight。
        # 无条件调虽然实测不产生 resizeEvent（Qt 内部短路），但白跑一遍
        # 尺寸协商没意义，而且一旦父控件进了 layout 就会触发重排。
        if self.width() != total or self.height() != T.DOT_HIT:
            self._sizing = True
            try:
                self.setFixedHeight(T.DOT_HIT)
                self.setFixedWidth(total)
            finally:
                self._sizing = False

        prev = self._draw_widths
        if (animate and prev and len(prev) == len(self._widths)
                and any(a != b for a, b in zip(prev, self._widths))):
            self._start_morph(prev, self._widths)
        else:
            self._stop_morph()
            self._settle_draw()

        self._center_in_parent()
        self.update()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._center_in_parent()

    # ── 形变动画 ──────────────────────────────────────────
    def _start_morph(self, frm, to) -> None:
        if self._morph is None:
            self._morph = QVariantAnimation(self)
            self._morph.valueChanged.connect(self._on_morph)
            self._morph.finished.connect(self._on_morph_done)
        self._morph.stop()
        self._morph.setDuration(MORPH_MS)
        self._morph.setStartValue(0.0)
        self._morph.setEndValue(1.0)
        self._morph.setEasingCurve(QEasingCurve.OutCubic)
        self._morph_from = [float(w) for w in frm]
        self._morph_to = [float(w) for w in to]
        self._morph.start()

    def _stop_morph(self) -> None:
        if self._morph is not None:
            self._morph.stop()

    def _on_morph(self, t: float) -> None:
        frm, to = self._morph_from, self._morph_to
        if not frm or len(frm) != len(to):
            self._settle_draw()
            self.update()
            return
        t = float(t)
        self._draw_widths = [a + (b - a) * t for a, b in zip(frm, to)]
        self._draw_centers = _centers_for(self._draw_widths, self._dot_gap())
        self.update()

    def _on_morph_done(self) -> None:
        self._settle_draw()
        self.update()

    # ── 状态 ──────────────────────────────────────────────
    def _sync_visibility(self) -> None:
        """
        单页时整条隐藏。`window._sync_dots` 也在做同样的事（window.py:260），
        这里再做一次是为了让控件自洽：调用方若只调 `set_pages` 不管显隐，
        页数回到 >1 时圆点会一直藏着。两边规则一致，重复设置无副作用
        （Qt 在状态未变时会早退）。
        """
        self.setVisible(self._count > 1)

    def set_pages(self, count: int, current: int, animate: bool = True) -> None:
        """
        网格页数 / 当前页的**权威**入口。

        无条件把 `_current` 覆盖成真值 —— 这就是乐观更新的自愈机制，
        任何漏掉的同步路径都会在这里被纠正回来。
        """
        count = max(1, int(count))
        cur = max(0, min(int(current), count - 1))
        self._sync_visibility()
        if count == self._count and cur == self._current:
            return
        # 页数变了就整体瞬移：此时控件宽度也在变，形变只会抖。
        page_only = (count == self._count)
        self._count = count
        self._current = cur
        if self._hover >= count:
            self._hover = -1
        if self._press >= count:
            self._press = -1
        self._relayout(animate=animate and page_only)

    # ── 命中 ──────────────────────────────────────────────
    def _hit_index(self, pos) -> int:
        """
        取**离点击位置最近**的那个圆心，而不是「必须在容差内」。
        见模块 docstring「命中测试沿用的结论」。

        用的是稳定的目标几何（`_centers`，整数），不是形变中的浮点快照：
        命中区不跟着动画抖，点击判定可复现。
        """
        if self._count <= 1 or not self._centers:
            return -1
        tol = theme.Theme.DOT_HIT // 2
        best, best_d = -1, None
        for i, cx in enumerate(self._centers):
            d = abs(pos.x() - cx)
            if best_d is None or d < best_d:
                best, best_d = i, d
        # 超出整条轴两端就当没点中，避免边缘空白区误触发
        if best_d is not None and best_d <= tol + self._dot_gap() // 2:
            return best
        return -1

    def mouseMoveEvent(self, ev):
        idx = self._hit_index(ev.pos())
        if idx != self._hover:
            self._hover = idx
            self.setCursor(Qt.PointingHandCursor if idx >= 0 else Qt.ArrowCursor)
            self.update()

    def leaveEvent(self, ev):
        if self._hover != -1 or self._press != -1:
            self._hover = -1
            self._press = -1
            self.setCursor(Qt.ArrowCursor)
            self.update()

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            super().mousePressEvent(ev)
            return
        idx = self._hit_index(ev.pos())
        if idx < 0:
            super().mousePressEvent(ev)
            return
        self._press = idx
        # 立刻把指示器挪到**目标页**（macOS 行为），不等网格的翻页动画播完。
        # 这是「响应慢」和「圆点和页面对不上」的同一个修复点。
        if idx != self._current:
            self._current = idx
            self._relayout(animate=True)
        else:
            self.update()
        self.picked.emit(idx)
        ev.accept()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = -1
            self.update()
        super().mouseReleaseEvent(ev)

    # ── 绘制 ──────────────────────────────────────────────
    def paintEvent(self, ev):
        # 必须调 super()。与 grid.py 同一条来之不易的教训：
        # 省掉它会让 Qt 认为本控件没画完（grid.py 的实测里更严重到
        # 连带跳过整棵子控件树）。它在本控件不设背景时不擦任何东西。
        super().paintEvent(ev)
        if self._count <= 1 or not self._draw_centers:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        T = theme.Theme
        cy = self.height() / 2.0
        # 宽度取自预计算的 _draw_widths，不再每帧每点重算 _dot_width()。
        for i in range(self._count):
            cx = self._draw_centers[i]
            r = float(T.DOT_R)
            if i == self._current:
                w = self._draw_widths[i]          # 胶囊，随形变平滑变化
                color = T.DOT_ACTIVE
                if i == self._press:
                    r += 1.0                       # 按下：胶囊轻微涨粗
            elif i == self._press:
                w = 2.0 * r
                color = T.DOT_HOVER
                r += 1.0                           # 按下：圆点涨大
            else:
                w = 2.0 * r
                color = T.DOT_HOVER if i == self._hover else T.DOT_IDLE
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawRoundedRect(QRectF(cx - w / 2.0, cy - r, w, r * 2.0), r, r)
        p.end()


__all__ = ["PageDots", "MORPH_MS"]