# -*- coding: utf-8 -*-
"""
主窗口：全屏纯黑、顶部搜索框、中部图标网格、底部页码点。

刻意保持简单：
- 不用 QScrollArea/QStackedWidget，直接按几何绝对定位
- 不用 QLineEdit 的默认外观（系统边框不可控），改用自绘 + 透明 QLineEdit 收键盘
- 不做实时背景渲染、不做玻璃折射 —— 用户明确要纯黑

键盘/滚轮全部收在 eventFilter 里。
原因（实测）：Qt 只把键盘事件发给**焦点控件**，滚轮发给**光标下控件**。
启动器唤出时焦点在搜索框的 QLineEdit 上，于是：
  - 方向键被输入框当光标移动吃掉，永远到不了网格
  - 滚轮在 Qt 层收不到
必须在事件送达目标前截住。
"""

import ctypes
import subprocess
import sys
import time
from pathlib import Path

from PyQt5.QtCore import (QEasingCurve, QEvent, QPoint, QRect, Qt, QTimer,
                          QVariantAnimation, pyqtSignal)
from PyQt5.QtGui import QColor, QIcon, QKeyEvent, QPainter
from PyQt5.QtWidgets import (QApplication, QFileDialog, QLineEdit,
                             QMessageBox, QWidget)

from . import theme
from .grid import Grid
from .icons import IconCache
from .library import Entry, Library, load_pinyin
from .pagedots import PageDots
from .paths import app_icon_file
from .settings import Settings


