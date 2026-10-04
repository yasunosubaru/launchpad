# -*- coding: utf-8 -*-
"""
空白区域（图标与图标之间的空隙）的命中判定，以及左右键语义。

需求原文：「将图标之间的空白区域做成跟图标无关的，
我左键空白区域就退出（返回后台），右键空白区域调出设置界面」

关键词是「跟图标无关」。这里把它拆成两件可以分别验证的事：

**一、命中区域是「图标本体 + 文字的实际绘制宽度」，不是 Tile 控件矩形。**

Tile 控件 352x184 占满整个单元格，而图标只有 128x128 居中在里面。
按控件矩形判定，控件内图标外的 224px 横向空白会被算成「点到图标」，
于是真正的空白被判成图标，点下去不退出 —— 这正是要修的 bug。

文字更隐蔽：文字区域横跨控件全宽（352px）。若把全宽算进命中矩形，
纵向间隙会塌到 0，整张网格都会变成「不是空白」。
所以这里用 QFontMetrics 量出文字的**真实像素宽度**，再按图标中心对齐，
判定范围与用户肉眼看到的完全一致。

**二、边界规则统一为半开区间 [x0, x1) × [y0, y1)**，与 Qt 的
``QRect.contains()`` 逐像素一致：

- 图标最外一圈像素**全部**算命中（含四个角点）；
- ``x0+w`` / ``y0+h`` 这两行两列**不算**命中；
- 两个图标紧邻（间隙恰好为 0）时，那条线上的像素归**右边 / 下边**那个；
  有间隙时两个图标都不含它，算空白。

规则写死并配了逐像素测试，避免「点在线上到底算不算」的歧义。

四种索引实现（线性 / 二分 / numpy / 桶网格）都保证同一套语义，
包括「重叠时取最小下标」。``bench()`` 可复现实测数据。

本模块不改动 grid.py / window.py，通过 ``install_on()`` 挂载。
"""

from __future__ import annotations

import time
from bisect import bisect_right

from PyQt5.QtCore import QEvent, QObject, QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import QAction, QApplication, QMenu, QWidget

try:                                   # numpy 可选，缺失时自动降级
    import numpy as _np
except ImportError:                    # pragma: no cover - 环境相关
    _np = None


# ══════════════════════════════════════════════════════════════
# 边界规则
# ══════════════════════════════════════════════════════════════
#
# 命中判定统一写成：
#
#     x <= px < x + w   and   y <= py < y + h
#
# 即半开区间 [x, x+w) × [y, y+h)。这么定有三条理由：
#
# 1. 与 Qt 的 QRect.contains() 逐像素一致。Tile 的几何是 QRect，
#    如果这里用闭区间，判定结果会和 Qt 自己的 contains() 在边界上打架，
#    排查时会出现「contains 说在里面、hit 说在外面」。
# 2. 无重叠、无歧义。矩形紧邻时 x1 == 下一个的 x0，
#    那个像素唯一地属于右边那个，不存在「两个都算」或「两个都不算」。
# 3. 逐像素扫描全区域时，面积守恒：命中像素数 == 各矩形面积之和，
#    可以直接当测试断言用（见 tests_blank.py::test_area_conservation）。


def rect_hit(x: int, y: int, w: int, h: int, px: int, py: int) -> bool:
    """单个矩形的半开区间命中判定。这是本模块唯一的语义来源。"""
    return x <= px < x + w and y <= py < y + h


def normalize_rect(item) -> tuple[int, int, int, int] | None:
    """
    把各种形态统一成 (x, y, w, h) 整数四元组。

    接受 4 元组 / list、QRect、带 x()/y()/width()/height() 的对象。
    返回 None 表示这个输入无法解释。
    """
    if isinstance(item, (tuple, list)):
        if len(item) != 4:
            return None
        try:
            return (int(item[0]), int(item[1]), int(item[2]), int(item[3]))
        except (TypeError, ValueError):
            return None
    if isinstance(item, QRect):
        return (item.x(), item.y(), item.width(), item.height())
    if all(hasattr(item, m) for m in ("x", "y", "width", "height")):
        try:
            return (item.x(), item.y(), item.width(), item.height())
        except TypeError:
            return None
    return None


# ══════════════════════════════════════════════════════════════
# 索引实现 —— 语义完全相同，只有复杂度不同
# ══════════════════════════════════════════════════════════════


class _LinearIndex:
    """
    基线：每次命中从头扫到尾，O(n)。grid.py 现在就是这么干的。
    留着不是为了用，是为了基准对比 —— 没有它就说不清「快了多少」。
    """

    name = "linear"

    def __init__(self, rects):
        self.rects = rects

    def hit(self, px: int, py: int) -> int | None:
        # enumerate 天然升序，遇到第一个就返回 = 重叠时取最小下标
        for i, (x, y, w, h) in enumerate(self.rects):
            if x <= px < x + w and y <= py < y + h:
                return i
        return None

    def worst_case(self) -> int:
        """单次查询最多检查几个矩形。见 _BucketIndex.worst_case()。"""
        return len(self.rects)

    def stats(self) -> dict:
        return {"items": len(self.rects)}


class _BisectIndex:
    """
    x 排序 + 二分 + 有界回扫。

    思路：按 x0 排序，对 px 二分找到插入点，然后**向左回扫**，
    扫描上界由「最宽图标的宽度」给出 ——
    x0 <= px - max_w 的图标，其 x1 = x0 + w <= px，绝不可能命中，
    所以回扫到 ``xs[j] <= px - max_w`` 就可以停，扫描量天然有界。

    规则网格（7 列）下每列约 17 个图标，回扫 17~34 项。

    最坏情况是所有图标 x 相同（单列长列表），此时退化成 O(n)，
    所以它不适合当默认；``HitTester`` 会按布局统计量自动选型。
    """

    name = "bisect"

    def __init__(self, rects):
        order = sorted(range(len(rects)), key=lambda i: rects[i][0])
        self.order = order
        self.xs = [rects[i][0] for i in order]
        self.x1 = [rects[i][0] + rects[i][2] for i in order]
        self.y0 = [rects[i][1] for i in order]
        self.y1 = [rects[i][1] + rects[i][3] for i in order]
        self.max_w = max((r[2] for r in rects), default=1)

    def hit(self, px: int, py: int) -> int | None:
        j = bisect_right(self.xs, px) - 1
        floor_x = px - self.max_w
        best = None
        while j >= 0 and self.xs[j] > floor_x:
            if self.x1[j] > px and self.y0[j] <= py < self.y1[j]:
                idx = self.order[j]
                # 回扫顺序是排序后的顺序，不是原始下标顺序，
                # 必须取 min 才能和其它实现的重叠语义一致
                if best is None or idx < best:
                    best = idx
            j -= 1
        return best

    def worst_case(self) -> int:
        # 所有图标 x 相同时退化成 O(n)（单列长列表就是这种）
        return len(self.order)

    def stats(self) -> dict:
        return {"items": len(self.order), "max_w": self.max_w}


