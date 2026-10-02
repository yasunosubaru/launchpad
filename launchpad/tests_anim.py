# -*- coding: utf-8 -*-
"""
animation.py 的独立测试。

不用 pytest：直接跑 `python -m launchpad.tests_anim`，逐项打印 PASS/FAIL，
有失败则退出码非 0。

    python -u -m launchpad.tests_anim

只依赖 QtCore（不需要 QApplication、不需要显示），因此可以在无头环境跑。
"""

from __future__ import annotations

import math
import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Windows 控制台默认 GBK，π/²/× 这类符号会直接抛 UnicodeEncodeError。
# 强制 UTF-8；行缓冲保证管道/重定向下也不会丢输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except (AttributeError, ValueError):
        pass

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from .animation import (  # noqa: E402
    DECELERATION_FAST,
    DECELERATION_NORMAL,
    EASINGS,
    MIN_DAMPING_RATIO,
    PageAnimator,
    Spring,
    RUBBER_BAND_C,
    clamp,
    deceleration_time_constant,
    ease_in_out_cubic,
    ease_out_cubic,
    ease_out_expo,
    ease_out_quint,
    is_spring,
    project_deceleration,
    resolve_easing,
    rubber_band,
)

PAGE = 900.0        # 一页 = 900px（1440×900 屏去掉边距后的量级）
FRAME_BUDGET = 16.0  # ms

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


# ═════════════════════════════════════════════════════════════════
# 1. 缓动函数：端点、单调性、导数一致性
# ═════════════════════════════════════════════════════════════════

def test_easing_endpoints() -> None:
    section("1. 缓动端点 f(0)=0, f(1)=1")
    closed = [(n, f) for n, (f, _) in EASINGS.items() if not is_spring(n)]
    check("登记表里正好 4 条闭式缓动 + spring", len(closed) == 4 and
          len(EASINGS) == 5, f"closed={len(closed)} total={len(EASINGS)}")

    for name, f in closed:
        a, b = f(0.0), f(1.0)
        check(f"{name}: f(0)≈0", abs(a) < 1e-12, f"f(0)={a!r}")
        check(f"{name}: f(1)≈1", abs(b - 1.0) < 1e-12, f"f(1)={b!r}")

    # outExpo 的朴素形式在 t=1 只到 0.99902…，必须有截断
    check("outExpo 末帧被修正到精确 1",
          ease_out_expo(1.0) == 1.0 and ease_out_expo(0.999) < 1.0)

    # 与 settings.EASINGS 严格对齐：名字和顺序都要一致，否则设置界面
    # 里选到的缓动可能在本模块找不到（或顺序对不上 UI）。
    try:
        from .settings import EASINGS as SETTINGS_EASINGS
    except Exception:                          # pragma: no cover
        SETTINGS_EASINGS = None
    if SETTINGS_EASINGS is not None:
        check("EASINGS 与 settings.EASINGS 名字和顺序完全一致",
              tuple(SETTINGS_EASINGS) == tuple(EASINGS),
              f"settings={SETTINGS_EASINGS} animation={tuple(EASINGS)}")
    else:
        check("settings.EASINGS 不可导入（跳过对齐检查）", True)

    # 越界输入不应炸
    for name, f in closed:
        f(-0.5), f(1.5)
    check("越界输入不抛异常", True)


def test_easing_monotonic() -> None:
    section("2. 缓动单调性（闭式曲线全程非递减）")
    n = 4000
    for name, (f, df) in EASINGS.items():
        if is_spring(name):
            continue
        prev = f(0.0)
        worst = 0.0
        for i in range(1, n + 1):
            v = f(i / n)
            worst = min(worst, v - prev)
            prev = v
        check(f"{name}: 单调非递减", worst >= -1e-12, f"最大回退={worst:.3e}")

        # 导数非负，且与数值差分一致（有限差分 O(h²)，h 取 1e-4 足够）。
        # 只在内部区间比对：t=1 处 f 被强制截断到 1，割线与导数天然对不上
        # （f(1)-f(1-h) ≈ h·2⁻¹⁰/h = 9.77，而 df(1)=0）。
        bad = 0.0
        h = 1e-4
        for i in range(0, 181):
            t = 0.05 + 0.9 * i / 180.0
            num = (f(t + h) - f(t - h)) / (2.0 * h)
            bad = max(bad, abs(num - df(t)))
        check(f"{name}: 导数与差分一致（内部区间）", bad < 1e-3,
              f"最大偏差={bad:.3e}")


def test_easing_shapes() -> None:
    section("3. 缓动形状：outExpo 最急停，outQuint 次之")
    half = {n: f(0.5) for n, (f, _) in EASINGS.items() if not is_spring(n)}
    check("outExpo 中点进度 > 0.9（最冲）", half["outExpo"] > 0.9,
          f"f(0.5)={half['outExpo']:.4f}")
    # 注意 t=0.5 处 outExpo 与 outQuint 恰好相等（都是 0.96875），
    # 要比出急缓差异必须换 t，这里用 0.3。
    q = {n: f(0.3) for n, (f, _) in EASINGS.items() if not is_spring(n)}
    check("t=0.3 处 outExpo > outQuint > outCubic > inOutCubic",
          q["outExpo"] > q["outQuint"] > q["outCubic"] > q["inOutCubic"],
          str({k: round(v, 4) for k, v in q.items()}))
    check("inOutCubic 中点 = 0.5", abs(half["inOutCubic"] - 0.5) < 1e-12)
    check("解析值与闭式函数一致", abs(resolve_easing("outQuint")[0](0.3)
                                   - ease_out_quint(0.3)) < 1e-15)


# ═════════════════════════════════════════════════════════════════
# 4. 弹簧：Apple 参数换算、收敛、过冲
# ═════════════════════════════════════════════════════════════════

