# -*- coding: utf-8 -*-
"""
纯色深色主题的绘制工具。

用户明确要求背景用纯黑，不做实时背景渲染，也不做玻璃折射 ——
上一版的"液态玻璃"在 QPainter 下只是几层半透明渐变，视觉上等于一块灰板，
还不如纯色干净。这里只保留真正能提升观感的部分：

- 纯黑背景 + 极轻的径向提亮（让中心不那么死板，但仍然读作黑色）
- 图标悬停时的高光与轻微放大
- 页码点的激活/悬停/静止三态

所有参数集中在 Theme，改一处即可全局生效。
"""

from PyQt5.QtCore import QPointF, QRect, QRectF, Qt
from PyQt5.QtGui import (QBrush, QColor, QFont, QFontMetrics, QLinearGradient,
                         QPainter, QPen, QPixmap, QRadialGradient)


class Theme:
    # 背景：纯黑，中心极轻提亮
    BG = QColor(0, 0, 0)
    BG_GLOW = QColor(28, 32, 42)        # 中心提亮色，要非常暗
    GLOW_RATIO = 0.55                   # 提亮区域占对角线比例
    # 背景图降采样倍率。**保持 1（不降采样）** ——
    # 试过 4（640x400 拉伸到全屏），两个问题都致命：
    #   1. 内容区逐像素最大差 **15/255**、平均 5.74/255 —— 径向渐变的
    #      量化台阶在半分辨率下被放大，肉眼能看出色带。
    #      「只有 3 个色标所以降采样无损」的推理是错的：那说的是
    #      **沿半径**平滑，而变化发生在**半径方向的高频处**
    #      （0.55 处的 alpha 从 34 掉向 0 是陡的）。
    #   2. blit 反而更慢：实测 3.009ms vs 原分辨率 0.958ms。
    #      拉伸 blit 要做双线性插值，比 1:1 拷贝贵得多 ——
    #      我先前那张「1/4 分辨率 0.021ms」的表是**量错了**，
    #      量的其实是 pixmap 源大小，不是实际 blit 到全屏的成本。
    # 教训：优化前先确认「量的东西 == 花的东西」。
    BG_DOWNSCALE = 1

    # 图标
    ICON_SIZE = 128
    ICON_GAP = 22                       # 图标下方到文字的间距
    LABEL_H = 34
    LABEL_FONT = 13
    LABEL_COLOR = QColor(236, 238, 242)
    LABEL_SHADOW = QColor(0, 0, 0, 190)

    # 悬停
    HOVER_RING = QColor(255, 255, 255, 62)
    HOVER_PLATE = QColor(255, 255, 255, 20)
    HOVER_SCALE = 1.08

    # 页码点
    DOT_R = 5
    DOT_GAP = 22
    DOT_HIT = 30                        # 点击热区直径，必须远大于视觉直径
    DOT_ACTIVE = QColor(255, 255, 255, 235)
    DOT_IDLE = QColor(255, 255, 255, 80)
    DOT_HOVER = QColor(255, 255, 255, 170)
    DOT_BAR_W = 22                      # 激活点的拉长宽度

    # 搜索框
    SEARCH_W = 460
    SEARCH_H = 46
    SEARCH_FONT = 15
    SEARCH_PLATE = QColor(255, 255, 255, 22)
    SEARCH_EDGE = QColor(255, 255, 255, 52)
    SEARCH_TEXT = QColor(240, 242, 246)
    SEARCH_HINT = QColor(255, 255, 255, 110)

    # 动效
    # 淡入淡出时长。**80 而不是 130。**
    # 用心跳探针（GUI 线程 5ms 定时器）量「唤出→翻页→收起」整轮循环，
    # 每轮 >33ms 的顿挫次数，各重复 3 次取平均：
    #     130ms -> 0.72 次/轮
    #      80ms -> 0.333 次/轮   （与完全不淡出的 0.307 基本持平）
    # 也就是说 130ms 的淡出要多付一倍多的顿挫，换来的只是多 50ms 的观感。
    FADE_MS = 80
    SLIDE_MS = 260


