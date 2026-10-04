# -*- coding: utf-8 -*-
"""
全局热键 —— 纯 Win32 原生实现，跑在独立线程。

为什么完全绕开 Qt
-----------------
实测：把 RegisterHotKey 绑到任何 QWidget 的 HWND 上，往那个 HWND
PostMessage 一个 WM_HOTKEY，Qt 的 nativeEvent() **一次都不会被调用**。
试过主窗口，试过专门建的 1x1 隐藏窗口，GetWindowLongW 确认样式是正常
WS_POPUP —— 不是窗口类型的问题，是 Qt 只把它自己关心的消息派发到
Python，WM_HOTKEY 不在其中。Qt 路线走不通。

于是自己开原生消息循环：
- CreateWindowEx 造 message-only 窗口（HWND_MESSAGE = -3）
  这类窗口不显示、不占屏幕、不在窗口列表里，是 Windows 官方推荐的
  消息接收端
- RegisterHotKey 绑在它上面
- 自己的 WndProc 处理 WM_HOTKEY / WM_SHOW_ME
- 事件转回 GUI 线程（跨线程操作 QWidget 会崩）

实现上踩过的三个坑，都写在对应位置的注释里。
"""

import ctypes
import ctypes.wintypes as wintypes
import threading

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_SHOW_ME = WM_APP + 1
WM_DESTROY = 0x0002

HWND_MESSAGE = wintypes.HWND(-3)
WND_CLASS_NAME = "LaunchpadHotkeyHost"

# 实测本机占用情况（2026-10）：
#   Ctrl+Space / Ctrl+Alt+Space  -> 被输入法等程序占用
#   Ctrl+Alt+K / F9             -> 可用
BINDINGS = [
    ("Ctrl+Alt+K", MOD_CONTROL | MOD_ALT, ord("K")),
    ("F9", 0, 0x78),
    ("Ctrl+Space", MOD_CONTROL, ord(" ")),
    ("Ctrl+Alt+Space", MOD_CONTROL | MOD_ALT, ord(" ")),
]

LRESULT = ctypes.c_ssize_t          # 64 位下 LRESULT 是 LONG_PTR
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASS(ctypes.Structure):
    """
    字段类型必须和 Win32 完全一致。
    第一版用 wintypes.UINT 拼 lpfnWndProc 之外的字段、结构体大小对不上，
    CreateWindowExW 直接抛 access violation —— 栈被踩了。
    """
    _fields_ = [
        ("style", ctypes.c_uint),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


def _apis():
    """
    显式声明 Win32 函数签名。

    这一步是必须的，不是可选的：ctypes 默认把整数参数当 32 位，
    而 WPARAM/LPARAM 在 64 位下是 64 位。用未声明签名的
    ctypes.windll.user32 调 DefWindowProcW，遇到大指针值会抛
    "argument 4: OverflowError: int too long to convert"，
    异常被 ctypes 吞掉 -> 窗口消息全丢 -> CreateWindowExW 失败。
    """
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    u.RegisterClassW.restype = ctypes.c_ushort

    u.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
    u.CreateWindowExW.restype = wintypes.HWND

    u.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.DefWindowProcW.restype = LRESULT

    u.DestroyWindow.argtypes = [wintypes.HWND]
    u.DestroyWindow.restype = wintypes.BOOL

    u.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int,
                                 ctypes.c_uint, ctypes.c_uint]
    u.RegisterHotKey.restype = wintypes.BOOL

    u.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    u.UnregisterHotKey.restype = wintypes.BOOL

    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              ctypes.c_uint, ctypes.c_uint]
    u.GetMessageW.restype = ctypes.c_int

    u.PostMessageW.argtypes = [wintypes.HWND, ctypes.c_uint,
                               wintypes.WPARAM, wintypes.LPARAM]
    u.PostMessageW.restype = wintypes.BOOL

    u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    u.FindWindowW.restype = wintypes.HWND

    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE

    return u, k


# ── 跨线程回调桥 ─────────────────────────────────────────
_gui_queue: list = []
_callback = [None]      # [0] 存真正的业务回调
_pump_timer = None


def enqueue_to_gui(fn) -> None:
    """
    从原生线程把调用排进 GUI 线程的事件循环。

    直接在原生线程里操作 QWidget 是未定义行为（会崩），
    所以只往队列塞函数，由常驻定时器在 GUI 线程取走。
    """
    _gui_queue.append(fn)
    _ensure_pump()


def _ensure_pump() -> None:
    """
    装一个常驻定时器把队列排进 GUI 线程。

    必须从 GUI 线程调用（在 Hotkeys.start() 里显式装一次）。
    从原生线程 new QTimer 是未定义行为，会静默失败 —— 实测就是这么
    排查出来的：定时器没装上，队列只进不出，热键永远唤不出窗口。
    """
    global _pump_timer
    if _pump_timer is not None:
        return
    from PyQt5.QtCore import QTimer
    t = QTimer()
    t.setInterval(30)
    t.timeout.connect(_flush)
    t.start()
    _pump_timer = t            # 必须持有引用，否则被 GC 后不再触发


