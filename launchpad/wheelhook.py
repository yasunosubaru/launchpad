# -*- coding: utf-8 -*-
"""
触控板两指滑动唤出启动器 —— WH_MOUSE_LL 低级鼠标钩子。

为什么需要它
------------
窗口**常驻**：收起时只是「不透明度 0 + 鼠标穿透」，从不调 ``hide()``
（重建全屏表面要 OS 重新合成整屏，实测每次 40~50ms，见 window.py 的对比数据）。
于是收起之后 HWND 还在，但 ``WA_TransparentForMouseEvents = True``，
Qt 一个滚轮事件都收不到 —— 没法像 Launchpad 那样用滚动翻页。

热键虽然注册了（Ctrl+Alt+K / F9 / Ctrl+Space / Ctrl+Alt+Space），但用户反馈
唤出不可靠，而他手上是**触控板**。触控板在 Win32 里走 HID 鼠标用法，驱动把
两指滑动翻译成 ``WM_MOUSEWHEEL`` —— 能收到，但窗口穿透了收不到。
所以只能在系统层面旁路观察：用 ``WH_MOUSE_LL`` 低级鼠标钩子。

设计上的三个坑（踩中任何一个都很难查，所以写在最前面）
----------------------------------------------------
1. **绝不吞事件。** 低级钩子链是阻塞式的：回调返回非 0 就等于吃掉该事件，
   后续钩子和目标窗口都收不到。钩子链上除了我们自己还有人（各种隐私/自动化/
   手势增强工具），我们必须**对每一个事件**调用 ``CallNextHookEx``，一个都不能
   漏 —— 哪怕它刚被判成「唤出手势」。不转发的话，全系统所有程序的滚轮都会失效
   （浏览器滚不动、Excel 滚不动），这是灾难级故障，而且极难定位到某个几十行的
   钩子文件上。我们是**旁路观察者**，不是拦截器。

2. **钩子回调不在 Qt 主线程上。** ``WH_MOUSE_LL`` 的回调在**安装钩子的那个线程**
   上执行（不是"调用它的那条消息的线程"）。在那个线程里直接调 ``on_wake``
   就是在非 GUI 线程操作 QWidget —— 未定义行为，实测是直接崩，而且崩在 Qt 内部
   栈上，堆栈里看不到我们的代码。所以用一个活在 GUI 线程的 ``QObject`` +
   ``pyqtSignal``，``Qt.QueuedConnection`` 连接，让 ``on_wake`` 回到 GUI 线程执行。
   钩子线程上**只做整数累加和一个 emit**，不碰任何 QWidget、不做 I/O ——
   顺便也保证了不触发 ``LowLevelHooksTimeout``（默认 3000ms，超时 Windows 会
   静默摘掉钩子，表现是「滚着滚着就失灵了，重启程序又好」）。

3. **不能被普通滚动误触发。** 这是全局钩子，用户在浏览器里正常滚动也会被看到，
   所以必须有 flick 判定：``flick_ms``（默认 250ms）时间窗内累积
   ``abs(delta)``，累积量 ≥ ``flick_delta``（默认 240 = 2 个滚轮刻度）才判为
   一次唤出手势。触发后进入 latch，必须等累积量回落到 ``flick_delta/3`` 以下
   才允许下一次触发 —— 否则一次长滚动会在同一条轨迹上触发十几次，把启动器
   反复弹出/收起。

关于 ``ignore_injected``
------------------------
默认忽略带 ``LLMHF_INJECTED`` 的**合成事件**。理由：别的自动化工具、录屏回放、
以及我们自己的测试注入的滚轮都不该把启动器弹出来。副作用是它同时也挡掉了
LLMHF 的测试手段 —— 所以它做成构造参数（默认 True），测试时关掉即可验证
flick 逻辑本身。**注意这个开关只影响"是否当作手势"，不影响是否转发**
（见坑 1）。

用法
----
    from launchpad.wheelhook import WheelHook
    hook = WheelHook(on_wake=lambda: win.show_me(),
                     is_showing=lambda: win.is_showing())
    if hook.start():
        win._wheelhook = hook      # 必须留住引用

不做的事：不 import ``launchpad.window``（循环依赖）。回调对象只当鸭子类型用，
调用方传 ``lambda`` 过来即可。``is_showing`` 只在 **GUI 线程**里被调用（见
``_Bridge._deliver``），所以它可以是 ``window.Launchpad.is_showing`` 那样的
真实方法 —— 但它必须返回一个不依赖 Qt 内部状态的普通值。
"""

import ctypes
import ctypes.wintypes as wintypes
import threading
import time

from PyQt5.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QApplication

# ── Win32 常量 ────────────────────────────────────────────
#
# WH_MOUSE_LL 是 **14**，不是 13 —— 13 是 WH_KEYBOARD_LL。这两个只差 1，
# 而且**写错之后完全不报错**：SetWindowsHookExW 照样返回一个非 0 句柄、
# GetLastError() 照样是 0，只是从此以后收键盘事件、永远收不到鼠标事件。
# 症状是「钩子明明装上了，滚轮一点反应都没有」，而且没有任何错误信息可查。
# （本文件第一版就是 13，排查了整整一轮才定位到，换键盘钩子立刻见效。）
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13      # 仅作对照注释：13 是这个

