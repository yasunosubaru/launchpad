# -*- coding: utf-8 -*-
"""
macOS Launchpad 风格翻页动画 —— 亚像素、手势驱动、速度连续的动画组件。

本模块是 `grid.py` 里那段 ``QVariantAnimation`` + ``OutCubic`` + ``int(v)``
的**替代品**，不是补丁。旧实现有三个硬伤：

1. ``int(v)`` 每帧截断成整数像素。一屏 900px、320ms、60fps 只有约 19 帧，
   整段动画的有效取值不到 20 个，视觉上就是「跳」过去的。
2. 只有位移，没有景深/缩放，因此没有 macOS 那种分页的立体感。
3. 动画一旦 ``start()`` 就与手势完全脱钩：按下即播，松手无处插手，
   速度在按键与动画之间是硬切。

本模块全部内部量都是 ``float``，只有真正写控件坐标时才转 int；
并且提供 ``begin() / update(delta) / end(velocity)`` 让页面跟手，
松手后由**真实速度**决定去向并把速度连续地交棒给弹簧。

────────────────────────────────────────────────────────────────────────
调研结论（出处见每条末尾）
────────────────────────────────────────────────────────────────────────

【1】弹簧用两参数描述：perceptual duration + bounce，不是 mass/stiffness/damping
    —— WWDC23 "Animate with springs" https://developer.apple.com/videos/play/wwdc2023/10158/
       · bounce = 0 是**临界阻尼**（平滑、长尾），bounce > 0 欠阻尼（过冲），
         bounce < 0 过阻尼（更扁的长尾）。
       · bounce = 100% 时阻尼为 0，退化成纯余弦，永不停；此时 duration 正好等于
         余弦的周期 —— 这反证了 ω₀ = 2π/duration。
       · "settling duration" 与 duration 是两回事：弹簧尾巴无限长，
         duration 是**感知**时长，可预期、不会随参数漂移。
    —— 参数换算（Apple 官方给出的形式，经 kvin.me 与 a327ex.com 两处独立复核）：
         mass      = 1
         stiffness = (2π / duration)²
         damping   = 4π(1 - bounce) / duration        (bounce ≥ 0)
         damping   = 4π / (duration × (1 + bounce))   (bounce < 0)
       换算成物理量即 damping = 2ζ√(k·m)，于是阻尼比
         ζ = 1 - bounce          (bounce ≥ 0)   → bounce=1 ⟺ ζ=0（无阻尼）✓
         ζ = 1 / (1 + bounce)    (bounce < 0)   → ζ > 1（过阻尼）✓
       与 WWDC18 "Designing Fluid Interfaces" 的 response/dampingRatio 形式
       （k = (2π/response)², c = 4π·ζ/response）完全同构，两者互推：
         bounce = 1 - dampingRatio
       —— 注意：WWDC23 幻灯片上那行被抄成 ``1 - 4π × bounce ÷ duration``，
         代入 bounce=1 会得到 ζ = -24，这跟同一场演讲里「bounce=100% 即无阻尼」
         的说法直接矛盾，是转录错误。上面用的才是数学上自洽的版本。

【2】弹簧必须维护**位置与速度的双连续**
    —— 同上 WWDC23："our animations have continuous position and velocity"。
       缓动曲线（贝塞尔）**无法**表示初始速度：手势松开时曲线速度被强制归零，
       "its motion jerks to a halt as the gesture ends"。
       弹簧可以把手势末速度直接当初速度 —— 这是它不可替代的地方。
       中途改目标（retarget）时沿用当前速度，WWDC23 称其为
       "velocity preservation"，"makes these kind of interruptions feel smooth"。
    —— 旧实现正是踩了这条：按下播 320ms 曲线，起止速度都是 0，交棒必然硬切。

【3】甩手后的惯性用**指数衰减**，衰减系数是「每毫秒」的
    —— UIScrollView.decelerationRate，https://developer.apple.com/documentation/uikit/uiscrollview/decelerationrate
       .normal = 0.998（每毫秒保留 99.8% 速度），.fast = 0.99。
       位置公式 x(t) = target - A·exp(-t/τ)，A = v₀·τ；
       τ = -1000/ln(rate) 毫秒 ⟹ normal ≈ 499.5ms、fast ≈ 99.5ms
       （旁证：medium.com/@esskeetit "Deceleration mechanics of UIScrollView"
         与 reactnative.dev ScrollView 文档均列出 0.998 / 0.99）。
    —— 本模块用它做「松手后会滑多远」的**预测落点**，据此决定进不进下一页，
       而非单纯的「过半就翻页」。这正是慢拖能自然回弹、快甩能果断前进的原因。

【4】越界阻尼用双曲压缩，系数 0.55
    —— iOS UIScrollView 的橡皮筋：f(x) = (x·d·c) / (d + c·x)，c = 0.55，
       d = 可视尺寸。x→∞ 时 f → d，即最大越界量等于一屏。
       （CodenameOne 的 rubberBandCompress() 注释明确写
        「hundredths, e.g. 55 = 0.55 which matches iOS UIScrollView」。）
    —— Launchpad 在首/末页继续拖必须有这个，否则会一拖到底、边界很硬。

【5】Qt 侧的三个坑
    —— QAbstractAnimation.currentTime 是 **int 毫秒**，分辨率 1ms，
       且官方文档明写「neither the interval between calls nor the number of
       calls to this function are defined; though, it will normally be 60
       updates per second」—— 既不能当高精度时钟，也不能假定 60fps。
       所以本类 ``duration()`` 返回 -1（不设时长），
       改用 ``QElapsedTimer`` 取真实 dt。
       https://doc.qt.io/qt-6/qabstractanimation.html
    —— **start() 会从 C++ 同步回调一次 updateCurrentTime(0)**。
       意味着：刚起播就已经发过一帧 on_frame，grid 的回调必须幂等；
       而且 on_frame 抛出的异常会跨过这个 C++ 虚函数帧 —— 那条路径上
       没有正常的异常传播，解释器直接 0xC0000409（STATUS_STACK_BUFFER_
       OVERRUN）终止，faulthandler 都抓不到，整个 GUI 进程被带走。
       所以 updateCurrentTime 内部必须兜住异常，改走 errorOccurred 信号。
       （本仓库实测踩过：这是最初整轮测试直接崩溃的原因。）
    —— QVariantAnimation 的 valueChanged 每帧发一个 QVariant，
       再 lambda 到控件，既装箱又丢亚像素。
    —— QTimeLine 的刻度是整数帧号，同样只有 ms 分辨率，且不提供「手势中途
       改目标并继承速度」的能力。

【6】macOS Launchpad 的景深
    —— Apple 从未公布过 Launchpad 分页时页面缩放/淡出的**具体系数**
       （HIG 只说 motion 要「instant response」「maintain spatial
       consistency」，见 https://developer.apple.com/design/human-interface-guidelines/motion）。
       本模块的 DEPTH_MIN_SCALE 是按 Big Sur Launchpad 的观感调出来的
       **经验值**，不是引用的官方数字，改它不影响其它任何逻辑。
    —— HIG 同时强调 Reduce Motion。本模块的 spring bounce 可以调到 0
       退化为临界阻尼（无过冲），配合调用方关掉缩放即可满足无障碍要求。

────────────────────────────────────────────────────────────────────────
用法（供 grid.py 接入）
────────────────────────────────────────────────────────────────────────
    anim = PageAnimator(on_frame=self._on_page_frame)
    anim.set_page_size(self._page_height())
    anim.set_bounds(-(self._pages - 1) * h, 0.0)
    anim.set_easing(settings.get("page_easing"))   # "spring" / "outQuint" / ...
    anim.set_duration(settings.get("page_duration"))
    anim.settled.connect(lambda: self.page_changed.emit(...))

    anim.animate_to(-target_page * h)               # 键盘/滚轮翻页（自动继承当前速度）
    anim.begin(); anim.update(drag); anim.end()     # 拖拽翻页，drag 是**累计**位移

接入时必须注意的四点：

1. ``on_frame`` 的 value 是 float。**不要**在动画过程中 round() 再写
   控件坐标 —— 那正是旧实现「动画僵硬」的成因。要取整就在 move()
   那一层做，并且用 QWidget 的 move() 接受 int 即可（Qt 自己会截断），
   保留本模块内部的 float。
2. ``update(drag)`` 收的是相对按下点的**累计**位移。mouseMoveEvent 给
   的是增量，要先自己累加。传增量会导致位置永不累加、翻页完全失灵。
3. ``on_frame`` 必须幂等：start() 会同步回调一次 updateCurrentTime(0)，
   所以刚起播就会收到一帧。
4. spring 的感知时长 0.40s 对应实际约 0.56s（1.39×）。这是苹果明确
   区分的 perceptual duration 与 settling duration，不是 bug；
   要更快就 set_spring(duration=0.30) → 约 0.42s。
   Reduce Motion 场景用 set_spring(duration=0.4, bounce=0.0)
   （临界阻尼、完全不过冲）并把 DEPTH_MIN_SCALE 设成 1.0 关掉景深。

非 spring 模式无法真正速度连续（见【2】）：本模块的做法是用
e'(0) 反解一个缩放后的时长 D' = Δ·e'(0)/v₀ 来近似匹配起始斜率，
并把 D' 夹在 [0.4D, 2.5D]。要精确的速度连续请用 spring。
"""