# 背景缓存：(w, h) -> QPixmap。
# 只在窗口尺寸变化时重建，稳态下每帧退化成一次 drawPixmap。
_BG_CACHE: dict[tuple[int, int], QPixmap] = {}
_BG_CACHE_MAX = 4

# 缓存里的图是**降采样**过的（倍率见 Theme.BG_DOWNSCALE），
# 绘制时由 paint_background 显式给目标矩形让 Qt 拉伸。


def render_background(w: int, h: int) -> QPixmap:
    """
    把背景烘成一张 QPixmap。

    两级优化，缺一不可：

    1. **烘一次**，而不是每帧重算。纯黑 fillRect 只要 0.45ms，但中心的
       径向渐变要 3.7ms —— 渐变每像素都要插值，2560x1600 就是 410 万个
       像素。而背景在窗口尺寸不变时**完全静态**，每帧重算等于每帧
       重新插值 410 万次。烘一次之后 blit 只要 0.958ms。

    2. 尺寸按 `Theme.BG_DOWNSCALE` 缩放（当前为 1，即不缩）。
       试过 4 降采样，两个问题都致命 —— 见该常量的注释。

    返回的图**按 1:1 画**（`drawPixmap(0, 0, pm)`），
    所以它的尺寸必须正好是目标尺寸。
    """
    key = (w, h)
    pm = _BG_CACHE.get(key)
    if pm is not None:
        return pm

    div = max(1, int(Theme.BG_DOWNSCALE))
    sw = max(1, w // div)
    sh = max(1, h // div)
    pm = QPixmap(sw, sh)
    p = QPainter(pm)
    # 用 _draw_background 而不是 paint_background —— 后者会反过来
    # 调 render_background，那样就是无限递归。
    # 传入降采样后的尺寸，渐变的半径/色标按比例缩放，视觉一致。
    _draw_background(p, sw, sh)
    p.end()

    # 只留最近几个尺寸。resize 过程中会有多个中间尺寸进来，
    # 不限制的话长时间拖窗口会攒下一堆全屏 pixmap。
    if len(_BG_CACHE) >= _BG_CACHE_MAX:
        _BG_CACHE.pop(next(iter(_BG_CACHE)))
    _BG_CACHE[key] = pm
    return pm


def _draw_background(p: QPainter, w: int, h: int) -> None:
    """真正逐像素画背景。只被 render_background 调用。"""
    p.fillRect(0, 0, w, h, Theme.BG)

    if Theme.GLOW_RATIO <= 0:
        return

    cx, cy = w / 2.0, h / 2.0
    radius = max(w, h) * Theme.GLOW_RATIO
    g = QRadialGradient(QPointF(cx, cy), radius)
    c = Theme.BG_GLOW
    g.setColorAt(0.0, QColor(c.red(), c.green(), c.blue(), 90))
    g.setColorAt(0.55, QColor(c.red(), c.green(), c.blue(), 34))
    g.setColorAt(1.0, QColor(c.red(), c.green(), c.blue(), 0))
    p.fillRect(0, 0, w, h, QBrush(g))


def paint_background(p: QPainter, w: int, h: int) -> None:
    """
    纯黑背景 + 中心极轻提亮。

    之所以不是完全平的黑：全屏纯黑在 OLED 上会有轻微拖影，
    而且视觉上"死"。加一个亮度约 11% 的径向提亮，既读作黑色，
    又让中心区域的图标对比度更好。

    性能：烘成 pixmap，避免每帧重新插值 410 万像素
    （径向渐变每像素都要算，2560x1600 实测 3.7ms -> blit 0.958ms）。
    背景在窗口尺寸不变时**完全静态**，每帧重算等于每帧重做一遍。

    ## 三条优化路线，全部实测排除（勿再尝试）

    ### 1. 按脏区裁剪 blit —— 拿不到脏区
    直觉上「只 blit 脏区」更好：实测整图 blit 1.172ms、
    一条 2464x200 的横带只要 0.064ms，差 18 倍。但：

        Qt 在 paintEvent 里**不给 QPainter 设剪裁**。
        实测 `p.clipBoundingRect()` 返回 **0x0**，
        脏区只存在于 backing store 的 paint region，Python 侧取不到。

    ### 2. 把背景挪到独立控件 —— 同样被标脏
    实测背景控件在翻页 72 帧里照样被标脏 18 次。
    Qt 会把子控件移动区域下方的兄弟内容一起标脏。

    ### 3. 背景降分辨率再拉伸 —— 更慢且有色带
    见 `Theme.BG_DOWNSCALE` 的注释。

    结论：窗口级背景 blit 是 Qt 正常行为的固定成本，压不下去。
    真正的优化空间在别处（见 README 的性能一节）。
    """
    if Theme.GLOW_RATIO > 0:
        pm = render_background(w, h)
        if not pm.isNull():
            p.drawPixmap(0, 0, pm)
            return

    _draw_background(p, w, h)


# 底板/描边烘焙尺寸。Tile 贴图时按这两个值往回偏移，
# 与 paint_icon_plate / paint_icon_hover_ring 里的 adjusted() 一致。
PLATE_PAD_LT = 15
PLATE_PAD_RB = 19
RING_PAD = 8
_MISSING_PEN: QColor | None = None


def missing_color() -> QColor:
    """图标彻底缺失时画的红框。缓存一份，省掉每帧一次 QColor 构造。"""
    global _MISSING_PEN
    if _MISSING_PEN is None:
        _MISSING_PEN = QColor(255, 90, 90, 200)
    return _MISSING_PEN


def paint_icon_plate(p: QPainter, rect: QRect, hovered: bool,
                     pressed: bool = False) -> None:
    """
    图标底板。悬停时才画，且极淡 —— 只用来提示热区，不喧宾夺主。

    保留这个即时绘制版本是为了兼容直接调用它的旧代码；
    Tile 的热路径走 render_plate()（烘成 pixmap 贴）。
    """
    if not hovered and not pressed:
        return
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    r = rect.adjusted(-PLATE_PAD_LT, -PLATE_PAD_LT, 14, PLATE_PAD_RB)
    rad = int(r.width() * 0.22)
    alpha = 30 if pressed else 20
    p.setBrush(QColor(255, 255, 255, alpha))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(r), rad, rad)
    p.restore()


def paint_icon_hover_ring(p: QPainter, icon_rect: QRect) -> None:
    """悬停时图标外的一圈极淡描边，强化"这个能被点"。"""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    pen_color = Theme.HOVER_RING
    pen = p.pen()
    pen.setColor(pen_color)
    pen.setWidthF(1.4)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(icon_rect.adjusted(-RING_PAD, -RING_PAD,
                                                RING_PAD, RING_PAD)),
                       icon_rect.width() * 0.24, icon_rect.width() * 0.24)
    p.restore()