class _NumpyIndex:
    """
    向量化：一次算完整张布尔表，再取最小命中下标。

    注意：n=116 时 numpy 的**每次调用开销**（约 1~2µs/次）就已经
    和 116 次纯 Python 比较相当了，要 4 次比较 + 3 次与 + 1 次 flatnonzero，
    反而比线性扫描还慢。n 上千以后才追平。它是候选，不是赢家。
    """

    name = "numpy"

    def __init__(self, rects):
        if _np is None:
            raise RuntimeError("numpy 不可用")
        if rects:
            arr = _np.asarray(rects, dtype=_np.int64)
            self.x0 = _np.ascontiguousarray(arr[:, 0])
            self.x1 = _np.ascontiguousarray(arr[:, 0] + arr[:, 2])
            self.y0 = _np.ascontiguousarray(arr[:, 1])
            self.y1 = _np.ascontiguousarray(arr[:, 1] + arr[:, 3])
        else:
            z = _np.zeros(0, dtype=_np.int64)
            self.x0 = self.x1 = self.y0 = self.y1 = z

    def hit(self, px: int, py: int) -> int | None:
        if self.x0.size == 0:
            return None
        mask = ((self.x0 <= px) & (self.x1 > px)
                & (self.y0 <= py) & (self.y1 > py))
        hits = _np.flatnonzero(mask)
        return int(hits[0]) if hits.size else None

    def worst_case(self) -> int:
        # 向量化：固定 4 次比较，与 n 无关
        return 1

    def stats(self) -> dict:
        return {"items": int(self.x0.size)}


class _BucketIndex:
    """
    均匀桶网格：默认方案。

    把图标包围盒切成 nbx × nby 个近似正方的桶，桶数取 ~n（平均每桶 1 项），
    每个图标登记到它覆盖的所有桶。命中时 O(1) 算出桶号，只比那一桶。

    选型要点：
    - 桶数按包围盒长宽比分配，否则宽屏上桶会又宽又扁、横向挤一堆。
    - 横跨过多桶的巨型图标放进 ``big`` 列表，每次必查。
      这一条是防退化的：没有它，一个 99999px 宽的图标会让 set_layout
      变成 O(桶数 × n)。
    - 命中前先做包围盒范围检查，**不夹取**桶号。夹取会让包围盒外的点
      落到边缘桶上，产生假命中。

    退化场景（所有图标同一位置）退化成 O(n)，但那种布局本来也没有
    「空白」可言；stats() 会报 max_occupancy，HitTester 据此降级。
    """

    name = "bucket"
    MAX_AXIS = 64          # 单轴桶数上限，防止 set_layout 退化
    BIG_SPAN = 24          # 一个图标跨的桶数超过它就进 big

    def __init__(self, rects):
        self.rects = rects
        self.big: list[int] = []
        n = len(rects)
        if n == 0:
            self.buckets: list[list[int]] = []
            self.nbx = self.nby = 1
            self.bx0 = self.by0 = 0
            self.bwpx = self.bhpx = 1
            self.max_occ = 0
            return

        bx0 = min(r[0] for r in rects)
        by0 = min(r[1] for r in rects)
        bx1 = max(r[0] + r[2] for r in rects)
        by1 = max(r[1] + r[3] for r in rects)
        self.bx0, self.by0 = bx0, by0
        self.bwpx = max(1, bx1 - bx0)
        self.bhpx = max(1, by1 - by0)

        # 桶数 ≈ n，并按包围盒长宽比修正成正方桶
        aspect = self.bwpx / self.bhpx
        nbx = int(round((n * aspect) ** 0.5)) or 1
        nbx = max(1, min(self.MAX_AXIS, nbx))
        nby = int(round(n / nbx)) or 1
        nby = max(1, min(self.MAX_AXIS, nby))
        self.nbx, self.nby = nbx, nby

        self.buckets = [[] for _ in range(nbx * nby)]
        for i, (rx, ry, rw, rh) in enumerate(rects):
            # 用 x+w-1（半开区间的最后一个像素）算覆盖范围，
            # 写成 x+w 会多占一格，桶数被白白放大
            i0, i1 = self._bx(rx), self._bx(rx + rw - 1)
            j0, j1 = self._by(ry), self._by(ry + rh - 1)
            if (i1 - i0 + 1) * (j1 - j0 + 1) > self.BIG_SPAN:
                self.big.append(i)
                continue
            for j in range(j0, j1 + 1):
                base = j * nbx
                for ii in range(i0, i1 + 1):
                    self.buckets[base + ii].append(i)

        self.max_occ = max((len(b) for b in self.buckets), default=0)

    def _bx(self, x: int) -> int:
        i = (x - self.bx0) * self.nbx // self.bwpx
        return 0 if i < 0 else self.nbx - 1 if i >= self.nbx else i

    def _by(self, y: int) -> int:
        j = (y - self.by0) * self.nby // self.bhpx
        return 0 if j < 0 else self.nby - 1 if j >= self.nby else j

    def hit(self, px: int, py: int) -> int | None:
        # 先范围检查再算桶号：包围盒外的点必然不在任何图标内
        if not (self.bx0 <= px < self.bx0 + self.bwpx
                and self.by0 <= py < self.by0 + self.bhpx):
            return None
        cand = self.buckets[self._by(py) * self.nbx + self._bx(px)]
        best = None
        for i in cand:
            if rect_hit(*self.rects[i], px, py):
                if best is None or i < best:
                    best = i
        for i in self.big:                     # 巨型图标必须每次查
            if rect_hit(*self.rects[i], px, py):
                if best is None or i < best:
                    best = i
        return best

    def worst_case(self) -> int:
        """
        单次查询最多要检查几个矩形。

        **必须把 big 也算进去**。all-overlap 这种退化布局下所有图标
        都进了 big，桶全空，max_occ 读出来是 0 —— 只看 max_occ 会
        误判成「桶很空，选 bucket」，实测 7.9µs，比 linear 慢 6 倍。
        这个数字是 auto 选型的依据，报错会让降级逻辑整个失效。
        """
        return self.max_occ + len(self.big)

    def stats(self) -> dict:
        return {"items": len(self.rects),
                "buckets": self.nbx * self.nby,
                "nbx": self.nbx, "nby": self.nby,
                "max_occupancy": self.worst_case(),
                "oversized": len(self.big)}