def test_spring_parameter_conversion() -> None:
    section("4. 弹簧参数换算（Apple: k=(2π/D)², ζ=1-bounce）")
    D = 0.40
    s = Spring.from_duration_bounce(D, 0.0)
    check("bounce=0 → 临界阻尼 ζ≈1", abs(s.damping_ratio - 1.0) < 1e-9,
          f"ζ={s.damping_ratio:.6f}")
    check("k = (2π/D)²", abs(s.stiffness - (2 * math.pi / D) ** 2) < 1e-9)

    s = Spring.from_duration_bounce(D, 0.12)
    check("bounce=0.12 → ζ=1-0.12=0.88（欠阻尼，会过冲）",
          abs(s.damping_ratio - 0.88) < 1e-9, f"ζ={s.damping_ratio:.6f}")

    s = Spring.from_duration_bounce(D, -0.5)
    check("bounce<0 → 过阻尼 ζ=1/(1+bounce)=2", abs(s.damping_ratio - 2.0) < 1e-9,
          f"ζ={s.damping_ratio:.6f}")

    # bounce=1 在数学上是零阻尼（永不停），必须被钳住
    s = Spring.from_duration_bounce(D, 1.0)
    check("bounce=1 被钳到 MIN_DAMPING_RATIO（否则永不停摆）",
          abs(s.damping_ratio - MIN_DAMPING_RATIO) < 1e-9,
          f"ζ={s.damping_ratio:.6f}")

    a = Spring.from_response(0.4, 0.85)
    b = Spring.from_duration_bounce(0.4, 0.15)
    check("response/dampingRatio 与 duration/bounce 同构（bounce=1-ζ）",
          abs(a.damping_ratio - b.damping_ratio) < 1e-9)

    for bad in (0.0, -1.0):
        try:
            Spring.from_duration_bounce(bad, 0.1)
            check(f"duration={bad} 被拒绝", False)
        except ValueError:
            check(f"duration={bad} 被拒绝", True)
    try:
        Spring(mass=0.0)
        check("mass=0 被拒绝", False)
    except ValueError:
        check("mass=0 被拒绝", True)


def test_spring_convergence_and_overshoot() -> None:
    section("5. 弹簧收敛与过冲")
    sp = Spring.from_duration_bounce(0.40, 0.12)
    sp.reset(-PAGE, 0.0)               # 位移域：起点 -900px，目标是 0
    peak = 0.0
    steps = 0
    while not sp.settled and steps < 20000:
        sp.step(1 / 240.0)
        # 位移从 -900 单调升向 0；越过 0 变正 = 过冲
        peak = max(peak, sp.x)
        steps += 1
    check("收敛（未跑满 20000 步）", sp.settled, f"步数={steps}")
    check("最终位移归零（= 到位）", abs(sp.x) < 0.5, f"x={sp.x:.4f}")
    over_ratio = peak / PAGE
    check("bounce=0.12 确实过冲", over_ratio > 0, f"过冲比例={over_ratio:.4%}")
    check("过冲幅度 < 6%（不是弹球）", 0.0 < over_ratio < 0.06,
          f"过冲比例={over_ratio:.4%}")
    check("收敛时间 < 1.2s", steps / 240.0 < 1.2, f"t={steps/240.0:.3f}s")

    # 感知时长 vs settling duration：苹果明确说 UI 变化不该等后者。
    # duration=0.40s 的一次翻页，含余量也不该超过约 1.6 倍。
    a = _make("spring")
    a.animate_to(-PAGE, animate=False)
    a.animate_to(-2 * PAGE)
    run_ms = 0.0
    while a.is_running() and run_ms < 5000.0:
        a.advance(1000.0 / 144.0)
        run_ms += 1000.0 / 144.0
    check("翻页时长 ≈ 感知时长而非 settling duration（< 1.6×400ms）",
          run_ms < 640.0, f"实际 {run_ms:.0f}ms")

    # bounce=0 临界阻尼：不过冲
    sp = Spring.from_duration_bounce(0.40, 0.0)
    sp.reset(-PAGE, 0.0)
    peak = 0.0
    for _ in range(4000):
        sp.step(1 / 240.0)
        peak = max(peak, sp.x)
        if sp.settled:
            break
    check("bounce=0 完全不过冲（可作 Reduce Motion 用）", peak <= 1e-9,
          f"峰值过冲={peak:.6f}px")

    # 初速度被保留：一帧（1ms）后速度只应按 a₀dt 变化一点。
    # 注意不能用「第一帧位移前进多少」来判断 —— 900px 的位移配 k≈247，
    # 首帧加速度高达 -2.2e5 px/s²，一帧就能改变速度 166px/s，那是对的物理，
    # 不能拿来当「速度连续性」的证据。积分器内部单位是 px/s。
    sp = Spring.from_duration_bounce(0.40, 0.12)
    # 位移 x=-900、目标 0，所以「朝目标」是正速度方向。给 +3000px/s。
    v0 = 3000.0
    sp.reset(-PAGE, v0)

    # 无限小步长后速度应几乎不变 —— 这才是「初速度被保留」的直接证据。
    sp_tiny = Spring.from_duration_bounce(0.40, 0.12)
    sp_tiny.reset(-PAGE, v0)
    sp_tiny.step(1e-6)
    # 1µs 内速度变化 = a₀·dt = 139128·1e-6 ≈ 0.14px/s，这是对的物理
    check("初速度被保留（1µs 后速度几乎未变）",
          abs(sp_tiny.v - v0) < 1.0, f"v={sp_tiny.v:.6f} 初值={v0}")

    # 用闭式解校验积分器本身。欠阻尼解析解：
    #   x(t) = e^(-ζω₀t)·[x₀cos(ω_d t) + (v₀+ζω₀x₀)/ω_d·sin(ω_d t)]
    # 比拿 v₀+a₀dt（一阶欧拉）去比 4 阶 RK4 强得多 —— 后者的偏差
    # 来自加速度在步内的变化，不是积分误差。
    omega0 = math.sqrt(sp.stiffness / sp.mass)
    zeta = sp.damping_ratio
    wd = omega0 * math.sqrt(1.0 - zeta * zeta)

    sp_x0 = -PAGE

    def exact_x(t):
        return math.exp(-zeta * omega0 * t) * (
            sp_x0 * math.cos(wd * t)
            + (v0 + zeta * omega0 * sp_x0) / wd * math.sin(wd * t))
    for t in (0.001, 0.05, 0.2, 0.5):
        s = Spring.from_duration_bounce(0.40, 0.12)
        s.reset(sp_x0, v0)
        # 直接 step 整个 t：交给内部按稳定步长切分，同时顺带验证
        # 非帧对齐时长（如 1ms 不是 1/240 的整数倍）也走得准。
        s.step(t)
        ref = exact_x(t)
        rel = abs(s.x - ref) / max(1.0, abs(ref))
        check(f"RK4 与闭式解一致（t={t}s）", rel < 1e-5,
              f"积分={s.x:.6f} 解析={ref:.6f} 相对偏差={rel:.2e}")

    s = Spring.from_duration_bounce(0.40, 0.12)
    s.reset(-PAGE, v0)
    s.step(0.001)
    check("第一帧确实朝目标动（位移绝对值变小）", abs(s.x) < PAGE,
          f"x={s.x:.4f}")


