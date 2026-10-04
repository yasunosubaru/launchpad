# -*- coding: utf-8 -*-
"""
验证触控板手势唤出的判定逻辑（``launchpad/wheelhook.py``）。

跑法::

    python -u -m launchpad.tests_wheelhook

## 为什么只测状态机，不测真实钩子

``_Flick`` 是个纯状态机（吃 delta + 时间戳，吐一个布尔），**bug 就在这里**，
所以对着它测就够了，而且是确定性的。

真实钩子那条路（``WH_MOUSE_LL`` -> Win32）**没法用自动化验证**，实测两条路
都不通：

* ``SendInput`` 注入的滚轮事件**根本不进低级钩子**。第一版脚本里
  ``stats()['events']`` 恒为 0；而同一时刻如果真人在滚动鼠标，计数立刻
  上百 —— 也就是说我第一版跑出来的 ``accum: 5400`` / ``events: 49`` 全部
  来自真人操作，不是我的注入。那组数据把我引向了一个错误结论。
* 触控板/鼠标由驱动与合成器产生，``SendInput`` 模拟不了。

所以硬件路径只能靠真人试。状态机能测，钩子不能 —— 两者分开说，不含糊。

## 依赖真实硬件/桌面会话的部分

``WheelHook.start()`` 要装系统钩子，本文件会真的装一个再摘掉，验证
「能装上 / running / 能干净摘除」。这一步需要真实桌面会话。
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtWidgets import QApplication                        # noqa: E402

app = QApplication(sys.argv)

from launchpad.wheelhook import WheelHook, _Flick                # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def head(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ── 一个可控时钟的驱动器 ────────────────────────────────
class Clock:
    """
    假装时间在走。``_Flick`` 只吃 ``now_ms``，所以我们完全掌控时序。

    这样测出来的结果是**确定性**的，不受机器负载影响 —— 而这正是第一版
    用真实 sleep 时的失败来源（请求 8ms 的等待实际可能过去 200ms）。
    """

    def __init__(self):
        self.t = 0

    def advance(self, ms):
        self.t += ms

    def feed(self, f, delta):
        """喂一个 delta，时间戳由时钟给。"""
        return f.feed(delta, self.t)


head("[1] 基本判定：一次手势 = 一次触发")
f = _Flick(240, 250)
c = Clock()

# 两格连击（间隔 60ms）-> 累积 240 -> 触发
c.advance(0)
r1 = c.feed(f, 120)
c.advance(60)
r2 = c.feed(f, 120)
check("两格连击 -> 触发", r1 is False and r2 is True, f"r1={r1} r2={r2}")
check("触发后进入 latch", f.latch is True)

head("[2] latch 期间的同一次手势不再重复触发")
extra = []
for _ in range(10):
    c.advance(30)
    extra.append(c.feed(f, 120))
check("继续连滚 10 格 -> 一次都不再触发", not any(extra),
      f"触发 {sum(1 for x in extra if x)} 次")
check("latch 仍保持", f.latch is True)

head("[3] 停手超过窗口 -> 解开 latch（这条是第一版的真 bug）")
# 停手 400ms（>250ms 窗口）后，应该允许下一次触发。
#
# 第一版用「累积量回落到 threshold/3 = 80 以下」解latch，但累积量复位成 0
# 之后**立刻**就被加上这一格的 abs(delta)，而一个滚轮刻度就是 120 > 80 ——
# 于是 accum 在数学上永远降不到 80 以下，latch 一旦置上就再也解不开。
# 实测症状：不带 --show 启动后，第一次触控板滑动正常唤出，之后**怎么滑都
# 没反应**，直到重启程序。静默到用户只会得出「这个功能时灵时不灵」。
c.advance(400)
c.feed(f, 120)                     # 新手势的第一个事件
check("停手后第一个事件解开了 latch", f.latch is False,
      f"latch={f.latch} accum={f.accum}")
c.advance(60)
r = c.feed(f, 120)
check("停手后第二次手势能再次触发（这是修复的核心）", r is True,
      f"r={r} accum={f.accum}")

head("[4] 慢慢滚不触发（时间窗语义）")
# 240 = 2 格。所以：
#   * 每格 120、间隔 <250ms  -> 第 2 格攒满 240 -> 恰好触发一次
#   * 每格 120、间隔 >250ms  -> 每次都是新手势，单格攒不满 -> 一次都不触发
f = _Flick(240, 250)
c = Clock()
fired = 0
for _ in range(10):
    c.advance(200)                # < 250ms，仍算同一次手势
    if c.feed(f, 120):
        fired += 1
check("200ms 间隔连滚 10 格 -> 恰好触发 1 次", fired == 1, f"触发 {fired} 次")

f = _Flick(240, 250)
c = Clock()
fired = 0
for _ in range(10):
    c.advance(400)                # > 250ms，每一格都是新手势
    if c.feed(f, 120):
        fired += 1
check("400ms 间隔连滚 10 格 -> 一次都不触发（每格都不够 240）",
      fired == 0, f"触发 {fired} 次")

head("[5] 方向无关（向上滚的 delta 是负的）")
f = _Flick(240, 250)
c = Clock()
c.feed(f, -120)
c.advance(60)
check("负 delta 两格 -> 一样触发", c.feed(f, -120) is True,
      f"accum={f.accum}")
f = _Flick(240, 250)
c = Clock()
c.feed(f, 120)
c.advance(60)
check("正 delta 两格 -> 一样触发", c.feed(f, 120) is True,
      f"accum={f.accum}")

head("[6] 单个大 delta（某些触控板一次给 240+）")
f = _Flick(240, 250)
c = Clock()
check("单个 delta=240 -> 触发", c.feed(f, 240) is True, f"accum={f.accum}")
f = _Flick(240, 250)
c = Clock()
check("单个 delta=120 -> 不触发", c.feed(f, 120) is False, f"accum={f.accum}")

head("[7] 参数容差")
f = _Flick(1, 1)                  # 极小阈值：任何非零 delta 都触发
c = Clock()
check("阈值=1 时单格就触发", c.feed(f, 1) is True)
f = _Flick(240, 250)
check("阈值/窗口可从参数改（不是写死常量）",
      f.threshold == 240 and f.window_ms == 250,
      f"threshold={f.threshold} window_ms={f.window_ms}")
check("_Flick 用 __slots__（无 __dict__，每事件无字典查找）",
      not hasattr(f, "__dict__"))

head("[8] 真实钩子：能装上、能干净摘除")
wakes = []
shown = {"v": False}
h = WheelHook(on_wake=lambda: wakes.append(1),
              is_showing=lambda: shown["v"])
started = h.start()
check("start() 返回 True", started is True)
check("running() 为 True", h.running() is True)
s = h.stats()
check("stats() 结构完整", isinstance(s, dict) and "events" in s
      and "flick_delta" in s and "flick_ms" in s, str(sorted(s))[:80])
check("stats 里有钩子线程名（便于诊断）",
      s.get("hook_thread") == "launchpad-wheelhook", str(s.get("hook_thread")))
check("error 字段为空", not s.get("error"), repr(s.get("error")))
h.stop()
time.sleep(0.4)
check("stop() 后 running() 为 False", h.running() is False)

head("[9] 图标缺失时静默降级")
try:
    import launchpad.paths as P

    _orig = P.app_icon_file
    P.app_icon_file = lambda: Path(r"V:\不存在的目录\launchpad.ico")
    h2 = WheelHook(on_wake=lambda: None, is_showing=lambda: False)
    ok = h2.start()                # 不该抛
    h2.stop()
    check("图标缺失不抛异常", True, f"start()={ok}")
finally:
    P.app_icon_file = _orig

head("[10] 已显示时抑制唤出（此时滚轮归翻页）")
shown["v"] = True
wakes2 = []
h3 = WheelHook(on_wake=lambda: wakes2.append(1),
               is_showing=lambda: shown["v"])
h3.start()
time.sleep(0.3)
# 直接驱动内部状态机并调投递槽，验证抑制路径（不依赖注入 ——
# 见模块 docstring：SendInput 的滚轮根本进不了低级钩子）。
h3._flick.accum = 0
h3._flick.latch = False
h3._flick._last_ms = -1
h3._flick.feed(120, 0)
trig = h3._flick.feed(120, 60)      # 累积 240 -> 状态机判定为一次手势
check("状态机本身判定了手势（前置条件）", trig is True, f"trig={trig}")
bridge = getattr(h3, "_bridge", None)
check("有跨线程投递桥", bridge is not None)
if bridge is not None:
    bridge._deliver()               # 这个槽在 GUI 线程查 is_showing
    time.sleep(0.3)
check("已显示时不产生 wake（滚轮归翻页，不该抢）", not wakes2,
      f"{len(wakes2)} 次")
h3.stop()

head("[11] 已收起时同一手势真的会 wake（对照上面那条）")
shown["v"] = False
wakes3 = []
h4 = WheelHook(on_wake=lambda: wakes3.append(1),
               is_showing=lambda: shown["v"])
h4.start()
time.sleep(0.3)
h4._flick.accum = 0
h4._flick.latch = False
h4._flick._last_ms = -1
h4._flick.feed(120, 0)
h4._flick.feed(120, 60)
b4 = getattr(h4, "_bridge", None)
if b4 is not None:
    b4._deliver()
    time.sleep(0.3)
check("已收起时会产生 wake", len(wakes3) == 1, f"{len(wakes3)} 次")
h4.stop()

head("[12] 边缘限制：手势起点必须在屏幕侧边")
# 为什么这是必须的：没有它的话，你在浏览器里**正常滚动**就会把启动器
# 弹到页面上。240 = 两格，而两指快滑在 250ms 内攒够两格是常事。
import ctypes                                                # noqa: E402
from launchpad import wheelhook as WH                        # noqa: E402

u = ctypes.WinDLL("user32", use_last_error=True)
try:
    u.MonitorFromPoint.argtypes = [WH._POINT, ctypes.c_ulong]
    u.MonitorFromPoint.restype = ctypes.c_void_p
    u.GetMonitorInfoW.argtypes = [ctypes.c_void_p,
                                  ctypes.POINTER(WH._MONITORINFO)]
    u.GetMonitorInfoW.restype = ctypes.c_int
    sw = u.GetSystemMetrics(0)
    sh = u.GetSystemMetrics(1)
    scr_ok = sw > 0
except Exception as exc:
    print(f"  [跳过多显示器探测] {exc}")
    scr_ok = False

if scr_ok:
    shown["v"] = False

    def mk(edge_px):
        w = []
        hh = WheelHook(on_wake=lambda: w.append(1),
                       is_showing=lambda: False, edge_px=edge_px)
        return hh, w

    h5, w5 = mk(40)
    h5.start()
    time.sleep(0.25)
    check("屏幕宽度探测到", sw > 100, f"{sw}x{sh}")
    check("起点贴着左边缘 -> 允许", h5._edge_ok(5) is True)
    check("起点贴着右边缘 -> 允许", h5._edge_ok(sw - 6) is True)
    check("起点在屏幕中间 -> 拒绝", h5._edge_ok(sw // 2) is False,
          f"x={sw // 2} band=40")
    h5.stop()

    h6, w6 = mk(0)
    h6.start()
    time.sleep(0.25)
    check("edge_px=0 时不限制（屏幕中间也允许）",
          h6._edge_ok(sw // 2) is True)
    h6.stop()

head("[13] 边缘不在时整个手势被作废（不会攒着等后半段）")
f = _Flick(240, 250)
c = Clock()
check("is_start：第一个事件是起点", f.is_start(0) is True)
f.feed(120, 0)
check("is_start：紧接的第二个事件不是起点", f.is_start(60) is False)
check("is_start：停手 400ms 后又是起点", f.is_start(460) is True,
      "（旧版漏了这个，导致停手后的新手势不被当起点）")
f2 = _Flick(240, 250)
c = Clock()
f2.feed(120, 0)
c.advance(60)
trig = f2.feed(120, 60)
check("（前置）累积够 2 格会触发", trig is True)
f2.discard()
check("discard() 把累积清零", f2.accum == 0, f"accum={f2.accum}")
check("discard() 也解除 latch（手势彻底作废）", f2.latch is False)
c.advance(60)
check("作废后同一手势继续滚也不会触发", f2.feed(120, 120) is False,
      f"accum={f2.accum}")

n_pass = sum(1 for _, ok, _ in _results if ok)
n_fail = len(_results) - n_pass
print()
print("=" * 72)
print(f"合计 {len(_results)} 项：PASS {n_pass} / FAIL {n_fail}")
for name, ok, detail in _results:
    if not ok:
        print(f"  FAIL  {name}   [{detail}]")
print("=" * 72)
print()
print("提醒：以上全部是状态机与装卸钩子的验证。")
print("真实手指的两指滑动**无法用自动化验证**（见模块 docstring），")
print("请手动试一次：收起状态下两指快速连滑两格。")
print("=" * 72)
sys.stdout.flush()
sys.exit(1 if n_fail else 0)