_BACKENDS = {
    "linear": _LinearIndex,
    "bisect": _BisectIndex,
    "numpy": _NumpyIndex,
    "bucket": _BucketIndex,
}


# ══════════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════════


class HitTester:
    """
    空白区命中判定。替换 grid.py 里的 O(n) ``_inside_icons`` 遍历。

    用法::

        ht = HitTester()
        ht.set_layout([(x, y, w, h), ...])     # 排版后调用
        idx = ht.hit(px, py)                    # None = 空白

    resize / 翻页 / 改列数之后**必须**重新 ``set_layout()``。
    ``BlankAreaController`` 已经把这个时机自动化（监听 Resize/Move/Show），
    直接用控制器的话不需要手动调；单独用 HitTester 时请自己在
    ``Grid.relayout()`` 之后调一次。

    backend:
        ``"auto"``（默认）按布局统计量选型，实测规则网格下 ``bucket`` 最快；
        也可以显式指定做对比测试。
    """

    def __init__(self, backend: str = "auto"):
        if backend not in ("auto", *_BACKENDS):
            raise ValueError(f"未知 backend: {backend}")
        self.requested = backend
        self._rects: tuple = ()
        self._index = _LinearIndex(())
        self._backend = "linear"
        self.version = 0          # 每次 set_layout 自增，便于外部判断是否过期

    # ── 布局 ──────────────────────────────────────────────
    def set_layout(self, items) -> None:
        """
        设置图标矩形列表。items 可以是 (x,y,w,h) 四元组、QRect，
        或任何带 x()/y()/width()/height() 的对象。

        零尺寸矩形（未排版的控件）保留在列表里而不是剔除 ——
        剔除会让下标与调用方的控件列表错位，命中结果指错对象。
        宽高为 0 的矩形永不命中，语义正确且下标稳定。
        """
        rects = []
        for it in items:
            r = normalize_rect(it)
            if r is None:
                raise TypeError(f"无法解释的矩形项: {it!r}")
            rects.append((r[0], r[1], max(0, r[2]), max(0, r[3])))
        self._rects = tuple(rects)
        self._index = self._build()
        self.version += 1

    def clear(self) -> None:
        self.set_layout(())

    def _build(self):
        name = self.requested
        if name == "auto":
            name = self._auto_backend()
        cls = _BACKENDS[name]
        if cls is _NumpyIndex and _np is None:
            cls = _BucketIndex
            name = "bucket"
        self._backend = name
        return cls(self._rects)

    def _auto_backend(self) -> str:
        """
        按「单次查询最多要比较几个矩形」选型。

        实测（116 / 1000 图标，规则网格，8 核机器）：
            linear  2.51 / 18.71 µs
            bisect  0.52 /  2.36 µs
            numpy   2.66 /  3.52 µs   ← n=116 时比线性还慢
            bucket  0.49 /  0.49 µs   ← 与 n 无关，O(1)

        numpy 输给线性不是实现问题，是每次数组调用的固定开销（约 2.5µs）
        已经等于扫完 116 个矩形。规模到几百上千才追平。

        选桶的前提是桶里候选少。不规则或重叠布局会把桶塞爆，
        那时桶的常数项就白付了，按 worst_case 降级。
        """
        n = len(self._rects)
        if n == 0:
            return "linear"
        occ = _BucketIndex(self._rects).worst_case()
        if occ <= 16:
            return "bucket"
        # 规模够大时 numpy 的固定开销才划算
        if _np is not None and n >= 256 and occ <= n:
            return "numpy"
        return "linear"

    # ── 命中 ──────────────────────────────────────────────
    def hit(self, px: int, py: int) -> int | None:
        """返回命中的图标下标；None 表示这是空白区域。"""
        return self._index.hit(int(px), int(py))

    def is_blank(self, px: int, py: int) -> bool:
        """True = 空白。这是本模块最常被用到的一个判断。"""
        return self.hit(px, py) is None

    # ── 调试 ──────────────────────────────────────────────
    @property
    def backend(self) -> str:
        return self._backend

    @property
    def rects(self) -> tuple:
        return self._rects

    def __len__(self) -> int:
        return len(self._rects)

    def stats(self) -> dict:
        d = {"requested": self.requested, "active": self._backend}
        d.update(self._index.stats())
        return d

    def describe_hit(self, px: int, py: int) -> str:
        i = self.hit(px, py)
        if i is None:
            return f"blank({px},{py})"
        x, y, w, h = self._rects[i]
        return f"icon#{i}@({x},{y},{w},{h})  点({px},{py})"


# ══════════════════════════════════════════════════════════════
# 从 Tile 控件提取命中矩形
# ══════════════════════════════════════════════════════════════


