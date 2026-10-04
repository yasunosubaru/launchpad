# -*- coding: utf-8 -*-
"""
系统托盘图标：窗口收起之后，程序唯一的常驻入口。

── 为什么需要它 ──────────────────────────────────────────

Launchpad 的窗口不是普通的「可以关掉」窗口。``__main__.py`` 里
``app.setQuitOnLastWindowClosed(False)``，``window.hide_me()`` 又**刻意不调
``hide()``** —— 全屏表面只创建一次，之后靠「不透明度 0 + 鼠标穿透」假装收起
（理由见 window.py 的性能对比：每次重建全屏表面都要让 OS 重新合成整屏，
实测每次按热键都来一次 40~50ms 卡顿）。

这套设计带来一个必然的推论：**「隐藏」不等于「退出」**。窗口在任何时刻都
``isVisible() == True``，所以

* 想知道「用户现在看得见吗」→ **不能看 isVisible()**，得看不透明度；
* 想结束进程 → **不能靠关窗口**，只能走 ``QApplication.quit()`` 让
  ``app.exec_()`` 返回。

这两件事在别的启动器里经常被混成同一个「最小化」，这里必须分开，否则用户
点了「退出」发现图标还在托盘里、或者按了热键唤不出任何东西。

── 为什么不 import window ────────────────────────────────

窗口对象是**鸭子类型**传进来的，本模块只调它的 ``show_me`` / ``hide_me`` /
``_reload_icons`` 三个方法，不 import ``launchpad.window``。
window.py 以后想反过来 import 本模块（比如加个托盘快捷键）时就不会循环依赖。

── 左键为什么只接 Trigger，不接 DoubleClick ──────────────

Windows 对双击发的是 ``WM_LBUTTONDBLCLK``，Qt 因此发 ``DoubleClick`` 而**不是**
第二个 ``Trigger``。若两个都当「切换」处理，双击就会被切换两次 = 净效果为零，
用户会发现「双击图标没反应」。所以只处理 ``Trigger``：双击时第一次点击已经
切换过一次，正好符合直觉。

── 右键为什么不自己 popup ────────────────────────────────

用了 ``setContextMenu()`` 之后，Qt 自己会在收到 ``WM_RBUTTONUP`` 时先发
``activated(Context)``、**再**自己 ``popup()`` 一次。若在 ``Context`` 分支里
手动 ``popup()``，同一位置会弹两次（闪烁、且第二次可能落在错的活动窗口上）。
所以这里明确只区分、不重复弹。
"""

import os
import sys

from PyQt5.QtCore import QObject
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (QAction, QApplication, QMenu, QSystemTrayIcon,
                             QWidget)

from . import paths

# 窗口「用户看不见」的不透明度阈值。
#
# 不用 0.0 / 非 0 的二值判断：淡入淡出是 QVariantAnimation 逐帧插值的，
# 中途的不透明度在 (0, 1) 之间。取 0.5 是为了让「淡出动画还没跑完时又点了
# 托盘」这件事落到「显示」这一侧 —— 那正是用户想要的（点一下又冒出来了）。
OPACITY_SHOWN = 0.5


def _icon_path():
    """
    解析自带 .ico 的路径；解析不出来才返回 None。

    注意这里**只解析、不检查文件是否存在**：把「路径算不出来」和「算出来了
    但文件没了」混成同一个 None 的话，``reason()`` 就只能报一句「路径缺失」，
    而后者其实是 assets/ 被裁掉这种可诊断的情况，路径本身很有用。
    """
    try:
        return paths.app_icon_file()
    except Exception as exc:
        # 缺失只该是「没图标」，不该是「程序起不来」。assets/ 可能被裁掉过。
        print(f"[Tray] 解析应用图标路径失败（不影响运行）: {exc}")
    return None


