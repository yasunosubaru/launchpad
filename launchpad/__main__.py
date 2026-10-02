# -*- coding: utf-8 -*-
"""
入口。

三个必须做对的事：
1. 单实例 —— 重复启动时把已有实例唤出，而不是开第二个窗口抢同一个热键。
2. 首次运行自动导入源目录 —— 装完即用，不要求用户手动添加。
3. 路径与 cwd 无关 —— 开机自启时 CWD 是 System32，用相对路径会崩。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import QObject, Qt, pyqtSignal                      # noqa: E402
from PyQt5.QtWidgets import QApplication                              # noqa: E402

from launchpad import paths                                           # noqa: E402
from launchpad.icons import warm_in_background                        # noqa: E402
from launchpad.library import Library, db_file                        # noqa: E402
from launchpad.paths import DEFAULT_SOURCE                            # noqa: E402
from launchpad.settings import Settings                               # noqa: E402
from launchpad.window import Launchpad                                # noqa: E402

APP_TITLE = "Launchpad"


def acquire_single_instance():
    """
    Windows 命名互斥体。拿到 None 说明已有实例在跑。
    句柄必须存到模块级变量，否则被 GC 掉后互斥体失效。
    """
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = wintypes.HANDLE
    handle = k.CreateMutexW(None, False, "Local\\LaunchpadSingleInstance")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())

    # 必须用 ctypes.get_last_error()，不能用
    # ctypes.windll.kernel32.GetLastError()。
    # 后者是**另一个** WinDLL 对象（没开 use_last_error），
    # 它读到的是 0，于是"已有实例"永远检测不到 —— 表现为：
    # 重复点桌面快捷方式会开出第二个进程，两个实例抢同一批热键，
    # 后启动的那个四个热键全部报"被其它程序占用"。
    # 实测：ctypes.get_last_error() = 183（正确），windll 版本 = 0。
    already = ctypes.get_last_error() == 183   # ERROR_ALREADY_EXISTS
    return handle, already


def notify_existing_instance() -> bool:
    """给已有实例发自定义消息，让它显示窗口。"""
    from launchpad.hotkeys import notify_running_instance
    return notify_running_instance()


def main() -> int:
    mutex, already = acquire_single_instance()
    if already:
        notify_existing_instance()
        return 0
    sys._launchpad_mutex = mutex      # 保持引用

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    lib = Library()
    try:
        lib.load()
    except Exception as exc:
        print(f"[Fatal] {exc}")
        return 1

    # 首次运行（或库为空）自动导入源目录，用户装完即用
    if not lib.entries:
        if DEFAULT_SOURCE.is_dir():
            rep = lib.import_folder(DEFAULT_SOURCE)
            print(f"[Import] 首次导入 {rep['added']} 个应用，"
                  f"跳过重复 {rep['skipped']}，失败 {rep['failed']}")
        else:
            # **必须说一句话。** 源目录不存在时静默跳过，用户看到的现象是
            # 「启动器打开了但一个图标都没有」，完全不知道该往哪放快捷方式。
            print(f"[Import] 源目录不存在，跳过自动导入：{DEFAULT_SOURCE}")
            print("[Import] 右键空白 →「添加应用…」可以拖 .lnk 进来；"
                  "或把快捷方式放到上面这个目录再重启。")
            print(f"[Import] 也可以设 {paths.DEFAULT_SOURCE_ENV} 环境变量"
                  f"指定别的目录。")

    # 用户设置必须在这里加载并传给窗口，否则设置界面改什么都不生效
    # —— 窗口会退回自己的默认 7×5，看起来像「保存了但没反应」。
    settings = Settings()
    settings.load()
    if settings.load_error:
        # 损坏的配置不该静默吞掉：用户会以为自己改了生效了。
        print(f"[Settings] {settings.load_error}")

    win = Launchpad(lib, settings.get("columns"), settings.get("rows"),
                    settings=settings)
    win.setWindowTitle(APP_TITLE)

    # 预热图标在后台线程做，避免首次显示卡住。
    # 必须持有返回的 QThread —— 它是局部变量的话，main() 返回后立刻被 GC，
    # 触发 "QThread: Destroyed while thread is still running" 并让进程退出。
    win._warm_thread = warm_in_background(lib.entries, settings.icon_size)

    # 热键：独立原生线程 + message-only 窗口。必须在主窗口显示前就绪，
    # 否则启动初期按热键无响应。
    from launchpad.hotkeys import Hotkeys
    win._hotkeys = Hotkeys(lambda: win.show_me())
    win._hotkeys.start()

    if "--show" in sys.argv:
        win.show_me()

    return app.exec_()


def theme_size() -> int:
    """保留给外部调用者；内部一律走 settings.icon_size。"""
    from launchpad import theme
    return theme.Theme.ICON_SIZE


if __name__ == "__main__":
    sys.exit(main())