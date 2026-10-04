# -*- coding: utf-8 -*-
"""
应用网格：分页、排列、翻页动画、搜索过滤、空白区交互。

## 设计要点（每条都是踩过坑之后定下来的）

**排布不写死间距**，一律走 `layouts.compute_layout()`：它按可用空间计算
单元格，是纯函数（同样输入永远同样输出），且三种排列方式共用同一套
分页逻辑，不会出现「grid 模式不漏、flow 模式漏一个」的分裂。

**页间距恒等于 `page_height`**（实测 grid/flow/compact 三种模式的页原点
间距都是 1422 = 页高）。注意 flow 是**页内**垂直居中，所以它第 0 页的
首图标 y=251 而不是 56，但页原点仍在 `page * page_height`。因此翻页
offset 沿用 `-index * page_height` 就对，不需要为每种模式写一套偏移。

**翻页用 `animation.PageAnimator`** 而不是裸 `QVariantAnimation`：
旧实现每帧 `int(v)` 截断成整像素，320ms 的动画只有 19 个不同的取值，
视觉上是「跳」过去的。新实现全程 float。

**空白区命中用 `blankarea.BlankAreaController`**（均匀桶网格，实测
0.6µs/次），不用 O(n) 遍历控件矩形 —— 控件是 352×184 铺满整个单元格，
按控件矩形判定等于把 62% 的屏幕都算成「点了图标」。
"""

import math
import time

from PyQt5.QtCore import QEvent, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeyEvent
from PyQt5.QtWidgets import QWidget

from . import theme
from .animation import PageAnimator
from .blankarea import BlankAreaController
from .icons import IconCache
from .layouts import compute_layout
from .library import Entry, Library
from .settings import Settings
from .tile import Tile