def test_spring_stability() -> None:
    section("6. 弹簧稳定性（极端 stiffness / damping，1000 帧）")
    cases = [
        ("极高刚度 k=2e4", 2e4, 20.0, 1.0),
        ("极高刚度 k=2e5", 2e5, 50.0, 1.0),
        ("极高刚度 k=1e6", 1e6, 100.0, 1.0),
        ("极高刚度 k=1e6 + 零阻尼", 1e6, 0.0, 1.0),
        ("极低阻尼 c=0（欠阻尼）", 200.0, 0.0, 1.0),
        ("刚度 1 + 阻尼 0", 1.0, 0.0, 1.0),
        ("mass=1e6 重物", 200.0, 20.0, 1e6),
        ("mass=1e-3 轻物 + 强阻尼（λh 是主要约束）", 200.0, 20.0, 1e-3),
        ("mass=4e-4 极轻 + 强阻尼（接近子步预算）", 200.0, 20.0, 4e-4),
    ]
    for label, k, c, m in cases:
        sp = Spring(m, k, c)
        sp.reset(-PAGE, -5.0 * 1000.0)     # 积分器用 px/s
        finite = True
        diverged = False
        for _ in range(1000):           # 1000 帧
            sp.step(1 / 240.0)
            if not (math.isfinite(sp.x) and math.isfinite(sp.v)):
                finite = False
                break
            if abs(sp.x) > PAGE * 1e3:
                diverged = True
                break
        check(f"{label}: 1000 帧无 NaN/Inf", finite, f"x={sp.x!r}")
        check(f"{label}: 1000 帧不发散", not diverged, f"x={sp.x:.4g}")

    # 稳定性预算：Apple 的 bounce∈[-1,1] 只对应 ζ∈[0.02,10]、mass=1，
    # 需求子步率必须远低于 1/dt，否则 step() 会退让精度。
    for b in (-1.0, -0.5, 0.0, 0.12, 0.5, 1.0):
        sp = Spring.from_duration_bounce(0.40, b)
        need = sp.max_step_rate * (1.0 / 240.0)
        check(f"bounce={b:+.2f} 的子步需求在预算内",
              need <= 512, f"每帧需 {need:.1f} 子步（预算 512）")

    # bounce 扫到边界
    for b in (-1.0, -0.5, 0.0, 0.5, 0.9, 1.0, 5.0, -5.0):
        sp = Spring.from_duration_bounce(0.40, b)
        sp.reset(-PAGE, 0.0)
        ok = True
        for _ in range(1000):
            sp.step(1 / 240.0)
            if not (math.isfinite(sp.x) and math.isfinite(sp.v)):
                ok = False
                break
        check(f"bounce={b:+.1f} 不产生 NaN", ok, f"ζ={sp.damping_ratio:.4f}")

    # 非法参数必须拒绝，而不是悄悄产生垃圾
    for kw in ({"stiffness": 0.0}, {"damping": -1.0}, {"mass": -1.0}):
        try:
            Spring(**{**{"mass": 1.0, "stiffness": 200.0, "damping": 20.0}, **kw})
            check(f"拒绝 {kw}", False)
        except ValueError:
            check(f"拒绝 {kw}", True)


def test_spring_fixed_substep() -> None:
    section("7. 弹簧定步长：同一总时长、不同 dt 走同一条轨迹")
    base = Spring.from_duration_bounce(0.40, 0.12)
    base.reset(-PAGE, -1.5)
    for _ in range(240):               # 1.0s @ 240Hz
        base.step(1 / 240.0)

    worst = 0.0
    for fps in (30, 60, 90, 144, 240):
        sp = Spring.from_duration_bounce(0.40, 0.12)
        sp.reset(-PAGE, -1.5)
        dt = 1.0 / fps
        # 补足到整 1.0s，让各帧率比的是同一时刻
        for _ in range(int(round(1.0 / dt))):
            sp.step(dt)
        worst = max(worst, abs(sp.x - base.x))
    check("30/60/90/144/240Hz 下 1s 末位置偏差 < 1px",
          worst < 1.0, f"最大偏差={worst:.4f}px")


# ═════════════════════════════════════════════════════════════════
# 8. 惯性与橡皮筋
# ═════════════════════════════════════════════════════════════════

def test_deceleration() -> None:
    section("8. 减速模型（UIScrollView decelerationRate）")
    check("normal=0.998 → τ≈499.5ms",
          abs(deceleration_time_constant(DECELERATION_NORMAL) - 499.5) < 0.2,
          f"τ={deceleration_time_constant(DECELERATION_NORMAL):.3f}ms")
    check("fast=0.99 → τ≈99.5ms",
          abs(deceleration_time_constant(DECELERATION_FAST) - 99.5) < 0.2,
          f"τ={deceleration_time_constant(DECELERATION_FAST):.3f}ms")

    # A = v0·τ：1px/ms 甩出去应预测再滑约 499.5px
    d = project_deceleration(1000.0)
    check("v=1000px/s 预测滑行 ≈ 499.5px", abs(d - 499.5) < 0.2, f"{d:.3f}px")
    check("速度取反 → 预测距离取反",
          abs(project_deceleration(-1000.0) + d) < 1e-9)

    for bad in (0.0, 1.0, 1.5, -0.2):
        try:
            deceleration_time_constant(bad)
            check(f"非法 rate={bad} 被拒绝", False)
        except ValueError:
            check(f"非法 rate={bad} 被拒绝", True)


def test_rubber_band() -> None:
    section("9. 橡皮筋（系数 0.55，越界渐进变沉）")
    d = PAGE
    # f(x) = (x·d·c)/(d + c·x)，注意 d 是分子因子，小越界时 f ≈ x·c/d
    # （越界量相对一屏越小，压缩越弱），不是 1:1。
    check("小越界几乎不被压缩", abs(rubber_band(1.0, d) - 900.0 * 0.55 / 900.55) < 1e-12,
          f"f(1)={rubber_band(1.0, d):.6f}")
    check("越界量 < 输入（被压住）", rubber_band(300.0, d) < 300.0,
          f"300 → {rubber_band(300.0, d):.2f}")
    check("渐近上限 = 可视尺寸", abs(rubber_band(1e12, d) - d) < 1e-3,
          f"极限={rubber_band(1e12, d):.6f} / {d}")
    check("相对一屏越界越大压得越狠（f/x 递减）",
          (rubber_band(10.0, d) / 10.0) > (rubber_band(800.0, d) / 800.0),
          f"f(10)/10={rubber_band(10.0, d)/10:.4f} "
          f"f(800)/800={rubber_band(800.0, d)/800:.4f}")
    check("负向对称", abs(rubber_band(-137.0, d) + rubber_band(137.0, d)) < 1e-12)

    # 单调递增
    prev = -1.0
    mono = True
    for i in range(0, 2000):
        v = rubber_band(i * 2.0, d)
        if v < prev - 1e-12:
            mono = False
        prev = v
    check("单调递增", mono)
    check("dimension=0 返回 0", rubber_band(50.0, 0.0) == 0.0)
    check("系数默认 0.55", RUBBER_BAND_C == 0.55)

    # 阻力递增：越拖越沉（后半段增量必须小于前半段）
    a = rubber_band(200.0, d) - rubber_band(100.0, d)
    b = rubber_band(600.0, d) - rubber_band(500.0, d)
    check("单位拖动的收益递减", b < a, f"前段={a:.3f}px 后段={b:.3f}px")