class Tray:
    """
    托盘图标 + 右键菜单。

    用法::

        tray = Tray(launchpad_window)     # 内部自己建 QSystemTrayIcon
        if tray.available():
            tray.show()

    调用方**必须**自己留住这个实例（``tray = Tray(win)`` 赋给一个变量）。
    C++ 对象另有 parent 兜底，但 Python 侧的方法连接不该依赖 GC 恰好不跑。
    """

    def __init__(self, window):
        """
        :param window: 鸭子类型的窗口对象，只需有
                       ``show_me`` / ``hide_me`` / ``_reload_icons``。
                       若是 QObject（正常情况）会被用作托盘和菜单的 parent，
                       保证生命周期跟着窗口走，不会被 GC 提前收掉。
        """
        self._window = window
        self._reason = ""      # available() 为 False 时的原因，调试用

        # 两个对象的 parent 要求**不一样**，不能图省事共用一个：
        #   QSystemTrayIcon(parent: QObject)  —— 任何 QObject 都行；
        #   QMenu(parent: QWidget)             —— 只收 QWidget，传 QSystemTrayIcon
        #                                        直接 TypeError（overload 不匹配）。
        # 正常情况窗口就是 QWidget，两个都能用；测试里的假窗口不是，所以各自判断。
        self._tray_parent = window if isinstance(window, QObject) else None
        self._menu_parent = window if isinstance(window, QWidget) else None

        self._icon = None
        self._path = _icon_path()
        if self._path is not None and self._path.is_file():
            try:
                # 从 .ico 构造；读不出内容时 QIcon.isNull() 为 True，
                # 那和「文件根本不在」一样当不可用，不去给托盘塞一个空图标。
                self._icon = QIcon(str(self._path))
                if self._icon.isNull():
                    self._icon = None
            except Exception as exc:
                print(f"[Tray] 载入应用图标失败（不影响运行）: {exc}")
                self._icon = None

        # 这两个类都有重载（QMenu 还多一个 title 版），位置传 None 有歧义风险，
        # 所以没有 parent 时就明确调无参构造。
        self._tray = (QSystemTrayIcon() if self._tray_parent is None
                      else QSystemTrayIcon(self._tray_parent))
        if self._icon is not None:
            self._tray.setIcon(self._icon)
        self._tray.setToolTip(self._tooltip_text())

        # 菜单挂到窗口上：Qt 负责在右键时弹出，位置/DPI/焦点都正确。
        # 菜单本身不 parent 到托盘图标 —— 见上面 parent 类型那两条注释。
        self._menu = QMenu() if self._menu_parent is None else QMenu(self._menu_parent)
        self._build_menu()

        self._tray.activated.connect(self._on_activated)

    # ── 菜单 ────────────────────────────────────────────────

    def _build_menu(self) -> None:
        """按需求的顺序建菜单项。分隔线只出现在「重新载入图标」之后。"""
        self._act_show = QAction("显示 Launchpad", self._menu)
        self._act_show.triggered.connect(self._on_show)

        self._act_hide = QAction("隐藏", self._menu)
        self._act_hide.triggered.connect(self._on_hide)

        self._act_open = QAction("打开「应用库文件夹」", self._menu)
        self._act_open.triggered.connect(self._on_open_dir)

        self._act_reload = QAction("重新载入图标", self._menu)
        self._act_reload.triggered.connect(self._on_reload)

        self._act_quit = QAction("退出", self._menu)
        self._act_quit.triggered.connect(self._on_quit)

        for a in (self._act_show, self._act_hide, self._act_open,
                  self._act_reload):
            self._menu.addAction(a)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)

    # ── 对外 API ────────────────────────────────────────────

    def available(self) -> bool:
        """
        托盘能不能用。

        两个条件都要满足：系统真的有托盘（``isSystemTrayAvailable``），以及
        图标真的载入成功了。图标缺失时返回 False 而不是抛异常 —— 精简部署
        下 assets/ 可能不在，那只该是没有图标。
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._reason = "系统托盘不可用"
            return False
        if self._icon is None:
            # 把路径带上：assets/ 被裁掉时，用户至少知道该去补哪个文件。
            self._reason = f"图标不可用: {self._path}"
            return False
        self._reason = ""
        return True

    def reason(self) -> str:
        """available() 为 False 时的原因，便于调用方打印一句人话。"""
        return self._reason

    def icon(self):
        """返回 QIcon（不可用时为 None）。"""
        return self._icon

    def menu(self):
        """返回 QMenu，供测试断言菜单项；正常路径下没人需要用它。"""
        return self._menu

    def show(self) -> None:
        """把图标放进托盘。不可用时安静地什么都不做。"""
        if self._icon is None or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self._refresh_tooltip()
        self._tray.show()

    def hide(self) -> None:
        """把图标从托盘移走（不等于退出程序）。"""
        self._tray.hide()

    # ── 信号处理 ────────────────────────────────────────────

    def _on_activated(self, reason) -> None:
        """
        左键单击 = 显示/隐藏切换。

        只处理 Trigger：DoubleClick 在 Windows 上是独立消息、双击再当一次
        切换会互相抵消（见模块 docstring）。MiddleClick 和 Context 也不动：
        前者没人用，后者由 setContextMenu 交给 Qt 自己弹。
        """
        if reason == QSystemTrayIcon.Trigger:
            self._toggle()

    def _toggle(self) -> None:
        """
        显示 ⇄ 隐藏。

        判据是**不透明度**而不是 isVisible()：常驻窗口在任何状态下
        ``isVisible()`` 都是 True，用它会导致「永远判定为可见」，
        于是每次单击都只会重复调 ``show_me()``，单击切换彻底失效。
        """
        if self._is_showing():
            self._call("hide_me")
        else:
            self._call("show_me")
        self._refresh_tooltip()

    def _is_showing(self) -> bool:
        """
        用户当前看不看得见窗口。

        **优先问窗口自己的 ``is_showing()``**，别的一律不猜。理由是这两个
        看起来最顺手的判据在真实运行里都会给出与实际不符的答案：

        * ``windowOpacity()`` —— QWidget 的初值就是 1.0，而开机自启
          （无 ``--show``）跑完 main() 之后窗口从没被显示过：屏幕上什么都
          没有，不透明度却是 1.0。结果是自启后**第一次**单击托盘去调
          ``hide_me()``，用户看到的就是「托盘点了没反应」。
        * ``isVisible()`` —— 窗口刻意不调 ``hide()``（见模块 docstring），
          收起后仍然恒为 True，于是单击永远只会重复 ``show_me()``，
          切换功能彻底失效。

        下面的 opacity / isVisible 分支只留给测试里的假窗口和将来
        非本项目的窗口对象，真实窗口一定会走到第一段。
        """
        w = self._window
        own = getattr(w, "is_showing", None)
        if callable(own):
            try:
                return bool(own())
            except Exception:
                pass
        op = getattr(w, "windowOpacity", None)
        if callable(op):
            try:
                return float(op()) > OPACITY_SHOWN
            except Exception:
                pass
        vis = getattr(w, "isVisible", None)
        if callable(vis):
            try:
                return bool(vis())
            except Exception:
                pass
        return False

    # ── 菜单动作 ────────────────────────────────────────────

    def _on_show(self) -> None:
        self._call("show_me")
        self._refresh_tooltip()

    def _on_hide(self) -> None:
        self._call("hide_me")
        self._refresh_tooltip()

    def _on_open_dir(self) -> None:
        """用资源管理器打开 %APPDATA%\\Launchpad。失败只打印，不打断程序。"""
        try:
            d = paths.app_dir()
        except Exception as exc:
            print(f"[Tray] 取应用库目录失败: {exc}")
            return
        startfile = getattr(os, "startfile", None)   # 只有 Windows 有
        if startfile is None:
            print(f"[Tray] 当前平台没有 os.startfile，跳过打开: {d}")
            return
        try:
            startfile(str(d))
        except Exception as exc:
            # 目录不存在 / 没有 shell / 权限不足都会走到这里。
            print(f"[Tray] 打开应用库目录失败: {d} — {exc}")

    def _on_reload(self) -> None:
        self._call("_reload_icons")

    def _on_quit(self) -> None:
        """
        真正结束进程。

        **绝不能看窗口的可见性来「优化」这段。** 常驻窗口收起时
        ``isVisible()`` 仍是 True，如果哪天有人图省事写成
        「窗口不可见就顺便退出」，用户点一次「隐藏」程序就没了。

        唯一正确的收尾是 ``QApplication.quit()``：它让 ``app.exec_()``
        直接返回，``__main__.main()`` 随即 return，进程结束。窗口因为
        ``setQuitOnLastWindowClosed(False)`` 从来不会「最后一个窗口关闭」
        而自动退出，所以 quit() 是必须的、也是唯一的一条路。

        热键线程是 daemon=True（hotkeys.py），不会把进程吊住，不用管。
        """
        self._tray.hide()          # 别让图标在进程收尾期间还挂在托盘上
        print("[Tray] 退出")
        QApplication.quit()

    # ── 小工具 ──────────────────────────────────────────────

    def _call(self, name: str) -> None:
        """调窗口的同名方法；不存在或抛异常都不许把托盘搞崩。"""
        fn = getattr(self._window, name, None)
        if not callable(fn):
            print(f"[Tray] 窗口对象没有 {name}()，跳过")
            return
        try:
            fn()
        except Exception as exc:
            # 托盘是程序的唯一常驻入口，它一崩程序就成了没有出口的僵尸。
            print(f"[Tray] 调用 {name}() 失败: {exc}")

    def _tooltip_text(self) -> str:
        state = "已显示" if self._is_showing() else "已收起"
        return f"{paths.APP_TITLE} — {state}（左键切换，右键菜单）"

    def _refresh_tooltip(self) -> None:
        """图标不可用时连 tooltip 都不去设，避免在 null icon 上做无用功。"""
        if self._icon is None:
            return
        try:
            self._tray.setToolTip(self._tooltip_text())
        except Exception:
            pass


# 单独跑本文件可以打印一次自检信息，方便在真实会话里确认托盘是否可用。
# ``isSystemTrayAvailable()`` 在没有托盘的机器（服务、部分远程会话）上返回
# False，那时静默启动比弹一个报错框友好得多 —— 主程序照样能用热键。
if __name__ == "__main__":
    _app = QApplication(sys.argv)
    _t = Tray(None)
    print(f"[Tray] available={_t.available()}  reason={_t.reason()!r}  "
          f"icon={_t.icon()}  path={_t._path}")