class Grid(QWidget):
    # 停手多久后清零触控板的像素累加器（ms）
    WHEEL_RESET_MS = 250
    #: 一串密集滚轮事件之间的「静默间隔」（ms）。小于它就算同一次滚动手势。
    #:
    #: 用户报「轻轻一划直接到底了，中间全跳过去」。实测：3 个滚轮事件
    #: （间隔 16ms）挤在 24 帧动画里，内容**连续滑过 3 页**，中间两页
    #: 各只停留不到 1 帧 —— 肉眼只看到「一滑到底」。
    #:
    #: 根因是「一个滚轮事件 = 翻一页」在高频设备上等于「轻轻一划翻很多页」。
    #: 修法不是改回按 delta 累加（那会让高精度轮划不动，见 wheelEvent 的
    #: 注释），而是**手势节流**：一次滚动手势内不管来多少事件，
    #: 只累计出**一页**的方向；停手超过 WHEEL_GESTURE_MS 才算下一次手势。
    #: 这样「轻轻一划」= 一页，与设备发多少事件无关。
    WHEEL_GESTURE_MS = 220

    page_changed = pyqtSignal(int)       # 当前页（动画结束时发）
    #: **目标页**（用户已经要求的页，动画一开始就发）。
    #:
    #: 为什么必须有它：滚轮连续翻 3 页时，三次 `goto` 各自起一段动画，
    #: `page_changed` 只在**最后一段结束**时才发一次 —— 中间两页的信号
    #: 根本没有发出机会。实测 24 帧里 `dots._current` 恒为 0，
    #: 而 `_page_target` 已经是 3、动画页位从 0 一路走到 -3：
    #: **圆点从头到尾没动**，直到一切停稳才跳到末页（用户报「标志跟不上」）。
    #:
    #: 页码指示器要跟的是**用户要求的页**，不是「动画此刻停在哪」。
    #: 语义与 `PageDots.mousePressEvent` 的乐观更新一致（见 pagedots.py
    #: 模块 docstring 第 1 条），权威值仍由 `page_changed` 在动画结束时纠正。
    page_intent = pyqtSignal(int)
    pages_changed = pyqtSignal(int)      # 总页数（重建后）
    launch_requested = pyqtSignal(object)
    closed_requested = pyqtSignal()      # 左键点空白 → 收起
    add_requested = pyqtSignal()         # 「添加应用」
    settings_requested = pyqtSignal()    # 右键点空白 → 设置
    reload_requested = pyqtSignal()      # 「重新载入图标」
    about_requested = pyqtSignal()       # 「关于 Launchpad」
    #: 右键点到图标 → 要把它从启动器移除。载荷是那个 Entry。
    delete_requested = pyqtSignal(object)

    def __init__(self, library: Library, icons: IconCache,
                 columns: int = 7, rows: int = 5, parent=None,
                 settings: Settings | None = None):
        super().__init__(parent)
        self.library = library
        self.icons = icons

        # 传了 settings 就以它为准（生产路径：__main__ 读用户配置）；
        # 没传就用位置参数初始化一份，测试里 `Grid(lib, icons, 3, 5)`
        # 才是 3×5 —— 否则 settings 的默认值 7 会反过来盖掉显式参数。
        if settings is not None:
            self.settings = settings
            self.cols = max(1, int(settings.get("columns")))
            self.rows = max(1, int(settings.get("rows")))
        else:
            self.settings = Settings()
            self.settings.set("columns", columns)
            self.settings.set("rows", rows)
            self.cols = max(1, columns)
            self.rows = max(1, rows)

        self._entries: list[Entry] = []      # 当前过滤结果（原始顺序）
        self._tiles: list[Tile] = []         # 与 _entries 一一对应
        self._page = 0
        self._pages = 1
        self._offset = 0.0                   # 当前滚动偏移（负值 = 向后翻）
        self._drag_origin_x = 0        # 拖拽起点（光标 X），算相对位移用
        # 用户**已经要求的**页，立刻更新，不等动画播完。
        #
        # `_page` 是滞后量：动画没结束它就还是旧页。滚轮事件密集到达时
        # （实测每 8ms 一个滚轮刻度）每次都读到同一个旧页码，于是
        # 20 次滚轮全部朝同一页去 —— 用力滑一整屏只翻 1 页就卡住。
        # 动画结束（_finish_anim）时再把 _page 追上来。
        self._page_target = 0
        # 上次已发过 page_intent 的页。goto() 只在目标页**变化**时发信号，
        # 否则连点同一页会重复发，而重复发会让圆点形变动画反复重启。
        self._page_intent_emitted = 0
        self._wheel_accum = 0.0          # 触控板像素累加（见 wheelEvent）
        self._wheel_timer: QTimer | None = None
        # 滚轮手势节流：本次手势已消费的方向（0 = 还没消费），
        # 以及距上一个滚轮事件的时间。见 class 里的 WHEEL_GESTURE_MS。
        self._wheel_gesture = 0
        self._wheel_last_ms = 0.0
        self._relayouting = False

        self._search = ""
        # 上次真正**构建**出来的搜索词。用来判断 set_search 能不能跳过
        # 重建（见该方法注释）。None = 还没建过，必须建。
        self._last_built_query = None
        self._message = ""
        # 已自建内容。set_search 会在首次调用时重建，这里预置 True 是为了
        # 让「忘记调 set_search 就显示窗口」不可能发生 —— 上一版就是因为
        # 漏掉那一步，界面上一个图标都没有，而库里明明有 116 条。
        self._populated = False
        # True = 已排好 _entries 但控件还没建（几何未就绪，见 _populate）
        self._pending_build = False

        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

        # 这里**刻意不设** WA_NoSystemBackground / WA_TranslucentBackground。
        # 实测这两个属性任一都会让 Qt 在合成阶段跳过整棵子树，
        # 结果是窗口里一个图标都不出现 —— 而 grid.grab() 单独截图却完全
        # 正常，因为那是另一条渲染路径。极具迷惑性。
        self.setAutoFillBackground(False)

        self._animator = PageAnimator(on_frame=self._on_anim_frame, parent=self)
        self._animator.finished.connect(self._on_anim_finished)
        self._apply_anim_settings()

        self._blank = BlankAreaController(
            self,
            click_through_empty=self.settings.get("click_through_empty"),
            hit_labels=not self.settings.get("hide_labels"),
            label_lines=self.settings.get("label_lines"),
        )
        # 必须传 callable：rebuild() 会整体替换 _tiles 列表对象，
        # 传列表快照的话重建之后就再也拿不到新 tile 了。
        self._blank.install_on(self, lambda: self._tiles)
        self._blank.clicked_outside.connect(self.closed_requested.emit)
        self._blank.settings_requested.connect(self.settings_requested.emit)
        self._blank.reload_requested.connect(self.reload_requested.emit)
        self._blank.add_requested.connect(self.add_requested.emit)
        # 这两个原先没接：about_requested / delete_requested 在 blankarea 里
        # 有定义、也有发射点，但 Grid 不往外传，于是主窗口永远收不到。
        # 「关于」是菜单里看得见却点了没反应的一项，比没有更糟。
        self._blank.about_requested.connect(self.about_requested.emit)
        self._blank.delete_requested.connect(self.delete_requested.emit)

        # 立刻填上全部条目，不等外部调用。
        self._populate()

    # ── 布局参数 ──────────────────────────────────────────
    @property
    def per_page(self) -> int:
        r = self._layout
        return r.per_page if r else max(1, self.cols * self.rows)

    @property
    def page(self) -> int:
        return self._page

    @property
    def page_count(self) -> int:
        return self._pages

    @property
    def _layout(self):
        """最近一次算出的排布结果。"""
        return getattr(self, "_layout_result", None)

    def _margins(self) -> tuple[int, int, int, int]:
        """上、右、下、左留白。搜索框和页码条的位置由主窗口决定，
        这里只留够图标不贴边即可。"""
        m = int(self.settings.get("margin"))
        return m, max(8, m // 2), m, max(8, m // 2)

    def _cell(self) -> tuple[int, int]:
        """
        单元格尺寸。

        **必须报 layouts 实际用的值**，不能自己按 _margins() 重算：
        排布已经交给 layouts.compute_layout，它对 margin 的解释和
        _margins() 不一致（实测 _margins() 推出 cell 宽 107，
        layouts 用的是 100），两套数字并存会让外部断言全部对不上，
        也会让 _icon_size() 的兜底算错。
        """
        r = self._layout
        if r is not None:
            return r.cell
        top, right, bottom, left = self._margins()
        cw = max(1, self.width() - left - right) // self.cols
        ch = max(1, self.height() - top - bottom) // self.rows
        return cw, ch

    def _tile_size(self) -> tuple[int, int]:
        """控件尺寸。宽度不能小于单元格宽度 —— 名字太长时要走省略号
        截断，不能被切边（实测「Agent Orchestrator」曾左右缺笔画）。"""
        cw, ch = self._cell()
        iw = min(self.settings.icon_size, cw - 24)
        ih = self.settings.get("icon_gap") + theme.Theme.LABEL_H
        return max(1, cw), min(ch - 8, iw + ih)

    def _page_height(self) -> int:
        """一页占据的高度 = 网格可视高度。
        翻页时下一页必须完全推出视野，否则底部会漏出图标。"""
        return max(1, self.height())

    def _geometry_ready(self) -> bool:
        """
        控件是否已拿到**真实**几何。

        Qt 给子控件的默认尺寸是 100×30，而 Grid 是在 Launchpad 里、
        窗口还没 show/resize 时构造的 —— 那一刻 width() 只有 100，
        按它算出来的一切（单元格、图标尺寸）都是垃圾值
        （实测 (100-48)//7-24 = -17，图标尺寸直接是负数）。
        """
        return self.width() >= 200 and self.height() >= 150

    def _icon_size(self) -> int:
        r = self._layout
        if r is not None and r.icon_size > 0:
            return r.icon_size
        if not self._geometry_ready():
            return self.settings.icon_size
        # 兜底必须有**下限**：单元格算不出宽度时减 24 会变负数，
        # 而负的图标尺寸会让 _icon_rect() 产出退化矩形
        # （实测 QRect(58, 0, -17, -17)），命中测试与泄漏检测全被带偏。
        return max(16, min(self.settings.icon_size, self._cell()[0] - 24))

    # ── 构建 ──────────────────────────────────────────────
    def rebuild(self, entries: list[Entry] | None = None,
                message: str = "") -> None:
        if entries is not None:
            self._entries = list(entries)
        self._message = message

        self._animator.cancel()

        for t in self._tiles:
            t.setParent(None)
            t.deleteLater()
        self._tiles.clear()
        # 旧控件已经 deleteLater，映射源必须一起丢掉，
        # 否则 _compute_base() 会从 _tiles_src 里取到已销毁的控件。
        self._tiles_src = []

        size = self._icon_size()
        for e in self._entries:
            t = Tile(e, self.icons.get(e), size)
            t.setParent(self)
            # 必须显式 show()。
            # setParent() 只是改归属，**不会**让控件可见；漏掉这一句时
            # 控件的 isVisible() 仍返回 True（因为父链可见），单独 grab
            # 也能截到，但窗口合成时整棵子树被跳过 —— 界面上一个图标都没有。
            # 这正是「库里 116 条、界面全空」的成因。
            t.show()
            t.launched.connect(self._on_launch)
            self._tiles.append(t)
        # 与 _entries 同序的映射源（_compute_base 靠它做 source_index 映射）
        self._tiles_src = list(self._tiles)

        # 先算一遍排布，拿到正确的 per_page / pages，再复位页码。
        self._compute_base()
        self._pages = self._layout.pages if self._layout else 1

        # 重建后必须回到第 1 页，且**显式把 _offset 归零**。
        #
        # 这里原来写的是 `self._page = 0` 后接 `self.goto(0, animate=False)`，
        # 看着等价，实际是错的：goto() 的第一行是
        #     if index == self._page and self._slide is None: return
        # 此刻 _page 刚被赋成 0，条件成立 → 直接早退，_offset 原封不动。
        # 于是「在第 3 页搜索」之后：page=0 而 offset 仍是 -2×页高，
        # 搜索结果全部落在可视区上方，界面一个图标都没有（实测 24 条结果
        # 0 个可见）。这正是用户最早报的「只有一页/快捷方式没加」。
        #
        # _animator.cancel() 必须排在最前：否则进行中的翻页动画下一帧
        # 会把旧 offset 又写回来，把刚归零的值覆盖掉。
        self._page = 0
        self._page_target = 0
        # 重建后当前页一定回到 0，所以要把「已发过意图」的记账同步复位 ——
        # 否则下一次 goto(0) 会因为记账还是 0 而**不发** page_intent，
        # 圆点就停在上一次的页上。
        self._page_intent_emitted = 0
        self._offset = 0.0
        self._animator.cancel()
        self._animator.set_page_size(self._page_height())
        self._animator.set_bounds(0.0,
                                  max(0, self._pages - 1) * self._page_height())
        self._animator.animate_to(0.0, animate=False)

        self.relayout()
        self._blank.refresh()
        self.pages_changed.emit(self._pages)
        self.page_changed.emit(self._page)

    def _recompute_pages(self) -> None:
        """按当前 _entries 重算页数，并把越界的 _page 拉回来。

        保留这个方法是给外部用的：库变动、搜索结果变化后不必整个
        rebuild()，只要重算页数再 relayout() 即可。
        """
        self._compute_base()
        self._pages = self._layout.pages if self._layout else 1
        if self._page >= self._pages:
            self._page = max(0, self._pages - 1)

    def _populate(self) -> None:
        """把当前过滤结果铺成控件。由 __init__ 与 set_search 共同调用。

        几何还没就绪时**推迟建控件**：按 100px 宽的默认尺寸建出来的
        Tile 全是错的图标尺寸，会白白触发一次按错误尺寸的全量图标提取
        （实测 175ms），等真实布局出来还得按真尺寸再提取一遍。
        推迟不是「忘记建」—— showEvent → relayout() 会保证补上，
        relayout() 里显式检查 _pending_build。
        """
        if not self._populated:
            self._entries = list(self.library.entries)
            self._populated = True
            # 未筛选列表 == library.search("") 的结果（同一批 Entry 对象）。
            # 记下来，好让推迟的那次构建能对上 set_search 的跳过判断，
            # 否则首次按热键唤出还会白白重建一遍 116 个控件。
            self._last_built_query = ""
        if not self._geometry_ready():
            self._pending_build = True
            return
        self._pending_build = False
        self.rebuild()

    def set_search(self, query: str, pinyin=None) -> None:
        """按搜索词过滤并重建。

        **结果没变就不重建。** show_me() 每次唤出窗口都会调这里，
        而热键唤出是最高频的路径 —— 无条件重建等于每次都白扔
        116 个 Tile 控件 + 116 次图标缓存查找。实测 rebuild 净耗时
        虽只有几毫秒，但它是 show_me 里除 Qt 全屏建立之外最大的一块。

        比较用**对象身份**而不是相等：Entry 是 dataclass，逐字段
        比较 116 条要 900 多次字符串比较；身份比较是 O(n) 指针比对，
        而且库里的条目对象在两次调用之间不会换（只有 reload 才会）。
        """
        hits = self.library.search(query, pinyin)
        unchanged = (
            self._last_built_query == query
            and len(hits) == len(self._entries)
            and self._tiles
            and all(a is b for a, b in zip(hits, self._entries))
        )
        if unchanged:
            self._search = query
            return

        self._search = query
        self._last_built_query = query
        msg = ""
        if not hits:
            msg = f"没有匹配「{query}」的应用" if query else "还没有添加任何应用"
        self._populated = True          # 之后由 query 决定内容，不再回落全部
        self.rebuild(hits, msg)

    # ── 排布 ──────────────────────────────────────────────
    def _compute_base(self) -> None:
        """用 layouts 纯函数算出每个控件在虚拟长轴上的位置。

        顺手把 _tiles 重排成**显示顺序**：排完之后
        `self._tiles[i]` 就是「显示上第 i 个图标」，下标 = 显示槽位。

        为什么必须这样：sort_mode 默认按名称排序，layouts 返回的
        positions/source_index 都是排序**之后**的次序，而 _tiles 原本
        是 _entries 的原始次序。两者错开时，任何「按下标切页」的代码
        （g._tiles[lo:lo+per_page] 当作第 N 页）都会错 —— 实测会把
        显示在第 1 页的图标算成「下一页泄漏」，一次冒出 6 个假阳性。

        _entries 保持原始次序不动：它是 compute_layout 的输入，
        改了会让 manual 排序这类依赖稳定性的模式失效。

        注意 _compute_base() 必须**幂等**：relayout() 会反复调它，
        而它自己会把 _tiles 改成显示序。所以映射源固定用 _tiles_src
        （始终与 _entries 同序），_tiles 只是它的一个显示序视图。
        直接拿 self._tiles 去按 source_index 取，第二次调用就全错位。
        """
        src_tiles = getattr(self, "_tiles_src", None)
        if src_tiles is None or len(src_tiles) != len(self._entries):
            src_tiles = list(self._tiles)
            self._tiles_src = src_tiles
        if not src_tiles:
            self._layout_result = None
            return
        result = compute_layout(self._entries, self.width(), self.height(),
                                self.settings)
        self._layout_result = result
        tw, th = result.tile_size

        ordered: list[Tile] = []
        for disp, src in enumerate(result.source_index):
            if not (0 <= src < len(src_tiles)):
                continue
            t = src_tiles[src]
            x, y = result.positions[disp]
            t.setGeometry(QRect(x, y, tw, th))
            t._base = QPoint(x, y)
            ordered.append(t)
        # 兜底：source_index 理论上全覆盖，但万一有条目被漏掉，
        # 不能让控件凭空消失（那正是「界面全空」类事故的成因）。
        if len(ordered) != len(src_tiles):
            seen = {id(t) for t in ordered}
            ordered.extend(t for t in src_tiles if id(t) not in seen)
        self._tiles = ordered
        self._geom = (result.cell[0], result.cell[1], tw, th,
                      result.page_height)

    def _apply(self) -> None:
        """
        按当前偏移摆放所有图标。

        必须遍历**全部**控件，不能只搬可视范围那几个 ——
        控件移出可视区后就再也不会被搬回来，翻页后界面会全空。

        符号：`_offset` 是**负数**（第 N 页 = -N * 页高），
        所以最终 y = base.y + _offset。写成减号会变成向上偏移，
        实测翻到第 2 页时 tile[35] 落在 y=2900（应在 56），
        屏幕上什么也看不到。
        """
        off = int(round(self._offset))
        for t in self._tiles:
            t.move(t._base.x(), t._base.y() + off)

    def relayout(self) -> None:
        if self._relayouting:
            return
        self._relayouting = True
        need_rebuild = False
        try:
            # 推迟建控件的情况（见 _populate）：现在几何应该就绪了，
            # 直接建。这一步放在最前，避免拿垃圾几何去算一次排布。
            if getattr(self, "_pending_build", False) and self._geometry_ready():
                self._pending_build = False
                need_rebuild = True

            self._compute_base()
            self._pages = self._layout.pages if self._layout else 1
            if self._page >= self._pages:
                self._page = max(0, self._pages - 1)

            # 图标尺寸随排布/窗口大小变化（compact 在 1440p 是 60px、
            # 在 2560x1422 是 90px；同一窗口 resize 也可能变），
            # 而 Tile._icon_size 是**构造参数**没有 setter，
            # 尺寸一变就必须重建控件，否则图标会保持旧尺寸。
            # 必须在 _relayouting 复位之后调 rebuild()，否则 rebuild
            # 内部的 relayout() 会被这里的守卫挡掉。
            #
            # **但 rebuild() 会把 _page 归 0**，而 resize 只是在改尺寸，
            # 用户没要求换页 —— 改个窗口大小就被弹回第 1 页是纯粹的 bug。
            # 所以这里记下当前页，重建完再回去。条目内容真变了
            # （搜索、库变动）时走 rebuild() 的正常路径，仍然回第 1 页。
            keep_page = None
            if self._layout is not None and self._tiles:
                want = self._layout.icon_size
                if want > 0 and self._tiles[0]._icon_size != want:
                    need_rebuild = True
                    keep_page = self._page

            # 不变量：_offset 必须恒等于 -_page × 当前页高。
            #
            # 页高依赖 self.height()，窗口一 resize 就变。_compute_base()
            # 用了新页高重排 _base，但 _offset 还是按**旧**页高算的，
            # 于是「在第 3 页把窗口缩小」会把视口剪切掉一块：
            # 实测 1920×1200 下每页应显示 35 个，实际只显示 25 个，
            # 本页外的图标被从底部剪了进来。
            #
            # 这里按当前页重新对齐而不是跳过：动画中 resize 属于边缘情况，
            # 宁可让动画吸附到目标位置（轻微跳动），也不能让视口长期错位。
            self._animator.cancel()
            ph = self._page_height()
            self._animator.set_page_size(ph)
            self._animator.set_bounds(0.0, max(0, self._pages - 1) * ph)
            self._offset = -self._page * ph
            self._animator.animate_to(self._page * ph, animate=False)
            self._apply()
        finally:
            self._relayouting = False

        if need_rebuild:
            self.rebuild()
            if keep_page is not None and keep_page > 0:
                # 尺寸变化不该换页。rebuild() 内部把 _page/_offset 归零，
                # 这里按**新页高**回到原页；页数变少时 goto() 会自己钳位。
                self.goto(keep_page, animate=False)
            return
        self._blank.refresh()

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self.relayout()

    def showEvent(self, ev):
        super().showEvent(ev)
        self.relayout()

    # ── 翻页 ──────────────────────────────────────────────
    def _apply_anim_settings(self) -> None:
        """把 settings 里的动效参数灌进 animator。"""
        a = self._animator
        if self.settings.get("reduce_motion"):
            a.set_easing("outCubic")
            a.set_duration(1.0)          # 实质关掉动画
        elif self.settings.get("paging_style") == "instant" or \
                not self.settings.get("paging_animation"):
            a.set_easing("outCubic")
            a.set_duration(1.0)
        else:
            name = self.settings.get("page_easing")
            if name == "spring":
                a.set_spring(duration=0.40, bounce=0.12)
            else:
                a.set_easing(name)
                a.set_duration(float(self.settings.get("page_duration")))

    def apply_settings(self, settings: Settings) -> None:
        """设置界面改动后由主窗口调用。"""
        self.settings = settings
        self.cols = max(1, int(settings.get("columns")))
        self.rows = max(1, int(settings.get("rows")))
        self._apply_anim_settings()
        self._blank.update_settings(
            click_through_empty=settings.get("click_through_empty"),
            label_lines=settings.get("label_lines"),
            hit_labels=not settings.get("hide_labels"),
        )
        # 图标尺寸是 Tile 的构造参数、没有 setter，换尺寸必须重建控件。
        self.rebuild()
        self._blank.refresh()

    def goto(self, index: int, animate: bool = True) -> None:
        index = max(0, min(index, self._pages - 1))
        # 意图立刻记账：滚轮/连点密集到达时，下一次要基于「用户要去哪」
        # 而不是「动画现在停在哪」。
        self._page_target = index
        # **目标页变化就立刻发信号**，不等动画结束。
        #
        # 这是页码指示器唯一的实时来源（window._sync_dots 订阅它）。
        # 之前只有 `page_changed`（动画结束时发），而滚轮连续翻 3 页时
        # 三次 goto 各起一段动画、page_changed 只在**最后一段**结束时
        # 发一次 —— 中间两页根本没有发信号的机会。实测 24 帧里圆点恒为 0，
        # 而目标页已经走到 3、动画页位从 0 一路滑到 -3：圆点一动不动，
        # 直到一切停稳才跳到末页（用户报「地下标志跟不上页面的节奏」）。
        if index != self._page_intent_emitted:
            self._page_intent_emitted = index
            self.page_intent.emit(index)
        if index == self._page and not self._animator.is_running():
            return
        ph = self._page_height()
        self._animator.set_page_size(ph)
        self._animator.set_bounds(0.0, max(0, self._pages - 1) * ph)
        self._animator.goto_page(index, animate)
        if not animate:
            self._finish_anim()

    def _on_anim_frame(self, value: float) -> None:
        # **符号转换**：PageAnimator 的约定是「正方向往后翻」
        # （goto_page 里 target = page * page_size），而网格摆放需要的是
        # 「负方向」（第 N 页整体上移 N × 页高）。两处约定不一致时
        # goto() 会静默不动 —— 翻页看起来完全失效，但没有任何报错。
        self._offset = -float(value)
        self._apply()

    def _on_anim_finished(self) -> None:
        # 同样要取负：漏掉这一处取负，动画结束后末帧会把正号写回 _offset，
        # 图标整体下移一整页到屏幕外，且此后每次翻页都如此。
        self._offset = -float(self._animator.value())
        self._apply()
        self._finish_anim()

    def _finish_anim(self) -> None:
        target = max(0, min(int(round(-self._offset / max(1, self._page_height()))),
                            self._pages - 1))
        self._page_target = target
        # 权威值也要把意图记账对齐：动画真正落定的页就是权威页，
        # 若与上次发过的意图不同（例如拖拽松手回弹），补发一次
        # page_intent 让圆点回到真值。这是 page_changed 的自愈配套。
        if target != self._page_intent_emitted:
            self._page_intent_emitted = target
            self.page_intent.emit(target)
        if target != self._page:
            self._page = target
            self.page_changed.emit(target)
        self._blank.refresh()

    def next_page(self):
        self.goto(self._page + 1)

    def prev_page(self):
        self.goto(self._page - 1)

    # ── 交互 ──────────────────────────────────────────────
    def _on_launch(self, entry: Entry) -> None:
        self.launch_requested.emit(entry)

    def mousePressEvent(self, ev):
        """空白区判定交给 BlankAreaController（0.6µs 桶网格）。
        旧的 O(n) 遍历按控件矩形判定，把 62% 的屏幕都算成「点了图标」，
        于是点在图标之间的缝隙毫无反应 —— 用户报的「空白区跟图标无关」。

        同时记下按下点的 X：拖拽翻页要的是**相对按下点的位移**，
        不是光标的绝对位置（见 mouseMoveEvent 的注释）。"""
        if ev.button() == Qt.LeftButton:
            self._drag_origin_x = ev.x()
        self._blank.handle_press(ev)

    def mouseMoveEvent(self, ev):
        # 拖动翻页：交给 animator 的手势接口，未达阈值不算翻页。
        #
        # **必须传相对按下点的累计位移，不能传 ev.x()。**
        # PageAnimator.update(drag_delta) 的语义是「相对按下点移动了多远」，
        # 传绝对光标 X 的话：在 x=800 按下并开始拖（哪怕手还没动），
        # 页面立刻猛跳 800px；移到 900 再跳 100；往回移 50 又跳回 50。
        # 表现就是「滑动着突然就卡了」—— 完全不可预测的位移。
        #
        # 起点必须在**这里**就地取，不能只依赖 mousePressEvent：
        # 按在图标上时，BlankAreaController 装在 Tile 上的事件过滤器会先
        # 消费掉 press 并返回 True，Grid.mousePressEvent 根本不会被调用。
        # 实测按在 (1200,700)（图标上）时 _drag_origin_x 停在 0，
        # 于是第一次移动就把页面拽走 1200px；而按在 (300,700)（空白处）
        # 才正常。取在第一帧 move 上就没这个分支。
        a = self._animator
        if ev.buttons() & Qt.LeftButton:
            if not a.is_dragging():
                self._drag_origin_x = ev.x()
                a.begin()
            a.update(float(ev.x() - self._drag_origin_x))
            ev.accept()
            return
        if a.is_dragging():
            a.end()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton and self._animator.is_dragging():
            self._animator.end()
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        """
        滚轮翻页。

        ## 密集滚动的真正 bug（已修）

        老代码是 `goto(self._page ± 1)`，而 `_page` 要等动画**结束**才更新。
        滚轮密集到达时（用力滑）20 次事件读到的是同一个旧页码，于是全部
        朝同一页去：滑一整屏只翻 1 页就停住，看着就是卡死。
        现在目标页基于 `_page_target`（**立刻记账**），20 格快速下滑会
        连翻 20 页，滚到头就停。

        ## 绝不能按 angleDelta 的绝对值累加

        这里踩过一次：写死「攒够 120 翻一页」（Windows 经典 WHEEL_DELTA）。
        但**一格的 angleDelta 因鼠标而异**，差别能到几十倍：经典滚轮 120、
        高精度/自由滚轮可能只发 1~60、某些设备发 240/480。
        按 120 累加，高精度轮要划十几下才翻一页 —— 用户报「划不动了」。
        按 480 的则一格跳四页。

        ## 也不能「一个事件 = 一页」（用户报「轻轻一划直接到底了」）

        改成只看符号之后，高频设备上「一划」会送来一串事件，每个都翻一页。
        实测 3 个事件（间隔 16ms）挤在 **24 帧**动画里 —— 而一格滚轮的
        正常动画是 22 帧（弹簧 0.4s）。也就是说内容是**连续滑过 3 页**，
        中间两页各只停留不到 1 帧，肉眼只看到「一滑到底，中间全跳过去」。

        所以既不按绝对值累加、也不一事件一页，而是**手势节流**：
        一次滚动手势（相邻事件间隔 < WHEEL_GESTURE_MS）内，
        不管设备送来多少事件，只消费**一页**的方向。
        停手超过 WHEEL_GESTURE_MS 才开始下一次手势。

        这样「轻轻一划」= 一页，与设备 delta 量级、事件个数都无关：
          经典滚轮一格 120      -> 一划一页 ✓
          高精度轮 20 个小事件  -> 合并成一页 ✓（不再是「划十几下才翻一页」）
          快速连划 3 次（>220ms 间隔）-> 翻 3 页，每页都能看清 ✓

        触控板的精细滚动走 pixelDelta（angleDelta 为 0），那边才用像素累积，
        阈值取页高的 1/4，和离散滚动手感区分得开。

        ## 触控板具体是什么手感

        触控板上报的是 **pixelDelta**（一串小像素位移），不是滚轮那种
        「一格 = 120」的离散刻度，所以走的是另一套判定：累加像素，
        每攒够**页高的 1/4** 翻一页。

        2560x1600、页高 1422 时一步 = 355px 累积量。触控板一次「轻推」
        大约 30~80px，所以要推 4~12 下才翻一页 —— 这就是两指滑动翻页
        该有的密度：能停在任意位置，不会一碰就跳页。指尖甩得快
        （单次 >355px）则直接翻一页，不会因为单次位移大就连翻好几页，
        因为翻页数取自 accum 的**总量**而不是单次值。

        `_wheel_accum` 每 250ms（`WHEEL_RESET_MS`）收不到新事件就清零：
        停手之后算新的一次，不把上次的余量带过来。

        注意：多指捏合缩放、四指横滑（切桌面/切窗口）是**驱动/系统**
        层面的手势，走 Win32 的 WM_POINTER / WM_GESTURE，**不会**变成
        QWheelEvent，本程序收不到，也就无法响应。本机 `SM_MAXTOUCHES=0`
        （触控设备被绑定在 Mouse 类上），连单指 Touch 事件都没有。
        """
        angle = ev.angleDelta().y()
        pixel = ev.pixelDelta().y()
        if not angle and not pixel:
            super().wheelEvent(ev)
            return
        ev.accept()

        if angle:
            # 离散滚轮：手势节流，一次手势只翻一页。
            now = time.monotonic() * 1000.0
            if now - self._wheel_last_ms > self._wheel_gesture_ms():
                self._wheel_gesture = 0        # 静默够久 -> 新手势
            self._wheel_last_ms = now
            d = 1 if angle > 0 else -1
            if self._wheel_gesture == 0:
                self._wheel_gesture = d
                self.goto(self._page_target - d)
            return

        # 触控板：像素累积，攒够页高 1/4 翻一页。
        #
        # turns 必须取**绝对值**：这里容易踩一个方向 bug ——
        # `turns = int(accum / step)` 会继承 accum 的符号，于是
        # `goto(target - turns if pixel > 0 else target + turns)` 里
        # 符号被算了两次。同一个手势方向（比如往上滑，pixel 为负），
        # 滚轮分支会翻到下一页、像素分支却翻到上一页 ——
        # 「鼠标能滚、触控板反着来」就是这么来的。
        # 现在两边统一成：turns = 翻的页数（正数），方向只看 pixel 的符号。
        self._wheel_accum += pixel
        step = max(20, self._page_height() // 4)
        if abs(self._wheel_accum) >= step:
            turns = int(abs(self._wheel_accum) / step)
            self._wheel_accum -= math.copysign(turns * step, self._wheel_accum)
            self.goto(self._page_target - turns if pixel > 0
                      else self._page_target + turns)
        self._arm_wheel_reset()

    def _arm_wheel_reset(self) -> None:
        if self._wheel_timer is None:
            self._wheel_timer = QTimer(self)
            self._wheel_timer.setSingleShot(True)
            self._wheel_timer.timeout.connect(
                lambda: setattr(self, "_wheel_accum", 0))
        self._wheel_timer.start(self.WHEEL_RESET_MS)

    def _wheel_gesture_ms(self) -> int:
        """
        手势节流窗口，读设置（缺省回落到类常量）。

        **必须是方法而不是常量。** 常量只在类定义时算一次，运行期改
        设置不生效；而且用户没法调手感 —— 「划快一点才翻第二页」和
        「连划容易一次滑两页」是两种人，两种都要能选。
        """
        try:
            v = int(self.settings.get("wheel_gesture_ms"))
        except (KeyError, TypeError, ValueError):
            return self.WHEEL_GESTURE_MS
        return max(80, min(600, v))

    def keyPressEvent(self, ev: QKeyEvent):
        k = ev.key()
        if k in (Qt.Key_Right, Qt.Key_Down, Qt.Key_PageDown, Qt.Key_Space):
            self.next_page()
            ev.accept()
        elif k in (Qt.Key_Left, Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Backspace):
            self.prev_page()
            ev.accept()
        else:
            super().keyPressEvent(ev)

    # ── 空状态 ────────────────────────────────────────────
    def paintEvent(self, ev):
        # 必须调用 super()。
        # 上一版为了「不擦出默认灰底」而省掉了它，结果 Qt 认为本控件没有
        # 完成绘制，**连带跳过整棵子控件树** —— 单独 grid.grab() 有 36138
        # 个亮像素，嵌在窗口里 render 只有 3199，图标等于消失。
        # 正确的做法是调 super()（它在本控件不设背景时不擦任何东西），
        # 透明由 setAutoFillBackground(False) 保证。
        super().paintEvent(ev)
        if self._tiles or not self._message:
            return
        from PyQt5.QtGui import QColor, QPainter
        p = QPainter(self)
        p.setPen(QColor(255, 255, 255, 90))
        f = theme.label_font()
        f.setPointSize(15)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignCenter, self._message)
        p.end()