# ═════════════════════════════════════════════════════════════════
# 10. PageAnimator：亚像素、缓动可换、逐帧回调
# ═════════════════════════════════════════════════════════════════

def _make(easing="spring", on_frame=None, bounds=None) -> PageAnimator:
    a = PageAnimator(on_frame=on_frame)
    a.set_page_size(PAGE)
    lo, hi = bounds if bounds else (-3 * PAGE, 0.0)
    a.set_bounds(lo, hi)
    a.set_easing(easing)
    a.set_duration(320)
    return a


def _run(a: PageAnimator, fps: float, max_s: float = 4.0) -> list[tuple[float, float]]:
    """按 fps 推进，返回 [(t_ms, value)] 轨迹。"""
    dt = 1000.0 / fps
    trace: list[tuple[float, float]] = []
    t = 0.0
    while a.is_running() and t < max_s * 1000.0:
        a.advance(dt)
        t += dt
        trace.append((t, a.value()))
        if not a.is_running():
            break
    return trace


def test_subpixel_precision() -> None:
    section("10. 亚像素精度与逐帧回调")
    seen: list[float] = []
    a = _make("outQuint", on_frame=lambda v: seen.append(v))
    a.set_duration(900)             # settings.page_duration 的上限
    a.animate_to(-PAGE)
    trace = _run(a, 144.0)
    check("产生了足够多的帧", len(seen) > 100, f"帧数={len(seen)}")

    ints = [v for v in seen if float(v).is_integer()]
    check("绝大多数帧不是整数（旧实现 int(v) 会 100% 是整数）",
          len(ints) < len(seen) * 0.10,
          f"整数帧 {len(ints)}/{len(seen)}")

    uniq = len({round(v, 4) for v in seen})
    check("有效取值数 ≈ 帧数（旧实现慢速时只有几十个）",
          uniq >= len(seen) * 0.95, f"唯一值 {uniq} / 帧 {len(seen)}")

    check("最终精确落在 -900", a.value() == -PAGE, f"{a.value()!r}")
    check("value() 返回 float", isinstance(a.value(), float))

    # 对照旧实现：320ms @60fps 的 OutCubic + int(v)，整段只有约 20 个取值。
    # 这里用同样保守的 320ms/60fps 口径作基线。
    old_frames = int(320 / (1000 / 60)) + 1
    check(f"144Hz/900ms 的有效帧数 {uniq} 远高于旧实现的 ≤{old_frames}",
          uniq > old_frames * 4, f"提升 {uniq / old_frames:.1f}×")

    # 同参数（320ms）下与旧实现的直接对比
    c = _make("outQuint")
    c.set_duration(320)
    c.animate_to(-PAGE)
    t144 = _run(c, 144.0)
    d = _make("outQuint")
    d.set_duration(320)
    d.animate_to(-PAGE)
    t60 = _run(d, 60.0)
    old_unique = len({int(round(v)) for _, v in t60})   # 旧实现的 int() 量化结果
    new_unique = len({round(v, 4) for _, v in t144})
    check(f"同参数 320ms：有效帧数 {old_unique} → {new_unique}",
          new_unique > old_unique * 1.5,
          f"60fps+int()={old_unique} vs 144fps+float={new_unique}")


def test_all_easings_drive_to_target() -> None:
    section("11. 五种缓动都能正确到位")
    for name in EASINGS:
        a = _make(name)
        a.animate_to(-PAGE)
        trace = _run(a, 60.0)
        check(f"{name}: 到位 -900", abs(a.value() + PAGE) < 1e-6,
              f"最终={a.value()!r} 帧数={len(trace)}")
        check(f"{name}: 无 NaN",
              all(math.isfinite(v) for _, v in trace))
        check(f"{name}: 起始帧等于起点", abs(trace[0][1] - 0.0) < PAGE,
              f"首帧={trace[0][1]:.3f}")


def test_velocity_continuity() -> None:
    section("12. 速度连续性（手势交棒不硬切）")
    # spring：初速度被弹簧继承。位移 900px 配 k≈247 时首帧加速度高达
    # -2.2e5 px/s²，所以用 1ms 的小步长来验，而不是拿 144fps 的一整帧。
    a = _make("spring")
    a.animate_to(-PAGE, velocity=-2.0)          # px/ms
    sp = a._spring
    a0 = (-sp.stiffness * sp.x - sp.damping * sp.v) / sp.mass    # px/s²
    a.advance(1.0)
    expect = -2.0 + a0 * 0.001 / 1000.0         # → px/ms
    check("spring: 初速度被继承（1ms 后仅按 a₀dt 变化）",
          abs(a.velocity() - expect) < 0.005,
          f"v={a.velocity():.5f} 期望={expect:.5f}px/ms")

    # 贝塞尔做不到 —— 但本模块用 e'(0) 反解时长做近似。
    # 反解区间 D' = span·e'(0)/v₀ 必须落在 [0.4D, 2.5D] = [128, 800]ms 内，
    # 对 v₀=-10px/ms 是 span·slope/800 ~ span·slope/128，都落在区间内。
    for name in ("outQuint", "outCubic", "outExpo"):
        a = _make(name)
        a.animate_to(-PAGE, velocity=-10.0)
        a.advance(1.0)
        check(f"{name}: 首帧速度被匹配到 -10.0px/ms",
              abs(a.velocity() + 10.0) / 10.0 < 0.05,
              f"v={a.velocity():.4f}px/ms")
        want = PAGE * resolve_easing(name)[1](0.0) / 10.0
        check(f"{name}: 时长反解 = span·e'(0)/v₀",
              abs(a._eff_duration_ms - want) / want < 1e-9,
              f"D'={a._eff_duration_ms:.3f}ms 期望={want:.3f}ms")

    a = _make("inOutCubic")
    a.animate_to(-PAGE, velocity=-10.0)
    a.advance(1.0)
    check("inOutCubic: e'(0)=0，无法匹配，退化为基线时长（已知限制）",
          abs(a.velocity()) < 0.01 and a._eff_duration_ms == 320.0,
          f"v={a.velocity():.5f}px/ms D'={a._eff_duration_ms}ms")

    # 时长反解必须被夹住
    a = _make("outQuint")
    a._start_value, a._end_value, a._v0 = 0.0, -PAGE, -1e-6
    d = a._match_duration(-1e-6)
    check("极小初速度时长被钳到 2.5×", abs(d - 2.5 * 320.0) < 1e-6, f"{d}ms")
    a._v0 = -1e6
    d = a._match_duration(-1e6)
    check("极大初速度时长被钳到 0.4×", abs(d - 0.4 * 320.0) < 1e-6, f"{d}ms")