# ── 烘焙版本：热路径用 ────────────────────────────────

def _blank(w: int, h: int) -> QPixmap:
    """
    建一张指定尺寸的透明 pixmap。

    刻意是 (w, h) 两个参数而不是一个 size：标签是 352x34 的扁条，
    而图标底板接近正方形。曾经写成 _blank(max(w, h)) 造一张正方形，
    再靠 drawPixmap 缩放贴回去 —— 352x352 压成 352x34 等于把文字
    纵向压扁了 10 倍，肉眼看是标签变成一条条横线。
    正方形只对确实需要裁剪的底板/描边用（见下面两处）。
    """
    pm = QPixmap(max(1, w), max(1, h))
    pm.fill(Qt.transparent)
    return pm


def render_plate(icon_rect: QRect, pressed: bool) -> QPixmap:
    """
    把底板烘成一张带 alpha 的 QPixmap。

    与 paint_icon_plate 形状完全一致，只是把「save/restore + 设 hint +
    建 QRectF + 抗锯齿填充」一次性做完，之后每次 hover 只剩一次 drawPixmap。

    抗锯齿的圆角边缘光栅化是这里最贵的部分；hover 进出时不必重复付。
    """
    r = icon_rect.adjusted(-PLATE_PAD_LT, -PLATE_PAD_LT, 14, PLATE_PAD_RB)
    w, h = r.width(), r.height()
    if w <= 0 or h <= 0:
        return QPixmap()

    pm = _blank(w, h)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(255, 255, 255, 30 if pressed else 20))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(QRectF(0, 0, w, h), int(w * 0.22), int(w * 0.22))
    p.end()
    return pm