def tile_hit_rect(tile, label_width: int = 0, bonus: int = 16) -> tuple[int, int, int, int]:
    """
    单个 Tile 的命中矩形（控件坐标 → 网格坐标）。

    = 图标矩形 ∪ 文字矩形（可选），两者的并集按外接矩形给出。

    label_width > 0 时把文字那一行也算成命中。这是必须的：
    点在应用名上如果被判成空白，左键就会把启动器关掉而不是启动应用。

    但文字命中宽度**必须封顶**，否则整个功能失效。实测：
    2560px 宽 / 7 列时单元格恰好 352px，而 Tile 占满单元格，
    于是「长名字的文字」（实测 "Agent Orchestrator" 一类量到 288px）
    比图标（128px）宽出一倍多。文字按真实宽度全收的话，
    相邻两列的文字命中矩形会**直接接上**（实测 x 80~368 与 368~656），
    横向间隙归零 —— 界面看着有缝，点下去却被当成点图标，
    「左键点空白退出」在图标行上完全失效。

    所以横向最多只比图标宽出 bonus*2。文字居中在图标下方，
    超出这个范围的点视觉上已经属于邻列的图标了。
    封顶后实测每列之间仍有约 190px 可点空白。
    bonus=0 表示文字命中宽度不超过图标宽度。
    """
    tx, ty = tile.x(), tile.y()
    getter = getattr(tile, "_icon_rect", None)
    r = getter() if callable(getter) else getattr(tile, "rect", lambda: None)()
    if r is None:
        r = QRect(tx, ty, tile.width(), tile.height())
        return (r.x(), r.y(), max(0, r.width()), max(0, r.height()))

    x = tx + r.x()
    y = ty + r.y()
    w = max(0, r.width())
    h = max(0, r.height())

    if label_width > 0 and w > 0:
        cx = x + w // 2                       # 文字以图标中心对齐
        lw = min(label_width, w + 2 * max(0, int(bonus)))
        lw = max(1, min(lw, max(1, tile.width())))
        x = min(x, cx - lw // 2)
        w = max(w, lw)
        ly = _label_top(tile, y, h)
        h = max(h, ly + _label_height() - y)
    return (x, y, w, h)


def _label_top(tile, icon_y: int, icon_h: int) -> int:
    """文字区域顶边（网格坐标）。复刻 tile.py 的 _label_rect 偏移。"""
    from . import theme
    return tile.y() + icon_h + theme.Theme.ICON_GAP - 6


def _label_height() -> int:
    from . import theme
    return theme.Theme.LABEL_H


def tile_label_width(tile, lines: int = 1) -> int:
    """
    量出 tile 实际会画出来的文字像素宽度（已省略号截断）。

    优先读 tile 的 ``_cached_elided`` / ``_name``：tile.py 里明确写了
    这两个字段是留给空白区命中用的（"blankarea 等外部代码会读"），
    直接用它的结果而不是在这里复刻一遍 elide —— 两边各算一次，
    改一边就会不一致，而不一致的表现是「点名字偶尔被当成空白」。

    取不到时退回自己算；再取不到就返回 0，表示只判图标。
    刻意不退回「控件整幅宽度」：那样纵向间隙会塌成 0，整张网格
    都会变成不是空白，空白区交互直接失效。
    """
    from . import theme
    try:
        text = getattr(tile, "_cached_elided", None) or getattr(tile, "_name", None)
        if not text:
            entry = tile.entry
            # label 是显示的唯一真相（自定义名优先）。用 getattr 兜一层是因为
            # 这里接受鸭子类型：测试和 bench 会传只有 name 的假条目，
            # 真实 Entry 一定有 label。
            #
            # 注意这两行**必须在 if 里面**：`entry` 只在该分支里赋值。
            # 写到 if 外面就会在 text 非空时等着用 `entry` 而能谈上来的
            # 名字 -> NameError -> 被下面的 `except Exception: return 0` 吞掉。
            # 后果是标签行宽度量出来 0 -> 整行被当成空白区 ->
            # 点应用名会关掉启动器。这个 bug 是被 tests_blank 拓出来的。
            _lbl = getattr(entry, "label", None) or getattr(entry, "name", "")
            text = _lbl + ("  ⚠" if getattr(entry, "missing", False) else "")
        font = theme.label_font()
        shown = theme.elide(text, font, tile.width())
        from PyQt5.QtGui import QFontMetrics
        w = max(QFontMetrics(font).horizontalAdvance(ln)
                for ln in shown.split("\n"))
    except Exception:
        return 0
    return int(w) if lines >= 1 else 0


def icon_rects(tiles, include_labels: bool = True, lines: int = 1,
               bonus: int = 16) -> list:
    """
    从 Tile 列表提取命中矩形列表，**下标与 tiles 一一对应**。

    下标必须对齐：HitTester 返回的下标要能被直接用来 ``tiles[i]``。
    bonus 是文字命中宽度相对图标的横向富余（见 tile_hit_rect）。
    """
    out = []
    for t in tiles:
        lw = tile_label_width(t, lines) if include_labels else 0
        out.append(tile_hit_rect(t, lw, bonus))
    return out


# ══════════════════════════════════════════════════════════════
# 右键菜单：位置钳制 + 深色样式
# ══════════════════════════════════════════════════════════════


def screen_rect_at(global_pos: QPoint) -> QRect:
    """
    全局坐标所在的屏幕可用区（已扣掉任务栏）。

    没有 QGuiApplication.screenAt（Qt 5.10 以前）就退回主屏幕。
    """
    app = QApplication.instance()
    if app is None:
        return QRect(0, 0, 1920, 1080)
    screen = QApplication.screenAt(global_pos)
    if screen is None:
        screen = app.primaryScreen()
    return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)


def clamp_to_screen(pos: QPoint, size, margin: int = 6,
                    screen: QRect | None = None) -> QPoint:
    """
    把菜单左上角钳制回屏幕内。

    必须做：启动器是全屏置顶窗口，右下角往右下弹菜单会**整块跑到屏幕外**，
    而屏幕外那块区域的点击会落到别的应用上 —— 这是真实的用户可见故障，
    不是理论风险。

    size 可以是 QSize 或 (w, h)。顺序是「先夹到屏幕内，再回退」：
    菜单比屏幕还大时优先保证菜单起点在屏幕内。
    """
    w = size.width() if hasattr(size, "width") else int(size[0])
    h = size.height() if hasattr(size, "height") else int(size[1])
    r = screen if screen is not None else screen_rect_at(pos)
    x = min(max(r.left() + margin, pos.x()), r.right() - margin - w + 1)
    y = min(max(r.top() + margin, pos.y()), r.bottom() - margin - h + 1)
    # 菜单大于可用区时退到边距处，宁可溢出也不要全跑到屏幕外
    x = max(x, r.left() + margin)
    y = max(y, r.top() + margin)
    return QPoint(int(x), int(y))