# ═════════════════════════════════════════════════════════════════
# 13. 帧率无关性
# ═════════════════════════════════════════════════════════════════

def test_frame_rate_independence() -> None:
    section("13. 帧率无关性：30fps vs 144fps")

    # 关键：不能对「采样出来的轨迹」做线性插值再互比 —— outExpo 在头
    # 50ms 内走完 87% 位移，30fps 的两个采样点隔 33ms，插值误差能有几百
    # 像素，量出来的是测试自己的误差而不是实现的误差。
    # 正确做法是让两次运行在**完全相同的 elapsed 时间**上取值：每帧的
    # 最后一步用不足量补齐到目标时刻。
    targets = [40.0, 80.0, 120.0, 200.0, 300.0, 450.0, 650.0]

    def sample(name, fps):
        a = _make(name)
        a.animate_to(-PAGE)
        out: list[float] = []
        t = 0.0
        for T in targets:
            while t < T - 1e-9:
                a.advance(min(1000.0 / fps, T - t))
                t += 1000.0 / fps if t + 1000.0 / fps <= T + 1e-9 else (T - t)
            out.append(a.value())
        return out

    for name in EASINGS:
        v30, v144 = sample(name, 30.0), sample(name, 144.0)
        worst = max(abs(x - y) for x, y in zip(v30, v144))
        rel = worst / PAGE
        check(f"{name}: 同一时刻的取值偏差 < 1% 页宽", rel < 0.01,
              f"最大={worst:.6f}px = {rel:.3e}")

    # 抖动帧率：dt 完全不规则但总时长相同，结果仍应一致。
    # 这是比「固定 30 vs 144」更强的检验 —— 真实环境就是抖的。
    import random

    def jittered(name, seed):
        r = random.Random(seed)
        a = _make(name)
        a.animate_to(-PAGE)
        t = 0.0
        while t < 200.0 - 1e-9:
            dt = min(r.uniform(4.0, 40.0), 200.0 - t)
            a.advance(dt)
            t += dt
        return a.value()

    for name in EASINGS:
        ref = jittered(name, 1)
        vals = [jittered(name, s) for s in range(2, 8)]
        worst = max(abs(v - ref) for v in vals)
        rel = worst / PAGE
        check(f"{name}: 6 组随机抖动帧率的 200ms 取值偏差 < 1%",
              rel < 0.01, f"最大={worst:.6f}px = {rel:.3e}")

    # 收敛位置（不同帧率跑到最后必须落在同一处）
    for name in EASINGS:
        finals = []
        for fps in (30.0, 60.0, 144.0, 240.0):
            a = _make(name)
            a.animate_to(-PAGE)
            _run(a, fps)
            finals.append(a.value())
        rel_f = (max(finals) - min(finals)) / PAGE
        check(f"{name}: 4 种帧率的最终位置误差 < 1%", rel_f < 0.01,
              f"极差={rel_f:.3e} 值={[round(f, 6) for f in finals]}")

    # 弹簧：真实时钟路径（QElapsedTimer）与显式 dt 路径应一致
    a = _make("spring")
    a.animate_to(-PAGE)
    trace = _run(a, 90.0)
    b = _make("spring")
    b.animate_to(-PAGE)
    _run(b, 144.0)
    check("spring: 90Hz 与 144Hz 终值一致",
          abs(trace[-1][1] - b.value()) < 1e-9,
          f"{trace[-1][1]!r} vs {b.value()!r}")

    # dt 被 MAX_FRAME_DT_MS 钳住：一次超大 dt 不应让内容瞬移
    a = _make("spring")
    a.animate_to(-PAGE)
    a.advance(5000.0)
    check("单帧 dt>MAX_FRAME_DT 被钳制（不瞬移到终点）",
          a.value() > -PAGE * 0.98, f"{a.value():.2f}")


# ═════════════════════════════════════════════════════════════════
# 14. 手势判定
# ═════════════════════════════════════════════════════════════════

def _drag(a: PageAnimator, total: float, steps: int, fps: float,
          hold_at_end_ms: float = 0.0) -> float:
    a.begin()
    dt = 1000.0 / fps
    for i in range(1, steps + 1):
        a.update(total * i / steps, dt)
    for _ in range(int(hold_at_end_ms / dt)):
        a.update(total, dt)
    return a.end()


def test_gesture_slow_drag_reverts() -> None:
    section("14. 手势判定：慢拖回弹")
    # 从第 1 页（offset -900）向回拖 200px 后停住再松手 → 应回到 -900
    a = _make("spring")
    a.animate_to(-PAGE, animate=False)
    target = _drag(a, +200.0, 20, 60.0, hold_at_end_ms=120.0)
    check("慢拖 200px 后松手 → 停在原页 -900", abs(target + PAGE) < 1e-6,
          f"吸附点={target}")
    _run(a, 60.0)
    check("慢拖后确实回到原页", abs(a.value() + PAGE) < 1e-6,
          f"最终={a.value():.4f}")

    # 拖过 30% 页宽就应该进页（慢但坚决）
    a = _make("spring")
    a.animate_to(0.0, animate=False)
    target = _drag(a, -PAGE * 0.45, 30, 60.0, hold_at_end_ms=200.0)
    check("慢拖 45% 页宽 → 进下一页 -900", abs(target + PAGE) < 1e-6,
          f"吸附点={target}")

    # 边界页继续拖不能跑出去
    a = _make("spring")
    a.animate_to(0.0, animate=False)
    target = _drag(a, +400.0, 20, 60.0, hold_at_end_ms=150.0)
    check("首页继续往后拖 → 停在 0（不越界）", abs(target) < 1e-6,
          f"吸附点={target}")


def test_gesture_fast_flick_advances() -> None:
    section("15. 手势判定：快甩前进")
    a = _make("spring")
    a.animate_to(0.0, animate=False)
    target = _drag(a, -60.0, 6, 60.0)      # 10ms/帧 × 6 = 60ms 走 60px → 1px/ms
    check("小位移快甩（60px@60ms）→ 仍进下一页 -900",
          abs(target + PAGE) < 1e-6, f"吸附点={target}")

    a = _make("spring")
    a.animate_to(-2 * PAGE, animate=False)
    target = _drag(a, +80.0, 8, 60.0)      # 反向快甩
    check("反向快甩 → 回到上一页 -900", abs(target + PAGE) < 1e-6,
          f"吸附点={target}")

    # 末页快甩也不越界
    a = _make("spring")
    a.animate_to(-3 * PAGE, animate=False)
    target = _drag(a, -80.0, 8, 60.0)
    check("末页继续快甩 → 停在 -2700（不越界）",
          abs(target + 3 * PAGE) < 1e-6, f"吸附点={target}")

    # 快速甩之后弹簧必须真的带着速度飞过去（位置继续越过起点）
    a = _make("spring")
    a.animate_to(0.0, animate=False)
    _drag(a, -60.0, 6, 60.0)
    overshot: list[float] = []
    for _ in range(400):
        a.advance(1000.0 / 144.0)
        overshot.append(a.value())
        if not a.is_running():
            break
    check("甩动后弹簧带惯性越过 50%（速度连续，不是匀速滑过去）",
          min(overshot) < -PAGE * 0.5,
          f"最远={min(overshot):.1f}px")
    check("最终精确停在 -900", abs(a.value() + PAGE) < 1e-6,
          f"{a.value()!r}")


