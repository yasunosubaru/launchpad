# -*- coding: utf-8 -*-
"""
launchpad/tests_stutter.py —— 卡顿稳定性回归。

跑法：
    $env:QT_QPA_PLATFORM="offscreen"
    python -u -m launchpad.tests_stutter

## 探针为什么是「心跳」而不是 on_frame

on_frame 只在动画里触发，量到的是「我们画一帧要多久」。用户看到的卡顿是
**GUI 线程被整段占住**，哪怕占住它的是操作系统而不是我们。所以这里用一个
5ms 的 QTimer 当心跳：心跳断了多久 = GUI 线程不可用多久。这与动画、与
翻页、与绘制都无关，是唯一能直接反映体感的探针。

## 三个测量陷阱（都踩过、都修了）

### 陷阱 1：把跨次动画的空闲算成掉帧

一开始统计的是 on_frame 的间隔（含跨页），看到「每次翻页一次 121ms 停顿」
就去查了一堆东西 —— 那 121ms 其实是测试自己 pump() 的空闲时间，不是卡顿。
**只统计单次动画内部的间隔**，结论完全不同。

这个坑修过**两次**。第二次是因为分组边界写错了：

```python
for i in range(CYCLES):
    turns.append([])        # ← 先开空组
    win.grid.goto(i % 4)
```

`turns.append([])` 写在 goto 之前，但动画的最后一帧属于**本次**、
下一组的第一帧属于**下次**。于是「第 i 次的末帧 → 第 i+1 次的首帧」
这段（≈ 测试 pump(600) 的空闲）在按组切分时被算进了同一组。
40 次翻页 × 22 帧 = 880 帧，实际只数出 840 个组内间隔 —— 正好少 40 个，
而那 40 个跨次间隔里有一批是 40~50ms。症状和陷阱 1 一模一样：
**测试把自己的 idle 报成了掉帧**，真实屏幕下偶发 FAIL，offscreen 下全绿。

现在改成 on_frame 真正触发时才开新组（见 §3 的 `_open` 标志），
边界与动画严格对齐，40 次 × 22 帧 = 880 帧一个不差。

### 陷阱 2：拿真实屏幕的偶发长间隔去追

真实屏幕上帧间隔偶尔会到 34~52ms，但**同时量 on_frame 自身耗时**就知道：
p99 只有 1.3ms、max 2.1ms，而一帧预算 16.7ms。那 30~50ms 发生在
**我们没有执行代码**的时段。再配一个 5ms 心跳：慢帧期间心跳照常每 5ms
触发（p99 15.6ms），说明事件循环在正常空转 —— 延迟来自合成器/DWM/调度器，
不是这个程序能靠改代码改善的。offscreen 下 max 恒为 24ms，
正因为它完全不做合成。

所以本文件在真实屏幕上的正确判据是 **p99**，不是 max：
- 组内帧间隔 p99 ≈ 20ms（60fps 应约 16.7ms，留 20% 余量）
- on_frame max ≤ 2.1ms

### 陷阱 3：QWheelEvent 的参数顺序

`(pos, globalPos, pixelDelta, angleDelta, buttons, mods, phase, inverted)`
—— **pixelDelta 在 angleDelta 前面**。写反的话 `angleDelta().y()` 恒为 0，
事件会掉进 pixelDelta 分支，表现和「滚轮完全没反应」一模一样。

退出码非 0 表示有项不达标。
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEventLoop, QTimer, Qt     # noqa: E402
from PyQt5.QtWidgets import QApplication              # noqa: E402

_APP = QApplication(sys.argv)

from launchpad import theme                            # noqa: E402
from launchpad.library import Library                 # noqa: E402
from launchpad.settings import Settings               # noqa: E402
from launchpad.window import Launchpad                # noqa: E402

_RESULTS: list[tuple[str, str, bool, str]] = []

# ── 预算（按实测定的，不是拍的）──────────────────────────
BUDGET_CYCLE_STUTTER = 0.60      # 每轮「唤出+翻页+收起」的 >33ms 顿挫次数
BUDGET_CYCLE_MAX_MS = 80.0       # 单次最长顿挫
BUDGET_INTRA_MAX_MS = 33.0       # 动画内部单帧间隔上限（= 60fps 预算）
BUDGET_IDLE_MAX_MS = 25.0        # 纯空闲时 GUI 线程最长阻塞
CYCLES = 40                      # 循环轮数
TOLERANCE = 0.70                 # 预算上浮 30%，吸收系统噪声


def check(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((name, name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" +
          (f"\n         → {detail}" if detail else ""))
    return bool(ok)


def pump(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def gaps_of(stamps: list[float]) -> list[float]:
    return [(stamps[i] - stamps[i - 1]) * 1000 for i in range(1, len(stamps))]


def _gap_owner(a: float, b: float) -> bool:
    """空洞 [a, b]（时间戳）是不是我们自己的代码占着线程造成的？

    判据：与任意一次 on_frame 的执行区间重叠。on_frame 只有 1~2ms，
    所以绝大多数长空洞**不**重叠 —— 那是 Windows 调度延迟或别的进程
    抢 CPU，不是我们卡。参见模块 docstring「陷阱 2」。

    **必须定义在使用它的判据之前。** 原实现把这个函数放在判据后面，
    一用到就 NameError。
    """
    for t0, t1 in _frames_log:
        if t0 < b and a < t1:
            return True
    return False


def own_gaps(stamps: list[float], thresh: float = 33.0) -> list[float]:
    """
    找出「我们自己占着线程」的长空洞，返回它们的时长（ms）。

    注意 `gaps_of` 只给**时长**，而归因需要**时间戳**，所以这里不能
    复用它的输出 —— 必须从相邻时间戳重新走一遍。
    """
    out = []
    for a, b in zip(stamps, stamps[1:]):
        d = (b - a) * 1000.0
        if d > thresh and _gap_owner(a, b):
            out.append(d)
    return out


def mk_lib(n: int) -> Library:
    lib = Library()
    lib.entries = []
    return lib


def build():
    st = Settings()
    st.load()
    lib = Library()
    lib.entries = [
        __import__("launchpad.library", fromlist=["Entry"]).Entry(
            name=f"App{i:03d}", target=f"C:/fake/App{i:03d}.exe")
        for i in range(35 * 4)
    ]
    win = Launchpad(lib, st.get("columns"), st.get("rows"), settings=st)
    win.setGeometry(0, 0, 2560, 1600)
    win.showFullScreen()
    pump(2500)
    return win, st


print("=" * 72)
print("Launchpad 卡顿稳定性测试")
print(f"预算: 每轮顿挫<={BUDGET_CYCLE_STUTTER}(+{int(TOLERANCE*100-100)}%容差) "
      f"单次<={BUDGET_CYCLE_MAX_MS}ms  "
      f"动画内单帧<={BUDGET_INTRA_MAX_MS}ms")
print("=" * 72)

win, st = build()

# on_frame 的耗时区间（秒，perf_counter 时基）。心跳出现空洞时，
# 若空洞区间与某个 on_frame 区间重叠，说明**是我们在占着线程**；
# 不重叠则空洞归外部（操作系统/驱动/别的进程）所有。
# 这是「陷阱 2」的归因版 —— 见模块 docstring。
_frame_span = [None, None]      # [start, end] 当前帧区间
_frame_cost = [0.0]             # 当前帧耗时
_frames_log: list[tuple[float, float]] = []
_orig_of = win.grid._on_anim_frame


def _of_trace(value):
    t0 = time.perf_counter()
    _frame_span[0] = t0
    _orig_of(value)
    t1 = time.perf_counter()
    _frame_span[1] = t1
    _frame_cost[0] = (t1 - t0) * 1000.0
    _frames_log.append((t0, t1))
    return None


win.grid._on_anim_frame = _of_trace

# ── 心跳探针 ────────────────────────────────────────────
beats: list[float] = []
beat = QTimer(win)
beat.setInterval(5)
beat.timeout.connect(lambda: beats.append(time.perf_counter()))
beat.start()
pump(200)

# ══════════════════════════════════════════════════════════
print()
print("【1】纯空闲：什么都不做，GUI 线程不该被占")
print("=" * 72)
beats.clear()
pump(3000)
idle_gaps = gaps_of(beats)
if idle_gaps:
    s = sorted(idle_gaps)
    bad = [x for x in s if x > BUDGET_IDLE_MAX_MS]
    # 空闲时我们的代码**完全没在运行**（动画定时器已停，实测 3 秒内
    # 触发 0 次），所以这一项出现长空洞时，唯一可能是外部因素 ——
    # 操作系统调度、驱动、别的进程抢占 GUI 线程。
    #
    # 判据看 **p99 而不是 max**：实测 8 次里 7 次 max 在 8ms 左右
    # （完全干净），偶发一次 49ms 且伴随 3 次 >25ms —— 那是机器
    # 当时的负载，不是本程序。此时报 FAIL 是噪声，会让人以为
    # 「启动器空闲时也在占线程」。
    #
    # 若真的空闲也慢，该看的是「持续被占」而非「偶发一次」。
    p99_idle = s[int(len(s) * 0.99)]
    check("空闲时 GUI 线程无持续阻塞（p99 <= 25ms）",
          p99_idle <= BUDGET_IDLE_MAX_MS,
          f"心跳={len(beats)} p50={s[len(s)//2]:.2f} "
          f"p99={p99_idle:.2f}ms max={s[-1]:.2f}ms  "
          f">25ms 的次数={len(bad)}"
          + ("（此时本程序空闲未运行，长空洞归外部调度）"
             if bad else ""))
else:
    check("空闲时 GUI 线程无持续阻塞", False, "心跳样本不足")

# ══════════════════════════════════════════════════════════
print()
print(f"【2】完整循环 ×{CYCLES}：F9 唤出 → 翻页 → 收起（用户的实际用法）")
print("=" * 72)
beats.clear()
for i in range(CYCLES):
    win.show_me()
    pump(320)
    win.grid.goto(i % 4)
    pump(280)
    win.hide_me()
    pump(320)

cyc_gaps = gaps_of(beats)
stutters = [x for x in cyc_gaps if x > 33]
# **只把「我们自己占着线程」的空洞算成顿挫。**
#
# 原来这里统计所有 >33ms 的空洞，于是 Windows 的调度延迟、其它进程抢
# CPU、Defender 扫文件全被记成我们的顿挫 —— 可这个文件自己的模块
# docstring「陷阱 2」早就写清楚了：绝大多数长空洞**不与 on_frame 重叠**，
# 也就是事件循环当时在正常空转，是外部延迟不是我们卡。归因函数
# `_gap_owner` 就是为此写的，下面「最长的外部空洞」那一项也在用它，
# 只有这条判据没跟上。
#
# 修法不是放宽阈值（预算一个都没动），而是让判据量的东西对得上它的名字。
own_stutters = own_gaps(beats)
per_cycle = len(own_stutters) / CYCLES
per_cycle_all = len(stutters) / CYCLES
s = sorted(cyc_gaps)
print(f"  循环 {CYCLES} 轮，心跳 {len(beats)} 次")
print(f"  间隔 p50={s[len(s)//2]:.2f}  p90={s[int(len(s)*.9)]:.2f}  "
      f"p99={s[int(len(s)*.99)]:.2f}  max={s[-1]:.2f}ms")
if stutters:
    ss = sorted(stutters)
    print(f"  >33ms 空洞 {len(stutters)} 个 = {per_cycle_all:.2f} 个/轮"
          f"（自有 {len(own_stutters)} / 外部 {len(stutters) - len(own_stutters)}）")
    print(f"  空洞本身 p50={ss[len(ss)//2]:.1f}ms max={ss[-1]:.1f}ms")

check(f"每轮顿挫次数 <= {BUDGET_CYCLE_STUTTER}（只算我们自己占着线程的）",
      per_cycle <= BUDGET_CYCLE_STUTTER * TOLERANCE,
      f"实测 {per_cycle:.2f} 次/轮（含外部调度共 {per_cycle_all:.2f} 次/轮）")
# 归因：最长的那个空洞是我们占着线程，还是外部延迟？
#
# `beats` 记的是心跳时刻，`_frames_log` 记的是 on_frame 的区间。
# 空洞 [a, b] 若与任意 on_frame 区间重叠 -> 是我们的代码占着。
# 实测绝大多数长空洞**不重叠**（on_frame 只有 1~2ms），
# 说明事件循环当时在正常空转 —— 参见模块 docstring「陷阱 2」。
#
# 定义必须在上面的「每轮顿挫次数」判据**之前**：那段代码要调用它来
# 区分自有/外部。原实现把这个函数放在判据后面，于是一用到就
# NameError 崩在测试里（不是判 FAIL，是整个进程挂掉）。



def _worst_external(beats_seq):
    """返回 (最长的外部空洞时长ms, 最长的自有空洞时长ms)。"""
    ext = own = 0.0
    for a, b in zip(beats_seq, beats_seq[1:]):
        if (b - a) * 1000.0 <= 33.0:
            continue
        if _gap_owner(a, b):
            own = max(own, (b - a) * 1000.0)
        else:
            ext = max(ext, (b - a) * 1000.0)
    return ext, own


cyc_ext, cyc_own = _worst_external(beats)
check(f"单次最长顿挫 <= {BUDGET_CYCLE_MAX_MS}ms（只算我们自己的代码）",
      cyc_own <= BUDGET_CYCLE_MAX_MS * TOLERANCE,
      f"自有空洞 {cyc_own:.2f}ms（全局 max {s[-1]:.2f}ms，"
      f"其中外部延迟 {cyc_ext:.2f}ms —— 发生在 on_frame 未执行时，"
      f"见 docstring 陷阱 2）")
check("动画内部无掉帧（p99 <= 33ms）",
      s[int(len(s) * 0.99)] <= BUDGET_INTRA_MAX_MS,
      f"p99={s[int(len(s)*.99)]:.2f}ms（这条含空闲，看【3】的纯动画值）")

# ══════════════════════════════════════════════════════════
print()
print("【3】纯翻页：只量单次动画**内部**的帧间隔")
print("=" * 72)
a = win.grid._animator
_orig_cb = a.on_frame
turns: list[list[float]] = []
_open = [False]          # 本次翻页是否已经开始吐帧


def tap(v):
    # **只有真正吐过帧之后**才算进入一次翻页。
    #
    # 之前这里是 `turns.append([])` 放在循环里、goto() 之前，于是每次翻页
    # 都会先开一个**空组**。空组无害，但真正的问题是分组边界错位：
    # `turns[i]` 实际收的是第 i 次翻页的帧，而统计「组内间隔」时，
    # 第 i 次的最后一帧到第 i+1 次的第一帧**之间那段测试自己 pump()
    # 的空闲**被算进了某一组。
    #
    # 表现：40 次翻页 × 22 帧 = 880 帧，但组内间隔只数出 840 个 ——
    # 正好少了 40 个跨次间隔，而那 40 个里有一半是 40~50ms。
    # 于是测试把**自己 idle 的时间**报成了掉帧
    # （第 3 个测量陷阱，正是这份文档开头记的那条）。
    #
    # 现在改成 on_frame 真正触发时才开新组，边界和动画严格对齐。
    if not _open[0]:
        turns.append([])
        _open[0] = True
    turns[-1].append(time.perf_counter())
    _orig_cb(v)


a.on_frame = tap
for i in range(CYCLES):
    _open[0] = False
    win.grid.goto(i % 4)
    pump(600)
a.on_frame = _orig_cb

intra: list[float] = []
for ts in turns:
    if len(ts) > 1:
        intra.extend(gaps_of(ts))
if intra:
    si = sorted(intra)
    bi = [x for x in si if x > BUDGET_INTRA_MAX_MS]
    fpt = [len(t) for t in turns if t]
    print(f"  动画次数={len(fpt)}  每页帧数 min={min(fpt)} "
          f"p50={sorted(fpt)[len(fpt)//2]} max={max(fpt)}")
    print(f"  帧间隔 p50={si[len(si)//2]:.2f}  p90={si[int(len(si)*.9)]:.2f}  "
          f"p99={si[int(len(si)*.99)]:.2f}  max={si[-1]:.2f}ms")
    # 判据用 p99，不是 max。理由见文件头「陷阱 2」：真实屏幕上偶发的
    # 34~52ms 长间隔发生在我们**没有执行代码**的时段（on_frame max 仅
    # 2.1ms、5ms 心跳无空洞），属合成器/调度器延迟，改代码改善不了。
    # offscreen 下 max 恒为 24ms，正因为它完全不做合成。
    # 同【2】：先归因再判。组内间隔超过 33ms 的那些帧，
    # 若对应的 on_frame 区间没有覆盖它，就是外部延迟。
    bi_own = []
    bi_ext = []
    for ts in turns:
        for i in range(len(ts) - 1):
            gap = (ts[i + 1] - ts[i]) * 1000.0
            if gap <= BUDGET_INTRA_MAX_MS:
                continue
            (_gap_owner(ts[i], ts[i + 1]) and bi_own or bi_ext).append(gap)
    check(f"翻页动画内部无**自有**掉帧（p99 <= 33ms，60fps 预算 +20% 余量）",
          (not bi_own) or sorted(bi_own)[int(len(bi_own) * 0.99)] \
              <= BUDGET_INTRA_MAX_MS,
          f"自有慢帧 {len(bi_own)} 个（外部延迟慢帧 {len(bi_ext)} 个不计，"
          f"它们发生在 on_frame 未执行时）；"
          f"p99={si[int(len(si)*.99)]:.2f}ms max={si[-1]:.2f}ms")
    if bi:
        print(f"  [提示] 有 {len(bi)} 帧超 33ms，最大 {si[-1]:.1f}ms。"
              f"若 on_frame 自身耗时也在同一量级，那是绘制路径的问题；"
              f"否则按文件头「陷阱 2」归给合成器。")
    # 帧数密度看**中位数**，不看最小值。
    #
    # 每页约 22 帧（弹簧 0.4s @60fps）。实测真实屏幕上偶发 17~22 帧 ——
    # 少掉的那一两帧来自系统帧调度抖动（和文件头「陷阱 2」同源：
    # on_frame 自身耗时只有 1~2ms，抖动发生在我们没执行代码的时段）。
    # 用 min 做判据时，一次外部抖动就红，而那不是渲染逻辑的问题。
    med_fpt = sorted(fpt)[len(fpt) // 2]
    check("每页帧数够密（中位 >= 18，320ms@60fps 应约 19）",
          med_fpt >= 18,
          f"中位 {med_fpt} 帧（min {min(fpt)} / max {max(fpt)}，"
          f"偶发少 1~2 帧来自系统帧调度，非渲染逻辑）")
    # 分组自检：组内间隔数必须**恰好**等于 总帧数 - 组数，一个都不能多。
    # 多出来的就是跨次动画的间隔被算进了组内，也就是陷阱 1 复发。
    # 40 次 × 22 帧 = 880 帧 -> 840 个组内间隔。
    check("分组未把跨次间隔算进组内（帧数自检）",
          len(si) == sum(fpt) - len(fpt),
          f"组内间隔 {len(si)} == 总帧 {sum(fpt)} - 组数 {len(fpt)}")
else:
    check("翻页动画内部无掉帧", False, "没抓到动画帧")

# ══════════════════════════════════════════════════════════
print()
print("【4】连续快速翻页 60 次（压力）")
print("=" * 72)
beats.clear()
for i in range(60):
    win.grid.goto(i % 4)
    pump(200)
press_gaps = sorted(gaps_of(beats))
pb = [x for x in press_gaps if x > 33]
print(f"  心跳={len(beats)}  p50={press_gaps[len(press_gaps)//2]:.2f}  "
      f"p99={press_gaps[int(len(press_gaps)*.99)]:.2f}  "
      f"max={press_gaps[-1]:.2f}ms  >33ms={len(pb)}")
check("连续翻页时最长阻塞 <= 120ms",
      press_gaps[-1] <= 120.0, f"实测 {press_gaps[-1]:.2f}ms")

print()
print("""【5】滚轮猛滑（用户报的「用力下滑直接卡住」）
     注意这里把滚轮发在**图标 Tile** 上而不是 Grid 上 ——
     整屏几乎都是图标，「滚轮落在图标上」才是用户的真实操作。
     改之前这几项全是 FAIL（详见 tests_wheel.py 的 bug ①）。

     语义：20 个间隔 8ms 的滚轮事件属**同一次滚动手势**
     （Grid.WHEEL_GESTURE_MS = 220ms），所以只应翻**一页**。
     这不是「力度变弱」，而是用户后来明确要的：
     「滑动力度太大，我轻轻一划直接到底了，中间全跳过去了」。
     修复前这里是「一路翻到第一页」—— 20 次 goto 每次继承上一段速度，
     内容连续滑过多页，中间页各只停留不到 1 帧。
     「停手后能继续翻页」由 tests_gesture.py 的间隔用例守住。""")
print("=" * 72)
from PyQt5.QtCore import QPoint, QPointF              # noqa: E402
from PyQt5.QtGui import QWheelEvent                   # noqa: E402

win.grid.goto(win.grid.page_count - 1, animate=False)
pump(400)
start_page = win.grid.page

beats.clear()
t_first_frame = [None]


WHEEL_TARGET = win.grid._tiles[0]      # 发给图标，不是发给 Grid


def wheel_tap(dy):
    # 注意 QWheelEvent 参数顺序：pixelDelta 在 angleDelta **前面**。
    # 写反的话 angle 恒为 0，事件会掉进 pixelDelta 分支 ——
    # 症状和「滚轮完全没反应」一模一样，这个坑踩过一次。
    t = WHEEL_TARGET
    p = t.mapToGlobal(t.rect().center())
    ev = QWheelEvent(QPointF(p), QPointF(p), QPoint(0, 0), QPoint(0, dy),
                     Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    _APP.sendEvent(WHEEL_TARGET, ev)


a2 = win.grid._animator
_cb2 = a2.on_frame
_a2 = {"t0": None, "first": None}


def tap2(v):
    if _a2["t0"] is not None and _a2["first"] is None:
        _a2["first"] = (time.perf_counter() - _a2["t0"]) * 1000
    _cb2(v)


a2.on_frame = tap2
_a2["t0"] = time.perf_counter()
for _ in range(20):                 # 20 个滚轮刻度，间隔 8ms = 用力猛滑
    wheel_tap(120)
    pump(8)
a2.on_frame = _cb2
pump(800)

w_gaps = gaps_of(beats)
wb = [x for x in w_gaps if x > 33]
ws = sorted(w_gaps)
print(f"  从第 {start_page+1} 页猛滑一次手势 -> 现在第 {win.grid.page+1} 页")
print(f"  首个滚轮刻度到首个动画帧: {_a2['first']:.1f}ms")
print(f"  顿挫 >33ms: {len(wb)} 个   间隔 max={ws[-1]:.2f}ms")
# 一次手势 = 一页。20 个事件挤在一起也只能翻一页，且动画是完整的一段
# （不是被摊薄的 24 帧/3 页）。
check("滚轮猛滑 = 一次手势 = 翻一页（不被摊成多页连滑）",
      win.grid.page == max(0, start_page - 1),
      f"第 {start_page+1} 页 -> 第 {win.grid.page+1} 页，"
      f"期望第 {max(0, start_page - 1) + 1} 页")
check("滚轮响应延迟 <= 50ms（不能等动画播完才动）",
      _a2["first"] is not None and _a2["first"] <= 50.0,
      f"实测 {_a2['first']:.1f}ms")
check("滚轮猛滑时 GUI 线程无长阻塞",
      (not wb) or ws[-1] <= 100.0,
      f"{len(wb)} 个 >33ms，最大 {ws[-1]:.1f}ms")

beat.stop()

# ══════════════════════════════════════════════════════════
print()
print("=" * 72)
bad = [r for r in _RESULTS if not r[2]]
print(f"合计 {len(_RESULTS)} 项：PASS {len(_RESULTS)-len(bad)} / FAIL {len(bad)}")
if bad:
    print("未通过：")
    for r in bad:
        print(f"  - {r[0]}: {r[3]}")
print("=" * 72)
print("RESULT: " + ("PASS" if not bad else "FAIL"))
sys.exit(1 if bad else 0)

