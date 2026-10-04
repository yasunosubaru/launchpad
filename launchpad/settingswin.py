# -*- coding: utf-8 -*-
"""
设置窗口。

基于 ``settings.Settings``（只读其接口，不改动 settings.py）。
所有可调项来自 SCHEMA，改动实时预览，取消可回滚。

设计要点
--------

**一、控件组合：滑块 + 数字框，不是二选一。**
只用滑块，用户没法精确填 13 列；只用数字框，116 个图标调列数要拖
上百次。所以每一项数值都是「滑块 + 数字框」双向绑定：拖滑块是粗调，
数字框是精调，两边永远一致。

**二、枚举值原样透传。**
下拉框显示中文，写回的必须是 settings.py 里那个字符串本身。
``page_easing`` 的 ``"spring"`` 尤其重要：布局 agent 正在实现对应的
物理动画，这里只负责透传，不解释、不校验、不改写。

**三、取消是真回滚。**
打开时拍一份快照。取消时把快照值写回 Settings 并回调 on_apply，
主窗口会跟着退回改之前的排布 —— 只把内存里的值不动是不行的，
那样界面上还留着改了一半的列数，看起来就像取消失灵了。

配色沿用 theme.py 的纯黑深色主题；系统默认的浅灰 QGroupBox / QSlider
放在纯黑界面上会非常刺眼，必须整套覆盖。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPalette, QPen
from PyQt5.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                             QGroupBox, QHBoxLayout, QLabel, QPushButton,
                             QScrollArea, QSlider, QSpinBox, QVBoxLayout,
                             QWidget)

from . import theme
from .settings import EASINGS, LAYOUT_MODES, SCHEMA, SORT_MODES

# ══════════════════════════════════════════════════════════════
# 界面规格（数据驱动：加一项只改这张表）
# ══════════════════════════════════════════════════════════════
#
# kind:
#   "int"   滑块 + 数字框（整数）
#   "float" 滑块 + 数字框（带小数，滑块按 scale 放大）
#   "bool"  复选框
#   "enum"  下拉框，值为 options 里的英文原串
#
# hint 是字段下面的一行灰字，说明这个设置什么时候生效 —— 放在这里是因为
# 「改了没反应」是这类窗口最高频的投诉。

LAYOUT_MODES_CN = {"grid": "规则网格", "flow": "按名称流式", "compact": "紧凑"}
SORT_MODES_CN = {"name": "名称正序", "name_rev": "名称倒序",
                 "recent": "最近使用", "manual": "手动排序"}
PAGING_STYLES_CN = {"smooth": "顺滑移动", "instant": "直接切换"}
EASINGS_CN = {
    "outQuint": "缓出（五次）",
    "outCubic": "缓出（三次）",
    "inOutCubic": "缓入缓出（三次）",
    "outExpo": "缓出（指数）",
    "spring": "弹簧（物理回弹）",
}

GROUP_SPECS: tuple = (
    ("布局", (
        ("columns", "每行列数", "int", 1, "改变后主窗口立即重排"),
        ("rows", "每页行数", "int", 1, "与列数共同决定每页图标个数"),
        ("icon_size", "图标大小", "int", 8, "像素"),
        ("icon_gap", "图标与文字间距", "int", None, "像素，0 = 贴紧"),
        ("label_lines", "文字行数", "int", None, "双行适合长名字"),
        ("layout_mode", "排列方式", "enum", None,
         "流式/紧凑由排版引擎提供，未实现时回退规则网格"),
        ("cell_ratio", "单元格宽高比", "float", 5, "1.0 = 方形格子"),
        ("margin", "网格外边距", "int", 4, "像素"),
    )),
    ("排列与排序", (
        ("sort_mode", "排序方式", "enum", None,
         "下次载入图标（重新载入图标 / 重启）时生效"),
        ("pinyin_search", "搜索支持拼音首字母", "bool", None,
         "输入 py 即搜到「拼音」"),
    )),
    ("动效", (
        ("reduce_motion", "减弱动效", "bool", None,
         "打开后下面几项动画一律不播，优先考虑此项"),
        ("open_anim", "启动窗口淡入", "bool", None, None),
        ("open_duration", "淡入时长", "int", 10, "毫秒"),
        ("paging_animation", "翻页动画", "bool", None, None),
        ("paging_style", "翻页方式", "enum", None, None),
        ("page_duration", "翻页时长", "int", 10, "毫秒"),
        ("page_easing", "缓动曲线", "enum", None,
         "「弹簧」需要新版排版引擎的物理动画支持"),
        ("hover_anim", "悬停放大", "bool", None, None),
    )),
    ("行为", (
        ("hide_labels", "隐藏图标文字", "bool", None,
         "同时会关掉文字的空白区命中判定"),
        ("click_through_empty", "左键点空白退出", "bool", None,
         "关掉后左键点空白无反应；右键点空白始终打开菜单"),
        ("hide_after_launch", "启动后收起启动器", "bool", None,
         "关掉（默认）= 点图标启动后启动器留在原地，可以接着点下一个；"
         "打开 = macOS 的行为，点一下就走"),
        ("wheel_gesture_ms", "滚轮手势节流", "int", 10, "毫秒。"
         "一次滑动只翻一页；调小 = 连划容易一次滑两页，"
         "调大 = 要划快一点才翻第二页"),
    )),
)

# 这些键一变就必须重排主窗口，on_apply 里据此决定要不要重建
LAYOUT_KEYS = frozenset({
    "columns", "rows", "icon_size", "icon_gap", "label_lines",
    "layout_mode", "cell_ratio", "margin", "hide_labels",
})
# 这些键由「减弱动效」总开关控制
MOTION_KEYS = ("open_anim", "open_duration", "paging_animation",
               "paging_style", "page_duration", "page_easing", "hover_anim")


def clamp(key: str, value):
    """
    按 SCHEMA 把值钳制到合法区间。

    和 settings._coerce 一样是防御性的第二道闸：QSpinBox 自己会钳制，
    但下拉框、setValue、以及测试里直接塞进去的值不一定走那条路。
    非法枚举一律回落到该项默认值，而不是静默放行。
    """
    default, typ, lo, hi = SCHEMA[key]
    if typ is bool:
        return bool(value) if isinstance(value, (bool, int, float)) else default
    if typ is int:
        try:
            return max(lo, min(hi, int(round(float(value)))))
        except (TypeError, ValueError):
            return default
    if typ is float:
        try:
            return max(lo, min(hi, float(value)))
        except (TypeError, ValueError):
            return default
    if not isinstance(value, str):
        return default
    allowed = {"layout_mode": LAYOUT_MODES, "sort_mode": SORT_MODES,
               "paging_style": ("smooth", "instant"), "page_easing": EASINGS}
    if key in allowed and value not in allowed[key]:
        return default
    return value


def _hex(c) -> str:
    """QColor -> '#rrggbb'。"""
    return c.name()


def _rgba(c, alpha: int) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


def _qss() -> str:
    """
    整套深色样式，**配色取自 theme.py**，不另起一套。

    项目是纯黑深色主题，系统默认的浅灰 QGroupBox / QSlider 摆在纯黑
    旁边会非常刺眼，所以必须整套覆盖。这里读 theme.Theme 而不是手写
    十六进制，是为了让主窗口以后调色时设置窗口自动跟着变 —— 两处各
    写一份颜色，迟早只会改其中一处。

    对应关系：
        Theme.BG_GLOW  #1c202a → 面板底色（在纯黑上提一级才看得出边界）
        Theme.LABEL_COLOR     → 主文字
        Theme.SEARCH_EDGE     → 控件描边与滑块轨道
        强调蓝 #2b6cb0        → 与 grid.py 右键菜单的选中色一致
    """
    T = theme.Theme
    text = _hex(T.LABEL_COLOR)
    accent = "#2b6cb0"
    accent_hi = "#3a7fd0"
    edge = _rgba(T.SEARCH_EDGE, 235)
    groove = _rgba(T.SEARCH_EDGE, 150)
    dim = _hex(T.BG_GLOW).replace("#1c202a", "#8d95a5")   # 次要文字
    disabled = "#5a606e"
    dlg = "#0b0d12"
    field = "#171b23"
    return f"""
    QDialog{{background:{dlg};color:{text};}}
    QGroupBox{{
        border:1px solid {edge};border-radius:10px;
        margin-top:16px;padding:12px 14px 14px 14px;
        background:#11141a;font-size:13px;
    }}
    QGroupBox::title{{
        subcontrol-origin:margin;left:14px;padding:0 6px;
        color:{dim};font-size:12px;
    }}
    QLabel{{color:{text};font-size:13px;}}
    QLabel#hint{{color:{dim};font-size:11px;}}
    QLabel#title{{color:#ffffff;font-size:14px;font-weight:600;}}
    QSlider::groove:horizontal{{height:4px;background:{groove};border-radius:2px;}}
    QSlider::sub-page:horizontal{{background:{accent};border-radius:2px;}}
    QSlider::handle:horizontal{{
        background:{text};border:1px solid {edge};
        width:13px;height:13px;margin:-6px 0;border-radius:7px;
    }}
    QSlider::handle:horizontal:hover{{background:#ffffff;}}
    QSlider::handle:horizontal:disabled{{background:{disabled};}}
    /* QDoubleSpinBox 必须与 QSpinBox 一起写。
       两者都直接继承 QAbstractSpinBox，**不是**彼此的子类 ——
       只写 QSpinBox 规则的话 QDoubleSpinBox 一条都匹配不上，
       会用系统默认的浅色外观（实测「单元格宽高比」那一行是个白框）。
       注意：这段是 f-string 里的 CSS 注释，里面不能出现单层花括号，
       否则会被当成空表达式直接 SyntaxError。 */
    QSpinBox,QDoubleSpinBox{{
        background:{field};color:{text};border:1px solid {edge};
        border-radius:6px;padding:3px 6px;min-width:78px;
        selection-background-color:{accent};
    }}
    QSpinBox:focus,QDoubleSpinBox:focus{{border-color:{accent_hi};}}
    QSpinBox:disabled,QDoubleSpinBox:disabled{{
        color:{disabled};background:#12151b;
    }}
    QSpinBox::up-button,QDoubleSpinBox::up-button{{
        subcontrol-origin:border;width:15px;border:none;
        border-left:1px solid {edge};border-bottom:1px solid {edge};
    }}
    QSpinBox::down-button,QDoubleSpinBox::down-button{{
        subcontrol-origin:border;width:15px;border:none;
        border-left:1px solid {edge};
    }}
    QComboBox{{
        background:{field};color:{text};border:1px solid {edge};
        border-radius:6px;padding:4px 26px 4px 10px;min-width:170px;
    }}
    QComboBox:focus{{border-color:{accent_hi};}}
    QComboBox:disabled{{color:{disabled};}}
    QComboBox QAbstractItemView{{
        background:{field};color:{text};
        selection-background-color:{accent};border:1px solid {edge};
        outline:none;padding:4px;
    }}
    /* 箭头自己用 CSS 三角形画。写 image:none 是不够的 ——
       Windows 风格仍会用系统原语画一个浅色箭头，压在文字上，
       「规则网格」会被盖成「规则网柊」。深色主题下这个箭头
       必须是深色面板上的浅色三角，所以干脆自己画。 */
    QComboBox::drop-down{{
        subcontrol-origin:padding;subcontrol-position:center right;
        border:none;width:0px;
    }}
    QComboBox::down-arrow{{image:none;width:0;height:0;}}
    QCheckBox{{color:{text};font-size:13px;spacing:8px;}}
    QCheckBox::indicator{{
        width:15px;height:15px;border-radius:4px;
        border:1px solid #39404f;background:{field};
    }}
    QCheckBox::indicator:checked{{background:{accent};border-color:{accent_hi};}}
    QCheckBox::indicator:disabled{{border-color:#262b35;background:#12151b;}}
    QPushButton{{
        background:{field};color:{text};border:1px solid {edge};
        border-radius:7px;padding:7px 16px;font-size:13px;
    }}
    QPushButton:hover{{background:#1e232d;border-color:#3a4150;}}
    QPushButton:pressed{{background:{accent};color:#fff;border-color:{accent_hi};}}
    QPushButton#primary{{background:{accent};color:#fff;border-color:{accent_hi};}}
    QPushButton#primary:hover{{background:{accent_hi};}}
    QScrollArea{{border:none;background:{dlg};}}
    QScrollBar:vertical{{background:{dlg};width:10px;margin:0;}}
    QScrollBar::handle:vertical{{background:#2a2e38;border-radius:5px;min-height:30px;}}
    QScrollBar::handle:vertical:hover{{background:#3a4150;}}
    QScrollBar::add-line,QScrollBar::sub-line{{height:0;width:0;}}
    QScrollBar::add-page,QScrollBar::sub-page{{background:transparent;}}
    """


# ══════════════════════════════════════════════════════════════
# 单行编辑器
# ══════════════════════════════════════════════════════════════


class ChevronComboBox(QComboBox):
    """
    下拉框，自己在右上角画一个倒三角。

    不用 QSS 的 ::down-arrow 画三角：那条路走不通 ——
    写 ``border-*`` 拼三角，Qt 会当成 0x0 的盒子画出一个**小方块**
    （实测就是个灰方块）；写 ``image:none``，Windows 风格又会用系统
    原语补一个浅色箭头，正好压在文字上，「规则网格」被盖成「规则网柊」。
    两条路都要靠猜样式引擎怎么实现。

    paintEvent 里画三角是确定的：几个 drawLine，任何风格下都长一样。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(28)

    def paintEvent(self, ev):
        super().paintEvent(ev)
        # 右侧留 22px 给三角，样式表里的 padding-right 与此保持一致
        w = self.width()
        h = self.height()
        cy = h // 2
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(self.palette().color(QPalette.Text)
                   if self.isEnabled() else QColor(90, 96, 110))
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        x = w - 15
        p.drawLine(x - 4, cy - 2, x, cy + 2)
        p.drawLine(x, cy + 2, x + 4, cy - 2)
        p.end()


class ValueRow(QWidget):
    """一行设置：标签 +（滑块 + 数字框 | 复选框 | 下拉框）+ 说明。"""

    # 带上自己的 key，调用方不用靠 sender() 反查。
    # 之前用 self.sender() 是错的：signal 由 ValueRow 发出，
    # SettingsWindow.sender() 拿到的并不是这一行，key 永远解析不出来，
    # 改动会被静默丢掉。
    changed = pyqtSignal(str, object)

    def __init__(self, key: str, title: str, kind: str, step=None,
                 hint: str = "", parent=None):
        super().__init__(parent)
        self.key = key
        self.kind = kind
        self._silent = False
        self._scale = 1                 # 浮点项才放大到 100
        lo, hi = SCHEMA[key][2], SCHEMA[key][3]

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)

        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(10)
        self.label = QLabel(title)
        self.label.setMinimumWidth(126)
        line.addWidget(self.label)

        if kind in ("int", "float"):
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setFocusPolicy(Qt.StrongFocus)
            if kind == "int":
                self.spin = QSpinBox()
                self.slider.setRange(int(lo), int(hi))
                self.slider.setSingleStep(int(step or 1))
                self.spin.setRange(int(lo), int(hi))
                self.spin.setSingleStep(int(step or 1))
            else:
                # QSpinBox 只能填整数，浮点项必须用 QDoubleSpinBox。
                # （早先图省事用了 QSpinBox.setDecimals —— 那个方法
                #   不存在，运行到第一行就 AttributeError。）
                self.spin = QDoubleSpinBox()
                self.spin.setDecimals(2)
                # 浮点没法直接塞进 QSlider，按 scale 放大成整数再换算。
                # scale=100 对应 0.01 精度，再高只会让滑块在小数位上抖。
                self._scale = 100
                self.slider.setRange(int(round(lo * self._scale)),
                                     int(round(hi * self._scale)))
                self.slider.setSingleStep(
                    int(round((step or 0.05) * self._scale)))
                self.spin.setRange(lo, hi)
                self.spin.setSingleStep(step or 0.05)
            self.slider.valueChanged.connect(self._from_slider)
            self.spin.valueChanged.connect(self._from_spin)
            line.addWidget(self.slider, 1)
            line.addWidget(self.spin)
        elif kind == "bool":
            self.check = QCheckBox()
            self.check.toggled.connect(self._from_check)
            line.addWidget(self.check)
            line.addStretch(1)
        else:
            self.combo = ChevronComboBox()
            opts = {"layout_mode": LAYOUT_MODES, "sort_mode": SORT_MODES,
                    "paging_style": ("smooth", "instant"),
                    "page_easing": EASINGS}[key]
            cn = {"layout_mode": LAYOUT_MODES_CN, "sort_mode": SORT_MODES_CN,
                  "paging_style": PAGING_STYLES_CN,
                  "page_easing": EASINGS_CN}[key]
            for o in opts:
                # 原文串存在 itemData 里：显示中文，写回英文
                self.combo.addItem(cn.get(o, o), o)
            self.combo.currentIndexChanged.connect(self._from_combo)
            line.addWidget(self.combo, 1)
            line.addStretch(1)

        outer.addLayout(line)
        if hint:
            h = QLabel(hint)
            h.setObjectName("hint")
            outer.addWidget(h)

    # ── 取值 ──────────────────────────────────────────────
    def value(self):
        if self.kind == "bool":
            return self.check.isChecked()
        if self.kind == "enum":
            return self.combo.currentData()
        return self.spin.value()

    def set_value(self, v) -> None:
        """静默写值。不触发 changed —— 程序性同步不能被当成用户改动。"""
        self._silent = True
        try:
            v = clamp(self.key, v)
            if self.kind == "bool":
                self.check.setChecked(bool(v))
            elif self.kind == "enum":
                idx = self.combo.findData(v)
                if idx < 0:                 # 未知值回落默认，绝不留在空项上
                    idx = self.combo.findData(SCHEMA[self.key][0])
                self.combo.setCurrentIndex(max(0, idx))
            else:
                self.spin.setValue(v)
                self._push_to_slider()
        finally:
            self._silent = False

    def set_row_enabled(self, on: bool) -> None:
        for w in (getattr(self, "slider", None), getattr(self, "spin", None),
                  getattr(self, "check", None), getattr(self, "combo", None)):
            if w is not None:
                w.setEnabled(on)

    # ── 信号汇聚 ──────────────────────────────────────────
    def _emit(self, v) -> None:
        if self._silent:
            return
        self.changed.emit(self.key, clamp(self.key, v))

    def _push_to_slider(self) -> None:
        v = self.spin.value()
        self._silent = True
        try:
            self.slider.setValue(int(round(v * self._scale)))
        finally:
            self._silent = False

    def _from_slider(self, raw: int) -> None:
        """
        滑块 -> 数字框。

        两处必须做对：

        1. **整数项要转成 int 再喂给 QSpinBox。** 直接把 ``raw / 1`` 的
           浮点结果 setValue 进去会抛 TypeError；而 PyQt5 规定「槽函数
           里未捕获的异常 = abort()」，整个进程直接以 0xC0000409 崩掉，
           连 traceback 都来不及打。这是实测踩过的，不是假想。

        2. **只由 _from_spin 发信号**，这里不再自己发一次。早先两个都发，
           拖一次滑块会回调两次 on_apply。

        已经是同一档位时更不能发：那不是改动。
        """
        v = raw / self._scale if self.kind == "float" else int(raw)
        if abs(self.spin.value() - v) <= 1e-9:
            return
        self.spin.setValue(v)          # 由 _from_spin 收敛并 emit

    def _from_spin(self, v) -> None:
        self._push_to_slider()
        self._emit(v)

    def _from_check(self, v: bool) -> None:
        self._emit(bool(v))

    def _from_combo(self, _idx: int) -> None:
        self._emit(self.combo.currentData())


# ══════════════════════════════════════════════════════════════
# 设置窗口
# ══════════════════════════════════════════════════════════════


class SettingsWindow(QDialog):
    """
    设置对话框。

        win = SettingsWindow(settings, parent=main, on_apply=cb)
        win.exec_()

    on_apply(changed: dict) 每当某项值变化就被调用一次，``changed`` 只含
    **自上次回调以来真正变过的键**，方便调用方做最小重排。
    保存时再调用一次（全量），取消时调用一次（回滚到打开时的快照）。
    """

    def __init__(self, settings, parent=None, on_apply=None):
        super().__init__(parent)
        self.settings = settings
        self.on_apply = on_apply
        self.rows: dict[str, ValueRow] = {}
        self._pending: dict = {}
        self._suspend = False

        self.setWindowTitle("Launchpad 设置")
        self.setModal(False)             # 不模态：主窗口还要实时预览
        self.setStyleSheet(_qss())
        self.resize(640, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 14)
        root.setSpacing(12)

        t = QLabel("设置")
        t.setObjectName("title")
        root.addWidget(t)
        sub = QLabel("改动即时生效；取消会回到打开时的状态。")
        sub.setObjectName("hint")
        root.addWidget(sub)

        # ── 分组表单（放进滚动区，小屏幕也不会被裁掉） ──────
        body = QWidget()
        body.setStyleSheet("background:#0b0d12;")
        form = QVBoxLayout(body)
        form.setContentsMargins(0, 0, 6, 0)
        form.setSpacing(12)
        for gtitle, specs in GROUP_SPECS:
            gb = QGroupBox(gtitle)
            gl = QVBoxLayout(gb)
            gl.setContentsMargins(6, 6, 6, 6)
            gl.setSpacing(10)
            for key, title, kind, step, hint in specs:
                row = ValueRow(key, title, kind, step, hint)
                row.changed.connect(self._on_row_changed)
                self.rows[key] = row
                gl.addWidget(row)
            form.addWidget(gb)

        form.addWidget(self._build_system_group())
        form.addStretch(1)

        area = QScrollArea()
        area.setWidget(body)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.NoFrame)
        root.addWidget(area, 1)

        # ── 底部按钮 ────────────────────────────────────
        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.setToolTip("把所有设置恢复为出厂默认值并立即生效")
        self.reset_btn.clicked.connect(self.reset_defaults)
        btns.addWidget(self.reset_btn)
        btns.addStretch(1)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.revert)
        btns.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.commit)
        btns.addWidget(self.save_btn)
        root.addLayout(btns)

        # 打开时的快照：取消要靠它回滚
        self._snapshot = self.settings.as_dict()
        self.load_values()
        self._sync_motion_enabled()
        self._center_on(parent)

    # ── 系统集成（自启 + 快捷方式修复） ──────────────────────
    def _build_system_group(self) -> QGroupBox:
        """
        「系统集成」组：开机自启开关 + 快捷方式图标修复。

        ## 为什么这两项不在 GROUP_SPECS 里

        GROUP_SPECS 的每一项都会被 :meth:`collect` 收进 ``values()``，
        进而写进 ``settings.json``。而这两项的真相**不在 settings.json 里**：
        自启状态存在注册表 ``HKCU\\...\\Run``，图标存在 ``.lnk`` 的
        IconLocation 字段里。把它们做成普通设置项会出现两个真问题：

        1. **状态会分叉。** 用户在「任务管理器 → 启动」里禁用了本程序，
           settings.json 里的 ``autostart=true`` 不会跟着变；下次打开设置
           界面显示的还是「已开启」，而实际开机不启动 —— 开关看起来坏了。
        2. **「保存」会偷偷改系统。** 其它设置项点保存只写一个文件，
           这两项点保存会去动注册表和磁盘上的快捷方式，失败也只能事后
           才知道，无法参与取消/回滚。

        所以这里做成**立即执行的动作**：勾上就写注册表，按钮就重写 .lnk，
        各自在下面那行状态文字里如实回报结果，不进快照、不进取消。

        这一组还刻意**不在设置界面打开时做任何写入**：构造过程中只读
        注册表来填勾选框，写入一律等用户真的去点。这很重要 ——
        设置界面是常被打开的窗口，每次打开都改一次系统状态会掩盖
        「用户到底改没改」这件事。

        顺带一个真实存在过的坑：自启曾经同时存在「注册表 Run 键」和
        「Startup 文件夹里的 .lnk」两处，开机时被拉起两次。所以状态行
        会在两处都在时明确说「有重复」，并给出清理按钮 —— 只显示
        「已开启」会把这个重复状态盖过去。
        """
        gb = QGroupBox("系统集成")

        gl = QVBoxLayout(gb)
        gl.setContentsMargins(6, 6, 6, 6)
        gl.setSpacing(8)

        self.autostart_cb = QCheckBox("开机自动启动（登录后静默进托盘）")
        self.autostart_cb.setToolTip(
            "写入 HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run。"
            "不会在登录时弹全屏界面，只留托盘图标，用热键唤出。")
        self.autostart_cb.stateChanged.connect(self._on_autostart_toggled)
        gl.addWidget(self.autostart_cb)

        self.autostart_status = QLabel()
        self.autostart_status.setObjectName("hint")
        self.autostart_status.setWordWrap(True)
        gl.addWidget(self.autostart_status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.repair_btn = QPushButton("修复开始菜单图标")
        self.repair_btn.setToolTip(
            "重写开始菜单两份 .lnk 的 IconLocation，指向\n"
            "assets\\launchpad.ico。\n"
            "现在它们用的是 Python 的图标，或者根本没设图标。\n"
            "桌面那份不再生成（用户要求桌面上不留东西），\n"
            "所以这个按钮不碰桌面。")
        self.repair_btn.clicked.connect(self._on_repair)
        row.addWidget(self.repair_btn)

        self.dup_btn = QPushButton("清理重复自启")
        self.dup_btn.setToolTip(
            "删掉 Startup 文件夹里的那份 Launchpad.lnk。\n"
            "自启已统一走注册表，两者同时存在会在登录时拉起两次。")
        self.dup_btn.clicked.connect(self._on_cleanup_dup)
        row.addWidget(self.dup_btn)

        self.desk_btn = QPushButton("清理桌面残留")
        self.desk_btn.setToolTip(
            "删掉桌面上的 Launchpad.lnk（如果还在）。\n"
            "启动器只从开始菜单和托盘进，桌面上不留东西。")
        self.desk_btn.clicked.connect(self._on_cleanup_desktop)
        row.addWidget(self.desk_btn)
        row.addStretch(1)
        gl.addLayout(row)

        self.repair_status = QLabel()
        self.repair_status.setObjectName("hint")
        self.repair_status.setWordWrap(True)
        gl.addWidget(self.repair_status)

        self._refresh_autostart_ui()
        return gb

    def _refresh_autostart_ui(self) -> None:
        """按注册表的真实状态刷新勾选框和状态行（不触发写入）。"""
        from . import shortcuts as SC
        try:
            st = SC.autostart_status()
        except Exception as exc:
            self.autostart_cb.setChecked(False)
            self.autostart_status.setText(f"读不到自启状态：{exc}")
            return

        # 加载界面时阻断信号，避免「刷新显示」被当成「用户改了开关」
        # 而去写一遍注册表。
        self._suspend = True
        try:
            self.autostart_cb.setChecked(bool(st["enabled"]))
        finally:
            self._suspend = False

        if st["startup_link_exists"]:
            self.autostart_status.setText(
                "已开启 —— 但 Startup 文件夹里还有一份重复的，"
                "登录时会被拉起两次。点「清理重复自启」删掉它。")
        elif st["enabled"]:
            self.autostart_status.setText("已开启（注册表 Run 键，登录后静默进托盘）")
        else:
            self.autostart_status.setText("未开启")

    def _on_autostart_toggled(self, _state: int) -> None:
        from . import shortcuts as SC
        if self._suspend:
            return
        want = self.autostart_cb.isChecked()
        try:
            ok = SC.set_autostart(want)
        except Exception as exc:
            self.repair_status.setText(f"写自启失败：{exc}")
            self._refresh_autostart_ui()      # 勾选框弹回真实状态
            return
        if not ok:
            self.repair_status.setText(
                "写注册表失败（HKCU Run 键没写进去），已恢复原状态。")
            self._refresh_autostart_ui()
            return
        self._refresh_autostart_ui()

    def _on_repair(self) -> None:
        from . import shortcuts as SC
        try:
            rep = SC.repair_all(shortcut=True)
        except Exception as exc:
            self.repair_status.setText(f"修复失败：{exc}")
            return

        parts = [f"重写了 {rep['ok']} 个快捷方式"]
        if rep["fail"]:
            parts.append(f"{len(rep['fail'])} 个失败（{rep['fail'][0]}）")
        if rep["skipped_icon"]:
            parts.append(
                f"{len(rep['skipped_icon'])} 个没设图标 —— "
                f"assets\\launchpad.ico 不存在，先跑 python make_icon.py")
        self.repair_status.setText("；".join(parts))

    def _on_cleanup_dup(self) -> None:
        from . import shortcuts as SC
        try:
            removed = SC.cleanup_duplicate_autostart()
        except Exception as exc:
            self.repair_status.setText(f"清理失败：{exc}")
            return
        self.repair_status.setText(
            "已删除 Startup 文件夹里的重复项" if removed
            else "Startup 文件夹里本来就没有重复项")
        self._refresh_autostart_ui()

    def _on_cleanup_desktop(self) -> None:
        """删掉桌面上的 Launchpad.lnk。

        桌面上原来那个是**坏桩**（target 是记事本、图标指向不存在的
        一个根本不存在的 .ico 路径、CWD 为空），根本不是能用的启动器入口。
        用户要求「桌面上不要留东西」，所以正确处理是删掉而不是修好。
        这个按钮留着是为了清掉升级过程中可能又被写回去的残留 ——
        ``shortcuts.py`` 已经完全不再往桌面写，但别人手工复制的、
        或者旧版本留下的，仍然需要一条清理路径。
        """
        from . import shortcuts as SC
        try:
            removed = SC.remove_desktop_link()
        except Exception as exc:
            self.repair_status.setText(f"清理失败：{exc}")
            return
        self.repair_status.setText(
            "已删除桌面上的 Launchpad.lnk（启动器只从开始菜单和托盘进）"
            if removed else "桌面上本来就没有 Launchpad.lnk")

    # ── 取值 ──────────────────────────────────────────────
    def load_values(self) -> None:
        """把 Settings 里的值刷进界面（静默，不触发 on_apply）。"""
        data = self.settings.as_dict()
        self._suspend = True
        try:
            for key, row in self.rows.items():
                if key in data:
                    row.set_value(data[key])
        finally:
            self._suspend = False
        self._pending.clear()
        # 自启勾选框也要跟着刷新：设置窗口可以一直开着，而用户在
        # 「任务管理器 → 启动」里禁用本程序、或用别的工具改了注册表，
        # 都不会经过本进程。切回这个窗口时必须显示真实状态。
        self._refresh_autostart_ui()

    def values(self) -> dict:
        """界面上当前的完整取值（已钳制）。"""
        return {k: r.value() for k, r in self.rows.items()}

    def collect(self) -> dict:
        """
        完整取值，且每一项都过一遍 clamp。

        钳制放在这里而不是只靠控件：QSpinBox 会钳制自己那份，但
        ``Values.set_value``、外部直接写 combo、以及测试直接构造
        脏值，都不一定走控件的钳制。
        """
        return {k: clamp(k, r.value()) for k, r in self.rows.items()}

    # ── 实时预览 ──────────────────────────────────────────
    def _on_row_changed(self, key: str, value) -> None:
        """某一行被用户改动。立即写内存并回调，主窗口据此实时重排。"""
        if self._suspend:
            return
        self._pending[key] = value
        self.settings.set(key, value)         # 内存即时生效（预览用）
        self._sync_motion_enabled()
        self._flush()

    def _flush(self) -> None:
        if not self._pending or self.on_apply is None:
            self._pending.clear()
            return
        changed = dict(self._pending)
        self._pending.clear()
        try:
            self.on_apply(changed)
        except Exception as exc:
            print(f"[Settings] on_apply 出错: {exc!r}")

    def _sync_motion_enabled(self) -> None:
        """「减弱动效」打开时，把动效项置灰 —— 让这个开关真的有语义。"""
        off = bool(self.settings.get("reduce_motion"))
        for key in MOTION_KEYS:
            row = self.rows.get(key)
            if row is not None:
                row.set_row_enabled(not off)

    # ── 动作 ──────────────────────────────────────────────
    def reset_defaults(self) -> None:
        """
        全部恢复默认。写进 Settings 并回调，落盘仍由「保存」负责 ——
        点一下就改磁盘上的文件、没给撤销机会，太粗暴。
        """
        defaults = {k: v[0] for k, v in SCHEMA.items()}
        self._suspend = True
        try:
            for key, row in self.rows.items():
                row.set_value(defaults[key])
        finally:
            self._suspend = False
        for key in self.rows:
            self._pending[key] = defaults[key]
        self.settings.update(defaults)
        self._sync_motion_enabled()
        self._flush()
        self.reset_btn.setText("已恢复默认")
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(1400, lambda: self.reset_btn.setText("恢复默认"))

    def revert(self) -> None:
        """
        取消：回滚到打开时的快照，并通知主窗口重排回去。

        「哪些键变了」必须在**恢复之前**算。恢复之后再拿快照跟
        自己的快照比，永远相等，changed 恒为空 —— 主窗口收不到任何
        通知，界面就停在改了一半的列数上，看起来正好是「取消没反应」。
        """
        before = self.settings.as_dict()
        touched = {k: v for k, v in self._snapshot.items()
                   if before.get(k) != v}

        self._suspend = True
        try:
            for key, row in self.rows.items():
                if key in self._snapshot:
                    row.set_value(self._snapshot[key])
        finally:
            self._suspend = False
        self.settings.update(self._snapshot)
        self._sync_motion_enabled()
        if self.on_apply is not None:
            try:
                self.on_apply(touched or dict(self._snapshot))
            except Exception as exc:
                print(f"[Settings] 回滚回调出错: {exc!r}")
        self._pending.clear()
        self.reject()

    def commit(self) -> None:
        """保存：钳制 → 写 Settings → 落盘 → 回调全量。"""
        values = self.collect()
        self.settings.update(values)
        try:
            self.settings.save()
        except Exception as exc:
            print(f"[Settings] 写入失败: {exc!r}")
        if self.on_apply is not None:
            try:
                self.on_apply(values)
            except Exception as exc:
                print(f"[Settings] on_apply 出错: {exc!r}")
        self._pending.clear()
        self.accept()

    # ── 便捷 ──────────────────────────────────────────────
    def set_value(self, key: str, value) -> None:
        """程序化改一项（走和用户操作同一条链路，会触发预览）。"""
        row = self.rows.get(key)
        if row is not None:
            row.set_value(value)
            self._on_row_changed(key, clamp(key, value))
        else:
            self.settings.set(key, value)

    def layout_changed(self, changed: dict) -> bool:
        """这批改动里有没有会影响排版的键。给 window.py 判断用。"""
        return bool(LAYOUT_KEYS & changed.keys())

    def _center_on(self, parent) -> None:
        if parent is None:
            scr = self.screen() if hasattr(self, "screen") else None
            if scr is not None:
                g = scr.availableGeometry()
                self.move(g.center().x() - self.width() // 2,
                          g.center().y() - self.height() // 2)
            return
        g = parent.frameGeometry()
        self.move(g.center().x() - self.width() // 2,
                  max(0, g.center().y() - self.height() // 2))


# 最近打开的设置窗口。必须留一个长命引用：
# open_settings() 返回后局部变量就没了，只剩 exec_() 内部那一个，
# 一旦中途被 GC，窗口会在用户眼前凭空消失。
_last_window: "SettingsWindow | None" = None


def open_settings(settings, parent=None, on_apply=None) -> int:
    """
    打开设置窗口的便捷入口。返回 exec_() 的结果（Accepted / Rejected）。
    window.py 的右键空白只要调这一个函数。
    """
    global _last_window
    w = SettingsWindow(settings, parent=parent, on_apply=on_apply)
    _last_window = w
    return w.exec_()