MENU_QSS = """
QMenu{background:#14161c;color:#eef0f4;border:1px solid #2a2e38;
     border-radius:10px;padding:6px;font-size:13px;}
QMenu::item{padding:8px 26px;border-radius:6px;}
QMenu::item:selected{background:#2b6cb0;color:#fff;}
QMenu::item:disabled{color:#5a606e;}
QMenu::separator{height:1px;background:#2a2e38;margin:5px 8px;}
"""

# (动作 key, 显示文本)。key 与信号一一对应，测试按 key 断言。
#
# **顺序按可发现性排，不按重要性排。** 「添加应用」是用户最常找的动作，
# 原来它排在设置后面、而且整条右键链路根本没接上（show_menu 零调用点，
# 右键直接 emit settings_requested 打开设置窗口）—— 表现是
# 「添加功能我根本没找到」。所以它排第一。
MENU_ITEMS = (
    ("add", "添加应用…"),
    ("reload", "重新载入图标"),
    ("sep1", None),
    ("settings", "设置…"),
    ("about", "关于 Launchpad"),
)

#: 图标（Tile）上的右键菜单。key 与信号一一对应。
#:
#: 「重命名」排在「从启动器移除」**前面** —— 它是不破坏性的、常用的；
#: 移除会写墓碑、有墓碑就再也回不来（只能手改 dismissed.json）。
#: 菜单里把不可逆的那项放后面，是让人右手肌肉记忆里的默认位置落在安全项上。
TILE_MENU_ITEMS = (
    ("rename", "重命名…"),
    ("delete", "从启动器移除"),
    ("sep1", None),
    ("add", "添加应用…"),
)

#: 菜单标题里应用名的显示上限。超了用省略号截断。
TILE_TITLE_MAX = 28


def _tile_title(name: str) -> str:
    """
    图标菜单标题里显示的应用名。

    单个应用的名字可能很长（实测最长的有 30 多字），而菜单宽度跟着
    标题走 —— 不截断的话在屏幕边缘右键，菜单会有一半跑到屏幕外。

    保留**开头**而不是结尾：用户是看着图标找过来的，开头才是他认得出的
    部分。
    """
    name = name or ""
    if len(name) <= TILE_TITLE_MAX:
        return name
    return name[:TILE_TITLE_MAX - 1] + "…"


def build_blank_menu(parent=None) -> tuple[QMenu, dict]:
    """
    构造右键菜单，返回 (菜单, {key: QAction})。

    自定义样式是必要的：项目是纯黑深色主题，系统 QMenu 默认浅灰底 +
    蓝色标题栏，弹出来和整个界面割裂。配色沿用 grid.py 已有的
    ``#14161c / #eef0f4 / #2a2e38 / #2b6cb0``。
    """
    m = QMenu(parent)
    m.setStyleSheet(MENU_QSS)
    acts: dict[str, QAction] = {}
    for key, text in MENU_ITEMS:
        if key.startswith("sep"):
            m.addSeparator()
            continue
        a = m.addAction(text)
        acts[key] = a
    return m, acts


def build_tile_menu(parent=None, name: str = "") -> tuple[QMenu, dict]:
    """
    构造图标上的右键菜单，返回 (菜单, {key: QAction})。

    菜单标题里带上应用名：删图标是不可逆操作（相对启动器而言），
    弹出一排一模一样的「从启动器移除」时用户必须知道自己正在删哪一个。
    名字长了按 ``_tile_title`` 截断 —— 菜单宽度跟着标题走，
    不截断的话在屏幕边缘右键会有一半跑到屏幕外。
    """
    m = QMenu(parent)
    m.setStyleSheet(MENU_QSS)
    label = _tile_title(name)
    if label:
        title = m.addAction(label)
        title.setEnabled(False)          # 灰字：只是标题，不是动作
    acts: dict[str, QAction] = {}
    for key, text in TILE_MENU_ITEMS:
        if key.startswith("sep"):
            m.addSeparator()
            continue
        a = m.addAction(text)
        acts[key] = a
    return m, acts


# ══════════════════════════════════════════════════════════════
# 控制器：把命中判定接进 QWidget 事件流
# ══════════════════════════════════════════════════════════════