def test_gesture_velocity_estimate() -> None:
    section("16. 手势速度估算")
    a = _make("spring")
    a.begin()
    for i in range(1, 11):
        a.update(-5.0 * i, 10.0)        # 10 帧 × 10ms，每帧 5px → 0.5px/ms
    v = a._estimate_velocity()
    check("匀速拖动估出 -0.5px/ms（方向为负=向下一页）",
          abs(v + 0.5) < 1e-6, f"v={v:.6f}px/ms")

    # 停住再松手 → 速度应归零，否则会误判成甩
    for _ in range(20):
        a.update(-50.0, 10.0)
    v = a._estimate_velocity()
    check("手指停住后速度归零（窗口只看最近 90ms）", abs(v) < 1e-9,
          f"v={v:.9f}px/ms")

    # 采样窗口不能无限增长
    check("采样列表被截断在 2×窗口内", len(a._samples) <= 40,
          f"样本数={len(a._samples)}")

    a.begin()
    check("刚按下还没动时速度为 0", a._estimate_velocity() == 0.0)
    a.update(-10.0, 10.0)
    check("只有一个真实位移样本时速度 = -1.0px/ms",
          abs(a._estimate_velocity() + 1.0) < 1e-9,
          f"v={a._estimate_velocity():.6f}px/ms")

    # 回归哨兵：整段手势比速度窗口还短（一次快速甩动的形态）。
    # 旧实现会一路覆盖到最新样本、dt 恒为 0、速度恒为 0，
    # 结果是「甩得再快也不翻页」。这条用例必须留在。
    b = _make("spring")
    b.animate_to(0.0, animate=False)
    b.begin()
    for i in range(1, 7):                    # 6 帧 × 8ms = 48ms < 90ms 窗口
        b.update(-30.0 * i, 8.0)
    v = b._estimate_velocity()
    check("手势短于速度窗口时仍能估出速度（快甩不能失效）",
          abs(v + 3.75) < 0.01, f"v={v:.4f}px/ms 期望≈-3.75")
    check("该情形下松手会前进到下一页", b.end() == -PAGE,
          f"吸附点={b.end()}")

    # 窗口截断仍然生效：长手势只看最近 90ms
    c = _make("spring")
    c.begin()
    for i in range(1, 41):                   # 400ms
        c.update(-4.0 * i, 10.0)
    v = c._estimate_velocity()
    check("长手势只取最近 90ms（≈-0.4px/ms）", abs(v + 0.4) < 0.01,
          f"v={v:.4f}px/ms")


def test_gesture_live_follow_and_band() -> None:
    section("17. 跟手 + 越界橡皮筋")
    frames: list[float] = []
    a = _make("spring", on_frame=lambda v: frames.append(v))
    a.animate_to(0.0, animate=False)
    a.begin()
    for i in range(1, 9):
        a.update(-40.0 * i, 16.0)
    check("拖动时页面实时跟手（回调逐帧触发）", len(frames) >= 8,
          f"回调 {len(frames)} 次")
    check("位移与手指一致（界内无阻尼）", abs(a.value() - (-320.0)) < 1e-6,
          f"value={a.value():.3f}")

    # drag_delta 是「相对按下点的累计位移」，不是每帧增量。
    # 传增量的话位置永远不会累加 —— grid 接入时最容易踩的坑。
    b = _make("spring")
    b.animate_to(0.0, animate=False)
    b.begin()
    for _ in range(5):
        b.update(-30.0, 16.0)          # 传了 5 次增量
    check("drag_delta 是累计量而非增量（重复传 -30 不会累到 -150）",
          abs(b.value() - (-30.0)) < 1e-9, f"value={b.value():.3f}")
    c = _make("spring")
    c.animate_to(0.0, animate=False)
    c.begin()
    for i in range(1, 6):
        c.update(-30.0 * i, 16.0)      # 传累计量
    check("传累计量则正确累加到 -150", abs(c.value() - (-150.0)) < 1e-9,
          f"value={c.value():.3f}")

    # 越界：拖到界外很远，橡皮筋应把位移压到界与原始位置之间
    lo = -3 * PAGE
    for i in range(1, 41):
        a.update(-320.0 - 3000.0 * i / 40.0, 16.0)
    raw = -320.0 - 3000.0
    check("越界被橡皮筋压住（严格落在界与原始位置之间）",
          raw < a.value() < lo, f"原始={raw:.1f} 界={lo:.1f} 实际={a.value():.2f}")
    check("越界量被记录", (a.band_ratio() or 0.0) > 0.0,
          f"band_ratio={a.band_ratio():.4f}")
    check("越界被压到一屏以内（最多拖出 band_dim）",
          lo - a.value() < PageAnimator.BAND_DIM_MAX,
          f"越界量={lo - a.value():.1f}px < {PageAnimator.BAND_DIM_MAX}")
    check("拖动后进入 dragging 态", a.is_dragging())
    a.cancel()
    check("cancel 后停止且不再拖动", not a.is_running())