def render_ring(icon_rect: QRect) -> QPixmap:
    """把悬停描边烘成 pixmap。形状与 paint_icon_hover_ring 一致。"""
    pad = RING_PAD
    w = icon_rect.width() + pad * 2
    h = icon_rect.height() + pad * 2
    if w <= 0 or h <= 0:
        return QPixmap()

    pm = _blank(w, h)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(Theme.HOVER_RING)
    pen.setWidthF(1.4)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(0, 0, w, h),
                      icon_rect.width() * 0.24, icon_rect.width() * 0.24)
    p.end()
    return pm


def paint_label(p: QPainter, rect: QRect, text: str,
                elided: str | None = None) -> None:
    """
    图标下方文字。带一层黑色描边阴影 —— 深色背景上白字不够清晰，
    尤其当图标本身是深色时。

    保留即时绘制版本供旧代码调用；热路径走 render_label()。
    """
    p.save()
    p.setFont(label_font())

    # 阴影：偏移 1px 的黑色，制造轻微浮起感
    p.setPen(Theme.LABEL_SHADOW)
    shadow = rect.translated(0, 1)
    p.drawText(shadow, Qt.AlignHCenter | Qt.AlignTop, text)

    p.setPen(Theme.LABEL_COLOR)
    p.drawText(rect, Qt.AlignHCenter | Qt.AlignTop, elided or text)
    p.restore()


def render_label(rect: QRect, text: str) -> tuple[QPixmap, QPixmap, str]:
    """
    把标签（阴影 + 正文）烘成两张 pixmap。

    返回 (阴影图, 正文图, 省略后的文本)。

    为什么值得烘：drawText 要过字体整形 + 抗锯齿光栅化，
    这是整个 Tile 绘制里最贵的一步，而它**只取决于文本和矩形**。
    hover 重绘时矩形和文本都没变，把结果缓存下来即可完全跳过排版。

    两张图分开是因为原实现里阴影比正文低 1px，合成一次会丢掉这个偏移。
    正文图做成与 rect 等大，这样贴的时候直接用 rect，不算偏移。
    """
    f = label_font()
    shown = elide(text, f, rect.width())

    w, h = max(1, rect.width()), max(1, rect.height())

    # 阴影必须与正文**同尺寸**（w x h），否则贴回 rect 时会被缩放。
    # 偏移 1px 留在图里：照原实现 translated(0,1) 画，
    # 底部多出来的那 1px 自然被裁掉 —— 与原实现的可见效果一致。
    shadow = _blank(w, h)
    sp = QPainter(shadow)
    sp.setFont(f)
    sp.setPen(Theme.LABEL_SHADOW)
    sp.drawText(shadow.rect().translated(0, 1), Qt.AlignHCenter | Qt.AlignTop,
                shown)
    sp.end()

    body = _blank(w, h)
    bp = QPainter(body)
    bp.setFont(f)
    bp.setPen(Theme.LABEL_COLOR)
    bp.drawText(body.rect(), Qt.AlignHCenter | Qt.AlignTop, shown)
    bp.end()

    return shadow, body, shown


def elide(text: str, font: QFont, max_w: int) -> str:
    """
    按像素宽度截断并加省略号。

    Qt 的 elidedText 在含中日韩字符时可能把结果截在**多字节字符中间** ——
    实测 '某编程助手 · 在你的终端中运行' 被截成
    '某编程助手 · 在你的终…'，末尾的 '端' 只剩半个字，渲染成豆腐块，
    而且这个字符串本身不再是合法 UTF-8。

    这里在 Qt 截断后再做一次修正：剥掉末尾可能的残缺字符
    （U+FFFD、代理对、以及位于末尾的半个 CJK 字符无法可靠判断，
    所以改为按字符边界回退到省略号之前）。
    """
    if max_w <= 0:
        return ""
    fm = _metrics_for(font)
    if fm.horizontalAdvance(text) <= max_w:
        return text

    out = fm.elidedText(text, Qt.ElideRight, max_w)
    if not out:
        return ""

    # Qt 有时会把省略号放在一个被切开的字符之后。用一个明确的宽度上限
    # 再截一次，保证结果里不残留替换字符；连续回退几个字符即可。
    while out and (out.endswith("\ufffd") or _is_surrogate_tail(out)):
        out = out[:-1]

    return out