def _flush() -> None:
    while _gui_queue:
        fn = _gui_queue.pop(0)
        try:
            fn()
        except Exception as exc:
            print(f"[Hotkey] 回调异常: {exc}")


def QApplication_instance():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance()


# ── 模块级 wndproc ───────────────────────────────────────
def _wndproc(hwnd, msg, wparam, lparam):
    """
    模块级窗口过程。

    必须是模块级 + 用配好签名的 user32 句柄：
    用 ctypes.windll.user32.DefWindowProcW（无签名）会把 64 位 LPARAM
    按 32 位解析，抛 OverflowError 后被 ctypes 吞掉，所有消息丢失。
    """
    if msg in (WM_HOTKEY, WM_SHOW_ME):
        cb = _callback[0]
        if cb:
            enqueue_to_gui(cb)
        return 0
    if msg == WM_DESTROY:
        return 0
    u, _ = _apis()
    return u.DefWindowProcW(hwnd, msg, wparam, lparam)


# WNDPROC 实例必须全局保活。放局部变量的话，CreateWindowExW 返回后
# 它可能被 GC，Windows 随后调用已释放的函数指针 -> 访问冲突。
_WNDPROC_KEEPALIVE = WNDPROC(_wndproc)


class Hotkeys:
    def __init__(self, on_trigger):
        _callback[0] = on_trigger
        self._thread: threading.Thread | None = None
        self._hwnd = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._quit = False
        self.ok: list[str] = []
        self.failed: list[str] = []

    def start(self) -> bool:
        # 定时器必须在 GUI 线程装。必须在起原生线程**之前**调用 ——
        # 否则原生线程可能在定时器就绪前就投递了事件。
        _ensure_pump()
        self._thread = threading.Thread(target=self._run,
                                        name="launchpad-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(5.0)
        return bool(self.ok)

    def stop(self) -> None:
        self._quit = True
        with self._lock:
            if self._hwnd:
                u, _ = _apis()
                u.DestroyWindow(self._hwnd)
                self._hwnd = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def summary(self) -> str:
        """
        一句人类可读的「按哪个键唤出」。

        存在的理由：这个程序正常是 pythonw.exe 启动的，**没有控制台**，
        ``[Hotkey] 可用: ...`` 那行 print 在真实使用方式下必定被丢弃。
        于是「我到底该按哪个键」在界面上无处可查 —— 用户只能一个个试，
        或者干脆改用别的方式（比如触控板手势）唤出。这句要摆到
        **看得见的地方**：托盘 tooltip 和设置窗口。

        注意 ``ok`` 是**实际注册成功**的，不是 ``BINDINGS`` 里声明的。
        两者可能不同（被输入法等程序占用时），照实说。
        """
        if self.ok:
            keys = " / ".join(self.ok)
        else:
            keys = "（都没注册成功）"
        s = f"唤出：{keys}"
        if self.failed:
            s += f"　（{', '.join(self.failed)} 被占用）"
        return s

    def _run(self) -> None:
        u, k = _apis()
        hinst = k.GetModuleHandleW(None)

        cls = WNDCLASS()
        cls.lpfnWndProc = _WNDPROC_KEEPALIVE
        cls.hInstance = hinst
        cls.lpszClassName = WND_CLASS_NAME
        u.RegisterClassW(ctypes.byref(cls))     # 失败通常只是重复注册

        hwnd = u.CreateWindowExW(
            0, WND_CLASS_NAME, WND_CLASS_NAME, 0,
            0, 0, 0, 0,
            HWND_MESSAGE, None, hinst, None)
        if not hwnd:
            print(f"[Hotkey] 创建消息窗口失败 err={ctypes.get_last_error()}")
            self._ready.set()
            return

        with self._lock:
            self._hwnd = hwnd

        rid = 0xB000
        for name, mods, vk in BINDINGS:
            rid += 1
            if u.RegisterHotKey(hwnd, rid, mods | MOD_NOREPEAT, vk):
                self.ok.append(name)
            else:
                self.failed.append(name)
                err = ctypes.get_last_error()
                why = "被其它程序占用" if err == 1409 else f"错误码 {err}"
                print(f"[Hotkey] {name} 不可用：{why}")
        if self.ok:
            print(f"[Hotkey] 可用: {', '.join(self.ok)}")
        if not self.ok:
            print("[Hotkey] 没有可用热键，请用桌面快捷方式启动。")

        self._ready.set()

        msg = wintypes.MSG()
        while not self._quit:
            r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

        for i in range(1, len(BINDINGS) + 1):
            u.UnregisterHotKey(hwnd, 0xB000 + i)


def notify_running_instance() -> bool:
    """找到已运行的实例（消息窗口）并让它显示主界面。"""
    u, _ = _apis()
    hwnd = u.FindWindowW(WND_CLASS_NAME, WND_CLASS_NAME)
    if not hwnd:
        return False
    u.PostMessageW(hwnd, WM_SHOW_ME, 0, 0)
    return True