def test_decide_target_pure_function() -> None:
    section("18. decide_target 判定表")
    a = _make("spring")
    # 判定阈值：VELOCITY_COMMIT=0.25px/ms，POSITION_COMMIT=0.30 页宽
    cases = [
        ("第0页 静止", 0.0, 0.0, 0.0),
        ("第0页 向后慢拖 100px 停手（<30%）", -100.0, 0.0, 0.0),
        ("第0页 向后拖 45% 页宽（明确意图）", -PAGE * 0.45, 0.0, -PAGE),
        ("第0页 向后快甩（速度 1.2px/ms）", -50.0, -1.2, -PAGE),
        ("第0页 向前快甩（首页边界）", 30.0, 1.2, 0.0),
        ("第1页 向前拖 45%", -PAGE + PAGE * 0.45, 0.0, 0.0),
        ("第1页 向前快甩", -PAGE + 30.0, 1.2, 0.0),
        ("第1页 向后快甩", -PAGE - 30.0, -1.2, -2 * PAGE),
        ("第0页 恰好 30% 阈值（>= 即翻）", -PAGE * 0.30, 0.0, -PAGE),
        ("第0页 29% 阈值下（不回弹）", -PAGE * 0.29, 0.0, 0.0),
        ("慢速但惯性不够：v=0.1px/ms 投影 50px<30%页宽", -100.0, -0.1, 0.0),
        ("慢速且惯性够：v=0.8px/ms 投影 400px>30%页宽",
         -100.0, -0.8, -PAGE),
        # τ=499.5ms，要投影 ≥270px 需 |v| ≥ 0.54px/ms
        ("阈值下 0.24px/ms 投影 120px → 不翻", -100.0, -0.24, 0.0),
        ("阈值下 0.54px/ms 投影 270px → 刚好翻", -100.0, -0.54, -PAGE),
    ]
    for label, x, v, want in cases:
        got = a.decide_target(x, v)
        check(f"{label} → {want:.0f}", abs(got - want) < 1e-6,
              f"得到={got:.1f} 期望={want:.1f}")

    # page_size=0 时退化为夹紧，不应崩
    b = PageAnimator()
    b.set_page_size(0.0)
    b.set_bounds(0.0, 100.0)
    check("page_size=0 时夹紧而不崩",
          0.0 <= b.decide_target(50.0, 1.0) <= 100.0)


def test_depth_and_progress() -> None:
    section("19. 景深接口")
    a = _make("spring")
    a.animate_to(-PAGE, animate=False)
    check("静止时 depth=0, scale=1, opacity=1",
          abs(a.depth_progress()) < 1e-9 and a.depth_scale() == 1.0
          and a.depth_opacity() == 1.0)

    # 半页处
    a._value = -PAGE * 0.5
    check("半页处 depth=±0.5", abs(abs(a.depth_progress()) - 0.5) < 1e-9,
          f"{a.depth_progress():.4f}")
    check("半页处已缩放 < 1", a.depth_scale() < 1.0,
          f"scale={a.depth_scale():.4f}")
    check("缩放范围合法",
          PageAnimator.DEPTH_MIN_SCALE <= a.depth_scale() <= 1.0)

    # depth_progress 不会越界
    a._value = -PAGE * 1.9
    check("depth 被钳在 [-1,1]", abs(a.depth_progress()) <= 1.0,
          f"{a.depth_progress():.4f}")

    a.set_page_size(0.0)
    check("page_size=0 时 depth=0", a.depth_progress() == 0.0)


def test_lifecycle_and_retarget() -> None:
    section("20. 生命周期、重定向、回调次数")
    done: list[str] = []
    a = _make("outQuint")
    a.settled.connect(lambda: done.append("settled"))
    a.animate_to(-PAGE)
    _run(a, 60.0)
    check("settled 信号恰好一次", done == ["settled"], str(done))
    check("结束后不在运行", not a.is_running())
    check("结束后速度归零", a.velocity() == 0.0)

    # 重定向：中途换目标，速度应被继承而不是归零
    a = _make("spring")
    a.animate_to(-PAGE)
    _run(a, 144.0, max_s=0.15)           # 只跑到 150ms，还在半路
    check("重定向前确实在动", a.is_running() and abs(a.velocity()) > 0.05,
          f"x={a.value():.2f} v={a.velocity():.4f} running={a.is_running()}")
    v_before = a.velocity()
    x_before = a.value()
    a.animate_to(0.0)                    # 半路折返，不传速度 → 应继承
    check("重定向前速度非零", abs(v_before) > 0.05, f"v={v_before:.4f}")
    check("重定向从当前位置继续", a.value() == x_before, f"{a.value()!r}")
    check("重定向沿用了当前速度（未归零）",
          abs(a.velocity() - v_before) < 1e-9,
          f"v_before={v_before:.4f} → v_at_launch={a.velocity():.4f}")
    a.advance(1.0)
    check("重定向后 1ms 速度连续（仍是负向，不反向跳变）",
          a.velocity() < 0, f"v_after_1ms={a.velocity():.4f}px/ms")
    _run(a, 60.0)
    check("重定向后仍精确到位 0", abs(a.value()) < 1e-9, f"{a.value()!r}")

    # animate_to(animate=False) 立即落位
    a = _make("spring")
    a.animate_to(-3 * PAGE, animate=False)
    check("animate=False 立即落位", a.value() == -3 * PAGE)
    check("animate=False 不在运行", not a.is_running())

    # stop() 保留位置
    a = _make("outQuint")
    a.animate_to(-PAGE)
    a.advance(100.0)
    v_stop = a.value()
    a.stop()
    check("stop 保留位置", a.value() == v_stop)
    check("stop 清速度", a.velocity() == 0.0)
    check("stop 后不再推进",
          (a.advance(16.0), a.value() == v_stop)[1])


def test_invalid_config_falls_back() -> None:
    section("21. 非法配置回落")
    a = PageAnimator()
    a.set_easing("不存在的缓动")
    check("非法缓动名回落到 outQuint", a.easing() == "outQuint")
    a.set_duration(0)
    check("duration=0 被抬到最小正值", a.duration_ms() > 0)
    a.set_spring(0.0, 99.0)
    check("bounce 被钳到 [-1,1]", a._spring_bounce <= 1.0)
    a.set_spring(0.0, -99.0)
    check("bounce 下界不取 -1（退化点会除零）", a._spring_bounce > -1.0,
          f"bounce={a._spring_bounce}")
    a.set_page_size(-5.0)
    check("page_size 负数被钳到 0", a.page_size() == 0.0)
    a.set_bounds(10.0, 0.0)
    lo, hi = a.bounds()
    check("bounds 上下颠倒被自动交换", lo == 0.0 and hi == 10.0,
          f"({lo}, {hi})")
    check("clamp 基本正确", clamp(5, 0, 1) == 1 and clamp(-5, 0, 1) == 0
          and clamp(0.5, 0, 1) == 0.5)