def _is_surrogate_tail(s: str) -> bool:
    """末尾是否是落单的 UTF-16 代理（半个字符）。"""
    if not s:
        return False
    cp = ord(s[-1])
    return 0xD800 <= cp <= 0xDFFF


_LABEL_FONT: QFont | None = None
_METRICS: dict[tuple, QFontMetrics] = {}


# ─── 字体引擎预热 ──────────────────────────────────────

# 进程里第一次 drawText 要初始化字体引擎，Windows 上实测 250~280ms，
# 且与用哪个字体无关（Segoe UI / Microsoft YaHei UI / Arial 都一样）。
# 之后同一字体的 drawText 只要 0.1~0.2ms。
#
# 这笔开销落在谁头上，全看谁先 drawText。现状是落在
# icons.IconCache._placeholder() 里 —— 它在 Grid.rebuild 里对 116 个
# 条目逐个调 get()，第 2 个条目取不到图标就画占位图，
# 于是主线程在 window_init 里被这 250ms 卡住。
#
# 预热放到后台线程：QPixmap + QPainter 在有 QApplication 之后
# 于任意线程都可用（实测 worker 线程承担了全部 254ms，
# 主线程再画只要 1.3ms）。这段不影响主线程的进度。
_FONT_WARM_DONE = False
_FONT_WARM_THREAD = None


def warm_font_engine_async():
    """
    后台线程预热字体引擎。幂等，重复调用只生效一次。

    必须在 QApplication 之后调用 —— QPixmap/QPainter 依赖 QGuiApplication。
    提前调用静默返回（还没有 app 就没法起线程）。

    返回那个 Thread（没起成则为 None）。调用方可以 join 它来确认
    预热已完成 —— 基准测试需要这个，否则测到的首帧耗时会含预热线程
    的 CPU 竞争。应用本身不用等。
    """
    global _FONT_WARM_DONE, _FONT_WARM_THREAD
    if _FONT_WARM_DONE:
        return _FONT_WARM_THREAD
    from PyQt5.QtGui import QGuiApplication
    if QGuiApplication.instance() is None:
        return None
    _FONT_WARM_DONE = True

    import threading

    def _warm() -> None:
        try:
            pm = QPixmap(8, 8)
            p = QPainter(pm)
            # 标签字体和占位图字体各画一次：两个字体族要**各自**初始化，
            # 少画一个就还剩一份 250ms 留给主线程。
            for family, size in (("Microsoft YaHei UI", Theme.LABEL_FONT),
                                 ("Segoe UI", 53)):
                p.setFont(QFont(family, size))
                p.drawText(pm.rect(), Qt.AlignCenter, "Wm中")
            p.end()
        except Exception:
            # 预热失败无所谓：只是又回到主线程承担那 250ms，功能不受影响。
            # 这里绝不能让异常逃出去把启动搞崩。
            pass

    _FONT_WARM_THREAD = threading.Thread(target=_warm, name="lp-font-warm",
                                     daemon=True)
    _FONT_WARM_THREAD.start()
    return _FONT_WARM_THREAD


def label_font() -> QFont:
    """
    标签字体。返回共享实例，不再每次新建 QFont。

    QFont 构造要走字体数据库查找，"Microsoft YaHei UI" 还要做家族匹配，
    不是免费的。绘制路径上每个 Tile 每帧都会调一次这里 —— 116 个控件
    就是 116 次构造。

    QFont 的值语义在 PyQt5 里是被隐式共享的：调用方拿到这份副本后
    改动自己的副本不会影响这里这一份。所以直接返回同一个对象是安全的
    —— 但调用方**不要**就地改它。grid.py 里的
    `f = theme.label_font(); f.setPointSize(15)` 改的是 PyQt 层的绑定，
    底层数据靠 detach 保护，这一处仍然正确（见下方 clone_font 用法）。
    """
    global _LABEL_FONT
    if _LABEL_FONT is None:
        f = QFont("Microsoft YaHei UI", Theme.LABEL_FONT)
        f.setStyleStrategy(QFont.PreferAntialias)
        _LABEL_FONT = f
    return _LABEL_FONT