from __future__ import annotations

import math
import sys

from PyQt5.QtCore import (QAbstractAnimation, QElapsedTimer, QTimer, Qt,
                          pyqtSignal)

__all__ = [
    "DECELERATION_NORMAL", "DECELERATION_FAST", "RUBBER_BAND_C",
    "MIN_DAMPING_RATIO", "SPRING_SUBSTEP",
    "ease_out_cubic", "ease_out_quint", "ease_out_expo", "ease_in_out_cubic",
    "EASINGS", "is_spring", "resolve_easing",
    "deceleration_time_constant", "project_deceleration",
    "rubber_band", "clamp",
    "Spring", "PageAnimator",
]


# ── 调参常量（出处见模块 docstring）────────────────────────────────
#: UIScrollView.decelerationRate.normal —— 每毫秒保留 99.8% 速度，τ≈499.5ms
DECELERATION_NORMAL = 0.998
#: UIScrollView.decelerationRate.fast —— 每毫秒保留 99%，τ≈99.5ms
DECELERATION_FAST = 0.99
#: iOS UIScrollView 橡皮筋压缩系数（hundredths）
RUBBER_BAND_C = 0.55

#: 阻尼比下限。ζ=0 意味着永不停摆（bounce=100%），无法 settle；
#: 弹簧积分必须收敛，所以把 β 抬到这个下限。0.02 对应 Q≈25，
#: 视觉上依旧极弹，但一定能停下来。
MIN_DAMPING_RATIO = 0.02

#: 阻尼比上限。bounce = -1 是「完全扁平」的退化情形，
#: ζ = 1/(1+bounce) 会除零；钳到这里等价于「有摩擦但过阻尼得很厉害」，
#: 仍然是收敛的，只是尾巴更长。
MAX_DAMPING_RATIO = 10.0

#: bounce 的合法区间。SwiftUI 也是 [-1, 1]，但 -1 端点退化，
#: 所以实际可用区间是 (-1, 1]，见 set_spring()。
MIN_BOUNCE = -1.0

#: 弹簧定步长积分步长（秒）。配合下面的稳定性上限使用。
SPRING_SUBSTEP = 1.0 / 240.0
#: 允许的最大 ω₀·h。RK4 对本系统（阻尼振子）的稳定域大约在 2.8，
#: 这里取 0.5 留足余量 —— 超出就会指数发散（实测 k=1e6 时 ω₀h≈4.2，
#: 1000 帧内冲到 5.3e6 而不是收敛到 0）。
_MAX_OMEGA_H = 0.5
_MAX_SUBSTEPS = 512


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


# ═════════════════════════════════════════════════════════════════
# 1. 缓动函数
#    每个缓动都有 (值函数, 导数函数)。导数不是装饰：它给
#    velocity() 提供精确值，也是松手后反解时长的依据。
#    全部强制 f(0)=0、f(1)=1 —— outExpo 的朴素形式 1-2^(-10t) 在 t=1
#    只到 0.9990234375，最后一帧会差 0.1% 页宽（约 1px），这里直接截断。
# ═════════════════════════════════════════════════════════════════

def ease_out_cubic(t: float) -> float:
    """三次缓出。e(0)=0, e(1)=1, e'(0)=3。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    u = 1.0 - t
    return 1.0 - u * u * u


def _d_out_cubic(t: float) -> float:
    if t <= 0.0:
        return 3.0
    if t >= 1.0:
        return 0.0
    u = 1.0 - t
    return 3.0 * u * u


def ease_out_quint(t: float) -> float:
    """五次缓出，比 outCubic 更「先冲后缓」，尾部更利落。e'(0)=5。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    u = 1.0 - t
    return 1.0 - u * u * u * u * u


def _d_out_quint(t: float) -> float:
    if t <= 0.0:
        return 5.0
    if t >= 1.0:
        return 0.0
    u = 1.0 - t
    u2 = u * u
    return 5.0 * u2 * u2


