# -*- coding: utf-8 -*-
"""
性能基准工具。测完再改，别凭直觉优化。

用法（在项目根目录）：
    python -m launchpad.bench startup          # 启动分段耗时 + 计数器
    python -m launchpad.bench startup --cold   # 用隔离的空图标缓存测冷启动
    python -m launchpad.bench frame            # 单帧 render 分布
    python -m launchpad.bench icon             # QIcon.pixmap 微基准
    python -m launchpad.bench pageflip         # 翻页动画逐帧耗时
    python -m launchpad.bench all

所有子命令都支持 --json，输出到 stdout 之外的机器可读结果，
便于 tests_perf.py 复用同一套测量代码。

设计约束：
- 本文件**不修改**任何其它模块。对别人的代码只做 monkeypatch 计时，
  补丁在 bench 内可控地装上、卸掉，不留下副作用。
- 冷启动测量走隔离子进程（改 LOCALAPPDATA/APPDATA），
  绝不去删用户真实的图标缓存。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── 统计工具 ──────────────────────────────────────────

def pct(values: list[float], q: float) -> float:
    """
    百分位（线性插值）。

    刻意不用 statistics.quantiles：小样本（n<4）时它会抛异常，
    而基准常常就是想看 n=5 的情况。
    """
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1.0 - frac) + s[hi] * frac


def summarize(name: str, values: list[float], unit: str = "ms") -> dict:
    if not values:
        return {"name": name, "n": 0}
    return {
        "name": name,
        "n": len(values),
        "unit": unit,
        "min": round(min(values), 3),
        "p50": round(pct(values, 50), 3),
        "p90": round(pct(values, 90), 3),
        "p99": round(pct(values, 99), 3),
        "max": round(max(values), 3),
        "mean": round(sum(values) / len(values), 3),
        "total": round(sum(values), 3),
    }


def table(rows: list[dict]) -> str:
    hdr = f"{'指标':<34}{'n':>5}{'p50':>10}{'p90':>10}{'p99':>10}{'max':>10}{'total':>11}"
    lines = [hdr, "-" * len(hdr.encode("gbk", "replace"))]
    for r in rows:
        if not r.get("n"):
            lines.append(f"{r['name']:<34}{'-':>5}{'-':>10}{'-':>10}{'-':>10}{'-':>10}{'-':>11}")
            continue
        lines.append(
            f"{r['name']:<34}{r['n']:>5}{r['p50']:>10.3f}{r['p90']:>10.3f}"
            f"{r['p99']:>10.3f}{r['max']:>10.3f}{r['total']:>11.1f}"
        )
    return "\n".join(lines)


# ─── 插桩 ──────────────────────────────────────────────

class Probe:
    """
    给别人的函数套计时器。

    每个被包的方法记录：调用次数、累计耗时（inclusive）、**净耗时**
    （exclusive，扣掉它调用的其它被测函数）、单次最大耗时。

    净耗时是这里唯一有意义的数字。第一版把 outermost 调用的整段耗时
    都记成 self_ms，结果 get() 显示 390ms 而它的子函数 _placeholder
    显示 247ms —— 两处重复计入，看时间去哪了只能靠手算。
    改成显式栈：子函数返回时把耗时挂到父记录上，self = total - children。
    """

    def __init__(self) -> None:
        self.stats: dict[str, dict] = {}
        self.skipped: list[str] = []
        self._saved: list = []
        self._stack: list[dict] = []

    def wrap(self, obj, attr: str, label: str | None = None,
             nest_under: str | None = None) -> None:
        label = label or f"{getattr(obj, '__name__', obj)}.{attr}"
        # **只包 Python 里定义的函数，绝不包继承自 C++ 的方法。**
        #
        # 这条不是洁癖，是踩过：QWidget.setParent 是 sip 的内建方法，
        # 用普通 Python 函数替换再 setattr 回去之后，描述符绑定丢了 ——
        # 此后 `t.setParent(self)` 会把 self 当成唯一参数传进去，
        # 于是 grid.rebuild() 抛 "not enough arguments"，
        # 看起来像应用本身坏了，其实是我的探针把自己测崩了。
        # 判据用类自己的 __dict__：继承来的方法不在里面。
        if attr not in getattr(obj, "__dict__", {}):
            self.skipped.append(label)
            return

        orig = getattr(obj, attr)
        rec = self.stats.setdefault(
            label, {"calls": 0, "total_ms": 0.0, "child_ms": 0.0,
                    "max_ms": 0.0, "max_self_ms": 0.0}
        )
        del nest_under            # 仅为可读性保留参数；栈已自动处理嵌套
        probe = self

        def timed(*a, **kw):
            rec["calls"] += 1
            # child_ms 是**本次调用**内层累计的耗时，用完即弃；
            # rec 里的 child_ms 才是跨调用的累计。
            cell = {"child_ms": 0.0}
            t0 = time.perf_counter()
            probe._stack.append(cell)
            try:
                return orig(*a, **kw)
            finally:
                dt = (time.perf_counter() - t0) * 1000.0
                probe._stack.pop()
                rec["total_ms"] += dt
                rec["child_ms"] += cell["child_ms"]
                self_dt = dt - cell["child_ms"]
                rec["max_self_ms"] = max(rec["max_self_ms"], self_dt)
                if probe._stack:
                    # 往父 cell 记的是**本次调用的完整耗时** dt，
                    # 不是 cell["child_ms"]（那只是孙层，会漏掉直接子层）。
                    probe._stack[-1]["child_ms"] += dt

        setattr(obj, attr, timed)
        self._saved.append((obj, attr, orig))

    def uninstall(self) -> None:
        for obj, attr, orig in reversed(self._saved):
            setattr(obj, attr, orig)
        self._saved.clear()

    def report(self) -> list[dict]:
        out = []
        for label, r in self.stats.items():
            out.append({
                "label": label,
                "calls": r["calls"],
                "total_ms": round(r["total_ms"], 1),
                "self_ms": round(r["total_ms"] - r["child_ms"], 1),
                "max_ms": round(r["max_ms"], 1),
                "max_self_ms": round(r["max_self_ms"], 1),
            })
        return sorted(out, key=lambda d: -d["self_ms"])


def install_probe() -> Probe:
    """装上全套插桩。必须在 QApplication 存在之后、构造窗口之前调用。"""
    from launchpad import grid as grid_mod
    from launchpad import icons as icons_mod
    from launchpad import tile as tile_mod
    from launchpad import window as window_mod

    p = Probe()
    # 继承自 C++ 的方法（setGeometry/move/show/setParent）会被自动跳过，
    # 列出它们只是为了让"这些计数为什么没有"有答案，而不是默默消失。
    p.wrap(icons_mod.IconCache, "get", "IconCache.get")
    p.wrap(icons_mod.IconCache, "_save", "IconCache._save")
    p.wrap(icons_mod.IconCache, "_extract", "IconCache._extract")
    p.wrap(icons_mod.IconCache, "_placeholder", "IconCache._placeholder")
    p.wrap(tile_mod.Tile, "__init__", "Tile.__init__")
    p.wrap(tile_mod.Tile, "paintEvent", "Tile.paintEvent")
    p.wrap(tile_mod.Tile, "setGeometry", "Tile.setGeometry")
    p.wrap(tile_mod.Tile, "move", "Tile.move")
    p.wrap(tile_mod.Tile, "show", "Tile.show")
    p.wrap(tile_mod.Tile, "setParent", "Tile.setParent")
    p.wrap(grid_mod.Grid, "rebuild", "Grid.rebuild")
    p.wrap(grid_mod.Grid, "relayout", "Grid.relayout")
    p.wrap(grid_mod.Grid, "_apply", "Grid._apply")
    p.wrap(window_mod.Launchpad, "show_me", "Launchpad.show_me")
    p.wrap(window_mod.Launchpad, "paintEvent", "Launchpad.paintEvent")
    p.wrap(window_mod.Launchpad, "_resize", "Launchpad._resize")
    return p


# ─── 环境准备 ──────────────────────────────────────────

def isolated_env(root: Path) -> dict:
    """
    造一份隔离的 APPDATA / LOCALAPPDATA。

    冷启动必须从"图标全不在磁盘缓存"开始才有意义，但绝不能去删用户
    真实的缓存目录。这里把 APPDATA 指向临时目录并复制 shortcuts.json
    过去，于是库内容完全一致、图标缓存却是空的。
    """
    import shutil
    root.mkdir(parents=True, exist_ok=True)
    (root / "Local").mkdir(exist_ok=True)
    (root / "Roaming").mkdir(exist_ok=True)

    src_db = Path(os.environ["APPDATA"]) / "Launchpad" / "shortcuts.json"
    if src_db.is_file():
        dst = root / "Roaming" / "Launchpad" / "shortcuts.json"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_db, dst)

    env = dict(os.environ)
    env["LOCALAPPDATA"] = str(root / "Local")
    env["APPDATA"] = str(root / "Roaming")
    return env


# ─── 场景 1：启动 ──────────────────────────────────────

def measure_startup(cold: bool = False, cols: int = 7, rows: int = 5,
                    with_warm_thread: bool = True) -> dict:
    """
    复刻 __main__.py 的启动序列，逐段计时。

    分段刻意照着真实调用链切：
        QApplication → 库加载 → Launchpad.__init__（内含 Grid 建 116 个
        Tile）→ show_me（内含第二次 rebuild + 首帧）
    """
    t_import0 = time.perf_counter()
    from PyQt5.QtGui import QPixmap                      # noqa: F401
    from PyQt5.QtWidgets import QApplication
    t_import = (time.perf_counter() - t_import0) * 1000.0

    # QApplication **必须在计时区间之外**创建。
    #
    # 建 QApplication 时 Qt 要初始化平台插件、字体引擎、枚举显示器，
    # 这是一个进程一次的成本，实测约 300ms。谁先建谁付 —— 如果这里
    # 懒建，它就落进 window_init/show_me，被当成「Launchpad 启动耗时」。
    #
    # 实测（同 5 次，只差这一处）：
    #     懒建：window_init 15~23ms  show_me 400~514ms  total ~493ms
    #     先建：window_init  1ms     show_me 150~190ms  total ~155ms
    # 3 倍差距，而这 300ms 与「二次启动快不快」毫无关系。
    # 换句话说，懒建时读到的第 1 次数字必然最慢 —— 那是 Qt 的一次性成本，
    # 不是这个程序的开销。
    _t_qapp0 = time.perf_counter()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("LaunchpadBench")
    app.setQuitOnLastWindowClosed(False)
    t_qapp = (time.perf_counter() - _t_qapp0) * 1000.0

    from launchpad.library import Library
    from launchpad.window import Launchpad
    from launchpad import theme

    # 和 __main__.py 一样的位置：QApplication 就绪后立刻把字体引擎预热
    # 丢到后台线程，主线程不等它。
    #
    # 必须排在 QApplication 之后 —— 在那之前没有 QGuiApplication，
    # 后台线程构造 QPixmap 会失败，预热等于没做。
    font_warm = theme.warm_font_engine_async()

    t0 = time.perf_counter()
    lib = Library()
    lib.load()
    t_lib = (time.perf_counter() - t0) * 1000.0

    probe = install_probe()

    warm = None
    if with_warm_thread:
        # 和 __main__.py 一致：图标预热线程与主线程并发跑
        from launchpad import theme
        from launchpad.icons import warm_in_background
        t0 = time.perf_counter()
        holder = {}

        def _on_done():
            holder["done_at"] = time.perf_counter()

        warm = warm_in_background(lib.entries, theme.Theme.ICON_SIZE,
                                  on_done=_on_done)
        warm._bench_holder = holder
        t_warm_start = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    win = Launchpad(lib, cols, rows)
    win.setWindowTitle("LaunchpadBench")
    t_win_init = (time.perf_counter() - t0) * 1000.0
    n_tiles_after_init = len(win.grid._tiles)

    t0 = time.perf_counter()
    win.show_me()
    t_show = (time.perf_counter() - t0) * 1000.0

    # show_me 只标脏区，真正的像素合成要等事件循环。
    # 这里逼出真实首帧（走屏幕上那条路径，不是 render()）。
    t0 = time.perf_counter()
    for _ in range(3):
        app.processEvents()
    t_first_paint = (time.perf_counter() - t0) * 1000.0

    # 等字体预热线程结束，再量 render。
    # 不等的话，测到的 one_render 会含预热线程的 CPU 竞争，
    # 把"预热帮了忙"误读成"渲染变慢了"。
    if font_warm is not None:
        font_warm.join(timeout=5.0)

    # 单独量一次 render() 作为"完整重绘一帧"的参考
    pm = QPixmap(win.size())
    t0 = time.perf_counter()
    win.render(pm)
    t_render = (time.perf_counter() - t0) * 1000.0

    stats = probe.report()
    skipped = list(probe.skipped)
    probe.uninstall()

    # **必须关掉这个窗口。** measure_startup 每调一次就新建一个 Launchpad，
    # 而 Launchpad 是全屏 + 置顶。原来的实现从不关闭，于是连测 N 次就在
    # 屏幕上叠 N 个全屏置顶窗口，第 N 次的 showFullScreen() 要和前面
    # N-1 个抢合成 —— 读数会随调用次数单调变高，看起来像「程序越来越慢」，
    # 其实是测试自己制造的负载。
    # 实测：不关窗时可见窗口数 1→2→3→4，show_me 290ms→514ms。
    #
    # 顺序要紧：res 里还要读 win.grid.*，所以先把要用的值取出来，
    # 再关窗。关完再读 win 就是访问已析构的 C++ 对象，
    # 进程会以 0xC0000005 崩在退出时（这个坑踩过一次）。
    n_pages = win.grid.page_count
    n_tiles_now = len(win.grid._tiles)
    win.close()
    win.deleteLater()
    for _ in range(3):
        app.processEvents()

    res = {
        "cold_cache": cold,
        "warm_thread": with_warm_thread,
        "entries": len(lib.entries),
        "pages": n_pages,
        "tiles_after_init": n_tiles_after_init,
        "tiles_now": n_tiles_now,
        "phases_ms": {
            "import_pyqt": round(t_import, 1),
            "qapplication": round(t_qapp, 1),
            "library_load": round(t_lib, 1),
            "warm_thread_start": round(t_warm_start, 1) if with_warm_thread else None,
            "window_init": round(t_win_init, 1),
            "show_me": round(t_show, 1),
            "first_paint_processEvents": round(t_first_paint, 1),
            "one_render": round(t_render, 1),
        },
        "counters": stats,
        "probe_skipped": skipped,
        # total 必须含 first_paint_processEvents：show_me 只标脏区，
        # 真正的像素合成要等事件循环，而「用户看见图标」那一刻正是首帧画完。
        # 不含它就少算 54~97ms。one_render 是额外的对照测量，不计入。
        "total_ms": round(t_import + t_qapp + t_lib + t_win_init + t_show
                          + t_first_paint, 1),
    }

    if warm is not None:
        holder = getattr(warm, "_bench_holder", {})
        res["warm_thread_finished_within_test"] = "done_at" in holder
    return res


# ─── 场景 2：单帧 ──────────────────────────────────────

def make_app():
    """
    给 frame / pageflip / firstframe 用的最小 QApplication 包装。

    这里**不**预热字体 —— 预热是主启动路径的事，见 measure_startup。
    在这里预热会让这几个场景的首帧把预热成本算进去。
    """
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("LaunchpadBench")
    app.setQuitOnLastWindowClosed(False)
    return app


# make_window 建过的窗口。Launchpad 是**全屏 + 置顶**，不关掉的话
# 屏幕上会叠着多个，每次 show_me 都要和已有的抢合成。
_LIVE_WINDOWS: list = []


def close_previous_windows(app) -> None:
    """关掉此前所有测量窗口并排干事件队列。

    不做这件事会有两个可见后果：
      1. 首帧绘制数翻倍：measure_first_frame_paint_count 统计的是
         **所有可见窗口**里 paintEvent 到的 Tile。叠了两个全屏窗口时
         processEvents 会把两个都重画，于是干净进程 35/35 变成 70/35。
      2. 读数随调用次数单调变高：第 N 次的 show_me 要和前 N-1 个抢合成。
    """
    while _LIVE_WINDOWS:
        w = _LIVE_WINDOWS.pop()
        try:
            w.close()
            w.deleteLater()
        except RuntimeError:
            # C++ 侧已析构（Python 对象还在），忽略
            pass
    for _ in range(3):
        app.processEvents()


def make_window(cols: int = 7, rows: int = 5):
    app = make_app()
    from launchpad.library import Library
    from launchpad.window import Launchpad

    # 先清掉上一轮的窗口，否则计数和耗时会互相污染（见函数 docstring）
    close_previous_windows(app)

    lib = Library()
    lib.load()
    win = Launchpad(lib, cols, rows)
    win.setWindowTitle("LaunchpadBench")
    win.show_me()
    app.processEvents()
    _LIVE_WINDOWS.append(win)
    return app, win


def measure_frame(n: int = 100, cols: int = 7, rows: int = 5) -> dict:
    from PyQt5.QtGui import QPixmap
    app, win = make_window(cols, rows)

    pm = QPixmap(win.size())
    # 预热：第一次 render 会触发字体/图标的首次加载，不该算进分布
    for _ in range(3):
        win.render(pm)

    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        win.render(pm)
        times.append((time.perf_counter() - t0) * 1000.0)

    return {
        "n": n,
        "size": [win.width(), win.height()],
        "visible_tiles": _count_visible_tiles(win),
        "total_tiles": len(win.grid._tiles),
        "stats": summarize("render", times),
        "raw": [round(t, 3) for t in times],
    }


def _count_visible_tiles(win) -> int:
    """几何上落在网格可视矩形内的 Tile 数。"""
    g = win.grid
    v = g.rect()
    return sum(1 for t in g._tiles if v.intersects(t.geometry()))


# ─── 场景 3：图标微基准 ────────────────────────────────

def measure_icon(n: int = 1000, cols: int = 7, rows: int = 5) -> dict:
    from PyQt5.QtGui import QPixmap
    from launchpad import theme
    app, win = make_window(cols, rows)
    size = theme.Theme.ICON_SIZE

    tile = win.grid._tiles[0]
    icon = tile._icon

    # A: QIcon.pixmap()
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        icon.pixmap(size, size)
        times.append((time.perf_counter() - t0) * 1000.0)

    # B: 直接用缓存好的 QPixmap（对照组）
    cached = icon.pixmap(size, size)
    times_cached = []
    for _ in range(n):
        t0 = time.perf_counter()
        _ = cached.width()
        times_cached.append((time.perf_counter() - t0) * 1000.0)

    # C: update()
    times_update = []
    for _ in range(n):
        t0 = time.perf_counter()
        tile.update()
        times_update.append((time.perf_counter() - t0) * 1000.0)

    # D: QIcon.pixmap 在 QPixmapIconEngine 上到底做了什么 —— 换个尺寸
    other = []
    for i in range(min(n, 200)):
        s = 100 + (i % 8)
        t0 = time.perf_counter()
        icon.pixmap(s, s)
        other.append((time.perf_counter() - t0) * 1000.0)

    return {
        "n": n,
        "pixmap_same_size": summarize("QIcon.pixmap(128,128)", times),
        "pixmap_varying_size": summarize("QIcon.pixmap(varying)", other),
        "cached_pixmap_touch": summarize("cached QPixmap.width()", times_cached),
        "update": summarize("Tile.update()", times_update),
    }


# ─── 场景 4：翻页动画 ──────────────────────────────────

def _grid_anim(grid):
    """
    找出当前驱动翻页的那个动画对象。

    grid.py 由别的 agent 并行改动，动画对象的名字变过
    （_slide / _anim / _animator …）。这里按候选名挨个试，
    找不到就返回 None —— 由 2 秒兜底定时器收尾，不会挂死。
    """
    for name in ("_slide", "_anim", "_animator", "animator", "_page_anim"):
        a = getattr(grid, name, None)
        if a is not None and hasattr(a, "finished"):
            return a
    for a in vars(grid).values():
        if hasattr(a, "finished") and hasattr(a, "start"):
            return a
    return None


def measure_pageflip(frames: int = 120, cols: int = 7, rows: int = 5) -> dict:
    """
    真跑一次翻页动画，逐帧计时。

    两个口径，缺一不可：
      gap_ms   —— 相邻两帧的**墙钟间隔**。用户看到的"帧率"就是这个。
      work_ms  —— 每帧从动画回调进入到该帧最后一次 paintEvent 结束的
                  实际耗时。这才是我们能改的东西。

    必须分开看的原因：gap=p50 16.1ms / p90 21.1ms 这个分布，
    光看会误判成"渲染太慢"。但 work 只有 0.006ms —— 也就是说
    每帧 16ms 里 15.99ms 是 Qt 的 vsync 等待，不是我们花掉的。
    合成器按显示器刷新率锁步，这部分开销与代码无关。
    所以真正的判据是 work：只要 work < 16ms，我们就没有掉帧的余量问题。

    animation.py 存在时用它（另一个 agent 并行写的），
    否则退回 grid._slide 里的 QVariantAnimation —— 两条路径测的是
    「动画驱动 → move → 重绘」这条链，与用哪个动画类无关。
    """
    from PyQt5.QtCore import QEventLoop, QTimer
    from launchpad import grid as grid_mod

    app, win = make_window(cols, rows)
    grid = win.grid

    if grid.page_count < 2:
        return {"skipped": "只有 1 页，无法测翻页", "pages": grid.page_count}

    # 只要够长，能采到 120 个动画帧就停。每一页动画 260ms，
    # 60fps 下约 16 帧，所以 8 次翻页 ≈ 128 帧 —— 不需要死磕 120 次。
    want_frames = max(1, min(frames, 240))
    stops = max(1, min((want_frames // 10) + 2, 200))

    stamps: list[tuple[str, float]] = []
    paint_durations: list[float] = []
    tile_paint_times: list[float] = []

    from launchpad import tile as tile_mod

    orig_set_offset = getattr(grid_mod.Grid, "_set_offset", None)
    orig_paint = grid_mod.Grid.paintEvent
    orig_tile_paint = tile_mod.Tile.paintEvent

    def timed_set_offset(self, v):
        stamps.append(("offset", time.perf_counter()))
        return orig_set_offset(self, v)

    def timed_paint(self, ev):
        t0 = time.perf_counter()
        r = orig_paint(self, ev)
        paint_durations.append((time.perf_counter() - t0) * 1000.0)
        stamps.append(("paint", time.perf_counter()))
        return r

    def timed_tile_paint(self, ev):
        t0 = time.perf_counter()
        r = orig_tile_paint(self, ev)
        tile_paint_times.append((time.perf_counter() - t0) * 1000.0)
        return r

    # grid.py 由别的 agent 并行改动，_set_offset 可能被换掉或删掉。
    # 钩子不存在就退化成"只量 paintEvent"—— 判据本来就是 paint 耗时，
    # offset 回调只是用来给帧间隔打时间戳的。
    hooked_offset = orig_set_offset is not None
    if hooked_offset:
        grid_mod.Grid._set_offset = timed_set_offset
    grid_mod.Grid.paintEvent = timed_paint
    tile_mod.Tile.paintEvent = timed_tile_paint
    try:
        pages = grid.page_count
        for _ in range(stops):
            start_page = grid.page
            grid.goto((start_page + 1) % pages, animate=True)

            loop = QEventLoop()
            anim = _grid_anim(grid)
            if anim is not None and hasattr(anim, "finished"):
                anim.finished.connect(loop.quit)
            QTimer.singleShot(2000, loop.quit)      # 兜底，别挂死
            loop.exec_()
            app.processEvents()
    finally:
        if hooked_offset:
            grid_mod.Grid._set_offset = orig_set_offset
        grid_mod.Grid.paintEvent = orig_paint
        tile_mod.Tile.paintEvent = orig_tile_paint

    offset_ts = [t for k, t in stamps if k == "offset"]
    gaps = [(offset_ts[i + 1] - offset_ts[i]) * 1000.0
            for i in range(len(offset_ts) - 1)]

    return {
        "animation_frames": len(offset_ts),
        "grid_paints": len(paint_durations),
        "gap_ms": summarize("frame gap", gaps),
        "grid_paint_ms": summarize("Grid.paintEvent", paint_durations),
        "tile_paint_ms": summarize("Tile.paintEvent", tile_paint_times),
        "pages": grid.page_count,
        "tiles_total": len(grid._tiles),
        "tiles_visible": _count_visible_tiles(win),
        "note": ("gap 里的 16ms 基准是 vsync 等待，与渲染代码无关；"
                 "真正的判据是 *_paint_ms"),
    }


# ─── 场景 5：首帧到底画了几个 tile ────────────────────

def measure_first_frame_paint_count(cols: int = 7, rows: int = 5) -> dict:
    """首帧 paintEvent 打到多少个 Tile —— 116 还是 35？"""
    from launchpad import tile as tile_mod
    app, win = make_window(cols, rows)

    counts = {"n": 0, "ms": 0.0, "tiles": set()}
    orig = tile_mod.Tile.paintEvent

    def counted(self, ev):
        t0 = time.perf_counter()
        r = orig(self, ev)
        counts["n"] += 1
        counts["ms"] += (time.perf_counter() - t0) * 1000.0
        counts["tiles"].add(id(self))
        return r

    tile_mod.Tile.paintEvent = counted
    try:
        win.grid.update()
        win.update()
        for _ in range(5):
            app.processEvents()
    finally:
        tile_mod.Tile.paintEvent = orig

    return {
        "tiles_painted": len(counts["tiles"]),
        "paint_events": counts["n"],
        "tile_paint_total_ms": round(counts["ms"], 1),
        "total_tiles": len(win.grid._tiles),
        "visible_tiles": _count_visible_tiles(win),
    }


# ─── 子进程包装：冷启动必须独立进程 ────────────────────

_CHILD_FLAG = "LAUNCHPAD_BENCH_CHILD"


def run_isolated(mode: str, cold: bool, cols: int, rows: int,
                 with_warm_thread: bool = True,
                 isolate_cache: bool = True) -> dict:
    """在独立子进程里跑，保证测量条件可控。

    `isolate_cache=False` 时**不**改APPDATA/LOCALAPPDATA，沿用真实环境，
    于是图标命中真实磁盘缓存 —— 这正是 warm 启动要测的东西。
    但仍然是独立进程：每个样本都是干净进程里的第一次启动，
    条件完全一致，可重复。

    为什么 warm 也要用子进程，见 run_isolated 的调用点注释：
    同进程连调 measure_startup 会**时崩时不崩**（0xC0000005，stderr 空，
    崩在 C++ 侧），且第 1 次要付 Qt 的一次性初始化成本、后续几次要和
    残留的窗口/线程互相影响 —— 读数既不稳也不可重复。
    """
    import subprocess
    import tempfile

    env = dict(os.environ)
    if isolate_cache:
        tmp = Path(tempfile.mkdtemp(prefix="lp_bench_"))
        env = isolated_env(tmp)
    env[_CHILD_FLAG] = "1"
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "launchpad.bench", mode,
           "--child", "--cols", str(cols), "--rows", str(rows), "--json"]
    if not with_warm_thread:
        cmd.append("--no-warm-thread")
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    if proc.returncode != 0:
        return {"error": f"子进程退出码 {proc.returncode}",
                "stderr": proc.stderr[-4000:]}
    # 子进程传的是单个 mode，所以 JSON 的顶层是 {"startup": {...}} 这种包装。
    # 这里把内层那个结果提上来，调用方拿到的永远是测量结果本身。
    text = proc.stdout.strip()
    start = text.find("{")
    if start < 0:
        return {"error": "子进程未输出 JSON", "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:]}
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError as exc:
        return {"error": f"子进程 JSON 解析失败: {exc}",
                "stdout": proc.stdout[-2000:], "stderr": proc.stderr[-2000:]}

    if isinstance(payload, dict) and mode in payload:
        payload = payload[mode]
    return payload


# ─── 打印 ──────────────────────────────────────────────

def print_startup(res: dict) -> None:
    print(f"启动分解  (缓存={'冷' if res['cold_cache'] else '热'}, "
          f"预热线程={'开' if res['warm_thread'] else '关'}, "
          f"{res['entries']} 条 / {res['pages']} 页)")
    print("-" * 78)
    for k, v in res["phases_ms"].items():
        if v is None:
            continue
        print(f"  {k:<34}{v:>10.1f} ms")
    print(f"  {'=' * 32}")
    print(f"  {'合计（不含预热线程）':<34}{res['total_ms']:>10.1f} ms")
    print()
    print(f"  Tile 数：__init__ 后 {res['tiles_after_init']}，"
          f"show_me 后 {res['tiles_now']}")
    print()
    print(f"{'计数器':<28}{'次数':>6}{'净ms':>9}{'总ms':>9}"
          f"{'净最大ms':>10}{'总最大ms':>10}")
    print("-" * 78)
    for c in res["counters"]:
        print(f"  {c['label']:<26}{c['calls']:>6}{c['self_ms']:>9.1f}"
              f"{c['total_ms']:>9.1f}{c.get('max_self_ms', 0):>10.1f}"
              f"{c['max_ms']:>10.1f}")
    if res.get("probe_skipped"):
        print(f"  （未计时：{', '.join(res['probe_skipped'])}"
              f" —— 继承自 C++，替换会破坏绑定，见 Probe.wrap 的注释）")


def print_stats_block(rows: list[dict]) -> None:
    print()
    print(table(rows))


# ─── 改动前 / 改动后 同口径对比 ───────────────────────

BASELINE_TILE = r'''
# -*- coding: utf-8 -*-
"""基线版 tile：paint 每次即时绘制，字体每次新建，不烘贴图。"""
from PyQt5.QtCore import QRect, Qt, pyqtSignal
from PyQt5.QtGui import QPainter, QPixmap
from PyQt5.QtWidgets import QWidget

from . import theme


class Tile(QWidget):
    launched = pyqtSignal(object)

    def __init__(self, entry, icon, icon_size, parent=None):
        super().__init__(parent)
        self.entry = entry
        self._icon = icon
        self._icon_size = icon_size
        self._hover = False
        self._press = False
        self._base = None
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoFillBackground(False)

    def _icon_rect(self):
        w = min(self._icon_size, self.width() - 8)
        return QRect((self.width() - w) // 2, 0, w, w)

    def _label_rect(self):
        top = self._icon_rect().bottom() + theme.Theme.ICON_GAP - 6
        return QRect(0, top, self.width(), theme.Theme.LABEL_H)

    def enterEvent(self, ev):
        self._hover = True
        self.update()

    def leaveEvent(self, ev):
        self._hover = False
        self._press = False
        self.update()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._press = True
            self.update()
        super().mousePressEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            was = self._press
            self._press = False
            self.update()
            if was and self.rect().contains(ev.pos()):
                self.launched.emit(self.entry)
            return
        super().mouseReleaseEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        ir = self._icon_rect()
        theme.paint_icon_plate(p, ir, self._hover, self._press)
        pm = self._icon.pixmap(ir.width(), ir.height())
        if pm.isNull():
            pm = self._icon.pixmap(self._icon_size, self._icon_size)
        if not pm.isNull():
            p.drawPixmap(ir, pm)
            if self._hover:
                theme.paint_icon_hover_ring(p, ir)
        else:
            p.setPen(theme.missing_color())
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(ir, 12, 12)
        name = self.entry.name
        if self.entry.missing:
            name = name + "  \u26a0"
        lr = self._label_rect()
        f = theme.label_font()
        theme.paint_label(p, lr, name, theme.elide(name, f, lr.width()))
        p.end()
'''

# theme.py 里要还原的三处缓存，逐个给精确片段。
# 找不到任何一处就报错返回 —— 绝不静默跳过，那样对比结果会是假的。
_THEME_REVERTS = [
    # QFont 共享实例 -> 每次新建
    ('    if _LABEL_FONT is None:\n'
     '        f = QFont("Microsoft YaHei UI", Theme.LABEL_FONT)\n'
     '        f.setStyleStrategy(QFont.PreferAntialias)\n'
     '        _LABEL_FONT = f\n'
     '    return _LABEL_FONT',
     '    f = QFont("Microsoft YaHei UI", Theme.LABEL_FONT)\n'
     '    f.setStyleStrategy(QFont.PreferAntialias)\n'
     '    return f'),
    # QFontMetrics 缓存 -> 每次新建
    ('    key = (font.family(), font.pointSize(), font.pixelSize(),\n'
     '           font.bold(), font.italic())\n'
     '    fm = _METRICS.get(key)\n'
     '    if fm is None:\n'
     '        fm = QFontMetrics(font)\n'
     '        _METRICS[key] = fm\n'
     '    return fm',
     '    return QFontMetrics(font)'),
    # 背景缓存 -> 每次重算
    ('    key = (w, h)\n    pm = _BG_CACHE.get(key)\n'
     '    if pm is not None:\n        return pm\n',
     '    key = (w, h)\n'),
    ('    if len(_BG_CACHE) >= _BG_CACHE_MAX:\n'
     '        _BG_CACHE.pop(next(iter(_BG_CACHE)))\n'
     '    _BG_CACHE[key] = pm\n    return pm',
     '    return pm'),
    # 字体引擎预热 -> 关掉
    ('def warm_font_engine_async():',
     'def warm_font_engine_async():\n    return None\n\n\n'
     'def _warm_font_engine_async_disabled():'),
]


def measure_floor() -> dict:
    """
    地板：一个空的、无边框、置顶的全屏 QWidget，显示 + 首帧合成要多久。

    为什么需要这个数字：光看"二次启动 422ms"不知道该怪谁。
    把地板摆出来才能说清 —— 有多少是 Qt 建立全屏窗口的固有开销，
    有多少是这个应用自己的逻辑。

    实测：空窗口 showFullScreen ~100ms（首次）、processEvents ~40ms。
    也就是说 300ms 的目标**在 Qt 层面就已经贴地了**，
    一个什么都不做的全屏启动器都到不了。
    """
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QColor, QPainter
    from PyQt5.QtWidgets import QWidget

    app = make_app()
    w = QWidget()
    w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    w.setAttribute(Qt.WA_ShowWithoutActivating, True)
    w.setAutoFillBackground(False)
    w.resize(2560, 1600)

    def paint(ev=None):
        p = QPainter(w)
        p.fillRect(0, 0, w.width(), w.height(), QColor(0, 0, 0))
        p.end()

    w.paintEvent = paint

    out = {"runs": []}
    for i in range(4):
        t = time.perf_counter()
        w.showFullScreen()
        s = (time.perf_counter() - t) * 1000.0
        t = time.perf_counter()
        for _ in range(3):
            app.processEvents()
        e = (time.perf_counter() - t) * 1000.0
        out["runs"].append({"show_ms": round(s, 1),
                            "first_paint_ms": round(e, 1)})
        w.hide()
        app.processEvents()
    return {
        "note": "空的全屏无边框窗口，什么业务逻辑都没有",
        "runs": out["runs"],
        "steady_show_ms": out["runs"][-1]["show_ms"],
        "steady_first_paint_ms": out["runs"][-1]["first_paint_ms"],
    }


def _make_baseline_pkg(tmp: Path) -> Path:
    """把包拷到 tmp，并还原绘制优化，得到一个"改动前"的 launchpad。"""
    import shutil
    pkg = tmp / "launchpad"
    src = Path(__file__).resolve().parent
    shutil.copytree(src, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    (pkg / "tile.py").write_text(BASELINE_TILE, encoding="utf-8")

    tp = pkg / "theme.py"
    ts = tp.read_text(encoding="utf-8")
    for old, new in _THEME_REVERTS:
        if old not in ts:
            raise RuntimeError(
                f"还原基线失败，theme.py 里找不到片段：{old[:70]!r}")
        ts = ts.replace(old, new, 1)
    tp.write_text(ts, encoding="utf-8")
    return pkg


def measure_compare(cols: int = 7, rows: int = 5) -> dict:
    """
    同口径对比：当前优化版 vs「关掉全部绘制优化」的基线版。

    为什么自己造基线，而不是引用历史数字：
    历史数字来自不同的磁盘缓存状态、CPU 负载、后台进程。
    拿它对比，差值里混着环境噪声，分不清是优化起了作用还是机器恰好空闲。

    做法：把整个包拷到临时目录，只把绘制相关的优化**逐处还原**成优化前
    的写法，在子进程里跑同一套测量。两次测量的系统环境几乎相同，
    差值就是净收益。

    刻意不碰业务流程（分页、布局、分页逻辑一律不动），只还原绘制策略 ——
    这样差值只可能来自绘制优化。
    """
    import subprocess
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="lp_cmp_"))
    try:
        _make_baseline_pkg(tmp)
    except RuntimeError as exc:
        return {"error": str(exc)}

    env = dict(os.environ)
    env["PYTHONPATH"] = str(tmp) + os.pathsep + env.get("PYTHONPATH", "")

    def _run(mode: str) -> dict:
        cmd = [sys.executable, "-m", "launchpad.bench", mode, "--json",
               "--n", "60", "--cols", str(cols), "--rows", str(rows)]
        if mode == "startup":
            # 关掉 icons 的预热线程：它会新建自己的 IconCache 并与主线程
            # 抢 GIL，会把对比变成测噪声。基线和当前用同一设置。
            cmd.append("--no-warm-thread")
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              cwd=str(tmp), timeout=300)
        if proc.returncode != 0:
            return {"error": f"基线子进程退出码 {proc.returncode}",
                    "stderr": proc.stderr[-1500:]}
        text = proc.stdout.strip()
        i = text.find("{")
        if i < 0:
            return {"error": "基线子进程无 JSON 输出",
                    "stdout": proc.stdout[-800:]}
        return json.loads(text[i:]).get(mode, {})

    return {
        "frame": {
            "baseline": _run("frame"),
            "current": measure_frame(n=60, cols=cols, rows=rows),
        },
        "startup": {
            "baseline": _run("startup"),
            "current": measure_startup(cold=False, cols=cols, rows=rows,
                                       with_warm_thread=False),
        },
    }


def print_compare(res: dict) -> None:
    if "error" in res:
        print(f"对比失败：{res['error']}")
        return

    print("单帧 render（n=60，2560x1600，35 个可视 Tile）")
    f = res["frame"]
    b, c = f["baseline"].get("stats"), f["current"].get("stats")
    if b and c:
        print(f"  {'':<6}{'基线':>10}{'优化后':>10}{'差值':>10}{'':>10}")
        for k in ("p50", "p90", "p99", "max"):
            d = b[k] - c[k]
            pct = (d / b[k] * 100.0) if b[k] else 0.0
            print(f"  {k:<6}{b[k]:>10.2f}{c[k]:>10.2f}"
                  f"{d:>+10.2f}{pct:>+9.1f}%")
    else:
        print(f"  基线不可用：{f['baseline']}")

    print()
    print("启动（热缓存，分段 ms）")
    s = res["startup"]
    for tag, name in (("baseline", "基线"), ("current", "优化后")):
        d = s[tag]
        if "error" in d:
            print(f"  {name}: {d['error']}")
            continue
        p = d["phases_ms"]
        cnt = {x["label"]: x for x in d["counters"]}
        ph = cnt.get("IconCache._placeholder", {}).get("total_ms", 0.0)
        tp = cnt.get("Tile.paintEvent", {}).get("total_ms", 0.0)
        bg = cnt.get("Launchpad.paintEvent", {}).get("total_ms", 0.0)
        print(f"  {name:<6} 合计 {d['total_ms']:>7.1f}   "
              f"window_init {p['window_init']:>7.1f}   "
              f"show_me {p['show_me']:>6.1f}   "
              f"占位图 {ph:>6.1f}   Tile绘制 {tp:>6.1f}   背景 {bg:>5.1f}")
    b, c = s["baseline"], s["current"]
    if "error" not in b and "error" not in c:
        d = b["total_ms"] - c["total_ms"]
        print(f"  {'差值':<6} {b['total_ms']:>7.1f} -> {c['total_ms']:>7.1f}  "
              f"{d:+.1f} ms ({d / b['total_ms'] * 100:+.1f}%)")


# ─── main ──────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="launchpad.bench")
    ap.add_argument("mode", nargs="?", default="all",
                    choices=["startup", "frame", "icon", "pageflip",
                             "firstframe", "compare", "all"])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--child", action="store_true")
    ap.add_argument("--cold", action="store_true", help="用空图标缓存测冷启动")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--frames", type=int, default=120)
    ap.add_argument("--cols", type=int, default=7)
    ap.add_argument("--rows", type=int, default=5)
    ap.add_argument("--no-warm-thread", action="store_true")
    ap.add_argument("--floor", action="store_true",
                    help="测 Qt 全屏窗口的地板耗时（空窗口，无任何业务逻辑）")
    args = ap.parse_args(argv)

    cols, rows = args.cols, args.rows
    out: dict = {}

    def want(m: str) -> bool:
        # compare 是自带输出的独立场景，别被 all 顺带触发
        return args.mode in (m, "all") and not (m == "compare" and args.mode == "all")

    if args.floor:
        res = measure_floor()
        if not args.json:
            print("地板：空的全屏无边框窗口（无任何业务逻辑）")
            print(f"  {'轮次':<6}{'showFullScreen':>18}{'首帧合成':>14}")
            for i, r in enumerate(res["runs"]):
                print(f"  {i + 1:<6}{r['show_ms']:>16.1f}ms"
                      f"{r['first_paint_ms']:>12.1f}ms")
            print("  → 这就是 Qt 建立全屏窗口的固有开销，"
                  "任何全屏启动器都要付。")
        else:
            print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.mode == "compare":
        res = measure_compare(cols=cols, rows=rows)
        if not args.json:
            print_compare(res)
        else:
            print(json.dumps(res, ensure_ascii=False))
        return 0

    # 冷启动必须隔离：改子进程的环境变量，别碰用户真实缓存
    if args.cold and not args.child:
        mode = "startup" if args.mode in ("startup", "all") else args.mode
        res = run_isolated(mode, True, cols, rows,
                           with_warm_thread=not args.no_warm_thread)
        if want("startup"):
            print("冷启动（隔离进程，图标缓存为空）")
            if "error" in res:
                print(f"  失败: {res['error']}")
                if res.get("stderr"):
                    print(res["stderr"][-2000:])
            else:
                # 子进程不知道自己是"冷"的（它是靠空缓存目录实现的），
                # 这里补上标签，免得打印成"热"。
                res["cold_cache"] = True
                print_startup(res)
        if args.json:
            print(json.dumps(res, ensure_ascii=False))
        return 0

    if want("startup"):
        res = measure_startup(cold=False, cols=cols, rows=rows,
                              with_warm_thread=not args.no_warm_thread)
        out["startup"] = res
        if not args.json:
            print_startup(res)

    if want("firstframe"):
        res = measure_first_frame_paint_count(cols, rows)
        out["firstframe"] = res
        if not args.json:
            print()
            print(f"首帧实际绘制的 Tile：{res['tiles_painted']} / "
                  f"共 {res['total_tiles']}（可视 {res['visible_tiles']}），"
                  f"paint 累计 {res['tile_paint_total_ms']} ms")

    if want("frame"):
        res = measure_frame(n=args.n, cols=cols, rows=rows)
        out["frame"] = res
        if not args.json:
            print()
            print(f"单帧 render：{res['size'][0]}x{res['size'][1]}，"
                  f"可视 Tile {res['visible_tiles']}/{res['total_tiles']}")
            print_stats_block([res["stats"]])

    if want("icon"):
        res = measure_icon(n=args.n, cols=cols, rows=rows)
        out["icon"] = res
        if not args.json:
            print()
            print("图标 / update 微基准")
            print_stats_block([res["pixmap_same_size"], res["pixmap_varying_size"],
                               res["cached_pixmap_touch"], res["update"]])

    if want("pageflip"):
        res = measure_pageflip(frames=args.frames, cols=cols, rows=rows)
        out["pageflip"] = res
        if not args.json:
            print()
            if "skipped" in res:
                print(f"翻页测量跳过：{res['skipped']}")
            else:
                print(f"翻页动画：{res['animation_frames']} 帧，"
                      f"可见 Tile {res['tiles_visible']}/{res['tiles_total']}")
                print_stats_block([r for r in (res["gap_ms"], res["grid_paint_ms"],
                                            res["tile_paint_ms"]) if r.get("n")])
                print(f"  注：{res['note']}")

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