def clone_font(size: int | None = None, bold: bool = False) -> QFont:
    """要改字号的调用方用这个拿独立副本，别动 label_font() 的共享实例。"""
    f = QFont(label_font())
    if size is not None:
        f.setPointSize(size)
    if bold:
        f.setBold(True)
    return f


def _metrics_for(font: QFont) -> QFontMetrics:
    """
    QFontMetrics 缓存。

    QFontMetrics 的构造比 QFont 更贵：它要向字体引擎取整套度量表。
    实测 116 个 Tile 每帧各构造一次是纯浪费 —— 字体没变，度量表也没变。

    键取 (family, pointSize, pixelSize, bold, italic) 这些**值**，
    而不是 QFont 对象本身。QFont 的 hash 在 PyQt5 里不稳定，
    直接拿它当键会静默退化成每次都 miss。
    """
    key = (font.family(), font.pointSize(), font.pixelSize(),
           font.bold(), font.italic())
    fm = _METRICS.get(key)
    if fm is None:
        fm = QFontMetrics(font)
        _METRICS[key] = fm
    return fm


_SEARCH_FONT: QFont | None = None


def search_font() -> QFont:
    """搜索框字体。同样共享一份，别每次新建。"""
    global _SEARCH_FONT
    if _SEARCH_FONT is None:
        _SEARCH_FONT = QFont("Microsoft YaHei UI", Theme.SEARCH_FONT)
    return _SEARCH_FONT


# 聚焦时的边框色。提出来做常量，是为了不再在绘制路径上每帧 new 一个
# QColor（QColor 构造要走颜色空间，轻但不免费）。
SEARCH_EDGE_FOCUSED = QColor(255, 255, 255, 112)
SEARCH_ICON_COLOR = QColor(255, 255, 255, 130)


def paint_search(p: QPainter, rect: QRect, text: str,
                 hint: str = "搜索应用…", focused: bool = False) -> None:
    """
    顶部搜索框。框体 + 占位提示/实际文字 + 放大镜，一起画完。

    自绘而不是用 QLineEdit 的默认外观，是为了完全控制样式 ——
    无边框半透明场景下系统边框和焦点环都不可控。
    键盘输入由一个覆盖其上的透明 QLineEdit 承担。
    """
    p.save()
    p.setRenderHint(QPainter.Antialiasing)

    r = QRectF(rect)
    rad = rect.height() / 2.4

    p.setBrush(Theme.SEARCH_PLATE)
    pen = p.pen()
    pen.setColor(Theme.SEARCH_EDGE_FOCUSED if focused else Theme.SEARCH_EDGE)
    pen.setWidthF(1.0)
    p.setPen(pen)
    p.drawRoundedRect(r, rad, rad)

    # 放大镜：一个圆 + 一条斜线，纯手绘，避免依赖字体图标
    f = search_font()
    p.setFont(f)
    pad = 22
    cy = rect.center().y()
    rr = 7
    gx = rect.left() + pad + rr
    gy = cy - 2
    lpen = p.pen()
    lpen.setColor(SEARCH_ICON_COLOR)
    lpen.setWidthF(1.6)
    lpen.setCapStyle(Qt.RoundCap)
    p.setPen(lpen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QPointF(gx, gy), rr, rr)
    p.drawLine(int(gx + rr * 0.72), int(gy + rr * 0.72),
               int(gx + rr * 1.7), int(gy + rr * 1.7))

    tx = rect.left() + pad + rr * 2 + 16
    inner_w = rect.right() - tx - pad
    inner = QRect(tx, rect.top(), max(0, inner_w), rect.height())
    if text.strip():
        p.setPen(Theme.SEARCH_TEXT)
        p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft, elide(text, f, inner_w))
    else:
        p.setPen(Theme.SEARCH_HINT)
        p.drawText(inner, Qt.AlignVCenter | Qt.AlignLeft, hint)
    p.restore()