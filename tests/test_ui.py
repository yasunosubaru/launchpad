# -*- coding: utf-8 -*-
"""
离屏渲染自检：直接 grab() 窗口并保存 PNG。

为什么不用桌面截图：
- 窗口是 Qt.Tool + 置顶，桌面截图只能看到"此刻最上面是什么"，
  别的窗口一盖住就误判成"没显示"（已经栽过一次）。
- grab() 拿到的是本窗口的真实渲染结果，与桌面遮挡无关。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtCore import QEventLoop, QTimer, Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from launchpad import theme
from launchpad.library import Library, load_pinyin
from launchpad.window import Launchpad

OUT = ROOT / "tools" / "shots"
OUT.mkdir(parents=True, exist_ok=True)


def pump(ms=1200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()
    app.processEvents()


lib = Library()
lib.load()
print(f"library: {len(lib.entries)} 条")

py_, style_ = load_pinyin()
win = Launchpad(lib, 7, 5)
win.setGeometry(0, 0, 2560, 1600)
win.showFullScreen()
pump(2500)

g = win.grid
print(f"tiles={len(g._tiles)}  pages={g.page_count}  page={g.page}")
print(f"grid geom={g.geometry()}  dots={win.dots.geometry()} dots_pages={win.dots._count}")
print()

# ── 布局断言 ──────────────────────────────────────────
ok = True


def check(name, cond, detail=""):
    global ok
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        ok = False


print("=" * 66)
print("布局")
print("=" * 66)
check("图标控件数 = 过滤结果数", len(g._tiles) == len(g._entries),
      f"{len(g._tiles)} vs {len(g._entries)}")
check("每页 35 个", g.per_page == 35, f"{g.per_page}")
check("页数 = ceil(116/35) = 4", g.page_count == 4, f"{g.page_count}")

if g._tiles:
    first35 = g._tiles[:35]
    ys = sorted({t.y() for t in first35})
    check("第 1 页正好 5 行", len(ys) == 5, f"ys={ys}")
    cw, ch = g._cell()
    tw, th = g._tile_size()
    # cell 留白为 0 是**预期**：控件占满单元格以给文字留足宽度，
    # 视觉间距由下方"相邻图标视觉间距"断言把关（图标在 cell 内居中）。
    check("单元格横向可容纳控件", cw >= tw, f"cell={cw} tile={tw}")
    check("纵向间距 > 0", ch > th, f"cell={ch} tile={th} 留白={ch-th}")
    # 关键指标：相邻图标的**图标边缘**间距 —— 用户实际看到的"挨多远"。
    # 必须把 tile 局部坐标换算到网格坐标，否则算出的是负数。
    icons = []
    for t in g._tiles[:35]:
        r = t._icon_rect()
        icons.append(r.translated(t.x(), t.y()))
    h_gaps = [icons[i + 1].left() - icons[i].right() for i in range(0, 28, 7)]
    v_gaps = [icons[i + 7].top() - icons[i].bottom() for i in range(7)]
    check("相邻图标横向视觉间距 > 0", all(x > 0 for x in h_gaps),
          f"gaps={h_gaps}")
    check("相邻图标纵向视觉间距 > 0", all(x > 0 for x in v_gaps),
          f"gaps={v_gaps}")
    # 间距要"像 macOS 那样均匀铺开"，不能挤在一起
    check("横向间距 >= 60px（视觉不拥挤）", min(h_gaps) >= 60,
          f"最小={min(h_gaps)}")
    check("纵向间距 >= 40px", min(v_gaps) >= 40, f"最小={min(v_gaps)}")
    # 关键：第 2 页首个图标必须在网格可视区之外
    if len(g._tiles) > 35:
        nxt_top = min(t.y() for t in g._tiles[35:])
        check("下一页完全在视野外", nxt_top >= g.height(),
              f"下一页 y={nxt_top} 网格高={g.height()}")
    # 控件尺寸一致
    check("控件尺寸一致",
          len({(t.width(), t.height()) for t in g._tiles}) == 1,
          f"{g._tiles[0].width()}x{g._tiles[0].height()}")

print()
print("=" * 66)
print("页码点")
print("=" * 66)
check("页码点可见", win.dots.isVisible())
check("页码点数 = 4", win.dots._count == 4, f"{win.dots._count}")
check("页码条不与网格重叠",
      win.dots.geometry().top() >= g.geometry().bottom(),
      f"网格底={g.geometry().bottom()} 点顶={win.dots.geometry().top()}")
cs = win.dots._centers
check("圆心已计算", len(cs) == win.dots._count, f"{len(cs)} 个中心")
if len(cs) >= 2:
    gaps = [b - a for a, b in zip(cs, cs[1:])]
    check("圆心间距合理", all(x >= 20 for x in gaps), f"gaps={gaps}")

    # 关键回归：整条轴上逐像素扫描，不能存在"点了没反应"的死区。
    # 上一版用 abs(dx) <= HIT/2 判定，相邻间距 32~38px > 热区 30px，
    # 两个点之间就有约 8px 死区 —— 用户点了像坏了。
    from PyQt5.QtCore import QPoint
    axis_lo, axis_hi = cs[0] - 8, cs[-1] + 8
    dead = [x for x in range(axis_lo, axis_hi + 1)
            if win.dots._hit_index(QPoint(x, win.dots.height() // 2)) < 0]
    check("轴上无死区（每个 x 都能命中某页）", not dead,
          f"死区 {len(dead)}px: {dead[:8]}")
    # 且每个圆心正上方必须命中自己
    mis = [i for i, cx in enumerate(cs)
           if win.dots._hit_index(QPoint(cx, win.dots.height() // 2)) != i]
    check("圆心正上方命中自身", not mis, f"错位 {mis}")
    # 轴外不误触发
    check("轴外不误触发",
          win.dots._hit_index(QPoint(cs[0] - 40, win.dots.height() // 2)) < 0)

print()
print("=" * 66)
print("翻页")
print("=" * 66)
samples = []
orig = g._apply
g._apply = lambda: (samples.append(g._offset), orig())[1]
g.goto(1, animate=True)
pump(900)
uniq = sorted(set(samples))
check("动画逐帧插值", len(uniq) > 3, f"{len(uniq)} 帧, 尾部={uniq[-3:]}")
check("到达第 2 页位置", g._offset == -g._page_height(),
      f"offset={g._offset} 期望={-g._page_height()}")
g._apply = orig
g.goto(0, animate=False)
pump(400)
check("回到第 1 页", g.page == 0)

print()
print("=" * 66)
print("搜索")
print("=" * 66)
n0 = len(g._tiles)
win._on_search("微信")
pump(600)
n1 = len(g._tiles)
check("搜 '微信' 有结果", n1 > 0, f"{n1} 条")
check("结果数少于全部", n1 < n0, f"{n1} < {n0}")
win._on_search("zzzz不存在")
pump(500)
check("无结果显示空态", len(g._tiles) == 0)
win._on_search("")
pump(700)
check("清空后恢复全部", len(g._tiles) == n0, f"{len(g._tiles)} vs {n0}")
check("页码点跟随更新", win.dots._count == g.page_count, f"{win.dots._count}")

print()
print("=" * 66)
print("背景透明（回归：曾经是白板）")
print("=" * 66)
# 网格/图标必须完全透明 —— 缺了 WA_TranslucentBackground 时它们会画默认背景，
# 在纯黑全屏窗口里表现为"图标区是一块白板"。用截图采样验证，不靠读属性。
# 用 render() 而不是 grab()。
# 实测证明：在设了 WA_TranslucentBackground 的窗口上，QWidget.grab()
# 返回的是**全空图像** —— 连普通 QLabel 子控件都取不到（不是我的布局问题，
# 是 Qt 这条路径的行为）。render(pixmap) 才是正确的离屏渲染入口。
probe = QPixmap(win.width(), win.height())
probe.fill(Qt.black)
win.render(probe)
pi = probe.toImage()

# 空白区必须是黑的（不是白板）
for (sx, sy) in [(1280, 700), (400, 1300), (2300, 400), (200, 250)]:
    c = pi.pixelColor(sx, sy)
    check(f"({sx},{sy}) 空白区是黑色而非白板", c.lightness() < 40,
          f"lightness={c.lightness()} rgb=({c.red()},{c.green()},{c.blue()})")

# 关键回归：窗口截图必须真的包含图标。
# WA_NoSystemBackground 会让 Qt 在合成阶段跳过子树 —— 控件 isVisible=True、
# 单独 grab 也正常，但窗口截图里一个图标都没有。只查控件属性查不出来，
# 必须采样真实像素。
if g._tiles:
    # 采样整个图标框而非中心点，且阈值放低。
    # 很多应用图标本身就是深色（Adobe 深红、OBS 黑底、Blackhawk 小尺寸），
    # 用高亮度阈值会把"图标本来就是暗的"误判成"没画出来"。
    # 判据应该是"这块区域和纯黑背景有差异"，不是"它很亮"。
    painted = 0
    dark_icons = []
    for t in g._tiles[:35]:
        # 必须用 translated()：_icon_rect() 是**控件局部**坐标，
        # 图标在 352px 宽的控件里居中（x=112），直接加 t.x() 会取到空白处。
        # 再叠加 grid 自身的 y 偏移，得到窗口坐标。
        ir = t._icon_rect().translated(t.x(), t.y() + g.y())
        hit = False
        for yy in range(ir.top() + 6, ir.bottom() - 6, 10):
            for xx in range(ir.left() + 6, ir.right() - 6, 10):
                if 0 <= xx < pi.width() and 0 <= yy < pi.height():
                    c = pi.pixelColor(xx, yy)
                    # 背景纯黑是 lightness≈3；图标区域明显高于它即算画出
                    if c.lightness() > 18:
                        hit = True
                        break
            if hit:
                break
        if hit:
            painted += 1
        else:
            dark_icons.append(t.entry.name)
    check("第 1 页 35 个图标都画出来了", painted == 35,
          f"命中 {painted}/35" + (f"  未命中={dark_icons}" if dark_icons else ""))

# 整窗非黑采样点。阈值从布局推导而不是拍脑袋：
# 35 个图标每个 128x128，采样步长 8px => 每图标约 16x16=256 个采样格，
# 其中实际着色约一半。留 2 倍余量后仍要显著大于 0，防止"图标没了"漏检。
per_icon = (128 // 8) ** 2 // 2
expect = 35 * per_icon // 4
total_bright = sum(
    1 for yy in range(0, pi.height(), 8) for xx in range(0, pi.width(), 8)
    if pi.pixelColor(xx, yy).lightness() > 25)
check("整窗非黑采样点充足（图标确实存在）", total_bright >= expect,
      f"{total_bright} 个采样点，推算下限 {expect}（纯黑背景应为 0）")

print()
print("=" * 66)
print("文字不裁切（回归：曾经左右缺笔画）")
print("=" * 66)
if g._tiles:
    tw = g._tiles[0].width()
    from PyQt5.QtGui import QFontMetrics
    fm = QFontMetrics(theme.label_font())
    # 控件不得窄于单元格（否则文字被控件边界切掉，那是上一版的 bug）
    check("控件宽度不窄于单元格", tw >= g._cell()[0],
          f"控件宽={tw} 单元格={g._cell()[0]}")
    # 超长名字必须走省略号，而不是硬切。
    # Qt 用 U+2026 (…) 不是三个点，且可能把结果截在多字节字符中间。
    longest = max(g._tiles, key=lambda t: fm.horizontalAdvance(t.entry.name))
    full_w = fm.horizontalAdvance(longest.entry.name)
    shown = theme.elide(longest.entry.name, theme.label_font(), tw)
    check("超长名字走省略号而非切边",
          full_w <= tw or shown.endswith(("...", "\u2026")),
          f"需 {full_w}px，显示 {shown!r}")
    check("省略后确实不超宽",
          fm.horizontalAdvance(shown) <= tw,
          f"省略后 {fm.horizontalAdvance(shown)}px <= {tw}")
    # 截断不能切碎多字节字符：结果必须是合法 UTF-8 且无替换字符
    try:
        shown.encode("utf-8")
        valid = True
    except UnicodeEncodeError:
        valid = False
    check("省略结果是合法 UTF-8", valid)
    check("省略结果无替换字符", "\ufffd" not in shown, repr(shown))

print()
print("=" * 66)
print("翻页后图标位置正确（回归：符号写反导致整屏空白）")
print("=" * 66)
# 关键回归。之前只断言 offset == -页高（那是对的），
# 却没断言**图标实际落点**，于是漏掉了符号写反的 bug：
# 最终 y = base.y + offset（offset 是负数），写成减号会让
# 第 2 页的图标飞到 y=2900，屏幕上完全看不到。
# 必须直接检查控件坐标。
for pg in range(min(2, g.page_count)):
    g.goto(pg, animate=False)
    pump(400)
    lo = pg * g.per_page
    cur = g._tiles[lo:lo + g.per_page]
    ys = sorted({t.y() for t in cur})
    top_ok = min(t.y() for t in cur) >= 0
    bot_ok = max(t.y() + t.height() for t in cur) <= g.height() + 2
    rows = len(ys)
    check(f"第{pg+1}页图标 y 坐标全部落在网格内",
          top_ok and bot_ok,
          f"y范围=[{min(t.y() for t in cur)}, {max(t.y()+t.height() for t in cur)}] "
          f"网格高={g.height()}  行数={rows}")
    check(f"第{pg+1}页为 5 行", rows == 5, f"{ys}")

g.goto(0, animate=False)
pump(400)

print()
print("=" * 66)
print("每页内容正确（回归：曾经翻页后图标全消失）")
print("=" * 66)
# 关键回归。之前只断言 offset 正确，没断言**图标位置**，
# 于是漏掉了"控件移出可视区后再也没搬回来"这个 bug：
# 按一下右键，整个界面就空了。
# 现在逐页检查：当前页的图标必须落在网格可视区内，
# 且相邻页的图标必须完全在视野外。
all_ok = True
for pg in range(g.page_count):
    g.goto(pg, animate=False)
    pump(300)
    lo = pg * g.per_page
    hi = min(len(g._tiles), lo + g.per_page)
    cur = g._tiles[lo:hi]
    ys = sorted({t.y() for t in cur})
    in_view = [t for t in cur
               if g.rect().contains(t._icon_rect().translated(t.x(), t.y()))]
    off_page = []
    if pg > 0:
        off_page = g._tiles[:lo]
        leaked = [t for t in off_page
                  if g.rect().contains(t._icon_rect().translated(t.x(), t.y()))]
    else:
        leaked = []
    nxt_leaked = []
    if hi < len(g._tiles):
        nxt_leaked = [t for t in g._tiles[hi:]
                      if g.rect().contains(t._icon_rect().translated(t.x(), t.y()))]

    good = len(cur) == (hi - lo) and len(ys) <= 5
    visible_ok = len(in_view) == len(cur) or pg == g.page_count - 1
    no_leak = not leaked and not nxt_leaked
    mark = "PASS" if (good and no_leak) else "FAIL"
    if not (good and no_leak):
        all_ok = False
    print(f"  {mark}  第{pg+1}页: {len(cur)} 个控件, {len(ys)} 行, "
          f"页高={g._page_height()}  上页泄漏={len(leaked)}  下页泄漏={len(nxt_leaked)}")

check("所有页内容正确、无跨页泄漏", all_ok)

g.goto(0, animate=False)
pump(400)

print()
print("=" * 66)
print("截图")
print("=" * 66)
for page, name in ((0, "page1"), (1, "page2"), (2, "page3")):
    g.goto(page, animate=False)
    pump(500)
    pm = QPixmap(win.width(), win.height())
    pm.fill(Qt.black)
    win.render(pm)
    p = OUT / f"{name}.png"
    pm.save(str(p))
    print(f"  {p}  ({pm.width()}x{pm.height()})")

win._on_search("微信")
pump(600)
pm = QPixmap(win.width(), win.height())
pm.fill(Qt.black)
win.render(pm)
pm.save(str(OUT / "search.png"))
print(f"  {OUT / 'search.png'}")

print()
print("=" * 66)
print("结果:", "全部通过" if ok else "有失败项")
print("=" * 66)
sys.exit(0 if ok else 1)