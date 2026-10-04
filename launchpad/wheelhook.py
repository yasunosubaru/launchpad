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

    - 相邻两次事件的间隔超过 ``window_ms`` ⇒ 前面的累积作废，从头算。
      这是「滑动」和「慢慢滚」的分界：慢慢滚每 100ms 一次，永远攒不到 2 格。
    - ``accum += abs(delta)``；``accum >= threshold`` 且未 latch ⇒ 触发一次，
      进入 latch。
    - latch 期间只有 ``accum < release_at`` 才解开，``release_at = threshold/3``。
      不这样的话一次长滚动（几十格）会一路触发。
    """

    __slots__ = ("threshold", "window_ms", "release_at",
                 "accum", "latch", "_last_ms")

    def __init__(self, threshold: int, window_ms: int) -> None:
        self.threshold = threshold
        self.window_ms = window_ms
        self.release_at = max(1, threshold // 3)
        self.accum = 0
        self.latch = False
        self._last_ms = -1          # -1 = 还没收到过事件

    def feed(self, delta: int, now_ms: int) -> bool:
        """吃一个滚轮事件，返回是否这次判定为一次唤出手势。"""
        if self._last_ms >= 0 and now_ms - self._last_ms > self.window_ms:
            self.accum = 0          # 时间窗过期，累积量作废
        self._last_ms = now_ms
        self.accum += abs(delta)

        if self.latch:
            if self.accum < self.release_at:
                self.latch = False  # 累积量回落了，允许下一次触发
            return False
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
                 ignore_injected: bool = True) -> None:
        self.on_wake = on_wake
        self.is_showing = is_showing
        self.ignore_injected = bool(ignore_injected)

        self._flick = _Flick(max(1, int(flick_delta)), max(1, int(flick_ms)))
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
        self._stat = {"events": 0, "injected": 0, "triggers": 0,
                      "wakes": 0, "suppressed": 0, "last_delta": 0,
                      "last_trigger_at": 0.0}

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
        s["ignore_injected"] = self.ignore_injected
        s["flick_delta"] = self._flick.threshold
        s["flick_ms"] = self._flick.window_ms
        s["error"] = self.error
        if s["last_trigger_at"]:
            s["last_trigger_age_s"] = round(
                time.time() - s["last_trigger_at"], 3)
        return s

    def _count(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._stat[key] += n

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
        if self._flick.feed(delta, now_ms):
            self._count_trigger(delta)
            bridge = self._bridge
            if bridge is not None:
                # 唯一的跨线程动作，QueuedConnection 把它排到 GUI 线程。
                # 真正的 on_wake 在 _Bridge._deliver 里，不在这里。
                bridge.woken.emit()