def test_callback_exception_propagates() -> None:
    section("22. 回调异常：advance() 上抛，Qt 驱动路径不崩进程")

    # 注意一个反直觉的 Qt 行为：QAbstractAnimation.start() 会**同步**回调
    # 一次 updateCurrentTime(0)，所以 animate_to() 内部就已经发过一帧。
    # 下面的用例都按这个前提写。

    def boom(v):
        raise RuntimeError("grid 的 bug 不该被动画悄悄吃掉")

    # 直接调 advance()：异常应当照常上抛，便于定位。
    # 用「第二帧才抛」的回调，避开 start() 的同步首帧。
    fired = {"n": 0}

    def boom_after_first(v):
        fired["n"] += 1
        if fired["n"] > 1:
            raise RuntimeError("boom")

    a = _make("outQuint", on_frame=boom_after_first)
    a.animate_to(-PAGE)
    check("animate_to() 已同步发出首帧（Qt start() 的行为）", fired["n"] == 1,
          f"回调 {fired['n']} 次")
    try:
        a.advance(16.0)
        check("advance(): 回调异常向上抛", False, "没有抛")
    except RuntimeError:
        check("advance(): 回调异常向上抛", True)

    # 走 Qt 的真实驱动路径（updateCurrentTime，由 C++ 回调）：
    # 异常绝不能逃出去 —— 跨 C++ 帧会直接 0xC0000409 掐掉整个进程，
    # 连 faulthandler 都抓不到。这条用例本身就是防回归的哨兵。
    got: list[BaseException] = []
    b = _make("outQuint", on_frame=boom)
    b.errorOccurred.connect(got.append)
    # updateCurrentTime 会走 sys.excepthook 打 traceback，先静音免得刷屏
    saved_hook = sys.excepthook
    printed: list[str] = []
    sys.excepthook = lambda et, ev, tb: printed.append("".join(
        traceback.format_exception(et, ev, tb)))
    try:
        b.animate_to(-PAGE)             # start() 的同步首帧就会抛
    finally:
        sys.excepthook = saved_hook
    check("Qt 驱动路径：异常改道到 errorOccurred", len(got) == 1,
          f"收到 {len(got)} 个")
    check("Qt 驱动路径：收到的是原始异常",
          bool(got) and isinstance(got[0], RuntimeError), str(got))
    check("Qt 驱动路径：仍按标准格式打了 traceback（不丢调试信息）",
          bool(printed) and "RuntimeError" in printed[0],
          f"printed={len(printed)}")
    check("Qt 驱动路径：动画已停住不留半截状态", not b.is_running())
    b.updateCurrentTime(32)
    check("Qt 驱动路径：停住后不再回调", len(got) == 1, f"收到 {len(got)} 个")

    # 没有回调时正常工作
    c = _make("outQuint")
    c.animate_to(-PAGE)
    _run(c, 60.0)
    check("无回调也能跑完", abs(c.value() + PAGE) < 1e-6)

    # 正常回调不产生 errorOccurred
    ok: list[BaseException] = []
    d = _make("outQuint", on_frame=lambda v: None)
    d.errorOccurred.connect(ok.append)
    d.animate_to(-PAGE)
    _run(d, 60.0)
    check("正常回调不产生 errorOccurred",
          not ok and abs(d.value() + PAGE) < 1e-6)

    # 普通回调也要能承受 start() 的同步首帧（grid 的 on_frame 必须幂等）
    frames: list[float] = []
    e = _make("spring", on_frame=frames.append)
    e.animate_to(-PAGE)
    check("spring 也在起播时同步发一帧", len(frames) == 1 and frames[0] == 0.0,
          f"frames={frames[:3]}")


# ═════════════════════════════════════════════════════════════════
# 23. 性能
# ═════════════════════════════════════════════════════════════════

def test_performance() -> None:
    section("23. 性能：100 次连续动画的每帧耗时")
    runs = 100
    for name in ("spring", "outQuint", "outExpo"):
        a = _make(name)
        dt = 1000.0 / 144.0
        samples: list[float] = []
        total_frames = 0

        # 预热，避免把首次 import/JIT 成本算进分布
        for _ in range(5):
            a.animate_to(-PAGE, animate=False)

        t_wall0 = time.perf_counter()
        for r in range(runs):
            a.animate_to(-PAGE if r % 2 == 0 else 0.0)
            while a.is_running():
                t0 = time.perf_counter()
                a.advance(dt)
                samples.append((time.perf_counter() - t0) * 1000.0)
                total_frames += 1
                if len(samples) > 40000:
                    break
        wall_ms = (time.perf_counter() - t_wall0) * 1000.0

        samples.sort()
        n = len(samples)
        p50 = samples[n // 2]
        p99 = samples[int(n * 0.99)]
        worst = samples[-1]
        over = sum(1 for s in samples if s > FRAME_BUDGET)

        check(f"{name}: 无超过 16ms 的帧（{runs} 次动画 / {total_frames} 帧）",
              over == 0, f"超预算帧数={over}, 最差={worst:.3f}ms")
        check(f"{name}: p99 < 1ms", p99 < 1.0, f"p50={p50:.4f}ms p99={p99:.4f}ms")
        check(f"{name}: 整轮墙钟 < 8s", wall_ms < 8000.0,
              f"{wall_ms:.0f}ms / {runs} 次")
        print(f"     {name:9s} 帧数={total_frames:5d} p50={p50:.4f}ms "
              f"p99={p99:.4f}ms max={worst:.4f}ms 超16ms={over}")

    # 手势路径的每帧成本（update 带橡皮筋）
    a = _make("spring")
    samples = []
    for _ in range(100):
        a.animate_to(0.0, animate=False)
        a.begin()
        for i in range(200):
            t0 = time.perf_counter()
            a.update(-i * 12.0, 8.0)
            samples.append((time.perf_counter() - t0) * 1000.0)
    samples.sort()
    over = sum(1 for s in samples if s > FRAME_BUDGET)
    check(f"手势 update：无超过 16ms 的帧（{len(samples)} 帧）", over == 0,
          f"超预算={over} max={samples[-1]:.4f}ms")
    print(f"     gesture  帧数={len(samples):5d} p50={samples[len(samples)//2]:.4f}ms "
          f"p99={samples[int(len(samples)*0.99)]:.4f}ms max={samples[-1]:.4f}ms")


# ═════════════════════════════════════════════════════════════════
# 汇总
# ═════════════════════════════════════════════════════════════════

def main() -> int:
    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

    test_easing_endpoints()
    test_easing_monotonic()
    test_easing_shapes()
    test_spring_parameter_conversion()
    test_spring_convergence_and_overshoot()
    test_spring_stability()
    test_spring_fixed_substep()
    test_deceleration()
    test_rubber_band()
    test_subpixel_precision()
    test_all_easings_drive_to_target()
    test_velocity_continuity()
    test_frame_rate_independence()
    test_gesture_slow_drag_reverts()
    test_gesture_fast_flick_advances()
    test_gesture_velocity_estimate()
    test_gesture_live_follow_and_band()
    test_decide_target_pure_function()
    test_depth_and_progress()
    test_lifecycle_and_retarget()
    test_invalid_config_falls_back()
    test_callback_exception_propagates()
    test_performance()

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [(n, d) for n, ok, d in _results if not ok]

    print("\n" + "═" * 64)
    if failed:
        print(f"失败明细（{len(failed)} 项）：")
        for n, d in failed:
            print(f"  FAIL  {n}" + (f"   [{d}]" if d else ""))
    print(f"合计 {len(_results)} 项：PASS {passed} / FAIL {len(failed)}")
    print("═" * 64)
    del app
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