class Launchpad(QWidget):
    """
    注意 nativeEvent 必须是**子类重写**。

    PyQt5 里 `win.nativeEvent = func` 这样赋值不会收到 Windows 消息 ——
    实测过：RegisterHotKey 成功了、PostMessage 也发了，
    回调一次都没被调用，窗口永远不弹出来。Qt 只在 C++ 虚函数被覆盖时
    才把 WM_HOTKEY 派发到 Python。
    """
    def __init__(self, library: Library, columns: int = 7, rows: int = 5,
                 parent=None, settings=None):
        super().__init__(parent)
        self.library = library
        if settings is not None:
            self.settings = settings
        else:
            # 没传配置就用位置参数建一份，测试里 Launchpad(lib, 3, 5)
            # 才真的是 3×5，而不是被默认值 7 覆盖。
            self.settings = Settings()
            self.settings.set("columns", columns)
            self.settings.set("rows", rows)
        self.icons = IconCache(self.settings.icon_size)
        self._py, self._style = load_pinyin()
        self._anim: QVariantAnimation | None = None
        self._closing = False
        self._last_save = 0.0         # 使用记录节流时间戳
        # 淡入淡出进度：1.0 = 完全可见，0.0 = 全黑。
        # 用内容遮罩做，**不用** setWindowOpacity —— 见 paintEvent 的注释。
        self._fade = 1.0
        # True = 全屏表面已创建，之后不再重建（见 show_me 的性能对比）
        self._resident = False

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)

        # Alt-Tab / 任务栏上的图标。不设的话 Windows 用默认的通用程序图标，
        # 一个全屏启动器在 Alt-Tab 里跟别的应用长得一模一样。
        #
        # 图标缺失**静默降级**，不抛：测试环境和某些精简部署下 assets/
        # 可能不在（仓库根目录被裁剪过），那也只该是没有图标，
        # 不该是程序起不来。
        self._app_icon_path = None
        try:
            ico = app_icon_file()
            if ico.is_file():
                self.setWindowIcon(QIcon(str(ico)))
                self._app_icon_path = ico
        except Exception as exc:
            print(f"[Window] 载入应用图标失败（不影响运行）: {exc}")

        # 刻意**不设** WA_TranslucentBackground。
        # 实测（tests/ 里有对照实验）：设了它之后 Qt 会跳过整个窗口的
        # 子控件渲染 —— 控件的 isVisible() 全为 True、单独 grab 每个图标
        # 都正常，但窗口整体渲染出来一个图标都没有。
        # 而且这里根本不需要半透明：背景就是纯黑，paintEvent 每帧铺满，
        # 视觉上和真透明无差别。留着这个属性只有害无益。
        self.setAutoFillBackground(False)

        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.StrongFocus)

        self.grid = Grid(library, self.icons, columns, rows, self,
                         settings=self.settings)
        self.grid.launch_requested.connect(self._launch)
        self.grid.closed_requested.connect(self.hide_me)
        self.grid.add_requested.connect(self._add_apps)
        self.grid.settings_requested.connect(self._open_settings)
        self.grid.reload_requested.connect(self._reload_icons)
        self.grid.delete_requested.connect(self._delete_entry)
        self.grid.about_requested.connect(self._show_about)
        self.grid.page_changed.connect(self._sync_dots)
        self.grid.pages_changed.connect(self._sync_dots)
        # 目标页（动画一开始就发）。**圆点的实时来源必须是它**，
        # 不是 page_changed —— 后者只在动画**结束**时发一次。
        # 滚轮连续翻 3 页时三次 goto 各起一段动画，page_changed 只在最后
        # 一段结束时才发，中间两页没有发信号的机会：实测 24 帧里圆点恒为 0，
        # 而目标页已到 3、动画页位从 0 滑到 -3 —— 圆点一动不动直到落定
        # （用户报「地下标志跟不上页面的节奏」）。
        self.grid.page_intent.connect(self._sync_dots)

        self.dots = PageDots(self)
        self.dots.picked.connect(self._on_dot)
        # 页码点必须能收鼠标事件：它的点击不冒泡到网格的"点空白收起"
        self.dots.installEventFilter(self)

        self._sync_dots()

        self.search = QLineEdit(self)
        self.search.setFrame(False)
        self.search.setAttribute(Qt.WA_TranslucentBackground)
        self.search.setStyleSheet("""
            QLineEdit{ background:transparent; border:none; color:#f0f2f6;
                       selection-background-color:#2b6cb0; padding:0px; }
        """)
        self.search.setPlaceholderText("")
        self.search.textChanged.connect(self._on_search)
        self.search.installEventFilter(self)
        # 收键盘焦点但不抢走整个窗口的焦点策略
        self.search.setFocusPolicy(Qt.StrongFocus)

        self._resize()

    # ── 布局 ──────────────────────────────────────────────
    def _resize(self) -> None:
        w, h = self.width(), self.height()
        T = theme.Theme

        self.search.setGeometry(
            (w - T.SEARCH_W) // 2, 40, T.SEARCH_W, T.SEARCH_H)

        top = 40 + T.SEARCH_H + 36
        bottom = 56                       # 页码条区
        self.grid.setGeometry(0, top, w, max(1, h - top - bottom))

        self.dots.move((w - self.dots.width()) // 2,
                       h - bottom + (bottom - self.dots.height()) // 2)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._resize()
        self.grid.relayout()

    # ── 显示 / 隐藏 ───────────────────────────────────────
    def show_me(self) -> None:
        # 必须先停掉可能还在跑的收起动画并复位 _closing。
        # 否则 hide_me() 里那个 finished 的连接会在淡入中途触发，
        # 把刚显示的窗口又藏起来，表现就是"热键按了没反应/闪一下就没"。
        self._closing = False
        if self._anim is not None:
            self._anim.stop()
            try:
                self._anim.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._anim = None

        # 窗口**常驻**：全屏表面只创建一次，之后靠不透明度 + 鼠标穿透
        # 切换可见性。实测（心跳探针，「唤出→翻页→收起」每轮
        # >33ms 的顿挫次数，各 30 轮）：
        #     每次 showFullScreen() 重建   0.37 次/轮  max 45.96ms
        #     只 show() 不重建             0.43 次/轮  max 49.12ms
        #     常驻 + 一次性设不透明度      0.00 次/轮  max 32.17ms
        # 重建全屏表面那一下，操作系统要重新合成整屏，正好是
        # 每次按 F9 都来一次的 40~50ms 卡顿。
        if not self._resident:
            self.showFullScreen()
            self._resident = True

        # 收起状态下窗口是「透明 + 鼠标穿透」，显示时先恢复这两项，
        # 否则点不到它，或者它会挡住下面的窗口。
        self.setAttribute(Qt.WA_TransparentForMouseEvents, False)
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._activate()
        self._resize()
        self._after_show()

    def _after_show(self) -> None:
        """窗口可见之后要做的事（两条显示路径共用）。"""
        # 每次显示都重建网格。必须放在窗口拿到真实尺寸之后 ——
        # 只有那时排布才有意义。
        # （漏掉这一步的后果是 tiles=0 —— 库里有 116 条但界面全空。）
        # 搜索词保留：用户可能在搜东西时按了热键又切走，切回来不该被清空。
        self.grid.set_search(self.search.text(), self._py)
        self._sync_dots()
        self.search.setFocus()

        # 显式触发整棵子树重绘。
        # 窗口重新可见时 Qt 认为「没有区域变脏」，不再重画，
        # 结果窗口亮起来却是一片空白（render() 截图却正常，
        # 因为那是另一条路径）。逐层 update() 把脏区标出来。
        for t in self.grid._tiles:
            t.update()
        self.grid.update()
        self.dots.update()
        self.update()

        self._start_fade(0.0, 1.0)

    def _start_fade(self, frm: float, to: float) -> None:
        """淡入淡出：动 _fade，由 paintEvent 画黑色遮罩（见那里的注释）。"""
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(theme.Theme.FADE_MS)
        self._anim.setStartValue(float(frm))
        self._anim.setEndValue(float(to))

        def apply(v):
            self._fade = float(v)
            self.update()

        self._anim.valueChanged.connect(apply)
        self._anim.start()

    def hide_me(self) -> None:
        """
        收起：**不调用 hide()**。

        窗口保持「已创建、全屏、不透明 0、鼠标穿透」的状态常驻。
        真正 hide() 掉全屏窗口再重建，就是每次按 F9 都来一次的
        40~50ms 卡顿（见 show_me 的对比数据）。

        鼠标穿透靠 Qt.WA_TransparentForMouseEvents —— 少了这一项，
        这个透明的全屏窗口会把用户所有点击都吃掉，下面的应用全都点不动。
        """
        if self._closing:
            return
        self._closing = True
        if self._anim:
            self._anim.stop()
        self._start_fade(self._fade, 0.0)
        self._anim.finished.connect(self._park)
        self._anim.start()

    def _park(self) -> None:
        """真正藏起来：透明 + 鼠标穿透，但窗口本身留着。"""
        self._fade = 0.0
        self.setWindowOpacity(0.0)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

    def _activate(self) -> None:
        """
        强制置前并接收输入。

        仅 raise() 不够：未激活的置顶工具窗口，其鼠标点击会被 Windows
        判给下层窗口，结果是点页码点时下面的应用被打开、启动器自己点不动。
        """
        try:
            hwnd = int(self.winId())
            u = ctypes.windll.user32
            k = ctypes.windll.kernel32
            HWND_TOPMOST = ctypes.c_void_p(-1)
            u.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                           0x0002 | 0x0001)     # NOMOVE | NOSIZE
            fg = u.GetForegroundWindow()
            cur_tid = k.GetCurrentThreadId()
            fg_tid = u.GetWindowThreadProcessId(fg, None) if fg else cur_tid
            u.AttachThreadInput(cur_tid, fg_tid, True)
            u.BringWindowToTop(hwnd)
            u.SetForegroundWindow(hwnd)
            u.AttachThreadInput(cur_tid, fg_tid, False)
        except Exception as exc:
            print(f"[Window] 置前失败: {exc}")

    # ── 事件拦截 ──────────────────────────────────────────
    def eventFilter(self, obj, ev):
        # Esc 无条件收起（焦点在 QLineEdit 时按 Esc 会被当成"清空输入"）
        if ev.type() == QEvent.KeyPress:
            if ev.key() == Qt.Key_Escape:
                self.hide_me()
                return True
            # 方向键翻页：必须在这里拦，否则被输入框当光标移动吃掉
            k = ev.key()
            if obj is self.search and k in (
                    Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                self.grid.goto(
                    self.grid.page + (1 if k in (Qt.Key_Right, Qt.Key_Down)
                                      else -1))
                return True

        # 滚轮翻页：搜索框和页码点会吞掉滚轮，所以在这里接手。
        #
        # **必须转发给 Grid.wheelEvent，不要自己实现翻页逻辑。**
        # 这里曾经有一份独立的实现，用的是 `self.grid.page` ——
        # 那是动画**结束**才更新的滞后值。用户用力下滑时 20 次滚轮
        # 全部读到同一个旧页码，于是全朝同一页冲：滑一整屏只翻 1 页
        # 就停住，体感就是「卡死」。
        # 转交之后全应用只有一套滚轮逻辑，不会再出现「在图标上无效、
        # 在搜索框上卡顿」这种分裂行为。
        if ev.type() == QEvent.Wheel:
            if ev.angleDelta().y() or ev.pixelDelta().y():
                self.grid.wheelEvent(ev)
                return True

        return super().eventFilter(obj, ev)

    def nativeEvent(self, eventType, message):
        """
        接收 Windows 原生消息。

        热键已经移到 hotkeys.HotkeyWindow（专用宿主），
        这里只保留一个透传，让 Qt 正常处理其它原生事件。
        仍然必须真重写 —— PyQt5 里 `win.nativeEvent = func` 赋值无效。
        """
        return super().nativeEvent(eventType, message)

    def keyPressEvent(self, ev: QKeyEvent):
        if ev.key() == Qt.Key_Escape:
            self.hide_me()
            ev.accept()
            return
        super().keyPressEvent(ev)

    # ── 搜索 ──────────────────────────────────────────────
    def _on_search(self, text: str) -> None:
        self.grid.set_search(text, self._py)
        self.update()                 # 重画搜索框占位提示

    # ── 页码点 ────────────────────────────────────────────
    def _sync_dots(self, *_args) -> None:
        """
        把网格的页数/**目标页**同步给指示器。

        单一数据源：网格发信号，这里只负责显示。
        之前是 show_me 里手动调一次 set_pages，结果绕过 show_me 的调用路径
        （测试、其它入口）页码点就永远停在"1 页" —— 和用户报的
        "页码点完全不可用"是同一类问题：靠调用顺序维持的隐式契约。

        **用的是 `_page_target` 而不是 `page`。**
        `page` 只在动画**结束**时才更新，页码点跟它就等于「页面停稳了
        圆点才动」—— 用户报「标志跟不上页面的节奏」。`_page_target`
        是用户已经要求的页，在 `goto()` 里立刻记账，动画一开始就对，
        语义与 `PageDots.mousePressEvent` 的乐观更新一致。

        权威值仍会纠正乐观值：`_finish_anim` 在动画落定时补发
        `page_intent`，所以拖拽松手回弹之类的路径圆点也会跟回真值。
        """
        n = self.grid.page_count
        self.dots.set_pages(n, self.grid._page_target)
        self.dots.setVisible(n > 1)

    def _on_dot(self, index: int) -> None:
        self.grid.goto(index)

    # ── 启动应用 ──────────────────────────────────────────
    def _launch(self, entry: Entry) -> None:
        if entry.missing:
            # 目标已失效，点了没反应只会让人以为程序坏了
            self.grid._message = f"「{entry.name}」的目标已不存在，无法启动"
            self.grid.update()
            return

        # 走 addapp.launch_entry 而不是自己 Popen：库里现在有一批
        # target = "shell:AppsFolder\<AUMID>" 的条目（Codex 等 UWP 应用，
        # .lnk 读不出 TargetPath，靠字节扫描捞出 AUMID 改写而成）。
        # 这些直接丢给 subprocess.Popen 必然失败 —— 它们不是文件路径，
        # 必须经由 explorer/os.startfile 走 shell 解析。
        # launch_entry 同时还覆盖 http(s)/mailto（.url 快捷方式）。
        try:
            from .addapp import launch_entry
            ok, why = launch_entry(entry)
        except ImportError as exc:
            print(f"[Launch] addapp 模块不可用: {exc}")
            ok, why = False, str(exc)
        if not ok:
            print(f"[Launch] 启动 {entry.name} 失败: {why}")
            self.grid._message = f"「{entry.name}」无法启动：{why}"
            self.grid.update()
            return
        self._mark_used(entry)
        self.hide_me()

    def _mark_used(self, entry: Entry) -> None:
        """记录最近使用时间，供排序方式 recent 用。

        带 60 秒节流：连续点同一个应用时不必每次都重写整个库文件
        （116 个条目，原子写盘不是免费的）。库文件只在真正需要时落盘。
        """
        now = time.time()
        entry.last_used = now
        if now - self._last_save < 60.0:
            return
        self._last_save = now
        try:
            self.library.save()
        except Exception as exc:
            print(f"[Library] 写入使用记录失败: {exc}")

    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "选择包含快捷方式的文件夹")
        if not d:
            return
        rep = self.library.import_folder(Path(d))
        print(f"[Import] 新增 {rep['added']}，重复 {rep['skipped']}，"
              f"失败 {rep['failed']}")
        self._on_search(self.search.text())

    def _add_apps(self) -> None:
        """「添加应用」对话框：拖放 / 浏览文件 / 浏览文件夹 / UWP 列表。"""
        try:
            from .addapp import AddAppDialog
        except ImportError as exc:
            print(f"[AddApp] 模块不可用: {exc}")
            return
        dlg = AddAppDialog(self.library, self)
        if dlg.exec_() == AddAppDialog.Accepted:
            self._on_search(self.search.text())

    def _reload_icons(self) -> None:
        """
        「重新载入图标」：清掉磁盘缓存后重建。

        缓存键**含**签名（源文件 mtime + 大小 + 提取策略版本号），
        所以源图标更新后会自动失效。这个入口是给「我知道之前提过 bug」
        用的手动出口 —— 比如缓存里躺着的是坏图，而提取逻辑改了但
        版本号忘了升。

        旧实现调的 ``invalidate_all`` 这个方法**不存在**（真名
        ``clear_cache``），异常被 except 吞掉只剩一行日志，于是这个
        菜单项点了**什么都不发生**、界面上也不报错。冒烟测试抓到的：
        日志里只有「清理缓存失败」，磁盘上一张图没删。
        """
        try:
            n = self.icons.clear_cache()
            print(f"[Icons] 已清理 {n} 个缓存图标")
        except Exception as exc:
            print(f"[Icons] 清理缓存失败: {exc}")
        self.grid.rebuild()
        self._on_search(self.search.text())

    def _delete_entry(self, entry: Entry) -> None:
        """
        「从启动器移除」。

        顺序是刻意的：**先确认、再改内存、最后落盘**。
        ``library.remove()`` 自己会 save()，而 save() 是原子写
        （临时文件 + os.replace）—— 成功即持久化，失败不会毁掉整个库。

        失败时**必须**把界面恢复原状：内存里已经少了这条，但磁盘上还在。
        不回滚的话，界面少一个图标、重启后它又回来，用户会以为删除失败
        但又说不出哪里不对。
        """
        try:
            from .removedlg import DeleteDialog
        except ImportError as exc:
            print(f"[Delete] 模块不可用: {exc}")
            return

        dlg = DeleteDialog(entry, self)
        if dlg.exec_() != DeleteDialog.Accepted:
            return

        # **顺序**：先落盘，成功了再改内存里的列表。
        #
        # 反过来做（先改内存、save() 失败再报错）的话，界面会少一个图标
        # 而磁盘上那条还在 —— 用户重启一次它就回来了，而且中途退出的话
        # 内存状态直接丢失，删除等于没发生。实测（注入 save 抛异常）：
        # 先改内存的写法会让库条目数从 2 掉到 1，而磁盘仍是 2 条。
        try:
            ok = self.library.remove(entry.uid)
        except Exception as exc:
            print(f"[Delete] 删除失败: {exc}")
            self.grid._message = f"移除失败：{exc}"
            self.grid.update()
            return

        if not ok:
            # uid 对不上：条目已经被别处改掉了。刷新一次让界面说实话。
            self._on_search(self.search.text())
            return

        # 库里的条目对象换了一批，_last_built_query 的身份比较会自然失配，
        # 于是 set_search 一定会重建 —— 不需要手动作废那个记账。
        self._on_search(self.search.text())
        print(f"[Delete] 已从启动器移除：{entry.name}")

    def _show_about(self) -> None:
        """关于对话框。"""
        n = len(self.library.entries)
        QMessageBox.about(
            self, "关于 Launchpad",
            f"<b>Launchpad</b><br><br>"
            f"启动器当前收录 <b>{n}</b> 个应用。<br>"
            f"<br>"
            f"左键点击图标启动；滚轮或方向键翻页；"
            f"右键图标可将其从启动器移除（不会删除源文件）；"
            f"右键空白处打开菜单。")

    def _open_settings(self) -> None:
        """右键空白 → 设置窗口。改动实时预览，取消真回滚。"""
        try:
            from .settingswin import SettingsWindow
        except ImportError as exc:
            print(f"[Settings] 模块不可用: {exc}")
            return
        before = self.settings.as_dict()
        dlg = SettingsWindow(self.settings, parent=self,
                             on_apply=self._apply_settings)
        if dlg.exec_() == SettingsWindow.Accepted:
            self.settings.save()
        else:
            # 取消：把改动集回滚回去，并让界面跟着回滚
            self.settings.update(before)
            self._apply_settings(before)
        self._on_search(self.search.text())

    def _apply_settings(self, changed=None) -> None:
        """设置改动生效。图标尺寸是 Tile 构造参数，grid 会自行重建控件。"""
        s = self.settings
        self.icons = self.icons  # 同一实例，仅为可读性
        self.grid.apply_settings(s)
        self._resize()
        self._sync_dots()

    # ── 绘制 ──────────────────────────────────────────────
    def paintEvent(self, ev):
        p = QPainter(self)
        theme.paint_background(p, self.width(), self.height())

        r = self.search.geometry()
        theme.paint_search(p, r, self.search.text())

        # 淡入淡出用**内容遮罩**，不要用 setWindowOpacity。
        #
        # 实测（心跳探针，5ms 定时器，直接量 GUI 线程被占多久）：
        # 每按一次 F9 的完整循环里，>33ms 的断档
        #     现状（逐帧 setWindowOpacity）  1.33 次/轮，max 70.7ms
        #     去掉淡入淡出                  0.40 次/轮，max 53.5ms
        # 原因是全屏窗口每改一次不透明度，DWM 就得把 2560x1600 的整个
        # 表面带 alpha 重新合成一遍 —— 而一帧只淡 1/130，最多 8~18 帧，
        # 每次都触发一次整屏合成。
        # 改成在自己 paintEvent 里盖一层纯黑，就是一次普通填充，
        # 视觉效果完全一样，成本低一个量级。
        if self._fade < 1.0:
            p.fillRect(self.rect(), QColor(0, 0, 0,
                                           int((1.0 - self._fade) * 255)))
        p.end()