def ease_out_expo(t: float) -> float:
    """指数缓出，前 30% 就走完 90% 位移 —— 最「急停」的一条。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 1.0 - math.pow(2.0, -10.0 * t)


def _d_out_expo(t: float) -> float:
    if t <= 0.0:
        return 10.0 * math.log(2.0)
    if t >= 1.0:
        return 0.0
    return 10.0 * math.log(2.0) * math.pow(2.0, -10.0 * t)


def ease_in_out_cubic(t: float) -> float:
    """两端对称，中点最慢。e'(0)=0 —— 起手就是静止，无法速度匹配。"""
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    if t < 0.5:
        return 4.0 * t * t * t
    u = 2.0 - 2.0 * t
    return 1.0 - u * u * u * 0.5


def _d_in_out_cubic(t: float) -> float:
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 0.0
    if t < 0.5:
        return 12.0 * t * t
    u = 2.0 - 2.0 * t
    return 3.0 * u * u


#: 名字与顺序都与 settings.EASINGS 保持一致（那边是设置界面下拉框的顺序）。
#: 值 = (f, f')
EASINGS: dict[str, tuple] = {
    "outQuint": (ease_out_quint, _d_out_quint),
    "outCubic": (ease_out_cubic, _d_out_cubic),
    "inOutCubic": (ease_in_out_cubic, _d_in_out_cubic),
    "outExpo": (ease_out_expo, _d_out_expo),
    # spring 不走闭式，由 RK4 积分器推进；登记在这里只为让
    # settings.EASINGS 的每个名字都能被 resolve_easing() 接受。
    "spring": (None, None),
}


def is_spring(name: str) -> bool:
    return name == "spring"


def _screen_refresh_hz() -> float:
    """读一次屏幕刷新率。失败/无 GUI 时返回 0（调用方会走折中值）。"""
    try:
        from PyQt5.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if app is None:
            return 0.0
        scr = QGuiApplication.primaryScreen()
        if scr is None:
            return 0.0
        hz = float(scr.refreshRate() or 0.0)
        return hz if hz > 0 else 0.0
    except Exception:      # noqa: BLE001 - 环境探测失败不该影响动画
        return 0.0


def resolve_easing(name: str):
    """返回 (f, f')；名字非法时回落到 outQuint（与 settings 默认值一致）。

    `f` 对 spring 是 None —— 调用方必须先 is_spring() 分流。
    """
    return EASINGS.get(name) or EASINGS["outQuint"]


# ═════════════════════════════════════════════════════════════════
# 2. 惯性与橡皮筋（松手判定要用）
# ═════════════════════════════════════════════════════════════════

def deceleration_time_constant(rate: float = DECELERATION_NORMAL) -> float:
    """rate（每毫秒保留因子）→ 时间常数 τ（毫秒）。

    v(t) = v₀·rate^t（t 以毫秒计），令其等于 v₀·e^(-t/τ)：
    t·ln(rate) = -t/τ ⟹ τ = -1/ln(rate)。
    0.998 → 499.5ms；0.99 → 99.5ms。
    """
    if not 0.0 < rate < 1.0:
        raise ValueError(f"deceleration rate 必须在 (0,1)，收到 {rate!r}")
    return -1.0 / math.log(rate)


def project_deceleration(velocity_px_per_s: float,
                         rate: float = DECELERATION_NORMAL) -> float:
    """按指数衰减模型预测「若现在松手，还会滑多远」（像素，可为负）。

    A = v₀(px/ms) · τ(ms)，由 x(t) = target - A·e^(-t/τ) 在 t=0 处取 v₀ 得到。
    """
    return (velocity_px_per_s / 1000.0) * deceleration_time_constant(rate)


def rubber_band(offset: float, dimension: float,
                c: float = RUBBER_BAND_C) -> float:
    """越界阻尼：把越界量压进 [-dimension, +dimension]。

    f(x) = (x·d·c) / (d + c·x)，保留符号。x 增大时 f 单调递增且 f < x
    （越拖越沉），x→∞ 时 f→d（最多拖出一整屏就拉不动）。
    """
    if dimension <= 0.0:
        return 0.0
    sign = 1.0 if offset >= 0.0 else -1.0
    x = abs(offset)
    return sign * dimension * ((x * c) / (dimension + c * x))


# ═════════════════════════════════════════════════════════════════
# 3. 真正的弹簧：RK4 定步长积分
# ═════════════════════════════════════════════════════════════════