WM_MOUSEWHEEL = 0x020A
WM_QUIT = 0x0012
HC_ACTION = 0
PM_NOREMOVE = 0x0000

# MSLLHOOKSTRUCT.flags / keyboard flags 里同名的注入标记，值一样。
# 分开写是为了对着 MSDN 查阅时不串味 —— 这里只需要鼠标版的。
LLMHF_INJECTED = 0x01

WHEEL_DELTA = 120

LRESULT = ctypes.c_ssize_t          # 64 位下 LRESULT 是 LONG_PTR
HOOKPROC = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


class _POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    """
    字段类型必须和 Win32 完全一致（见 hotkeys.py 里 WNDCLASS 的同类说明）。

    dwExtraInfo 是 ``ULONG_PTR``，不是 ``DWORD`` —— 64 位下用 DWORD 会让结构体
    变成 24 字节而不是 32 字节，``pt`` 之后所有字段整体前移，flags 里读到的
    是坐标高 32 位。默认对齐下 ctypes 会自动补齐到正确的 32 字节。
    """
    _fields_ = [
        ("pt", _POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.ULONG),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    """``MONITORINFO``。字段顺序/类型必须和 Win32 一致（cbSize 在最前）。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


# ── Win32 API ────────────────────────────────────────────
# 只建一次并缓存。hotkeys.py 每次进 wndproc 都重建一遍 argtypes，那在消息
# 频率下没问题；**低级鼠标钩子是全系统每个鼠标事件的频率**（含移动），每次
# 重设 argtypes/restype 是纯粹的浪费，所以这里缓存。
_API = None


def _api():
    """
    显式声明签名 —— 这一步不是可选的。

    ctypes 默认把整数参数当 32 位，WPARAM/LPARAM 在 64 位下是 64 位。
    不声明签名读 lParam（指针）就会 OverflowError，异常被 ctypes 吞掉。

    也必须用 ``ctypes.WinDLL(..., use_last_error=True)`` 而不是
    ``ctypes.windll.*``：后者是另一个对象、没开 ``use_last_error``，
    ``windll`` 版读到的 ``GetLastError()`` **恒为 0** —— 钩子装不上的原因就
    永远打印成「错误码 0」。这条 hotkeys.py 已经踩过并处理了，这里保持一致。
    """
    global _API
    if _API is not None:
        return _API
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    u.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                    wintypes.HINSTANCE, wintypes.DWORD]
    u.SetWindowsHookExW.restype = wintypes.HHOOK

    u.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    u.UnhookWindowsHookEx.restype = wintypes.BOOL

    # hhk 对低级钩子是被忽略的参数（文档明说），传 NULL 是官方认可的写法。
    # 传本钩子句柄也行，但在 SetWindowsHookExW 刚返回、句柄还没写回字段的那个
    # 瞬间可能有回调打过来（其它线程注入事件），读到 None 反而更乱。传 NULL 无竞态。
    u.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                 wintypes.WPARAM, wintypes.LPARAM]
    u.CallNextHookEx.restype = LRESULT

    # 边缘判定用：取指针**所在那块显示器**的矩形。
    #
    # 为什么不用 GetSystemMetrics(SM_CXSCREEN)：那个给的是**主屏**宽度。
    # 多显示器下指针在副屏上时，拿主屏宽度去比会得到完全错误的结论
    # （副屏在主屏右边时 x 永远大于主屏宽度，于是「离右边缘 <= 40px」
    # 恒为真，边缘限制形同虚设）。
    u.MonitorFromPoint.argtypes = [_POINT, wintypes.DWORD]
    u.MonitorFromPoint.restype = wintypes.HANDLE
    u.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(_MONITORINFO)]
    u.GetMonitorInfoW.restype = wintypes.BOOL

    u.PostThreadMessageW.argtypes = [wintypes.DWORD, ctypes.c_uint,
                                     wintypes.WPARAM, wintypes.LPARAM]
    u.PostThreadMessageW.restype = wintypes.BOOL

    u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               ctypes.c_uint, ctypes.c_uint,
                               ctypes.c_uint]      # 末尾是 wRemoveMsg，5 个参数
    u.PeekMessageW.restype = wintypes.BOOL

    u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                              ctypes.c_uint, wintypes.UINT]
    u.GetMessageW.restype = ctypes.c_int

    u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

    u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    u.DispatchMessageW.restype = LRESULT

    # GetCurrentThreadId 在 kernel32，不在 user32。写到 u. 上会 AttributeError，
    # 而这行在钩子线程里执行 -> 线程直接死掉，start() 只会报一句莫名其妙的
    # 「5 秒内没有就绪」，排查起来要多绕一大圈。
    k.GetCurrentThreadId.argtypes = []
    k.GetCurrentThreadId.restype = wintypes.DWORD

    k.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    k.GetModuleHandleW.restype = wintypes.HMODULE

    _API = (u, k)
    return _API


def _wheel_delta(mouse_data: int) -> int:
    """
    从 ``MSLLHOOKSTRUCT.mouseData`` 的 HIWORD 取**有符号**的滚轮增量，
    即 ``GET_WHEEL_DELTA_WPARAM(mouseData)``。

    **不要从 wParam 取。** 这是本文件第二个静默失败的坑：

    ``GET_WHEEL_DELTA_WPARAM`` 这个宏在窗口消息里是 ``HIWORD(wParam)``，
    因为窗口消息的 wParam 低 16 位是按键状态（Ctrl/Shift）、高 16 位是
    delta。但**低级钩子的回调签名是另一个约定**::

        LRESULT CALLBACK LowLevelMouseProc(int nCode, WPARAM wParam, LPARAM lParam)

    这里的 ``wParam`` 是**消息编号本身**，滚轮时恒为 ``WM_MOUSEWHEEL``
    (0x020A)，压根没有 delta。delta 挪到了 ``lParam`` 指向的
    ``MSLLHOOKSTRUCT.mouseData`` 里，而且同样放在高 16 位。实测（注入 +120）：

        wParam     = 0x020A        <- 只是消息编号
        HIWORD(wParam) = 0x0       <- 所以照抄宏会永远读到 0
        mouseData  = 0x00780000    <- delta 在这

    照抄「取 HIWORD(wParam)」的后果同样是**不报任何错**：delta 恒为 0，
    于是所有滚轮事件都在 ``if delta == 0: return`` 处被丢掉，手势永远不触发。
    （本文件第一版就是这么写的，靠这一步实测才发现。）

    带符号这件事也要做：向上滚的 delta 是正的，不做符号扩展的话负方向
    （0xFF88 = -120）会被当成正的 65416，一个滚轮事件就能攒满阈值。
    """
    d = (mouse_data >> 16) & 0xFFFF
    return d - 0x10000 if d >= 0x8000 else d


# ── flick 判定 ───────────────────────────────────────────
class _Flick:
    """
    滑动判定状态机。**只在钩子线程上被调用**，所以不需要锁。

    语义（时间窗 sliding window + latch）：

    - 相邻两次事件的间隔超过 ``window_ms`` ⇒ **手势结束**：累积量作废，
      并且解除 latch。这是「滑动」和「慢慢滚」的分界：慢慢滚每 100ms 一格，
      永远攒不到 2 格。
    - ``accum += abs(delta)``；``accum >= threshold`` 且未 latch ⇒ 触发一次，
      进入 latch。
    - 一次长滚动（几十格）不该一路触发，latch 就是防这个的。

    ## latch 曾经永远解不开（本文件第一版的真 bug）

    第一版用「累积量回落到 ``threshold/3`` 以下」来解 latch::

        if expired: self.accum = 0
        self.accum += abs(delta)        # ← 复位后立刻又加
        if self.latch and self.accum < self.release_at: self.latch = False

    这两行是矛盾的：``accum`` 复位成 0 之后**马上**就被加上这一格的
    ``abs(delta)``，而**一个滚轮刻度就是 120**，已经大于
    ``release_at = threshold/3 = 80``。于是 ``accum`` 在数学上**永远**
    降不到 80 以下 —— latch 一旦置上就再也解不开。

    实测症状：不带 --show 启动（开机自启形态），第一次触控板滑动正常唤出，
    **之后无论怎么滑都不再有任何反应**，直到重启程序。静默到用户只会
    得出「这个功能时灵时不灵」。

    正确的判据是**时间**而不是累积量：「用户停手了」的直接可观测量就是
    「距上一个滚轮事件过去了多久」。手势窗口过期本身就是停手的证据，
    不需要第二个阈值。

    ## 这个状态机只管「滑得快不快」

    「起点在不在屏幕边缘」那道限制**不归它管** —— 见
    :meth:`WheelHook._on_wheel` 里关于手势级前提的说明。把两件事混在
    一起，会让「一个手势算不算数」取决于「哪个事件负责判」：那是本文件
    第二版的真 bug，一格 120、阈值 240，于是**攒满阈值的那一格恰好
    ``is_start`` 为 False**，边缘检查被整条跳过，``edge_px`` 从来没生效。
    """

    __slots__ = ("threshold", "window_ms", "accum", "latch", "_last_ms")

    def __init__(self, threshold: int, window_ms: int) -> None:
        self.threshold = threshold
        self.window_ms = window_ms
        self.accum = 0
        self.latch = False
        self._last_ms = -1          # -1 = 还没收到过事件

    def is_start(self, now_ms: int) -> bool:
        """
        即将喂进来的这个事件是不是**手势的第一个**（即 ``feed`` 之后累积量
        会从0 开始重新算）。

        调用方需要在知道「本次手势是否刚起头」之后才决定要不要接受，
        所以必须在 ``feed`` **之前**取。判据与 :meth:`feed` 里的窗口
        过期判断保持一致：没收到过事件，或者距上次已超过 ``window_ms``。

        **必须传 ``now_ms``。** 光看 ``_last_ms >= 0`` 是不够的 ——
        那只能区分「第一个事件」和「后续事件」，区分不了「长时间停手后的
        第一个事件」，而后者恰恰也是手势起点（还要解除 latch）。
        """
        return self._last_ms < 0 or (now_ms - self._last_ms) > self.window_ms

    def discard(self) -> None:
        """把本次手势作废（累积清零、解除 latch），不触发。

        用于「判定为手势但条件不满足」的场景（比如调用点按别的附加条件
        否决）——那种情况下不应该让累积继续攒着，否则一个手势的前半段
        条件满足、后半段不满足时仍会触发。
        """
        self.accum = 0
        self.latch = False

    def feed(self, delta: int, now_ms: int) -> bool:
        """吃一个滚轮事件，返回是否这次判定为一次唤出手势。"""
        expired = (self._last_ms >= 0
                   and (now_ms - self._last_ms) > self.window_ms)
        if expired:
            # 停手超过窗口 = 上一次手势结束：累积作废 + 允许下一次触发
            self.accum = 0
            self.latch = False
        self._last_ms = now_ms
        self.accum += abs(delta)

        if self.latch:
            return False            # 本次手势已经触发过，不重复
        if self.accum >= self.threshold:
            self.latch = True
            return True
        return False


# ── 跨线程桥 ─────────────────────────────────────────────
class _Bridge(QObject):
    """
    活在 GUI 线程的信号桥（坑 2）。

    必须在 GUI 线程构造（``WheelHook.start()`` 由 ``__main__`` 在 GUI 线程调用），
    线程亲和性才正确。用 ``Qt.QueuedConnection`` 显式指定投递方式：虽然
    发送方在别的线程时 Qt 本来就会自动选 QueuedConnection，但显式写出来是为了
    「一旦哪天有人在同线程调用也不会变成 Direct」。

    ``is_showing`` 的检查刻意放在**这一侧**而不是钩子线程：
    窗口状态只有 GUI 线程说了算，而且到这一步用户可能已经自己把窗口叫出来了，
    这时候再判一次比在几百毫秒前判更准。
    """

    # 不带参数：跨线程传 Python 对象要额外处理生命周期，钩子线程上一点元数据
    # 不值得冒这个险。需要的信息（delta / 时间）在 WheelHook 那边已经记好了。
    woken = pyqtSignal()

    def __init__(self, hook: "WheelHook") -> None:
        super().__init__()
        self._hook = hook

    @pyqtSlot()
    def _deliver(self) -> None:
        """这个槽在 GUI 线程执行 —— 这里才可以碰 QWidget。"""
        h = self._hook
        is_showing = None
        try:
            is_showing = h.is_showing
            showing = bool(is_showing()) if is_showing is not None else False
        except Exception as exc:
            # is_showing 抛了就不能放行：宁可这次不唤醒，也不要在没有确认
            # 窗口状态的情况下把启动器糊到用户脸上。
            print(f"[WheelHook] is_showing() 异常，本次放弃唤醒：{exc}")
            h._count("suppressed")
            return
        if showing:
            h._count("suppressed")
            return
        h._count("wakes")
        try:
            if h.on_wake is not None:
                h.on_wake()
        except Exception as exc:
            print(f"[WheelHook] on_wake 异常：{exc}")


# ── 对外接口 ─────────────────────────────────────────────
class WheelHook:
    """
    系统级滚轮观察者。判定为「触控板两指滑动」时在 **GUI 线程** 调 ``on_wake``。

    :param on_wake:      判定为唤出手势时调用（GUI 线程）。
    :param is_showing:   启动器当前是否已显示；返回 True 时一律不触发
                         （那时滚轮该交给翻页，抢了就是 bug）。
                         None 视为「未显示」。
    :param flick_delta:  触发所需累积量，默认 240 = 2 个滚轮刻度。
    :param flick_ms:     累积时间窗，默认 250ms。
    :param ignore_injected: 是否忽略 LLMHF_INJECTED 合成事件，默认 True。
    """

    def __init__(self, on_wake=None, is_showing=None, *,
                 flick_delta: int = 240, flick_ms: int = 250,
                 ignore_injected: bool = True, edge_px: int = 40) -> None:
        self.on_wake = on_wake
        self.is_showing = is_showing
        self.ignore_injected = bool(ignore_injected)
        #: 手势**起点**必须离所在显示器的左/右边缘不超过这么多像素。
        #: 0 = 不限（全屏任何位置都算）。默认 40 —— 见 :meth:`_edge_ok`。
        self.edge_px = max(0, int(edge_px))

        self._flick = _Flick(max(1, int(flick_delta)), max(1, int(flick_ms)))
        #: 当前手势是否通过了「起点在边缘带内」这道前提。**跟着整个手势
        #: 走**，不随中途的指针位置改变。详见 :meth:`_on_wheel` 里的说明
        #: —— 这里曾经是个 bug：前提被判在了错误的那一格上（攒够阈值的那
        #: 格恰好 started=False），于是 edge_px 从来没生效过。
        self._gesture_ok = False
        #: 最近一次手势起点的光标坐标，供诊断用。见下面触发时的日志。
        self._gesture_x = -1
        self._gesture_y = -1
        #: 最近一次手势起点离屏幕边缘的距离（px），供诊断用。-1 = 未知。
        #: 有它才能在用户说「没反应」时直接报出「差 340px，阈值 120px」，
        #: 而不是又一次靠猜。
        self._last_edge_dist = -1
        #: 是否把「起点不在边缘带」也打日志。默认 False —— 因为正常滚动
        #: 在屏幕中间进行时**每一次**都会被拒，不静音的话日志会被刷爆，
        #: 真正有用的触发记录反而被埋掉。设True 只用于排查。
        self._verbose = False
        #: 上次写「限流拒绝日志」的时刻（ms）。见 :meth:`_log_rejection`。
        self._last_reject_log_ms = -10 ** 9
        self._hook_proc = None       # HOOKPROC 实例，必须保活（见下方注释）
        self._bridge = None
        self._thread = None
        self._hook = None
        self._tid = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stopping = False
        self.error = ""

        # 统计。_count 在钩子线程和 GUI 线程两边都会写，所以统一走锁。
        # off_edge = 「手势起点不在屏幕边缘带内」而被丢弃的次数 ——
        # 这个计数器是这类 bug 的直接证据：用户抱怨「滚轮一划就弹」时，
        # 它应该很大（说明钩子看到了事件但判定把它们放过去了）；修好之后
        # 它会大于等于 triggers，因为正常滚动全落在这一支。
        self._stat = {"events": 0, "injected": 0, "triggers": 0,
                      "wakes": 0, "suppressed": 0, "last_delta": 0,
                      "last_trigger_at": 0.0, "off_edge": 0}

    # ── 生命周期 ─────────────────────────────────────────
    def start(self) -> bool:
        """
        起钩子。**必须在 GUI 线程调用**（要在这里建 ``_Bridge``）。

        失败一律返回 False 并打印原因，绝不抛 —— 唤不出启动器只是少了个
        手势入口，绝不能让整个启动器起不来。
        """
        if self.running():
            return True
        # 要比「是不是 GUI 线程」只能比 QThread 对象：threading.current_thread()
        # 是 Python 线程对象，跟 QThread 实例永远不是同一个东西，拿它比会恒为
        # False（第一版就这么写，于是无论从哪调用都误报警告）。
        app = QApplication.instance()
        if app is not None and app.thread() is not QThread.currentThread():
            # 不致命，但信号桥的线程亲和性会错，on_wake 会跑到错误的线程。
            print("[WheelHook] start() 不在 GUI 线程调用，"
                  "on_wake 可能不在 GUI 线程执行")
        self._stopping = False
        self.error = ""
        self._bridge = _Bridge(self)
        self._bridge.woken.connect(self._bridge._deliver, Qt.QueuedConnection)
        # HOOKPROC 必须留活：它是 ctypes 生成的回调包装对象，被 GC 之后
        # Windows 仍会调那个地址 -> 直接 access violation。它也必须由
        # 安装钩子的**同一个线程**持有/使用，所以存在实例上，不放局部变量。
        self._hook_proc = HOOKPROC(self._proc)
        self._ready.clear()
        self._thread = threading.Thread(target=self._thread_main,
                                        name="launchpad-wheelhook",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            self.error = "钩子线程 5 秒内没有就绪"
            print(f"[WheelHook] {self.error}")
            self._hook_proc = None
            self._bridge = None
            return False
        if self.error:
            self._hook_proc = None
            self._bridge = None
            return False
        print(f"[WheelHook] 已接管全局滚轮（阈值 {self._flick.threshold} / "
              f"{self._flick.window_ms}ms，忽略合成事件={self.ignore_injected}）")
        return True

    def stop(self) -> None:
        """
        干净地摘钩子。

        顺序：先 ``PostThreadMessage(WM_QUIT)``，让**钩子线程自己**从
        ``GetMessageW`` 里出来、由它自己调 ``UnhookWindowsHookEx``，再 join。
        不从别的线程硬摘 —— 钩子线程此刻可能正卡在 ``CallNextHookEx`` 里，
        跨线程摘会和回调撞车。

        线程没退（PostThreadMessage 失败等极端情况）时，才从当前线程兜底摘钩子。
        泄漏的 ``WH_MOUSE_LL`` 会一直挂在系统上，影响全局输入，必须兜住。
        """
        self._stopping = True
        u, _ = _api()
        t = self._thread
        with self._lock:
            tid = self._tid
        if t is not None and t.is_alive() and tid:
            if not u.PostThreadMessageW(tid, WM_QUIT, 0, 0):
                print(f"[WheelHook] PostThreadMessage(WM_QUIT) 失败 "
                      f"err={ctypes.get_last_error()}，改用兜底路径")
            else:
                t.join(3.0)
        with self._lock:
            h = self._hook
            self._hook = None
            self._tid = 0
        if h:
            # 要么线程没能退出、要么它已经摘过了但没清字段（不会，清了）——
            # 这里是唯一可能真正残留的钩子句柄。
            if not u.UnhookWindowsHookEx(h):
                print(f"[WheelHook] UnhookWindowsHookEx 失败 "
                      f"err={ctypes.get_last_error()}")
        if t is not None:
            t.join(0.5)
        self._thread = None
        self._hook_proc = None
        self._bridge = None

    def running(self) -> bool:
        t = self._thread
        with self._lock:
            h = self._hook
        return bool(h) and bool(t is not None and t.is_alive())

    # ── 诊断 ─────────────────────────────────────────────
    def stats(self) -> dict:
        with self._lock:
            s = dict(self._stat)
        t = self._thread
        with self._lock:
            tid = self._tid
        s["running"] = self.running()
        s["hook_thread"] = t.name if (t is not None and t.is_alive()) else None
        s["hook_thread_id"] = tid or None
        s["accum"] = self._flick.accum
        s["latched"] = self._flick.latch
        s["gesture_ok"] = self._gesture_ok
        s["edge_px"] = self.edge_px
        s["last_edge_dist"] = self._last_edge_dist
        s["last_gesture_x"] = self._gesture_x
        s["last_gesture_y"] = self._gesture_y
        s["verbose"] = self._verbose
        s["ignore_injected"] = self.ignore_injected
        s["flick_delta"] = self._flick.threshold
        s["flick_ms"] = self._flick.window_ms
        s["error"] = self.error
        if s["last_trigger_at"]:
            s["last_trigger_age_s"] = round(
                time.time() - s["last_trigger_at"], 3)
        return s

    def diagnose(self) -> str:
        """
        一行人话，说明「手势为什么没反应」。

        ## 为什么需要这个函数

        这个功能之前**完全没有可观测性**：统计只在启动时打一次到日志，
        用户试了手势之后到底收到了几个事件、是被边缘判定拒了、还是
        累积没攒够，日志里一个字都没有。于是每次反馈都是「不行」，
        而我只能靠猜 —— 先后猜错了两次（「钩子收不到触控板事件」、
        「DPI 坐标换算错了」）。

        有了它，「不行」就能立刻变成一个具体的数字：到底是
        ``events=0``（钩子没收到输入）、``off_edge>0``（位置不在边缘带）、
        还是 ``events>0 且 off_edge=0``（位置对了但没攒够格数）。
        """
        s = self.stats()
        if not s.get("running"):
            return (f"手势不可用：钩子没在运行（{s.get('error') or '原因未知'}）")
        if s.get("events", 0) == 0:
            return ("手势不可用：钩子在跑，但**一个滚轮事件都没收到**。"
                    "说明这个钩子在这台机器上看不到输入（多见于驱动不"
                    "上报合成滚轮的情况）。")
        bits = [f"收到 {s['events']} 个滚轮事件"]
        if s.get("off_edge", 0):
            bits.append(f"其中 {s['off_edge']} 次手势起点不在边缘 "
                        f"{s['edge_px']}px 内被拒")
        else:
            bits.append("起点都在边缘带内")
        if s.get("triggers", 0):
            bits.append(f"触发 {s['triggers']} 次")
        else:
            need = s.get("flick_delta", 240)
            bits.append(f"**一次都没触发** —— 没能攒够 {need}"
                        f"（{need // 120} 格）")
        if s.get("suppressed", 0):
            bits.append(f"因窗口已显示而抑制 {s['suppressed']} 次")
        return "；".join(bits)

    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._stat[key] += n

    # 限流拒绝日志的最小间隔（毫秒）。
    _REJECT_LOG_EVERY_MS = 10_000

    def _log_rejection(self, now_ms: int) -> None:
        """
        「起点不在边缘带」的**限流**日志。

        ## 为什么要有它

        触发那一行本来就无条件写日志，所以「误触」本身留下了记录。缺的是
        **分母**：没有「被正确拒掉多少次、拒的时候光标在哪」，就无法回答
        「边缘带到底收窄到多少才既不误触、又能瞄得中」—— 这正是用户报
        「滚轮一划就弹出来」时唯一需要的那组数。

        逐次写会刷爆日志（屏幕中间的任何一次正常滚动都会被拒），所以只写
        每 :attr:`_REJECT_LOG_EVERY_MS` 毫秒的一条，并把累积计数带上：
        看到「第 7 次拒绝（累计 1284 次）」就知道分母有多大。

        只在**非 verbose** 下走这条；``--verbose-wheel`` 仍然逐次写。
        """
        with self._lock:
            if now_ms - self._last_reject_log_ms < self._REJECT_LOG_EVERY_MS:
                return
            self._last_reject_log_ms = now_ms
            n = self._stat["off_edge"]
        print(f"[WheelHook] 手势被边缘门拒绝（第 {n} 次，限流）："
              f"起点 ({self._gesture_x}, {self._gesture_y}) 离边缘 "
              f"{self._last_edge_dist}px > 阈值 {self.edge_px}px")

    def _count_trigger(self, delta: int) -> None:
        with self._lock:
            self._stat["triggers"] += 1
            self._stat["last_delta"] = delta
            self._stat["last_trigger_at"] = time.time()

    # ── 钩子线程 ─────────────────────────────────────────
    def _thread_main(self) -> None:
        try:
            self._thread_main_inner()
        except BaseException as exc:            # noqa: BLE001
            # 线程里抛异常 = 钩子线程悄悄死掉，start() 只能报「没就绪」，
            # 真正的原因就丢了。必须在这里截住并写进 error。
            self.error = f"钩子线程异常：{exc!r}"
            print(f"[WheelHook] {self.error}")
            self._ready.set()

    def _thread_main_inner(self) -> None:
        u, k = _api()
        msg = wintypes.MSG()
        # PeekMessage 是「确保本线程已有消息队列」的唯一可靠手段（哪怕不取消息）。
        # 没有队列的话 stop() 里的 PostThreadMessageW 直接失败 1449
        # (ERROR_INVALID_THREAD_ID)，钩子线程就永远退不出来。
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_NOREMOVE)
        tid = k.GetCurrentThreadId()

        # SetWindowsHookEx 必须在跑消息循环的这个线程上调 —— 低级钩子的回调
        # 就跑在这里。
        #
        # dwThreadId 只能传 0（全局）。传本线程 id 会拿到 1429
        # (ERROR_HOOK_NEEDS_HMOD)：线程私有钩子要求钩子过程在 DLL 里。
        #
        # hMod 按 MSDN 应该传 NULL —— 钩子过程在我们这个 EXE（python.exe）
        # 里而不是某个 DLL 里，全局钩子只有「不在 DLL 里」才允许 NULL。
        # 实测两种都能装上并正常回调，这里先用 NULL（文档规定的形式），
        # 万一某些机器/安全策略下 NULL 被拒（126 ERROR_MOD_NOT_FOUND），
        # 退回模块句柄再试一次。起不来也只是少个手势入口，不该拖垮启动器。
        hook = u.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, None, 0)
        if not hook:
            err1 = ctypes.get_last_error()
            hinst = k.GetModuleHandleW(None)
            hook = u.SetWindowsHookExW(WH_MOUSE_LL, self._hook_proc, hinst, 0)
            if hook:
                print(f"[WheelHook] hMod=NULL 被拒({err1})，改用模块句柄重试成功")
            else:
                err = ctypes.get_last_error()
                self.error = (f"SetWindowsHookExW(WH_MOUSE_LL) 失败："
                              f"hMod=NULL 时 {err1}，模块句柄时 {err}")
                print(f"[WheelHook] {self.error}"
                      "（手势唤出不可用，热键与托盘不受影响）")

        with self._lock:
            self._tid = tid
            self._hook = hook
        self._ready.set()
        if not hook:
            return

        while True:
            r = u.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):        # 0 = WM_QUIT, -1 = 出错
                break
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))

        # 循环退出后由本线程摘钩子 —— 和安装是同一个线程，不存在跨线程竞态。
        with self._lock:
            h = self._hook
            self._hook = None
            self._tid = 0
        if h and not u.UnhookWindowsHookEx(h):
            print(f"[WheelHook] UnhookWindowsHookEx 失败 "
                  f"err={ctypes.get_last_error()}")

    # ── 钩子回调（钩子线程，全系统每个鼠标事件的频率）──
    def _proc(self, nCode, wParam, lParam):
        """
        跨 FFI 边界，**绝不能抛异常** —— 抛了会破坏栈，而且 ctypes 会吞掉它，
        表现是随机的、跟本文件毫无关系的崩溃。

        同时**必须转发每一个事件**（坑 1）：这里无论走哪条分支，最后都
        落回 ``CallNextHookEx``。

        注意 ``wParam`` 是**消息编号**，不是打包了 delta 的值 ——
        见 ``_wheel_delta`` 的注释。
        """
        try:
            if nCode == HC_ACTION and (wParam & 0xFFFFFFFF) == WM_MOUSEWHEEL:
                self._on_wheel(wParam, lParam)
        except Exception:
            pass
        try:
            u, _ = _api()
            return u.CallNextHookEx(None, nCode, wParam, lParam)
        except Exception:
            return 0

    def _on_wheel(self, wParam: int, lParam: int) -> None:
        """钩子线程上的全部工作：两个整数判断 + 一次 emit。不做 I/O、不碰 Qt。"""
        self._count("events")
        hs = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
        if self.ignore_injected and (hs.flags & LLMHF_INJECTED):
            # 只是「不当成手势」，注意不是 return 掉整个事件 ——
            # 外层照样转发（坑 1）。
            self._count("injected")
            return
        delta = _wheel_delta(hs.mouseData)
        if delta == 0:
            return
        now_ms = int(time.monotonic() * 1000)
        flick = self._flick

        # ── 手势级前提：起点必须在屏幕侧边 ──
        #
        # 这里曾经是本文件最严重的 bug。原写法是::
        #
        #     started = flick.is_start(now_ms)      # 只有第 1 格为 True
        #     if flick.feed(delta, now_ms):         # 累积够 240 才 True
        #         if started and not self._edge_ok(x):   # ← 只在第 1 格上判
        #             return
        #         self._count_trigger(delta)        # ← 没判，直接弹
        #
        # 一格 = 120、阈值 = 240，所以第 1 格 started=True 但攒不够、
        # 不触发；**第 2 格攒满了、触发，而它的 started 恰好是 False** ——
        # 于是边缘检查被整条跳过。结论：``edge_px`` 从来没生效过，
        # 屏幕任何地方快速滚两下都弹，实测症状正是用户报的
        # 「鼠标滚轮一划就弹出来」。
        #
        # 修法：判定结果存在 ``self._gesture_ok`` 里，跟着整个手势走。
        # 它在手势的第一格定一次，之后本手势内不再改判 —— 这正是
        # 「从侧边滑进来」的语义（起点在边上，中途指针会滑到中间）。
        # 停手超过窗口 = 新手势，下一格重新定。
        if flick.is_start(now_ms):
            # y 一起传：多屏上下排列时，同一个 x 可能落在两块屏上，
            # 只看 x 会把「上面那块屏的中间」误判成贴边。
            self._gesture_x = int(hs.pt.x)
            self._gesture_y = int(hs.pt.y)
            self._gesture_ok = self._edge_ok(hs.pt.x, hs.pt.y)
            self._last_edge_dist = self._edge_distance(hs.pt.x, hs.pt.y)
            if not self._gesture_ok:
                self._count("off_edge")
                # 记下被拒时的实际距离。��户报「手势没反应」时，这一行
                # 直接告出差多少：比如「起点离边缘 340px，阈值 120px」，
                # 于是知道该调阈值还是该改动作。日志在此之前完全没有
                # 这个信息，是「两次猜错」的直接原因。
                if self._verbose:
                    print(f"[WheelHook] 手势起点离边缘 "
                          f"{self._last_edge_dist}px（阈值 "
                          f"{self.edge_px}px），不触发")
                else:
                    self._log_rejection(now_ms)

        # **无论手势合不合格都要 feed。**
        #
        # feed 的第二个作用是推进 ``_last_ms``，也就是「手势窗口有没有过期」
        # 的唯一依据。合格手势直接 return 掉不喂的话，``_last_ms`` 永远停在
        # -1，于是 ``is_start`` 对每个事件都返回 True，前提被反复重判——
        # 一个起点在屏幕中间的长滚动，指针漂到边上之后就会「转正」触发。
        # 这正是 tests_wheelhook 里那条「整个手势起点在中间、后半段移到
        # 边缘 -> 全程不唤出」抓到的问题。
        fired = flick.feed(delta, now_ms)
        if not self._gesture_ok:
            # 不合格手势：把累积扔掉，让它攒不满，也就不会 latch。
            flick.discard()
            return

        if fired:
            self._count_trigger(delta)
            # 光标坐标一并记下来：只记「离边缘多少像素」看不出**是哪一种
            # 误触**。用户报「往下滚滚轮就会弹出来」时，唯一能分辨的
            # 办法是看那个坐标是不是落在滚动条上（x ≈ 屏幕宽度 - 17）
            # —— 而滚动条就在屏幕最右缘，任何合理的边缘带都盖住了它。
            print(f"[WheelHook] 手势触发：起点 ({self._gesture_x},"
                  f"{self._gesture_y}) 离边缘 {self._last_edge_dist}px，"
                  f"delta={delta}，累积 {self._flick.accum}/"
                  f"{self._flick.threshold}")
            bridge = self._bridge
            if bridge is not None:
                # 唯一的跨线程动作，QueuedConnection 把它排到 GUI 线程。
                # 真正的 on_wake 在 _Bridge._deliver 里，不在这里。
                bridge.woken.emit()

    def _edge_distance(self, x: int, y: int = 0) -> int:
        """
        起点离所在显示器**左右边缘**的最近距离（像素）。

        单独抽出来是为了诊断：只要能报出「差多少」，用户说没反应时
        就不用再猜 —— 阈值不够就调阈值，动作不对就改动作。拿不到显示器
        信息时返回 -1（表示未知，而不是假装是 0）。
        """
        u, _ = _api()
        try:
            hmon = u.MonitorFromPoint(_POINT(int(x), int(y)), 2)
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if not u.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return -1
            r = mi.rcMonitor
            return min(int(x) - r.left, r.right - int(x))
        except Exception:
            return -1

    def _edge_ok(self, x: int, y: int = 0) -> bool:
        """
        手势起点是否落在屏幕侧边的「边缘带」里。

        ## 为什么必须有这个限制

        没有它的话，你在浏览器里**正常滚动**就会把启动器弹到页面上：
        240 = 两格，而两指快滑在 250ms 内攒够两格是常事。这个钩子是
        全系统的，任何程序里的一次快滑都会被看到。

        限制成「从屏幕左侧/右侧边缘往里滑」之后就基本不会误触 ——
        而从侧边滑入本来就是触控板用户最熟悉的动作（macOS 的 hot corner）。

        ## 一次被实测推翻的错误结论（不要再走一遍）

        我曾断定「钩子给的是物理坐标、``GetMonitorInfoW`` 给的是虚拟化
        逻辑坐标，缩放 1.5，所以 ``right - x`` 会算出负数、边缘判定彻底失效」。
        **那是错的。** 两条实测证据：

        * ``GetCursorPos`` 与 ``GetPhysicalCursorPos`` 在五个已知坐标上
          （含 ``(1706,1066)`` 主屏右下、``(-1707,0)`` 副屏最左）
          返回**完全相同**的值 -> 本机缩放就是 1.0，不存在虚拟化。
        * ``sizeof(MSLLHOOKSTRUCT) == 32``，与 MSDN 的 x86_64 布局一致；
          写入 ``pt.x=1897`` 能原样读出 -> 不是结构体错位。

        当时采到的 ``x`` 最大 1897（超出主屏 1707）是**真实光标位置**——
        采样期间屏幕布局还在变动，后来才稳定。所以算术是对的，
        **真正的问题是触发带只有 40px（屏宽的 2.3%），根本瞄不准。**

        因此这里**不需要任何坐标换算**，只把band 放宽到 120px。

        ## 多显示器

        用 ``MonitorFromPoint`` 取指针**所在那块屏**的矩形，而不是主屏 ——
        指针在副屏上时，主屏的宽度会给出完全错误的判断。

        ``y`` 用来在多屏上下排列时区分同一 x 上的不同屏；默认 0 表示
        「不关心纵向位置」（单屏、或左右排列时的常见情况）。
        """
        band = int(getattr(self, "edge_px", 0) or 0)
        if band <= 0:
            return True
        dist = self._edge_distance(x, y)
        if dist < 0:
            # 拿不到显示器信息就放行。宁可多弹一次，也不要让用户
            # 明明做了手势却完全没反应 —— 后者会让人以为功能坏了。
            return True
        # dist 是「到最近那条竖边的距离」，已经取过 min，所以
        # 「贴左边」和「贴右边」不用分别判。
        #
        # dist 为负 = 指针在显示器矩形之外。多屏布局变动时 rect 可能
        # 瞬时不包含当前 x；此时判成贴边是合理的（指针确实在某块屏的
        # 边上或已经越过去了），而且负值天然 <= band，符合直觉。
        return dist <= band