# -*- coding: utf-8 -*-
"""
单个应用图标控件：图标 + 文字，悬停高亮，点击发射 launched。
"""

from PyQt5.QtCore import QPoint, QRect, Qt, pyqtSignal
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QApplication, QWidget

from . import theme


class Tile(QWidget):
    launched = pyqtSignal(object)

    def __init__(self, entry, icon, icon_size: int, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._icon = icon
        self._icon_size = icon_size
        self._hover = False
        self._press = False
        self._base = None

        # ── 绘制缓存 ──────────────────────────────────────
        # 稳态 paintEvent 里**不允许**有 QPixmap / QFont / QFontMetrics 的构造，
        # 也不允许有 QRectF。只有纯读取和几次 drawPixmap。
        #
        # 每项缓存独立失效（_geom_key 是总闸），而不是一个大 key 全重建：
        # 尺寸不变时，一项都不用重烘。
        self._geom_key: tuple | None = None
        self._icon_key: tuple | None = None
        self._overlay_key: tuple | None = None
        self._text_key: tuple | None = None
        self._cached_pm: QPixmap | None = None
        self._cached_plate = QPixmap()
        self._cached_plate_pressed = QPixmap()
        self._cached_ring = QPixmap()
        self._cached_shadow = QPixmap()
        self._cached_text = QPixmap()
        self._icon_rect_cache = QRect()
        self._label_rect_cache = QRect()

        # 显示用的名字。失效快捷方式加 ⚠ —— 一次算好，绘制路径不再拼字符串。
        self._name = entry.name + ("  ⚠" if entry.missing else "")
        # elide() 的结果，blankarea 等外部代码会读
        self._cached_elided = self._name

        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        # 不设 WA_TranslucentBackground —— 见 grid.py 里的注释：
        # 那会让 Qt 在合成时跳过整棵子树，窗口里一个图标都不出现。
        # Tile 的 paintEvent 不擦背景，因此天然透明。
        self.setAutoFillBackground(False)

    # ── 命中 ──────────────────────────────────────────────
    def _icon_rect(self) -> QRect:
        w = min(self._icon_size, self.width() - 8)
        return QRect((self.width() - w) // 2, 0, w, w)

    def _label_rect(self) -> QRect:
        top = self._icon_rect().bottom() + theme.Theme.ICON_GAP - 6
        return QRect(0, top, self.width(), theme.Theme.LABEL_H)

    # ── 事件 ──────────────────────────────────────────────
    def enterEvent(self, ev):
        self._hover = True
        self._update_full()

    def leaveEvent(self, ev):
        self._hover = False
        self._press = False
        self._update_full()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = True
            # 只重画底板那一小块：按下时变化的只有底板的不透明度，
            # 图标和文字像素完全一样，没必要重画整个矩形。
            self.update(self._plate_rect())
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            was = self._press
            self._press = False
            self._update_full()
            # 必须在控件矩形内才响应，避免拖动到外面松手也触发
            if was and self.rect().contains(ev.pos()):
                self.launched.emit(self.entry)
            return
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):
        """
        把滚轮**转交**给父控件（Grid）统一处理。

        ## 为什么必须显式转交

        Qt 5 **不会**把未处理的滚轮事件沿父链往上传播 ——
        它只投递给光标下的那个控件（和鼠标按下一样，不回溯父级）。
        Tile 没实现 wheelEvent 时，事件就到此为止，被默认实现 ignore 掉。

        而 Launchpad 整屏几乎都被图标铺满：120 个 tile 之外的空隙
        只有边距和行间距。用户「在图标上滚滚轮」是最常见的操作，
        却是**完全无效**的 —— 症状就是「划不动」。

        实测（sendEvent 到 Grid 会翻页、sendEvent 到 Tile 纹丝不动）
        证实了这一点。

        用 sendEvent 而不是直接调 parent.wheelEvent，是为了让父控件上
        已安装的事件过滤器仍有机会先看到事件（正常路径上 Grid 自己
        没装过滤器，但别把这条约定写死）。
        """
        p = self.parentWidget()
        if p is not None and hasattr(p, "wheelEvent"):
            QApplication.sendEvent(p, ev)
        else:
            super().wheelEvent(ev)

    # ── 重绘范围 ──────────────────────────────────────────
    def _plate_rect(self) -> QRect:
        """底板覆盖的区域。press 切换只重画这块。"""
        return self._icon_rect_cache.adjusted(-15, -15, 15, 19)

    def _ring_rect(self) -> QRect:
        """悬停描边覆盖的区域。hover 进出时只重画这块。"""
        return self._icon_rect_cache.adjusted(-8, -8, 8, 8)

    def _update_full(self) -> None:
        """
        底板 + 描边一起变，两个区域并起来重画。

        注意这里**不能**只 update(底板) 然后 update(描边) 分两次 ——
        两次 update() 会让 Qt 排两次 paintEvent，反而更慢。
        一次 update(并集) 只触发一次。
        """
        self.update(self._plate_rect().united(self._ring_rect()))

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        # 尺寸变了 → 所有贴图缓存失效。真正的重建留到 paintEvent 里的
        # geom_key 比较之后，避免 resizeEvent 里就做一串 pixmap 构造。
        self._geom_key = None
        self.update()

    # ── 缓存构建（唯一的重活都集中在这里）─────────────────
    def _build_geometry(self, geom_key: tuple) -> None:
        """
        尺寸变了才走这里：算矩形，并让所有贴图缓存失效。

        贴图**不**在这里一起烘 —— 每个 Tile 只有真正画到的那几项才烘。
        116 个 Tile 里有 81 个从翻到的那一页起就不在可视区，
        启动时 window.show_me() 却对全部 116 个调 update()，
        于是一次性全烘 = 白烘 81 套。改成按需。
        """
        T = theme.Theme
        w = min(self._icon_size, self.width() - 8)
        ir = QRect((self.width() - w) // 2, 0, w, w)
        self._icon_rect_cache = ir

        top = ir.bottom() + T.ICON_GAP - 6
        self._label_rect_cache = QRect(0, top, self.width(), T.LABEL_H)

        self._geom_key = geom_key
        self._icon_key = None
        self._overlay_key = None
        self._text_key = None
        self._cached_elided = self._name

    def _icon_pixmap(self) -> QPixmap | None:
        """
        烘图标。QIcon.pixmap() 单次其实不贵（实测 0.0004ms），
        但它每次都要过 QIcon 的内部查表。缓存住之后 hover 重绘
        就完全跳过这条路径。
        """
        key = self._geom_key
        if self._icon_key != key:
            ir = self._icon_rect_cache
            pm = self._icon.pixmap(ir.width(), ir.height())
            if pm.isNull():
                pm = self._icon.pixmap(self._icon_size, self._icon_size)
            self._cached_pm = pm
            self._icon_key = key
        return self._cached_pm

    def _overlays(self) -> tuple[QPixmap, QPixmap]:
        """烘底板（常态/按下两张）与悬停描边。形状只取决于尺寸。"""
        key = self._geom_key
        if self._overlay_key != key:
            ir = self._icon_rect_cache
            self._cached_plate = theme.render_plate(ir, False)
            self._cached_plate_pressed = theme.render_plate(ir, True)
            self._cached_ring = theme.render_ring(ir)
            self._overlay_key = key
        return self._cached_plate, self._cached_ring

    def _label_pixmaps(self) -> tuple[QPixmap, QPixmap]:
        """
        烘标签（阴影 + 正文）。

        drawText 要过字体整形 + 抗锯齿光栅化，是单次绘制里最贵的一步，
        而它只取决于文本和矩形 —— 烘一次，之后 hover 重绘一个字都不重排。
        """
        key = self._geom_key
        if self._text_key != key:
            shadow, body, shown = theme.render_label(
                self._label_rect_cache, self._name)
            self._cached_shadow = shadow
            self._cached_text = body
            self._cached_elided = shown
            self._text_key = key
        return self._cached_shadow, self._cached_text

    # ── 绘制 ──────────────────────────────────────────────
    def paintEvent(self, ev):
        # 尺寸变了才重算几何。稳态下只有一次三元组比较。
        geom_key = (self.width(), self.height())
        if self._geom_key != geom_key:
            self._build_geometry(geom_key)

        # ── 全部烘图都在 QPainter 之前完成 ──────────────────
        # 这不是洁癖。实测在 QPainter 处于活动状态时**嵌套**构造
        # 另一个 QPainter（render_label / render_plate 内部都要建），
        # 会让文字图偶发不生成 —— 同一份代码连跑三次，
        # 有一次第一页最下一行的标签整条消失（像素比对 191 个点）。
        # 基线版本不会这样，因为它只在活动 painter 上直接 drawText，
        # 从不嵌套。顺序改成"先烘完再画"既消除这个不确定性，
        # 也少一次嵌套 painter 的进出开销。
        show_plate = self._hover or self._press
        plate = ring = None
        if show_plate:
            plate, ring = self._overlays()
        pm = self._icon_pixmap()
        shadow, body = self._label_pixmaps()
        # 到这里所有贴图都在手上，后面不再有任何惰性构造。

        # 只重画了 dirty 区域时（enter/leave/press 走的就是这条），
        # ev 的 region 被限制在那小块。目标 pixmap 是按整个控件尺寸
        # 烘的，所以把 painter 原点挪到裁剪区左上角即可，
        # 下面照常按控件坐标绘制，偏移一行都不用改。
        #
        # 注意 QRegion 在 PyQt5 里没有 count()，是 rectCount()。
        # 这里写成 count() 会在 paintEvent 里抛 AttributeError，
        # 而 PyQt5 遇到 paintEvent 里的异常会直接 0xC0000409 崩进程 ——
        # 见 icons.py 里关于 fromWinHICON 那段注释的同类教训。
        region = ev.region()
        clip = region.boundingRect() if region.rectCount() > 1 else None
        if clip is not None and clip == self.rect():
            clip = None

        # 从这里往下只做纯绘制，不再有任何对象构造。
        p = QPainter(self)
        # 这两个 hint 必须留着。
        # SmoothPixmapTransform 决定 drawPixmap 在目标尺寸与原图不同时
        # 走平滑重采样还是最近邻。控件尺寸在 show_me 的排布过程中会变，
        # 那一帧图标尺寸与烘好的贴图差一两个像素 —— 去掉这个 hint，
        # 那几帧的图标就会变成锯齿块。
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        if clip is not None:
            # 目标 pixmap 是按整个控件尺寸烘的，把原点挪到裁剪区左上角，
            # 后面照常按控件坐标画，偏移一行都不用改。
            p.translate(clip.topLeft())
        # 刻意不调用 p.fillRect：Tile 必须透明，才能透出下层的纯黑窗口背景。
        # Windows 上父窗口非完全不透明时，Qt 不会自动给子控件补背景，
        # 所以这里不擦就是真透明。

        ir = self._icon_rect_cache

        # 底板与描边只在真的要画时才贴。不可见的 Tile 永远不碰这两项。
        if show_plate:
            p.drawPixmap(self._plate_offset(),
                         self._cached_plate_pressed if self._press else plate)

        if pm is not None and not pm.isNull():
            p.drawPixmap(ir, pm)
            if self._hover:
                p.drawPixmap(self._ring_offset(), ring)
        else:
            # 图标彻底缺失：画一个空框而不是留白，让问题可见
            p.setPen(theme.missing_color())
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(ir, 12, 12)

        p.drawPixmap(self._label_rect_cache, shadow)
        p.drawPixmap(self._label_rect_cache, body)
        p.end()

    # 供外部读取的省略文本（blankarea.py 算命中区要用）
    @property
    def elided_text(self) -> str:
        return self._cached_elided

    # ── 贴图偏移 ──────────────────────────────────────────
    # 烘出来的 pixmap 比目标矩形大（底板/描边都往外扩），
    # 贴的时候要退回那个偏移，否则会整体错位。
    def _plate_offset(self) -> QPoint:
        r = self._icon_rect_cache
        return QPoint(r.x() - 15, r.y() - 15)

    def _ring_offset(self) -> QPoint:
        r = self._icon_rect_cache
        return QPoint(r.x() - 8, r.y() - 8)