class Spring:
    """阻尼谐振子，以「离目标点的位移」为状态量。

        m·x'' = -k·x - c·x'

    用 RK4 而非三次贝塞尔近似，也不用半隐式欧拉：贝塞尔根本表达不了
    任意初速度，半隐式欧拉在 k 大时阻尼项会数值发散。

    外部 dt 会被拆成**定长**子步（步长见 ``_stable_step``，由弹簧自身
    参数决定而非由 dt 决定），这样 30fps 和 144fps 把同一段时间切成
    同一种子步，走的是同一条相空间轨迹 —— 这是帧率无关的前提；把 dt
    原样喂给积分器，结果必然随帧率漂移。

    积分器内部用**秒**和 **px/s**：step() 收秒，x 是 px，v 是 px/s。
    对外的 PageAnimator 统一换算成 px/ms。

    状态在「位移域」，所以 x 直接就是「离目标多远」，收敛判据
    |x|<eps_x 且 |x'|<eps_v。
    """

    __slots__ = ("mass", "stiffness", "damping", "x", "v",
                 "eps_x", "eps_v", "_steps")

    def __init__(self, mass: float = 1.0, stiffness: float = 220.0,
                 damping: float = 26.0):
        if mass <= 0.0:
            raise ValueError(f"mass 必须 > 0，收到 {mass!r}")
        if stiffness <= 0.0:
            raise ValueError(f"stiffness 必须 > 0，收到 {stiffness!r}")
        if damping < 0.0:
            raise ValueError(f"damping 必须 ≥ 0，收到 {damping!r}")
        self.mass = float(mass)
        self.stiffness = float(stiffness)
        self.damping = float(damping)
        self.x = 0.0
        self.v = 0.0
        # 位移 / 速度的收敛阈值，单位分别是 px 与 px/s。
        # 苹果明确说过不要等 settling duration（WWDC23：settling duration
        # 不可预测，"you shouldn't wait for the settling duration for
        # user-facing changes"）。阈值取「剩余位移 < 行程的 0.1%」，
        # 900px 的行程就是 0.9px —— 收尾时直接吸附过去，肉眼看不出来，
        # 但能省掉近 0.4s 的尾巴。
        self.eps_x = 0.02
        self.eps_v = 0.5
        self._steps = 0

    # ── 构造 ─────────────────────────────────────────────
    @classmethod
    def from_duration_bounce(cls, duration: float, bounce: float,
                             mass: float = 1.0) -> "Spring":
        """Apple 的两参数形式（duration 秒 / bounce -1..1）。

            stiffness = (2π / duration)²
            damping   = 4π(1-bounce)/duration      (bounce ≥ 0)
            damping   = 4π/(duration×(1+bounce))    (bounce < 0)

        对应阻尼比 ζ = 1-bounce / 1/(1+bounce)。ζ<MIN_DAMPING_RATIO 时
        钳到下限 —— 否则 bounce 接近 1 会得到近乎无阻尼的系统，
        弹簧永远不 settle，动画停不下来。
        """
        if duration <= 0.0:
            raise ValueError(f"duration 必须 > 0，收到 {duration!r}")
        stiffness = (2.0 * math.pi / duration) ** 2
        # bounce 必须落在 (-1, 1]：bounce=1 给出 ζ=0（被下方钳到下限），
        # bounce=-1 会让 1/(1+bounce) 除零，越界还会得到负 ζ（负阻尼 = 发散）。
        b = clamp(float(bounce), MIN_BOUNCE + 1e-9, 1.0)
        zeta = (1.0 - b) if b >= 0.0 else (1.0 / (1.0 + b))
        zeta = clamp(zeta, MIN_DAMPING_RATIO, MAX_DAMPING_RATIO)
        damping = 2.0 * zeta * math.sqrt(stiffness * mass)
        return cls(mass, stiffness, damping)

    @classmethod
    def from_response(cls, response: float, damping_ratio: float,
                      mass: float = 1.0) -> "Spring":
        """WWDC18 形式：response（周期，秒）+ dampingRatio ζ。"""
        if response <= 0.0:
            raise ValueError(f"response 必须 > 0，收到 {response!r}")
        stiffness = (2.0 * math.pi / response) ** 2
        zeta = max(MIN_DAMPING_RATIO, damping_ratio)
        return cls(mass, stiffness, 2.0 * zeta * math.sqrt(stiffness * mass))

    # ── 状态 ─────────────────────────────────────────────
    @property
    def damping_ratio(self) -> float:
        """ζ = c / (2√(km))。<1 欠阻尼（过冲），=1 临界，>1 过阻尼。"""
        return self.damping / (2.0 * math.sqrt(self.stiffness * self.mass))

    @property
    def settled(self) -> bool:
        return abs(self.x) < self.eps_x and abs(self.v) < self.eps_v

    def reset(self, x: float = 0.0, v: float = 0.0) -> None:
        self.x = float(x)
        self.v = float(v)
        self._steps = 0

    # ── 积分 ─────────────────────────────────────────────
    def _accel(self, x: float, v: float) -> float:
        return (-self.stiffness * x - self.damping * v) / self.mass

    def _rk4(self, h: float) -> None:
        x, v = self.x, self.v
        a1 = self._accel(x, v)
        x2 = x + 0.5 * h * v
        v2 = v + 0.5 * h * a1
        a2 = self._accel(x2, v2)
        x3 = x + 0.5 * h * v2
        v3 = v + 0.5 * h * a2
        a3 = self._accel(x3, v3)
        x4 = x + h * v3
        v4 = v + h * a3
        a4 = self._accel(x4, v4)
        self.x = x + (h / 6.0) * (v + 2.0 * v2 + 2.0 * v3 + v4)
        self.v = v + (h / 6.0) * (a1 + 2.0 * a2 + 2.0 * a3 + a4)

    @property
    def _stable_step(self) -> float:
        """满足数值稳定条件的最大步长（秒）。

        稳定性由**两个**特征时间决定，缺一不可：

        * 振荡：ω₀ = √(k/m)，要 ω₀·h ≲ 2.8（RK4 的稳定域）
        * 阻尼：λ = c/m，要 |λ|·h ≲ 2.8

        只管 ω₀ 会在强过阻尼时翻车：m=1e-6、c=20 时 λ=2e7，
        而 ω₀=1.4e4 看着人畜无害，h=1/240 给出 |λ|h≈83000，
        一步就溢出成 NaN。所以取两者中较快的那个来定步长。

        步长**只取决于弹簧自身参数**，与传入的 dt 无关 —— 这样 30fps 和
        144fps 把同一段时间切成同一种子步，轨迹才能逐点重合。
        """
        omega = math.sqrt(self.stiffness / self.mass)
        fastest = max(omega, self.damping / self.mass)
        return min(SPRING_SUBSTEP, _MAX_OMEGA_H / fastest)

    @property
    def max_step_rate(self) -> float:
        """每秒最多需要的子步数（= 1/_stable_step）。

        用来判断参数是否超出积分器的预算：dt·max_step_rate 超过
        _MAX_SUBSTEPS 时 step() 会退让精度，此时若参数再极端就可能发散。
        Apple 的 bounce∈[-1,1] 只对应 ζ∈[0.02,10]、mass=1，
        离这个预算还差好几个数量级，UI 弹簧不会碰到。
        """
        return 1.0 / self._stable_step

    def step(self, dt: float) -> None:
        """推进 dt 秒（自动拆成稳定的定长步）。"""
        if dt <= 0.0:
            return
        h = self._stable_step
        # 子步数被 _MAX_SUBSTEPS 兜住：极端参数（极轻的物体配极高的
        # 刚度）下 h 会小到离谱，此时宁可牺牲一点精度也不能卡死一帧。
        if h * _MAX_SUBSTEPS < dt:
            h = dt / _MAX_SUBSTEPS
        remaining = float(dt)
        done = 0
        while remaining > 1e-12 and done < _MAX_SUBSTEPS:
            step = h if remaining > h else remaining
            self._rk4(step)
            remaining -= step
            done += 1
        self._steps += done

    @property
    def steps(self) -> int:
        """累计积分步数（调试 / 性能用）。"""
        return self._steps


# ═════════════════════════════════════════════════════════════════
# 4. PageAnimator —— 对外的动画组件
# ═════════════════════════════════════════════════════════════════