class BlankAreaController(QObject):
    """
    空白区域交互控制器。挂到 Grid 上即可，**不需要改 grid.py**。

    职责：
    1. 维护 HitTester，并在排版变化后自动重建（Resize/Move/Show 事件）
    2. 左键空白 → ``clicked_outside``；右键空白 → ``settings_requested``
    3. 图标本身的点击**不产生**任何空白信号（「跟图标无关」）
    4. 点图标悬停手型、点空白箭头型，用光标把「哪里能点」画出来

    为什么必须在 Tile 上也装事件过滤器：
    Tile 是子控件，鼠标点它的事件**不会冒泡**给 Grid。
    而 Tile 控件矩形 352x184 远大于图标 128x128，
    点在「控件内、图标外」的那一圈只会到 Tile，永远到不了 Grid。
    只在 Grid 上装过滤器 = 这块区域既不发射也不识别，按空白处理不了。
    在 Tile 上装之后，按图标外的部分被判成空白并转发给主流程，
    同时吃掉事件，Tile 不会把它当成按下图标而亮起底板。

    接线见模块末尾 INTEGRATION 注释。
    """

    clicked_outside = pyqtSignal()          # 左键空白 → 收起（返回后台）
    settings_requested = pyqtSignal()       # 右键空白 → 打开设置
    blank_pressed = pyqtSignal(int, int)    # 诊断：空白处的网格坐标
    icon_pressed = pyqtSignal(int)          # 诊断：命中的图标下标
    add_requested = pyqtSignal()
    reload_requested = pyqtSignal()
    about_requested = pyqtSignal()
    #: 右键点到某个图标 → 要删它。载荷是那个 Entry 对象。
    delete_requested = pyqtSignal(object)
    #: 右键点到某个图标 → 要给它改名。载荷是那个 Entry 对象。
    #: 与 delete 分成两个信号而不是共用一个带参数的：两种动作的
    #: **后果严重程度不同**（改名不破坏、移除会写墓碑），共用一个信号再
    #: 靠载荷猜意图的话，加新动作时很容易把不可逆的那个挂错。
    rename_requested = pyqtSignal(object)

    # 排版变化时置位；hit 前惰性重建，避免每次点击都重算
    _LAYOUT_EVENTS = (QEvent.Resize, QEvent.Move, QEvent.Show,
                      QEvent.Hide, QEvent.LayoutRequest, QEvent.ParentChange)

    def __init__(self, parent: QWidget | None = None,
                 click_through_empty: bool = True,
                 hit_labels: bool = True, label_lines: int = 1,
                 label_bonus: int = 16, backend: str = "auto"):
        super().__init__(parent)
        self.hit = HitTester(backend)
        self.click_through_empty = bool(click_through_empty)
        self.hit_labels = bool(hit_labels)
        self.label_lines = max(1, int(label_lines))
        self.label_bonus = max(0, int(label_bonus))
        self._host: QWidget | None = None
        self._tiles_provider = None
        self._dirty = True
        self._armed = False
        self._last_menu: QMenu | None = None
        self._cursor_over_icon = False
        # 弹菜单的方式。默认是模态 exec_（生产路径要的就是这个：菜单
        # 开着的时候应用不应该继续响应）。
        #
        # 做成可替换是为了测试：`menu.exec_()` 会起一个**嵌套事件循环**，
        # 阻塞到用户点某一项为止。在无头测试里没人会点，进程就永久挂住
        # （实测 tests_blank 直接卡死、退出码 -1073740791）。
        # 测试注入一个立即返回 None 的替身，于是「点了右键会走到弹菜单
        # 这一步」照样能验，只是不会真等。
        #
        # 用**实例属性**而不是子类或全局开关：它是每个控制器自己的行为，
        # 放实例上不会在测试之间互相污染，也不会影响生产实例。
        self._menu_exec = self._exec_modal

    # ── 挂载 ──────────────────────────────────────────────
    def install_on(self, host: QWidget, tiles_provider) -> "BlankAreaController":
        """
        挂到 host（通常是 Grid），tiles_provider 是返回当前 Tile 列表的
        可调用对象 —— 用可调用而不是直接传 list，是因为 Grid.rebuild()
        会**整体替换** ``_tiles``，持有旧 list 会拿到已销毁的控件。
        """
        self._host = host
        self._tiles_provider = tiles_provider
        host.installEventFilter(self)
        self._retile()                       # 给现存 tile 也装上
        self.sync()
        self._armed = True
        return self

    def uninstall(self) -> None:
        for t in self._tiles():
            if t is not None:
                t.removeEventFilter(self)
        if self._host is not None:
            self._host.removeEventFilter(self)
        self._armed = False

    def _tiles(self) -> list:
        if self._tiles_provider is None:
            return []
        try:
            return [t for t in (self._tiles_provider() or []) if t is not None]
        except Exception:
            return []

    # ── 索引维护 ──────────────────────────────────────────
    def sync(self) -> None:
        """重建索引。排版变化、resize、设置改动后调用。"""
        self.hit.set_layout(icon_rects(self._tiles(), self.hit_labels,
                                        self.label_lines, self.label_bonus))
        self._dirty = False

    def mark_dirty(self) -> None:
        self._dirty = True

    def _ensure_fresh(self) -> None:
        if self._dirty:
            self.sync()

    def _retile(self) -> None:
        """给新出现的 tile 装过滤器。"""
        for t in self._tiles():
            if t is not None:
                t.installEventFilter(self)

    def refresh(self) -> None:
        """公开的重排完成钩子：Grid.relayout() 之后调它即可，不必等事件。"""
        self._retile()
        self.sync()

    def update_settings(self, click_through_empty=None, label_lines=None,
                        hit_labels=None, label_bonus=None) -> None:
        """设置窗口改动后同步过来。"""
        changed = False
        if click_through_empty is not None:
            v = bool(click_through_empty)
            changed |= v != self.click_through_empty
            self.click_through_empty = v
        if label_lines is not None:
            v = max(1, int(label_lines))
            changed |= v != self.label_lines
            self.label_lines = v
        if hit_labels is not None:
            v = bool(hit_labels)
            changed |= v != self.hit_labels
            self.hit_labels = v
        if label_bonus is not None:
            v = max(0, int(label_bonus))
            changed |= v != self.label_bonus
            self.label_bonus = v
        if changed:
            self.mark_dirty()

    # ── 命中 ──────────────────────────────────────────────
    def hit_test(self, px: int, py: int) -> int | None:
        """公开的命中查询（网格坐标）。"""
        self._ensure_fresh()
        return self.hit.hit(px, py)

    def is_blank_at(self, px: int, py: int) -> bool:
        return self.hit_test(px, py) is None

    def blank_pixel_ratio(self, step: int = 8) -> float:
        """
        按 step 像素网格采样，返回空白占比（0.0 ~ 1.0）。

        这是判断「空白区还有没有」的回归闸。不能用命中矩形面积之和
        除以网格面积来算 —— 矩形之间会重叠（长名字的文字会盖到邻列），
        那样的和可以超过网格面积，算出负的空白占比（实测 -19.4%）。

        比值塌到接近 0 就说明命中矩形把间隙吃光了，整个「点空白退出」
        处于失效状态但界面上看不出任何异常。
        """
        if self._host is None:
            return 0.0
        self._ensure_fresh()
        w, h = self._host.width(), self._host.height()
        step = max(1, int(step))
        blank = total = 0
        for py in range(0, h, step):
            for px in range(0, w, step):
                total += 1
                if self.hit.hit(px, py) is None:
                    blank += 1
        return blank / total if total else 0.0

    def blank_area_count(self) -> int:
        """
        命中矩形的面积之和（**可能超过网格面积**，因为矩形之间会重叠）。

        仅作诊断参考；要判断空白占比请用 blank_pixel_ratio()。
        """
        self._ensure_fresh()
        return sum(w * h for _x, _y, w, h in self.hit.rects if w and h)

    # ── 事件过滤 ──────────────────────────────────────────
    def eventFilter(self, obj, ev):
        et = ev.type()

        if et == QEvent.ChildAdded:
            # Grid.rebuild() 会 new 出新 Tile；子控件加入时补装过滤器。
            # 不靠这个也能工作（_ensure_fresh 会重算矩形），
            # 但补装能让 tile 上的点击当场被识别，不用等索引刷新
            child = getattr(ev, "child", None)
            if child is not None and self._is_tile(child):
                child.installEventFilter(self)
        elif et in self._LAYOUT_EVENTS:
            self._dirty = True
            if et == QEvent.ChildAdded or et in (QEvent.Show,):
                pass
        elif et == QEvent.MouseButtonPress and self._armed:
            if self._handle_press(ev, obj):
                return True
        elif et == QEvent.MouseMove and self._armed:
            if obj is self._host:
                self._update_cursor(ev.pos())
        return super().eventFilter(obj, ev)

    def _is_tile(self, obj) -> bool:
        """是不是一个应用图标控件（而不是页码点之类）。"""
        from .tile import Tile
        return isinstance(obj, Tile)

    def _update_cursor(self, pos: QPoint) -> None:
        """空白 = 箭头，图标 = 手型。光标就是最直白的「哪里能点」。"""
        self._ensure_fresh()
        over = self.hit.hit(pos.x(), pos.y()) is not None
        if over == self._cursor_over_icon:
            return
        self._cursor_over_icon = over
        if self._host is not None:
            self._host.setCursor(
                QCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor))

    def _handle_press(self, ev, obj) -> bool:
        """
        处理一次按下。返回 True 表示吃掉事件。

        点击位置一律换算到 host 坐标，这样 tile 上的空白点击和
        host 上的空白点击走完全相同的分支 —— 这正是「跟图标无关」的
        实现方式：空白判定只看坐标和索引，不看事件来自哪个控件。
        """
        btn = ev.button()
        if btn == Qt.MidButton:
            return False

        if obj is not self._host and self._is_tile(obj):
            local = self._host.mapFromGlobal(obj.mapToGlobal(ev.pos()))
        else:
            local = ev.pos()

        idx = None
        if obj is not self._host and self._is_tile(obj):
            self._ensure_fresh()
            idx = self.hit.hit(local.x(), local.y())
            if idx is not None:
                if btn == Qt.RightButton:
                    # 右键点到图标上：弹图标菜单（删除）。
                    #
                    # 左键碰到图标一律返回 False 让 Tile 自己处理，
                    # 绝不发空白信号；右键没有 Tile 的默认行为，可以在这里
                    # 截下来。位置用光标的全局坐标 —— Tile 控件矩形
                    # (352x184) 比图标本身大得多，菜单贴着事件坐标弹会偏移。
                    self._show_tile_menu_at(idx, QCursor.pos())
                    return True
                # 图标本身：让 Tile 自己处理，绝不发空白信号
                return False
        else:
            idx = self.hit_test(local.x(), local.y())
            if idx is not None:
                return False

        x, y = local.x(), local.y()
        if btn == Qt.RightButton:
            self.blank_pressed.emit(x, y)
            # 右键空白 → **弹菜单**，不再直接打开设置窗口。
            #
            # 原来这里是 `self.settings_requested.emit()`，于是
            # MENU_ITEMS 里那五项（含「添加应用」）整条链路都是死代码：
            # show_menu() 没有任何调用点，add_requested 也就永远不会发。
            # 表现就是「添加功能我找不到」—— 功能写了但用户碰不到。
            self.show_menu(QCursor.pos())
        elif btn == Qt.LeftButton:
            if not self.click_through_empty:
                # 开关关掉时不收起，但**仍然吃掉事件** ——
                # 否则这个点击会落到 Qt 事件链的下一站
                self.blank_pressed.emit(x, y)
                return True
            self.blank_pressed.emit(x, y)
            self.clicked_outside.emit()
        else:
            return False
        return True                    # 吃掉：绝不透传给下层窗口

    # ── 给 grid.mousePressEvent 用的入口 ──────────────────
    def handle_press(self, ev) -> bool:
        """
        直接从 ``Grid.mousePressEvent`` 里调用（可选的接线方式）。

        已经装了事件过滤器的话就不需要这行；保留它是为了让
        「在 grid.mousePressEvent 里改两行」这种最小改动方案可用。
        """
        if ev.button() == Qt.RightButton:
            # 与事件过滤器那条路径保持一致：右键一律弹菜单。
            # 两条路径的右键行为必须相同，否则会出现「有时弹菜单、
            # 有时直接开设置窗口」这种没法复现的分裂行为。
            self.show_menu(QCursor.pos())
            ev.accept()
            return True
        if ev.button() != Qt.LeftButton:
            return False
        self._ensure_fresh()
        if self.hit.hit(ev.pos().x(), ev.pos().y()) is not None:
            return False              # 点到图标，不关
        ev.accept()
        if self.click_through_empty:
            self.clicked_outside.emit()
        return True

    # ── 菜单 ──────────────────────────────────────────────
    def _exec_modal(self, menu: QMenu, pos: QPoint):
        """生产路径的弹菜单方式：模态，阻塞到用户选完。"""
        return menu.exec_(pos)

    def set_menu_exec(self, fn) -> None:
        """
        替换弹菜单的方式（测试用）。传 None 恢复默认模态。

        见 ``__init__`` 里 ``_menu_exec`` 的注释：默认的模态 exec_ 在
        无头测试里会永久阻塞。
        """
        self._menu_exec = self._exec_modal if fn is None else fn

    def _pick_action(self, acts: dict, menu: QMenu, pos: QPoint) -> str | None:
        """
        弹菜单并把选中的 QAction 映射回 key。

        ``self._last_menu`` 必须在这里持有引用 —— 菜单是个 QWidget，
        局部变量一返回就可能被 PyQt 的垃圾回收销毁，
        症状是菜单刚弹出就消失（不是崩溃，是静默失效）。
        """
        self._last_menu = menu
        chosen = self._menu_exec(menu, pos)
        if chosen is None:
            return None
        for key, a in acts.items():
            if a is chosen:
                return key
        return None

    def show_menu(self, global_pos: QPoint) -> str | None:
        """
        在点击位置附近弹出菜单，返回被选中的动作 key（或 None）。

        位置经 ``clamp_to_screen`` 钳制，菜单不会跑到屏幕外。
        """
        menu, acts = build_blank_menu(self._host)
        pos = clamp_to_screen(global_pos, menu.sizeHint())
        key = self._pick_action(acts, menu, pos)
        if key is not None:
            self._dispatch(key)
        return key

    def _entry_at(self, index: int):
        """
        第 ``index`` 个命中图标对应的 Entry，取不到就返回 None。

        ``index`` 是 HitTester 的下标，与 ``self._tiles()`` 同一顺序
        （``sync()`` 就是拿 ``icon_rects(self._tiles(), ...)`` 建的索引）。
        越界返回 None 而不是抛 —— 索引可能在下一次 rebuild 之后过期，
        而调用点在事件过滤器里，抛出去会被 Qt 当成未处理异常。
        """
        tiles = self._tiles()
        if not (0 <= index < len(tiles)):
            return None
        return getattr(tiles[index], "entry", None)

    def _dispatch_tile(self, key: str, entry) -> None:
        """图标菜单的动作分发。独立成方法是为了能被测试直接调用 ——
        ``_show_tile_menu_at`` 里那个 ``menu.exec_()`` 是模态阻塞的。"""
        if key == "rename":
            self.rename_requested.emit(entry)
        elif key == "delete":
            self.delete_requested.emit(entry)
        elif key == "add":
            self.add_requested.emit()

    def _show_tile_menu_at(self, index: int, global_pos: QPoint) -> str | None:
        """弹出第 ``index`` 个图标的右键菜单，返回选中的 key（或 None）。"""
        entry = self._entry_at(index)
        if entry is None:
            return None
        # 菜单标题用 label（自定义名优先）而不是 name —— 用户刚改完名，
        # 右键菜单里还写着旧名会很别扭。
        name = getattr(entry, "label", "") or getattr(entry, "name", "") or ""
        menu, acts = build_tile_menu(self._host, name)
        pos = clamp_to_screen(global_pos, menu.sizeHint())
        key = self._pick_action(acts, menu, pos)
        if key is not None:
            self._dispatch_tile(key, entry)
        return key

    def _dispatch(self, key: str) -> None:
        if key == "settings":
            self.settings_requested.emit()
        elif key == "add":
            self.add_requested.emit()
        elif key == "reload":
            self.reload_requested.emit()
        elif key == "about":
            self.about_requested.emit()

    # ── 诊断 ──────────────────────────────────────────────
    def stats(self) -> dict:
        self._ensure_fresh()
        d = {"tiles": len(self.hit),
             "click_through_empty": self.click_through_empty,
             "hit_labels": self.hit_labels,
             "label_lines": self.label_lines}
        d.update(self.hit.stats())
        return d