class PageAnimator(QAbstractAnimation):
    """翻页动画驱动器。喂它 float 位移，它把 float 回调出来。

    与 QVariantAnimation 的关键差别：

    * ``duration()`` 返回 **-1**（无固定时长），时基走 QElapsedTimer。
      QAbstractAnimation 的 currentTime 是 int 毫秒、且官方不保证调用
      间隔，既当不了高精度时钟也假定不了 60fps。
    * 内部全程 float，``value()`` 返回 float。
    * 支持 ``begin/update/end`` 手势协议；``end()`` 用**真实速度**决定
      去向，并把该速度作为弹簧初速度交棒，做到位置与速度双连续。

    ``advance(dt_ms)`` 是公开的核心推进函数：``updateCurrentTime`` 会用
    真实时钟调它，测试则可以显式传入 dt —— 帧率无关性就是这么测的。
    """

    frameAdvanced = pyqtSignal(float)   # 每帧一个 float 位移
    settled = pyqtSignal()              # 动画自然到位（区别于被 stop）
    #: on_frame 回调抛出的异常。见 updateCurrentTime 的 docstring：
    #: 异常无法跨过 Qt 的 C++ 虚函数帧，只能改道这里。
    errorOccurred = pyqtSignal(object)

    # ── 松手判定阈值 ──────────────────────────────────────
    #: 速度超过这个值（px/ms）就认定是「甩」，无条件翻页。
    #: 250px/s 约等于一次普通触控板快扫。
    VELOCITY_COMMIT = 0.25
    #: 速度不够时改看惯性预测落点：预计还能滑过这么多页宽比例就翻页。
    #: 取 0.30 而非 0.50：Launchpad 这类「一甩就过」的手感偏灵敏，
    #: 过半才翻会显得迟钝。
    POSITION_COMMIT = 0.30
    #: 速度估计的时间窗（ms）。太短会被单帧抖动带偏，太长会把
    #: 「松手前先停一下」误判成还在快速移动。
    VELOCITY_WINDOW_MS = 90.0
    #: 单帧 dt 上限。窗口被拖住 / 切后台回来后 dt 可能上百毫秒，
    #: 一次灌进积分器会让内容瞬移。
    MAX_FRAME_DT_MS = 100.0
    #: 越界橡皮筋的参考尺寸上限（px）。用页宽，一页就是「一屏」。
    BAND_DIM_MAX = 640.0

    # ── 景深（经验值，非 Apple 公布，见模块 docstring【6】）──
    #: 完全离开页面时缩到多少
    DEPTH_MIN_SCALE = 0.92
    #: 完全离开页面时的不透明度
    DEPTH_MIN_OPACITY = 0.45

    def __init__(self, on_frame=None, parent=None):
        super().__init__(parent)
        #: 逐帧 Python 回调，形如 on_frame(value: float)。
        #: 由 `_emit_frame` 直接调用；异常会向上抛，不静默吞掉。
        self.on_frame = on_frame

        self._value = 0.0
        self._velocity = 0.0            # px/ms
        self._target = 0.0
        self._mode = "idle"             # idle | dragging | easing | spring

        self._easing_name = "spring"
        self._ef = None                 # 当前缓动的值函数
        self._edf = None                # 当前缓动的导数函数
        self._duration_ms = 320.0
        self._eff_duration_ms = 320.0   # 经速度匹配缩放后的实际时长
        self._elapsed_ms = 0.0
        self._start_value = 0.0
        self._end_value = 0.0
        self._v0 = 0.0                  # 起始速度 px/ms

        self._spring: Spring | None = None
        self._spring_duration = 0.40    # 秒
        self._spring_bounce = 0.12

        self._page_size = 0.0
        self._bounds = (-0.0, 0.0)
        # update() 记下未经橡皮筋压缩的原始值，band_ratio() 才算得准
        self._raw_value: float | None = None

        # 手势采样
        self._drag_anchor = 0.0
        self._gesture_t = 0.0
        self._samples: list[tuple[float, float]] = []

        self._clock = QElapsedTimer()
        self._gesture_clock = QElapsedTimer()

        # 自驱动定时器。见 TICK_MS_MIN 上方的「为什么不再用
        # QAbstractAnimation 驱动」—— Qt 的动画驱动在 144Hz 屏上
        # 只给 31fps，那才是卡顿的真凶。
        self._tick: QTimer | None = None
        self.tick_ms = self.tick_ms_for(_screen_refresh_hz())

        self._apply_easing("spring")

    # ── QAbstractAnimation 接口 ───────────────────────────
    def duration(self) -> int:
        """恒为 -1：弹簧的感知时长与实际 settle 时长不是一回事
        （WWDC23 专门讲过 settling duration 不可预测），不能交给
        Qt 的整数毫秒时钟去掐。"""
        return -1

    # ── 自驱动 ───────────────────────────────────────────
    #
    # ## 为什么不再用 QAbstractAnimation 的 start() 驱动
    #
    # Qt 的动画驱动（QUnifiedTimer）在 Windows 上与 vsync 对齐，实际
    # 帧率**远低于**显示器刷新率。实测（本机 144Hz 屏）：
    #
    #     驱动方式                        帧率      帧间隔 p50
    #       QAbstractAnimation（原来）    ~31Hz     35.08ms
    #       QTimer(0)                  142717Hz      0.01ms
    #       QTimer(2ms)                   499Hz      2.02ms
    #       QTimer(7ms = 1/144s)          144Hz      6.98ms
    #
    # **144Hz 的屏上，动画只跑 31fps** —— 每一帧之间空 35ms。
    # 这才是用户说的「仍有一定卡顿」的真凶，而且它跟「单帧算得慢」
    # 毫无关系：单帧 4.43ms 远小于 6.94ms 的一帧预算，完全跑得动。
    #
    # 换自驱动后帧率精确等于设定值：QTimer(7ms) 实测 144Hz、
    # 帧间隔 p50=6.98ms。
    #
    # ## 为什么要按屏幕刷新率取间隔，而不是固定 7ms
    #
    # 60Hz 屏上用 7ms 只会白跑一半的帧（CPU 空转、徒增功耗），
    # 而 240Hz 屏上又不够。所以启动时读一次 refreshRate()，
    # 取 `max(1, round(1000 / hz))`，并夹在一个合理区间内
    # （见 `TICK_MS_MIN` / `TICK_MS_MAX`）。
    TICK_MS_MIN = 1
    TICK_MS_MAX = 17          # 低于 ~59Hz 的屏不需要更快

    @staticmethod
    def tick_ms_for(hz: float) -> int:
        """按屏幕刷新率算自驱动间隔（ms）。

        144Hz -> 7ms（实测 144Hz 帧率、帧间隔 p50=6.98ms）
         60Hz -> 17ms
        未知刷新率（返回 <=0）-> 8ms，一个折中值。
        """
        if hz is None or hz <= 0:
            return 8
        ms = int(round(1000.0 / float(hz)))
        return max(PageAnimator.TICK_MS_MIN,
                   min(PageAnimator.TICK_MS_MAX, ms))

    def _start_timer(self) -> None:
        """挂上自驱动定时器（若尚未挂）。"""
        if self._tick is not None:
            return
        self._tick = QTimer(self)
        self._tick.setTimerType(Qt.PreciseTimer)
        self._tick.setInterval(self.tick_ms)
        self._tick.timeout.connect(self._tick_advance)
        self._clock.start()

    def _stop_timer(self) -> None:
        if self._tick is not None:
            self._tick.stop()

    def _tick_advance(self) -> None:
        """定时器的一拍。异常绝不能逃到 Qt 的 C++ 帧外面去。

        理由与 `updateCurrentTime` 完全相同（QTimer.timeout 同样从 C++
        回调 Python），处理方式也相同：停住 + 走 errorOccurred 信号 +
        sys.excepthook。写成裸的 `self.advance()` 的话，
        on_frame 抛异常会直接 0xC0000409 杀掉整个 GUI 进程。
        """
        try:
            self.advance()
        except BaseException as exc:      # noqa: BLE001 - 见 docstring
            self._mode = "idle"
            self._velocity = 0.0
            self._stop_timer()
            try:
                super().stop()
            except RuntimeError:
                pass
            self.errorOccurred.emit(exc)
            sys.excepthook(type(exc), exc, exc.__traceback__)

    def updateCurrentTime(self, currentTime: int) -> None:
        """Qt 的驱动入口。

        自驱动启用后**不再使用** —— `_launch` 不调 `super().start()`，
        而是自己挂 QTimer。这里保留是为了让外部若真去调 `QAbstractAnimation`
        的接口（比如 `state()`、`stateChanged` 信号）时仍能正常工作，
        并且只在没有自驱动定时器时才真正推进。

        **绝不能在这里让异常逃出去。** QAbstractAnimation.start() 会从 C++
        同步回调本函数，而回调里会调用用户的 on_frame。Python 异常一旦跨过
        这个 C++ 帧就没有正常的传播路径，解释器直接以 0xC0000409
        （STATUS_STACK_BUFFER_OVERRUN）终止 —— 整个 GUI 进程被带走，
        而且 faulthandler 抓不到栈，看起来完全像是随机崩溃。

        所以：先停住（避免状态卡在半路），把异常交给 errorOccurred 信号，
        并通过 sys.excepthook 走标准的 traceback 打印路径。调试信息一条
        不少，但不会再有进程猝死。advance() 本身仍然照常向上抛异常，
        测试可以直接断言它抛了。
        """
        try:
            self.advance()
        except BaseException as exc:      # noqa: BLE001 - 见 docstring
            self._mode = "idle"
            self._velocity = 0.0
            try:
                super().stop()
            except RuntimeError:
                pass
            self.errorOccurred.emit(exc)
            sys.excepthook(type(exc), exc, exc.__traceback__)

    # ── 配置 ─────────────────────────────────────────────
    def set_easing(self, name: str) -> None:
        self._apply_easing(name)

    def _apply_easing(self, name: str) -> None:
        self._easing_name = name if name in EASINGS else "outQuint"
        self._ef, self._edf = resolve_easing(self._easing_name)

    def easing(self) -> str:
        return self._easing_name

    def set_duration(self, ms: float) -> None:
        self._duration_ms = max(1.0, float(ms))

    def duration_ms(self) -> float:
        return self._duration_ms

    def set_spring(self, duration: float = 0.40, bounce: float = 0.12) -> None:
        """spring 专用：Apple 两参数（秒 / -1..1）。bounce=0 即临界阻尼，
        无过冲，可用于 Reduce Motion。"""
        self._spring_duration = max(1e-3, float(duration))
        # 上界 1.0 允许（会被 MIN_DAMPING_RATIO 接管），下界取不到 -1.0
        self._spring_bounce = clamp(float(bounce), MIN_BOUNCE + 1e-9, 1.0)

    def set_page_size(self, px: float) -> None:
        """一页 = 一个吸附点间隔。必须 > 0，否则松手判定没有参照。"""
        self._page_size = max(0.0, float(px))

    def page_size(self) -> float:
        return self._page_size

    def set_bounds(self, minimum: float, maximum: float) -> None:
        """可达区间。越界部分走橡皮筋。"""
        lo, hi = float(minimum), float(maximum)
        if lo > hi:
            lo, hi = hi, lo
        self._bounds = (lo, hi)

    def bounds(self) -> tuple[float, float]:
        return self._bounds

    # ── 读状态 ───────────────────────────────────────────
    def value(self) -> float:
        """当前位置（float，**不要**在这里取整）。"""
        return self._value

    def velocity(self) -> float:
        """当前速度，px/ms。"""
        return self._velocity

    def target(self) -> float:
        return self._target

    def is_running(self) -> bool:
        return self._mode != "idle"

    def is_dragging(self) -> bool:
        return self._mode == "dragging"

    def progress(self) -> float:
        """本次动画的完成度 0..1。非 easing 模式下退化为按时间估算。"""
        if self._mode == "spring" and self._spring is not None:
            denom = self._start_value - self._end_value
            if abs(denom) > 1e-9:
                return clamp(1.0 - self._spring.x / denom, 0.0, 1.0)
            return 1.0
        if self._eff_duration_ms > 0:
            return clamp(self._elapsed_ms / self._eff_duration_ms, 0.0, 1.0)
        return 1.0

    # ── 景深（供 grid 做缩放/淡出）────────────────────────
    def depth_progress(self) -> float:
        """离最近吸附点的相对距离，归一到 [-1, 1]（整页 = 1）。

        一次翻页里它从 0 平滑走到 ±1，用它驱动缩放即可得到
        「远去的页缩小变暗、到来的页放大归位」的景深。
        """
        if self._page_size <= 0.0:
            return 0.0
        frac = (self._value - self._snap_at(self._value)) / self._page_size
        return clamp(frac, -1.0, 1.0)

    def depth_scale(self) -> float:
        d = abs(self.depth_progress())
        return 1.0 - (1.0 - self.DEPTH_MIN_SCALE) * d

    def depth_opacity(self) -> float:
        d = abs(self.depth_progress())
        return 1.0 - (1.0 - self.DEPTH_MIN_OPACITY) * d

    def band_ratio(self) -> float:
        """越界阻尼吃掉了多少（0 = 在界内）。可用于界内时压暗做提示。"""
        lo, hi = self._bounds
        raw = self._raw_value
        if raw is None:
            return 0.0
        if raw < lo:
            return min(1.0, (lo - raw) / max(1e-6, self._band_dim()))
        if raw > hi:
            return min(1.0, (raw - hi) / max(1e-6, self._band_dim()))
        return 0.0

    # ── 吸附点 ───────────────────────────────────────────
    def _band_dim(self) -> float:
        if self._page_size > 0.0:
            return min(self._page_size, self.BAND_DIM_MAX)
        return self.BAND_DIM_MAX

    def _clamp_bound(self, x: float) -> float:
        lo, hi = self._bounds
        return clamp(x, lo, hi)

    def _snap_at(self, x: float) -> float:
        if self._page_size <= 0.0:
            return self._clamp_bound(x)
        return self._clamp_bound(round(x / self._page_size) * self._page_size)

    # ── 越界橡皮筋 ───────────────────────────────────────
    def _apply_band(self, x: float) -> float:
        lo, hi = self._bounds
        if x < lo:
            return lo + rubber_band(x - lo, self._band_dim())
        if x > hi:
            return hi - rubber_band(hi - x, self._band_dim())
        return x

    # ── 帧推进（核心）────────────────────────────────────
    def advance(self, dt_ms: float | None = None) -> None:
        """推进一帧。

        dt_ms 为 None 时用 QElapsedTimer 取真实间隔（生产路径）；
        传入显式值则完全确定（测试路径，也是帧率无关性测试的基础）。
        """
        if self.state() != QAbstractAnimation.Running:
            return
        if dt_ms is None:
            if not self._clock.isValid():
                return
            dt_ms = float(self._clock.restart())
        dt_ms = clamp(float(dt_ms), 0.0, self.MAX_FRAME_DT_MS)

        if self._mode == "spring":
            if self._spring is None:
                return
            self._spring.step(dt_ms / 1000.0)
            self._value = self._end_value + self._spring.x
            # 积分器内部用秒，弹簧的 v 是 px/s；对外一律用 px/ms
            self._velocity = self._spring.v / 1000.0
            if self._spring.settled:
                self._settle()
            else:
                self._emit_frame()

        elif self._mode == "easing":
            self._elapsed_ms += dt_ms
            p = clamp(self._elapsed_ms / self._eff_duration_ms, 0.0, 1.0)
            span = self._end_value - self._start_value
            self._value = self._start_value + span * self._ef(p)
            if self._eff_duration_ms > 0:
                self._velocity = span * self._edf(p) / self._eff_duration_ms
            else:
                self._velocity = 0.0
            if p >= 1.0:
                self._settle()
            else:
                self._emit_frame()

        elif self._mode == "dragging":
            # 手指按住不动：位置不变，速度由 update() 采样时决定
            return

    def _settle(self) -> None:
        """到位收尾：强制精确落到目标（消除积分残差与缓动末帧误差）。"""
        self._value = self._end_value
        self._velocity = 0.0
        self._mode = "idle"
        self._raw_value = None
        self._emit_frame()          # 最后一帧仍要回调，保证位置精确
        self.stop()                 # → stateChanged(Stopped)
        self.finished.emit()        # 与 QAbstractAnimation 语义一致
        self.settled.emit()

    def stop(self) -> None:
        """停止。保留当前位置，但不再有速度。"""
        if self._mode != "idle":
            self._mode = "idle"
            self._velocity = 0.0
        # 自驱动定时器必须一起停，否则它在 mode=idle 时会一直空转
        # （advance() 会因 state()!=Running 直接返回，但仍在耗 CPU）。
        self._stop_timer()
        super().stop()

    def _emit_frame(self) -> None:
        cb = self.on_frame
        if cb is not None:
            cb(self._value)            # 异常上抛，不静默吞
        self.frameAdvanced.emit(self._value)

    # ── 非手势翻页（键盘 / 滚轮）─────────────────────────
    def animate_to(self, target: float, velocity: float | None = None,
                   animate: bool = True) -> None:
        """翻到 target。

        velocity 单位 px/ms；**None 表示沿用当前速度**。这是 WWDC23 说的
        velocity preservation：动画播到一半改目标时，弹簧要把改目标
        那一刻的速度带进新目标，而不是从静止重新起步。传 0.0 才会强制
        归零（那正是「按下即播」式硬切的来源）。

        animate=False 直接落位（不做动画），用于重建后复位。
        """
        target = float(target)
        if velocity is None:
            velocity = self._velocity
        if not animate or self._duration_ms <= 0.0:
            self.stop()
            self._value = target
            self._target = target
            self._end_value = target
            self._mode = "idle"
            self._raw_value = None
            self._emit_frame()
            return
        self._launch(target, float(velocity))

    # ── 手势协议 ─────────────────────────────────────────
    def begin(self) -> None:
        """手指按下。若正在动画则保留当前位置与速度（WWDC23 的
        velocity preservation），随后进入拖拽。"""
        self.stop()
        self._mode = "dragging"
        self._drag_anchor = self._value
        self._gesture_t = 0.0
        self._samples = [(0.0, self._value)]
        self._raw_value = self._value
        self._gesture_clock.start()

    def update(self, drag_delta: float, dt_ms: float | None = None) -> float:
        """手指移动到「相对按下点的累计位移」drag_delta。返回当前位置。

        ⚠ drag_delta 是**累计量**，不是每帧增量。
        Qt 的 mouseMoveEvent 给的是相对上次的位置增量，接进来要先累加：

            self._drag += (ev.pos() - self._last_pos)
            self._last_pos = ev.pos()
            self.animator.update(self._drag)

        传增量的话位置永远不累加，翻页会完全失灵 —— 这条在
        tests_anim.py 的第 17 组里有专门的断言钉住。

        dt_ms 为 None 时用真实时钟；传入显式值则可无事件循环地测试。
        """
        if self._mode != "dragging":
            return self._value
        if dt_ms is None:
            dt = float(self._gesture_clock.restart()) \
                if self._gesture_clock.isValid() else 0.0
        else:
            dt = max(0.0, float(dt_ms))
        self._gesture_t += dt
        raw = self._drag_anchor + float(drag_delta)
        self._raw_value = raw
        self._value = self._apply_band(raw)
        self._samples.append((self._gesture_t, self._value))
        # 只保留最近两倍时间窗，防止长按拖拽把列表撑大
        cutoff = self._gesture_t - self.VELOCITY_WINDOW_MS * 2.0
        while len(self._samples) > 2 and self._samples[1][0] < cutoff:
            self._samples.pop(0)
        self._emit_frame()
        return self._value

    def end(self, velocity: float | None = None) -> float:
        """手指抬起。

        velocity 为 None 时按最近 VELOCITY_WINDOW_MS 的采样估算（px/s）；
        传入显式值（px/s）则直接用 —— 便于测试。
        返回最终吸附点。
        """
        if self._mode != "dragging":
            return self._target
        v_ms = (self._estimate_velocity() if velocity is None
                else float(velocity) / 1000.0)
        target = self.decide_target(self._value, v_ms)
        self._samples = []
        self._raw_value = None
        self._launch(target, v_ms)
        return target

    def cancel(self) -> None:
        """手势中途被抢走 / 窗口关闭：就地停住，不回弹。"""
        self._samples = []
        self._raw_value = None
        self.stop()

    # ── 速度估算与松手判定 ───────────────────────────────
    def _estimate_velocity(self) -> float:
        """最近时间窗内的平均速度，px/ms。手指停住后再松手 → ≈0。

        取窗口内**最早**的那个样本做基准。不能用「从旧到新遍历、每次都
        覆盖」的写法：那会一路覆盖到最新的样本，dt 恒为 0 → 速度恒为 0。
        那正是「一次快速甩动」的形态（整段手势比 90ms 窗口还短），
        等于把快甩判定整个废掉，甩多少都不翻页。
        """
        if len(self._samples) < 2:
            return 0.0
        t_end, x_end = self._samples[-1]
        cutoff = t_end - self.VELOCITY_WINDOW_MS
        t_start, x_start = self._samples[0]
        for t, x in self._samples:
            if t >= cutoff:
                t_start, x_start = t, x
                break
        dt = t_end - t_start
        if dt <= 1e-6:
            return 0.0
        return (x_end - x_start) / dt

    def decide_target(self, x: float, v: float) -> float:
        """松手去哪。纯函数，可单独测试。

        规则（对应 WWDC23 的 velocity preservation + UIScrollView 的
        deceleration 预测）：

        1. |v| ≥ VELOCITY_COMMIT → 认定是「甩」，沿速度方向至少进一页。
        2. 否则先看**已经拖出去多远**：离吸附点 ≥ POSITION_COMMIT 倍页宽
           说明用户是明确要翻页的（哪怕已经停手）→ 顺着拖动方向进一页。
        3. 再看**惯性预测落点**：按指数衰减还能滑 ≥ POSITION_COMMIT 倍
           页宽 → 进一页。
        4. 都不够 → 回到最近的吸附点（回弹）。

        第 2 条不能省：手指停住时 v≈0，投影位移也是 0，若只看投影，
        「慢慢拖过半页再松手」会被判成想回弹 —— 那是纯粹的迟钝感。
        """
        ps = self._page_size
        if ps <= 0.0:
            return self._clamp_bound(x)

        snap = self._snap_at(x)
        nxt = snap
        threshold = self.POSITION_COMMIT * ps

        if abs(v) >= self.VELOCITY_COMMIT:
            if v > 0.0 and nxt <= x:
                nxt += ps
            elif v < 0.0 and nxt >= x:
                nxt -= ps
            return self._clamp_bound(nxt)

        # 已经拖出去够远：这是明确意图，与当前速度无关
        if abs(x - snap) >= threshold:
            if x > snap:
                nxt = snap + ps
            elif x < snap:
                nxt = snap - ps
            return self._clamp_bound(nxt)

        # 惯性预测落点够远
        proj = x + project_deceleration(v * 1000.0)
        if abs(proj - x) >= threshold:
            if proj > x and nxt <= x:
                nxt += ps
            elif proj < x and nxt >= x:
                nxt -= ps
        return self._clamp_bound(nxt)

    # ── 起播 ─────────────────────────────────────────────
    def _launch(self, target: float, v0: float) -> None:
        """从当前位置、给定起始速度出发，飞向 target。"""
        self._start_value = self._value
        self._end_value = self._target = target
        self._v0 = v0
        self._raw_value = None

        if is_spring(self._easing_name):
            self._mode = "spring"
            sp = Spring.from_duration_bounce(self._spring_duration,
                                             self._spring_bounce)
            # 位移域：x = 起点 - 目标。v0 是 px/ms，积分器要 px/s
            sp.reset(self._start_value - target, v0 * 1000.0)
            # 阈值按行程缩放：剩余位移收到行程的 0.1%（900px → 0.9px），
            # 速度阈值取到「速度条件不成为更晚的瓶颈」为止。
            # 两者都远小于一个像素的感知阈值，收尾吸附无感。
            span = abs(target - self._start_value)
            sp.eps_x = max(0.05, min(4.0, span * 1e-3))
            sp.eps_v = max(0.5, min(200.0, span * 0.02))
            self._spring = sp
        else:
            self._mode = "easing"
            self._elapsed_ms = 0.0
            self._eff_duration_ms = self._match_duration(v0)

        if self.state() != QAbstractAnimation.Running:
            self._clock.start()
            # **不用 super().start()** —— Qt 的动画驱动在 144Hz 屏上
            # 只给 31fps（实测帧间隔 p50=35ms），见 TICK_MS_MIN 上方。
            # 改为自驱动 QTimer，帧率精确等于屏幕刷新率。
            self._start_timer()
            super().start()
        if self._tick is not None:
            self._tick.start()
        # 已在运行时**不要**重启时钟：那会把「距上一帧」的时间抹掉，
        # 凭空吞掉一帧的 dt，重定向就会比预期慢一点。

    def _match_duration(self, v0: float) -> float:
        """用 e'(0) 反解时长，让曲线的起始斜率≈真实手势速度。

        曲线初速 = span·e'(0)/D，令其等于 v₀ ⟹ D' = span·e'(0)/v₀。
        贝塞尔无法表示初速度（WWDC23），这是能做到的最好近似；
        真要精确请用 spring。D' 夹在 [0.4D, 2.5D] 免得算出荒唐时长。
        """
        d0 = self._duration_ms
        span = self._end_value - self._start_value
        if v0 == 0.0 or span == 0.0:
            return d0
        slope = self._edf(0.0)
        if slope <= 0.0:
            return d0                 # inOutCubic 起手静止，无从匹配
        want = span * slope / v0
        return clamp(want, 0.4 * d0, 2.5 * d0)

    # ── 便利：直接落到某页 ───────────────────────────────
    def goto_page(self, page: int, animate: bool = True) -> None:
        if self._page_size <= 0.0:
            return
        target = self._snap_at(page * self._page_size)
        # 已经在这一页且不在动画中，就不要白跑一趟空动画
        if abs(target - self._value) < 1e-6 and self._mode == "idle":
            return
        # velocity=0.0 —— 程序性翻页必须**清零**初速度，不能继承。
        #
        # 速度继承（WWDC23 velocity preservation）是为「拖拽松手后的回弹」
        # 设计的：手指甩出去多快，页面就该多快接住。但点页码点、按方向键
        # 是**离散的定位请求**，不是甩动的延续。实测继承后：连点 5 次页码点，
        # 速度匹配把时长从 320ms 拉长到 eff_dur=640ms，页面整整两倍时间才
        # 落位 —— 主观感受就是「点了没反应 / 响应慢」。
        # 拖拽松手路径 end() 不受影响，仍然带速度。
        self.animate_to(target, velocity=0.0, animate=animate)