# ══════════════════════════════════════════════════════════════
# 基准测试
# ══════════════════════════════════════════════════════════════


def _grid_rects(n: int, cols: int, rows: int, cell_w: int, cell_h: int,
                icon: int, icon_h: int, label: int) -> list:
    """构造规则网格的图标矩形，用于基准测试。跨页，纵向按页高堆叠。"""
    out = []
    per = cols * rows
    page_h = rows * cell_h
    for i in range(n):
        page, slot = divmod(i, per)
        row, col = divmod(slot, cols)
        cx = col * cell_w + (cell_w - icon) // 2
        cy = page * page_h + row * cell_h + (cell_h - icon_h) // 2
        out.append((cx, cy, icon, icon_h + label))
    return out


def bench(items, hits=None, repeat: int = 20000) -> dict:
    """
    测量各索引的构建时间与单次命中耗时。

    hits 是要探测的坐标列表；不给就用均匀网格采样，
    保证样本里既有命中也有空白（只测空白会低估 numpy 的开销）。
    """
    rects = [tuple(map(int, r)) for r in items]
    if not hits:
        xs = [r[0] for r in rects]
        ys = [r[1] for r in rects]
        span_x = max(r[0] + r[2] for r in rects) - min(xs) + 40
        span_y = max(r[1] + r[3] for r in rects) - min(ys) + 40
        hits = []
        for k in range(24):
            for j in range(24):
                px = min(xs) + span_x * k // 23
                py = min(ys) + span_y * j // 23
                hits.append((px, py))
        # 一半样本强制落在图标中心，排除「全是空白」的空跑
        for i in range(0, len(rects), 2):
            x, y, w, h = rects[i]
            hits.append((x + w // 2, y + h // 2))

    results = {}
    for name, cls in _BACKENDS.items():
        if cls is _NumpyIndex and _np is None:
            continue
        t0 = time.perf_counter()
        idx = cls(rects)
        build_ms = (time.perf_counter() - t0) * 1000.0

        best = None
        for _ in range(3):
            t0 = time.perf_counter()
            for px, py in hits:
                idx.hit(px, py)
            per = (time.perf_counter() - t0) / len(hits)
            best = per if best is None else min(best, per)
        row = {"build_ms": round(build_ms, 4),
               "hit_us": round(best * 1e6, 4)}
        row.update(idx.stats())
        results[name] = row
    return results


# ══════════════════════════════════════════════════════════════
# 集成说明
# ══════════════════════════════════════════════════════════════
#
# grid.py（只改这 4 处，window.py 改 3 处）：
#
#   # __init__ 末尾
#   from .blankarea import BlankAreaController
#   self._blank = BlankAreaController(self, click_through_empty=settings.get("click_through_empty"))
#   self._blank.install_on(self, lambda: self._tiles)      # ← 关键：传 callable
#   self._blank.clicked_outside.connect(self.closed_requested.emit)  # 注意：grid.py 现有信号叫 closed_requested
#   self._blank.settings_requested.connect(self._on_settings_menu)
#   self._blank.add_requested.connect(self.add_requested.emit)
#   self._blank.reload_requested.connect(self._on_reload_icons)
#
#   # mousePressEvent 整体替换（原来那个 O(n) 遍历删掉）
#   def mousePressEvent(self, ev):
#       self._blank.handle_press(ev)
#
#   # relayout() 末尾可选；不写也能靠 Resize/Move 事件自动同步
#   def relayout(self):
#       ...
#       self._blank.refresh()
#
# window.py：
#   self.grid.settings_requested.connect(self._open_settings)
#
#   def _open_settings(self):
#       from .settingswin import open_settings
#       open_settings(self._settings, parent=self, on_apply=self._apply_settings)
#
#   def _apply_settings(self, changed: dict):
#       if {"columns", "rows", "icon_size", "label_lines"} & changed.keys():
#           self.grid.set_geometry_cols_rows(...)   # 由布局 agent 提供
#           self.grid.relayout()
#       self.grid._blank.update_settings(
#           click_through_empty=self._settings.get("click_through_empty"),
#           label_lines=self._settings.get("label_lines"))
#       self.grid._blank.refresh()
