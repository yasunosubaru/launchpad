# -*- coding: utf-8 -*-
"""
layouts.py 的自检。跑法：

    python -m launchpad.tests_layout        （从项目根目录）
    python launchpad\\tests_layout.py

打印 PASS/FAIL 汇总，退出码非 0 表示有失败项。

断言的分类，以及为什么每类都要有：

- **合法性**：坐标在可视区内、控件之间不重叠、控件尺寸自洽。
  这一类防的是"图标画到屏幕外/互相压住"。
- **分页**：第 k 页全在第 k 页、第 k±1 页完全在可视区外。
  这一类防的是 grid.py 历史上栽过的那两次：页高取成"网格高度"近似值
  导致下一页从底边漏出，以及滑动偏移符号写反导致整屏空白。
- **确定性**：同输入两次调用结果逐字节相同。
  不纯的排布没法测、也没法缓存，动画中途重算就会跳。
- **排序**：断言**具体顺序**而不是"有序"。中文排序的坑全在
  "看起来有序"里（按 Unicode 码点排也满足 sorted()），只断言有序
  等于没测。
"""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from launchpad import layouts as L
from launchpad.library import Entry
from launchpad.settings import Settings


# ── 迷你测试框架 ──────────────────────────────────────────
_TOTAL = 0
_FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global _TOTAL
    _TOTAL += 1
    ok = bool(cond)
    if not ok:
        _FAILED.append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def _raises(exc, fn) -> bool:
    try:
        fn()
    except exc:
        return True
    except Exception:
        return False
    return False


# ── 测试素材 ──────────────────────────────────────────────
def mk_settings(**kw) -> Settings:
    """不碰 %APPDATA% 的 Settings 实例：给一个不存在的路径，
    再把内部字典重置成 SCHEMA 默认值，连 app_dir() 的 mkdir 都不触发。"""
    from launchpad.settings import SCHEMA
    s = Settings(path=Path(os.environ["TEMP"]) /
                 "launchpad-layouts-does-not-exist.json")
    s._data = {k: v[0] for k, v in SCHEMA.items()}
    s.update(kw)
    return s


def mk_entry(name: str, idx: int) -> Entry:
    return Entry(name=name, target=f"C:/apps/{idx}/{name}.exe")


# 116 个，名字里混中文、拉丁、数字、空格、各种长度
_NAMES_116 = [
    "微信", "网易云音乐", "钉钉", "飞书", "知乎", "百度网盘", "阿里云盘",
    "爱奇艺", "夸克浏览器", "WPS Office", "Adobe Photoshop", "VS Code",
    "Visual Studio Code", "7-Zip", "Zoom", "Slack", "Notion", "Obsidian",
    "Figma", "Blender", "Chrome", "Edge", "Firefox", "Thunderbird",
    "Steam", "Epic Games", "Battle.net", "Ubisoft Connect", "Origin",
    "Spotify", "NetEase", "PotPlayer", "VLC", "OBS Studio", "DaVinci Resolve",
    "Premiere Pro", "After Effects", "Illustrator", "Photoshop",
    "Lightroom", "Audition", "Premiere", "Acrobat Reader", "Foxit Reader",
    "Sublime Text", "Notepad++", "Atom", "PyCharm", "IntelliJ IDEA",
    "Android Studio", "Xcode", "Terminal", "PowerShell", "Command Prompt",
    "Git Bash", "Docker Desktop", "Postman", "Insomnia", "DBeaver",
    "Navicat", "TablePlus", "Aseprite", "Krita", "GIMP", "PaintTool SAI",
    "Clip Studio Paint", "Paint", "Photos", "相机", "计算器", "时钟",
    "日历", "邮件", "记事本", "画图", "设置", "控制面板", "任务管理器",
    "资源监视器", "命令提示符", "注册表编辑器", "服务", "事件查看器",
    "远程桌面连接", "字符映射表", "系统信息", "磁盘清理", "磁盘管理",
    "计算机管理", "设备管理器", "声音录制器", "录音机", "截图工具",
    "步骤记录器", "Windows PowerShell", "Windows 终端", "任务栏",
    "OneDrive", "OneNote", "Outlook", "Excel", "Word", "PowerPoint",
    "Access", "Publisher", "Visio", "Project", "Teams", "To Do",
    "Everything", "AutoHotkey", "ShareX", "PicGo", "uTorrent",
    "qBittorrent", "Motrix", "IDM", "Internet Download Manager",
]
assert len(_NAMES_116) >= 116, len(_NAMES_116)

ENTRIES_116 = [mk_entry(n, i) for i, n in enumerate(_NAMES_116[:116])]

# 中文排序断言素材：pinyin 与 GBK 两种排序器给出的顺序**故意不同**，
# 这样两个 collator 都能被真正验到，而不是"碰巧一样"。
SORT_NAMES = [
    "微信", "网易云音乐", "钉钉", "飞书", "知乎", "百度网盘", "阿里云盘",
    "爱奇艺", "WPS Office", "Adobe Photoshop", "VS Code", "7-Zip",
    "Zoom", "item10", "item2", "微信开发者工具",
]
EXPECTED_PY = [
    "7-Zip", "Adobe Photoshop", "item2", "item10", "VS Code", "WPS Office",
    "Zoom",
    "爱奇艺", "阿里云盘", "百度网盘", "钉钉", "飞书", "网易云音乐",
    "微信", "微信开发者工具", "知乎",
]
EXPECTED_GBK = [
    "7-Zip", "Adobe Photoshop", "item2", "item10", "VS Code", "WPS Office",
    "Zoom",
    "阿里云盘", "爱奇艺", "百度网盘", "钉钉", "飞书", "网易云音乐",
    "微信", "微信开发者工具", "知乎",
]

# 2560x1600 屏幕：grid 区域 = 1600 - 122(搜索框区) - 56(页码条) = 1422
W, H = 2560, 1422


# ── 合法性检查 ────────────────────────────────────────────
def rects_overlap(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def assert_legal(res, label: str, *, overlap=True) -> None:
    """对单个结果跑全套合法性断言。返回是否全过。"""
    ok = True
    n = len(res)
    ok &= check(f"{label} 位置数 = 条目数", len(res.positions) == n
                and len(res.local_positions) == n and len(res.sizes) == n,
                f"{n}/{len(res.positions)}")
    ok &= check(f"{label} 页数 = ceil(n/per_page)",
                res.pages == max(1, -(-n // res.per_page)),
                f"n={n} per_page={res.per_page} pages={res.pages}")
    ok &= check(f"{label} 页高等于可视区高度（跨页泄漏的根因）",
                res.page_height == res.viewport[1],
                f"{res.page_height} vs {res.viewport[1]}")
    # 1x1 这种病态尺寸下任何排布都必然溢出，靠夹取兜底钉在原点。
    # 那里 clamp_count > 0 是正确行为，不该判失败。
    if res.viewport[0] >= 200 and res.viewport[1] >= 200:
        ok &= check(f"{label} 没有触发兜底夹取", res.clamp_count == 0,
                    f"clamp_count={res.clamp_count}")
    else:
        check(f"{label} 病态尺寸：夹取兜底已把控件钉在视口内", True,
              f"clamp_count={res.clamp_count}（1x1 之类的尺寸必然溢出）")

    # 逐项：不越界、不被切掉
    worst = None
    for i in range(n):
        x, y, w, h = res.rect_on_page(i)
        if x < 0 or y < 0 or x + w > res.viewport[0] or y + h > res.viewport[1]:
            worst = (i, (x, y, w, h))
            break
    ok &= check(f"{label} 全部控件完整落在可视区内（不被切掉）",
                worst is None, f"越界: {worst}" if worst else "")

    # 分页正确性：第 k 页在第 k 页；第 k±1 页完全在外
    leak_prev = leak_next = wrong_page = 0
    for pg in range(res.pages):
        vw, vh = res.viewport
        shift = pg * res.page_height
        for i in range(n):
            x, y, w, h = res.rect_absolute(i)
            sy = y - shift
            visible = (0 <= sy and sy + h <= vh)
            want = (res.page_of(i) == pg)
            if want and not visible:
                wrong_page += 1
            if not want and visible:
                if res.page_of(i) < pg:
                    leak_prev += 1
                else:
                    leak_next += 1
    ok &= check(f"{label} 本页图标全部可见", wrong_page == 0, f"不可见 {wrong_page}")
    ok &= check(f"{label} 相邻页没有渗进可视区", leak_prev == 0 and leak_next == 0,
                f"上页渗入 {leak_prev} / 下页渗入 {leak_next}")

    # 越界 k 也夹到合法范围
    ok &= check(f"{label} page_range 越界安全",
                res.page_range(-5).start >= 0
                and res.page_range(999).start <= max(0, n - 1),
                f"{res.page_range(-5)} {res.page_range(999)}")

    if overlap and n > 1:
        clash = None
        # O(n^2) 但只在单页内比；116 个够快
        for pg in range(res.pages):
            idxs = res.page_indices(pg)
            rects = [res.rect_on_page(i) for i in idxs]
            for a in range(len(rects)):
                for b in range(a + 1, len(rects)):
                    if rects_overlap(rects[a], rects[b]):
                        clash = (pg, idxs[a], idxs[b], rects[a], rects[b])
                        break
                if clash:
                    break
            if clash:
                break
        ok &= check(f"{label} 同页控件互不重叠", clash is None,
                    f"重叠: {clash}" if clash else "")

    # 虚拟长轴上第 2 页的起点必须 >= 一个页高，否则翻到第 1 页时
    # 第 2 页第一个图标会从底边露出来（历史 bug）。
    if res.pages > 1:
        p2_top = res.positions[res.per_page][1]
        check(f"{label} 第 2 页首图标绝对 y >= page_height",
              p2_top >= res.page_height,
              f"y={p2_top} page_height={res.page_height}")
    return ok


# ── 1. 全部 mode × sort 组合 ──────────────────────────────
section("1. 排列方式 x 排序方式 全组合（2560x1422 / 116 个应用）")
L.set_manual_order([])
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    for smode in L.SORT_MODES:
        r = L.compute_layout(ENTRIES_116, W, H, st, sort_mode=smode)
        assert_legal(r, f"{mode}/{smode}")
        print(f"        {r.describe()}")

section("1b. 3x4 组合的坐标摘要")
print(f"  {'mode':<8}{'cols x rows':<12}{'per_page':<10}"
      f"{'icon':<7}{'cell':<12}{'tile':<12}{'pages':<7}纵向图标间距")
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    r = L.compute_layout(ENTRIES_116, W, H, st)
    # 相邻两行同列图标的间距
    vgap = "n/a"
    if r.rows > 1:
        a = r.rect_on_page(0)
        b = r.rect_on_page(r.columns)
        vgap = str(b[1] - (a[1] + r.tile_size[1]))
    hgap = (r.cell[0] - r.tile_size[0]) if r.columns > 1 else "n/a"
    print(f"  {mode:<8}{f'{r.columns} x {r.rows}':<12}{r.per_page:<10}"
          f"{r.icon_size:<7}{f'{r.cell[0]}x{r.cell[1]}':<12}"
          f"{f'{r.tile_size[0]}x{r.tile_size[1]}':<12}{r.pages:<7}"
          f"v={vgap} h={hgap}")

section("1c. grid 复现原有实现（回归基线）")
_r = L.compute_layout(ENTRIES_116, W, H, mk_settings(layout_mode="grid"))
# grid.py 原值：margins(8,48,8,48) cell 352x281 tile 352x184 icon 128
check("grid 单元格 = 352x281", _r.cell == (352, 281), f"{_r.cell}")
check("grid 控件 = 352x184", _r.tile_size == (352, 184), f"{_r.tile_size}")
check("grid 图标 = 128", _r.icon_size == 128, f"{_r.icon_size}")
check("grid 起点 = (48, 8)", _r.origin == (48, 8), f"{_r.origin}")
# 控件占满整个单元格（文字要地方放），所以"视觉间距"要看图标之间：
# 单元格宽 - 图标宽 = 352 - 128 = 224，与 grid.py 实测的 225 同量级。
check("grid 横向视觉间距(单元格-图标) = 224",
      _r.cell[0] - _r.icon_size == 224, f"{_r.cell[0] - _r.icon_size}")
check("grid 纵向视觉间距 = 97",
      (_r.cell[1] - _r.tile_size[1]) == 97, f"{_r.cell[1] - _r.tile_size[1]}")
check("grid 每页 35 / 共 4 页", _r.per_page == 35 and _r.pages == 4,
      f"{_r.per_page}/{_r.pages}")

_c = L.compute_layout(ENTRIES_116, W, H, mk_settings(layout_mode="compact"))
check("compact 一屏能放下全部 116 个", _c.pages == 1,
      f"per_page={_c.per_page} pages={_c.pages}")
check("compact 密度明显高于 grid", _c.per_page >= 3 * _r.per_page,
      f"compact={_c.per_page} grid={_r.per_page} "
      f"倍数={_c.per_page / _r.per_page:.1f}x")
print(f"        grid   : {_r.describe()}")
print(f"        compact: {_c.describe()}")
_f = L.compute_layout(ENTRIES_116, W, H, mk_settings(layout_mode="flow"))
print(f"        flow   : {_f.describe()}")


# ── 2. 边界：条目数 ───────────────────────────────────────
section("2. 边界：条目数量（0 / 1 / per_page / per_page+1 / 116 / 200）")
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    probe = L.compute_layout(ENTRIES_116, W, H, st)
    pp = probe.per_page
    for label, cnt in (("0 个", 0), ("1 个", 1), ("恰好 per_page", pp),
                       ("per_page+1", pp + 1), ("116 个", 116), ("200 个", 200)):
        sub = ENTRIES_116[:cnt] if cnt <= 116 else \
            ENTRIES_116 + [mk_entry(f"X{i}", 900 + i) for i in range(cnt - 116)]
        r = L.compute_layout(sub, W, H, st)
        exp_pages = max(1, -(-cnt // r.per_page))
        ok = check(f"{mode} / {label}: {cnt} 个 -> {r.pages} 页 (期望 {exp_pages})",
                   r.pages == exp_pages and len(r) == cnt,
                   f"per_page={r.per_page}")
        if cnt and r.clamp_count == 0:
            bad = [i for i in range(cnt)
                   if not (0 <= r.local_positions[i][0]
                           and r.local_positions[i][0] + r.tile_size[0] <= r.viewport[0]
                           and 0 <= r.local_positions[i][1]
                           and r.local_positions[i][1] + r.tile_size[1] <= r.page_height)]
            ok &= check(f"{mode} / {label}: 无越界", not bad, f"越界下标 {bad[:5]}")
        if cnt == 0:
            check(f"{mode} / 0 个: positions 为空且不炸", len(r.positions) == 0)
        if cnt == 1:
            p = r.positions[0]
            check(f"{mode} / 1 个: 落在可视区左上区", 0 <= p[0] < W and 0 <= p[1] < H,
                  f"{p}")


# ── 3. 边界：窗口尺寸 ────────────────────────────────────
section("3. 边界：窗口尺寸")
SIZES = [
    (800, 600, "小窗 800x600"),
    (640, 480, "极小 640x480"),
    (1024, 768, "1024x768"),
    (1280, 720, "1280x720"),
    (1920, 1080, "1920x1080"),
    (2560, 1422, "2560x1422（实测）"),
    (3840, 2000, "4K 3840x2000"),
    (5120, 2880, "5K 5120x2880"),
    (200, 150, "退化 200x150"),
    (60, 40, "极端小 60x40"),
    (1, 1, "病态 1x1"),
]
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    for (w, h, label) in SIZES:
        r = L.compute_layout(ENTRIES_116, w, h, st)
        # 小窗口下控件必然互相挤压，"不重叠"无从谈起，只保证不越界
        overlap_ok = (w >= 640 and h >= 480)
        assert_legal(r, f"{mode} @ {label}", overlap=overlap_ok)

section("3b. 极端小窗口：即使重叠也不能出界")
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    for (w, h) in ((60, 40), (200, 150), (1, 1), (3, 2)):
        r = L.compute_layout(ENTRIES_116, w, h, st)
        bad = []
        for i in range(len(r)):
            x, y, tw, th = r.rect_on_page(i)
            if x < 0 or y < 0 or x + tw > w or y + th > h:
                bad.append((i, (x, y, tw, th)))
        check(f"{mode} @ {w}x{h}: 控件不越界", not bad, f"{bad[:3]}")


# ── 4. 排序正确性 ─────────────────────────────────────────
section("4. 排序：中文 / 拉丁 / 数字 混合")
st = mk_settings()
sort_entries = [mk_entry(n, i) for i, n in enumerate(SORT_NAMES)]

L.set_collator("pinyin")
r = L.compute_layout(sort_entries, W, H, st, sort_mode="name")
got = [L.name_of(e) for e in r.entries]
print("  pinyin 实际顺序:")
print("    " + " | ".join(got))
check("name/pinyin 顺序符合预期（断言具体列表）", got == EXPECTED_PY,
      f"\n      期望 {EXPECTED_PY}\n      实际 {got}" if got != EXPECTED_PY else "")
check("name/pinyin 确实不是按 Unicode 码点排",
      got != sorted(SORT_NAMES), "恰好与码点序相同，这个断言就没有区分力")

L.set_collator("gbk")
r2 = L.compute_layout(sort_entries, W, H, st, sort_mode="name")
got2 = [L.name_of(e) for e in r2.entries]
print("  gbk 实际顺序:")
print("    " + " | ".join(got2))
check("name/gbk 降级顺序符合预期（断言具体列表）", got2 == EXPECTED_GBK,
      f"\n      期望 {EXPECTED_GBK}\n      实际 {got2}" if got2 != EXPECTED_GBK else "")
check("两个 collator 确实不同（否则降级路径没被测到）", got != got2)
L.set_collator(None)

r3 = L.compute_layout(sort_entries, W, H, st, sort_mode="name_rev")
got3 = [L.name_of(e) for e in r3.entries]
check("name_rev 是 name 的严格逆序", got3 == list(reversed(EXPECTED_PY)),
      f"\n      期望 {list(reversed(EXPECTED_PY))}\n      实际 {got3}")
check("name_rev 集合相同", sorted(got3) == sorted(EXPECTED_PY))

print()
print("  按 pinyin 首字母的分组（汉字组内部）:")
cn = [g for g in EXPECTED_PY if g and L._cjk(g[0])]
print("    " + " | ".join(cn))
check("汉字组按拼音递增",
      cn == sorted(cn, key=lambda s: L.transliterate(s)), f"{cn}")
# 逐对验证递增，而不是只看整体有序
pairs = [(cn[i], cn[i + 1]) for i in range(len(cn) - 1)]
bad_pairs = [pr for pr in pairs
             if not L.transliterate(pr[0]) < L.transliterate(pr[1])]
check("汉字组每一对相邻拼音都递增", not bad_pairs, f"逆序对: {bad_pairs}")
check("数字自然序：item2 < item10",
      L.sort_key("item2", "a", 0) < L.sort_key("item10", "a", 1))
check("7-Zip < Adobe（数字先于字母）",
      L.sort_key("7-Zip", "a", 0) < L.sort_key("Adobe", "b", 1))

section("4b. 同名条目的稳定性（全序，不是只按名字）")
dup = [mk_entry("微信", i) for i in range(5)] + [mk_entry("阿里", 9)]
d1 = [e.target for e in L.compute_layout(dup, W, H, st, sort_mode="name").entries]
d2 = [e.target for e in L.compute_layout(
    list(reversed(dup)), W, H, st, sort_mode="name").entries]
check("同名 5 个 + 另一个名字，输出无重复无丢失",
      len(d1) == 6 and len(set(d1)) == 6, f"{d1}")
check("输入顺序不影响输出（排序键是全序）", d1 == d2,
      f"\n      正序 {d1}\n      逆序 {d2}")

section("4c. recent：需要 library 侧提供 last_used")
sent = [mk_entry(f"App{i}", i) for i in range(5)]
for e, t in zip(sent, (500.0, 100.0, 900.0, None, 700.0)):
    e.last_used = t            # 就地塞，模拟 library 补上字段
r = L.compute_layout(sent, W, H, st, sort_mode="recent")
got4 = [L.name_of(e) for e in r.entries]
check("recent 按时间倒序、无记录的排最后",
      got4 == ["App2", "App4", "App0", "App1", "App3"], f"{got4}")
check("recent 提示说明了缺记录的数量",
      any("没有使用记录" in n for n in r.notes), f"{r.notes}")

r = L.compute_layout([mk_entry(f"B{i}", i) for i in range(4)], W, H, st,
                     sort_mode="recent")
check("全部无记录时降级为按名称排（不报错）",
      [L.name_of(e) for e in r.entries] == ["B0", "B1", "B2", "B3"],
      f"{[L.name_of(e) for e in r.entries]}")

# 下面两条用**全新条目**（不带 last_used 字段），否则 usage_stamp
# 会先命中 entry.last_used，table / provider 根本轮不到 —— 那样测的就
# 是字段优先级，不是这两个接口本身。
fresh = [mk_entry(f"B{i}", 100 + i) for i in range(4)]
tbl = {L.uid_of(fresh[i]): float(100 * (i + 1)) for i in range(4)}
r = L.compute_layout(fresh, W, H, st, sort_mode="recent", usage_table=tbl)
check("usage_table 生效（不经由 Entry 字段）",
      [L.name_of(e) for e in r.entries] == ["B3", "B2", "B1", "B0"],
      f"{[L.name_of(e) for e in r.entries]}")
check("usage_stamp 字段优先于时间表",
      L.usage_stamp(sent[0], tbl) == 500.0, f"{L.usage_stamp(sent[0], tbl)}")

calls: list[int] = []


def _prov(e):
    calls.append(1)
    return 42.0 if e.name.endswith("3") else None


L.register_usage_provider(_prov)
r = L.compute_layout(fresh, W, H, st, sort_mode="recent")
check("register_usage_provider 生效（provider 优先于字段）",
      [L.name_of(e) for e in r.entries] == ["B3", "B0", "B1", "B2"],
      f"{[L.name_of(e) for e in r.entries]}")
L.register_usage_provider(None)
check("provider 被调用过", len(calls) == 4, f"{len(calls)} 次")

probe = mk_entry("Probe", 999)
check("无任何来源时 usage_stamp 为 None", L.usage_stamp(probe) is None)
L.record_use(probe, when=12345.0)
check("record_use 写入内置时间表",
      L.usage_stamp(probe) == 12345.0, f"{L.usage_stamp(probe)}")
check("record_use 按 uid 落表", L.uid_of(probe) in L.get_usage_table())
L.get_usage_table().pop(L.uid_of(probe), None)
check("退化时间戳（0 / 负数 / 布尔）不当成有效",
      L.usage_stamp(mk_entry("Q", 1)) is None)

section("4d. manual：持久化顺序 + 重排后重算位置")
L.set_manual_order([])
man = [mk_entry(n, i) for i, n in enumerate(["微信", "钉钉", "飞书", "知乎"])]
uids = [L.uid_of(e) for e in man]
L.set_manual_order([uids[2], uids[0], uids[3], uids[1]])
r = L.compute_layout(man, W, H, st, sort_mode="manual")
check("manual 按保存的顺序排",
      [L.name_of(e) for e in r.entries] == ["飞书", "微信", "知乎", "钉钉"],
      f"{[L.name_of(e) for e in r.entries]}")

merged = L.merge_manual_order([uids[0], uids[1]], [uids[1], uids[0], "已删除uid"])
check("merge 剔除已删除的 uid", "已删除uid" not in merged, f"{merged}")
check("merge 保留仍在的 uid 及其相对顺序", merged == [uids[1], uids[0]], f"{merged}")
check("merge 补上新 uid 时按名称排（非字典序）",
      L.merge_manual_order(["uid-钉钉", "uid-阿里"], ["uid-钉钉"])
      == ["uid-钉钉", "uid-阿里"],
      f"{L.merge_manual_order(['uid-钉钉', 'uid-阿里'], ['uid-钉钉'])}")

# 新增一个条目 -> 应按名称追加到末尾，且原顺序不变
extra = mk_entry("阿里巴巴", 77)
r = L.compute_layout(man + [extra], W, H, st, sort_mode="manual")
got5 = [L.name_of(e) for e in r.entries]
check("manual 遇到新条目：原顺序不变，新条目追加末尾",
      got5 == ["飞书", "微信", "知乎", "钉钉", "阿里巴巴"], f"{got5}")
check("manual 提示说明了追加了几个",
      any("追加" in n for n in r.notes), f"{r.notes}")

# 重排（把最后一个拖到最前）后重算位置
L.set_manual_order([L.uid_of(extra)] + uids[:0] + [uids[2], uids[0], uids[3], uids[1]])
r2 = L.recompute_after_reorder(man + [extra], W, H, st, sort_mode="manual")
check("recompute_after_reorder 产生新顺序",
      [L.name_of(e) for e in r2.entries] == ["阿里巴巴", "飞书", "微信", "知乎", "钉钉"],
      f"{[L.name_of(e) for e in r2.entries]}")
check("重排后页面几何不变（只换顺序）",
      r2.cell == r.cell and r2.tile_size == r.tile_size and r2.per_page == r.per_page)
# 位置坐标本身按槽位固定（内容高亮/低亮网格同一槽位坐标不变），
# 变的是"哪个条目落在哪个槽位"。所以要断言条目与坐标的对应关系变了。
check("重排后同一坐标上的条目确实换了",
      L.name_of(r2.entries[0]) == "阿里巴巴" and L.name_of(r.entries[0]) == "飞书"
      and r2.positions[0] == r.positions[0],
      f"{[L.name_of(e) for e in r2.entries]} @ {r2.positions[0]}")
assert_legal(r2, "manual 重排后", overlap=False)

# positions_for_order：直接给顺序，绕过排序
r3 = L.positions_for_order(man, W, H, st, order=[man[3], man[1], man[2], man[0]])
check("positions_for_order 按给定顺序摆放",
      [L.name_of(e) for e in r3.entries] == ["知乎", "钉钉", "飞书", "微信"],
      f"{[L.name_of(e) for e in r3.entries]}")

# 顺序表里只有部分 uid 时，其余按名称追加（feishu < weixin < zhihu）
L.set_manual_order([uids[1]])
r4 = L.compute_layout(man, W, H, st, sort_mode="manual")
check("顺序表不完整时不丢条目",
      [L.name_of(e) for e in r4.entries] == ["钉钉", "飞书", "微信", "知乎"],
      f"{[L.name_of(e) for e in r4.entries]}")

# 空表 = 纯按拼音。dingding < feishu < weixin < zhihu，所以钉钉在第一。
L.set_manual_order([])
check("顺序表为空时全部按名称排",
      [L.name_of(e) for e in L.compute_layout(
          man, W, H, st, sort_mode="manual").entries]
      == ["钉钉", "飞书", "微信", "知乎"],
      f"{[L.name_of(e) for e in L.compute_layout(man, W, H, st, sort_mode='manual').entries]}")

# apply_manual_order 是 compute_layout 排序的等价物，必须给出一致的顺序
L.set_manual_order([uids[2], uids[0], uids[3], uids[1]])
check("apply_manual_order 与 compute_layout 顺序一致",
      [L.uid_of(e) for e in L.apply_manual_order(man)]
      == [L.uid_of(e) for e in
          L.compute_layout(man, W, H, st, sort_mode="manual").entries],
      f"{[L.name_of(e) for e in L.apply_manual_order(man)]}")
L.set_manual_order([])


# ── 5. 纯函数性 ───────────────────────────────────────────
section("5. 纯函数性 / 确定性")
st = mk_settings(layout_mode="flow", sort_mode="name")
snapshot = [(e.name, e.target, e.args, list(e.keywords), e.missing)
            for e in ENTRIES_116]
snap_settings = st.as_dict()
a = L.compute_layout(ENTRIES_116, W, H, st)
b = L.compute_layout(ENTRIES_116, W, H, st)
check("两次调用结果完全相同", a.positions == b.positions
      and a.entries == b.entries and a.sizes == b.sizes)
check("没有修改 entries",
      snapshot == [(e.name, e.target, e.args, list(e.keywords), e.missing)
                   for e in ENTRIES_116])
check("没有修改 settings", snap_settings == st.as_dict())

shuffled = list(reversed(ENTRIES_116))
c = L.compute_layout(shuffled, W, H, st)
check("输入顺序不同 -> 同一份显示顺序（排序归一）",
      [e.uid for e in a.entries] == [e.uid for e in c.entries],
      f"{len(a.entries)} vs {len(c.entries)}")
check("source_index 把显示序映射回输入下标",
      all(a.entries[i] is ENTRIES_116[a.source_index[i]] for i in range(len(a)))
      and all(c.entries[i] is shuffled[c.source_index[i]] for i in range(len(c))))

r_before = copy.deepcopy(st)
_ = L.compute_layout(ENTRIES_116, W, H, st, sort_mode="recent")
check("recent 排序也不改 settings", r_before.as_dict() == st.as_dict())


# ── 6. page_height 与虚拟长轴 ─────────────────────────────
section("6. 页高 / 虚拟长轴（跨页泄漏的直接防线）")
for mode in L.LAYOUT_MODES:
    st = mk_settings(layout_mode=mode)
    r = L.compute_layout(ENTRIES_116, W, H, st)
    check(f"{mode}: page_height == 可视区高度", r.page_height == H,
          f"{r.page_height} vs {H}")
    if r.pages >= 2:
        p2 = r.positions[r.per_page]
        check(f"{mode}: 第 2 页首个图标 y >= page_height", p2[1] >= r.page_height,
              f"y={p2[1]} ph={r.page_height}")
        check(f"{mode}: 第 1 页最后一个图标底边 <= page_height",
              r.positions[r.per_page - 1][1] + r.tile_size[1] <= r.page_height,
              f"bottom="
              f"{r.positions[r.per_page - 1][1] + r.tile_size[1]}")
    # on_screen 模拟翻页：offset = -page*page_height
    bad = 0
    for pg in range(r.pages):
        for i in r.page_indices(pg):
            x, y = r.on_screen(i, pg)
            w, h = r.tile_size
            if not (0 <= x and x + w <= r.viewport[0]
                    and 0 <= y and y + h <= r.viewport[1]):
                bad += 1
    check(f"{mode}: 每一页的 on_screen 坐标都完整在屏内", bad == 0, f"越界 {bad}")


# ── 7. 参数敏感性 ─────────────────────────────────────────
section("7. 可调参数确实起作用")
st = mk_settings(layout_mode="flow")
r10 = L.compute_layout(ENTRIES_116, W, H, st, cell_ratio=1.0)
r15 = L.compute_layout(ENTRIES_116, W, H, st, cell_ratio=1.5)
r20 = L.compute_layout(ENTRIES_116, W, H, st, cell_ratio=2.0)
check("flow: cell_ratio 1.0 -> 1.5 纵向节拍变松",
      r15.cell[1] > r10.cell[1], f"{r10.cell[1]} -> {r15.cell[1]}")
check("flow: cell_ratio 2.0 被可用高度夹住（不越界）",
      r20.cell[1] * r20.rows <= r20.viewport[1],
      f"block={r20.cell[1] * r20.rows} <= {r20.viewport[1]}")

st = mk_settings(layout_mode="flow", hide_labels=True)
rl = L.compute_layout(ENTRIES_116, W, H, st, cell_ratio=1.0)
check("hide_labels 缩小 tile 高度", rl.tile_size[1] < r10.tile_size[1],
      f"{rl.tile_size[1]} < {r10.tile_size[1]}")
check("hide_labels 后不再预留文字块",
      not rl.labels_visible and rl.label_lines == 0)
assert_legal(rl, "flow/hide_labels")

st = mk_settings(layout_mode="flow", label_lines=2)
r2l = L.compute_layout(ENTRIES_116, W, H, st)
check("label_lines=2 增加文字块高度",
      r2l.tile_size[1] > r10.tile_size[1], f"{r10.tile_size[1]} -> {r2l.tile_size[1]}")

st = mk_settings(layout_mode="grid", icon_size=96)
r96 = L.compute_layout(ENTRIES_116, W, H, st)
check("icon_size 96 -> 图标 96", r96.icon_size == 96, f"{r96.icon_size}")
check("icon_size 影响 tile 高度", r96.tile_size[1] < _r.tile_size[1],
      f"{_r.tile_size[1]} -> {r96.tile_size[1]}")

st = mk_settings(layout_mode="grid", columns=9, rows=6)
r96c = L.compute_layout(ENTRIES_116, W, H, st)
check("columns/rows 生效", (r96c.columns, r96c.rows) == (9, 6),
      f"{r96c.columns}x{r96c.rows}")
assert_legal(r96c, "grid 9x6")

st = mk_settings(layout_mode="grid", margin=120)
rm = L.compute_layout(ENTRIES_116, W, H, st)
check("margin 增大 -> 单元格变小", rm.cell[0] < _r.cell[0],
      f"{_r.cell[0]} -> {rm.cell[0]}")
st = mk_settings(layout_mode="grid", margin=0)
rm0 = L.compute_layout(ENTRIES_116, W, H, st)
check("margin=0 -> 单元格最大", rm0.cell[0] > _r.cell[0],
      f"{_r.cell[0]} -> {rm0.cell[0]}")
assert_legal(rm0, "grid margin=0")

section("7b. hide_labels + compact + 大 columns 组合")
st = mk_settings(layout_mode="compact", hide_labels=True, icon_size=256,
                 columns=3, rows=1)
r = L.compute_layout(ENTRIES_116, W, H, st)
assert_legal(r, "compact/无文字/3x1 请求")
check("compact 忽略 columns/rows 里的 3/1", (r.columns, r.rows) != (3, 1),
      f"{r.columns}x{r.rows}")
check("compact 图标不因 icon_size=256 撑爆窗口",
      r.icon_size + r.tile_size[0] <= r.viewport[0],
      f"icon={r.icon_size} tile={r.tile_size}")
print(f"        {r.describe()}")

# icon_size 扫描：compact 的图标是 icon_size*0.7（小窗口下还要再收）
for isz in (48, 128, 256):
    r = L.compute_layout(ENTRIES_116, W, H,
                         mk_settings(layout_mode="compact", icon_size=isz))
    assert_legal(r, f"compact icon_size={isz}")
    check(f"compact icon_size={isz} -> 图标不超过设定值且装得下",
          0 < r.icon_size <= isz and r.icon_size + 8 <= r.viewport[0]
          and r.icon_size <= r.cell[1],
          f"icon={r.icon_size} {r.columns}x{r.rows}={r.per_page}")
    print(f"        icon_size={isz:<4} -> {r.describe()}")


# ── 8. API 健壮性 ─────────────────────────────────────────
section("8. API 健壮性")
st = mk_settings()
check("entries=None 视作空", len(L.compute_layout([], W, H, st)) == 0)
check("settings=None 用默认值也能算",
      L.compute_layout(ENTRIES_116, W, H, None).per_page == 35)
check("dict 设置也能吃", L.compute_layout(
    ENTRIES_116, W, H, {"layout_mode": "compact", "columns": 8}
).per_page == 160)
check("width/height 传 0 不会除零",
      L.compute_layout(ENTRIES_116, 0, 0, st).page_height >= 1)
check("page_of 越界抛 IndexError", _raises(
    IndexError, lambda: L.compute_layout(ENTRIES_116, W, H, st).page_of(116)))
check("未知 sort_mode 抛 ValueError", _raises(
    ValueError, lambda: L.compute_layout(ENTRIES_116, W, H, st, sort_mode="nope")))
check("未知 layout_mode 抛 ValueError", _raises(
    ValueError, lambda: L.compute_layout(ENTRIES_116, W, H, st,
                                         layout_mode="nope")))
check("乱序的 order 不会越界", len(L.compute_layout(
    ENTRIES_116, W, H, st, order=[999, -5, 3, 1])) == 116)
check("重复的 order 不产生重复项", len(L.compute_layout(
    ENTRIES_116, W, H, st, order=[1, 1, 1, 2])) == 116)
check("entry_at(3) 返回排序后第 3 个条目",
      L.compute_layout(ENTRIES_116, W, H, st).entry_at(3) is
      L.compute_layout(ENTRIES_116, W, H, st).entries[3])
check("name_of / uid_of 对残缺对象不抛", L.name_of(object()) != ""
      and isinstance(L.uid_of(object()), str))
def _last_rows_ok() -> bool:
    """每页占几行：满页 = rows，末页 = ceil(剩余/columns)。"""
    for mode in L.LAYOUT_MODES:
        r = L.compute_layout(ENTRIES_116, W, H, mk_settings(layout_mode=mode))
        for pg in range(r.pages):
            n = len(r.page_indices(pg))
            want = (n + r.columns - 1) // r.columns
            if r.page_occupied_rows(pg) != want:
                print(f"        {mode} 页 {pg}: {n} 个 -> "
                      f"{r.page_occupied_rows(pg)} 行 (期望 {want})")
                return False
    return True


def _rects_consistent() -> bool:
    """rect_absolute / rect_on_page / positions 三者必须自洽。"""
    for mode in L.LAYOUT_MODES:
        r = L.compute_layout(ENTRIES_116, W, H, mk_settings(layout_mode=mode))
        for i in range(len(r)):
            ax, ay, aw, ah = r.rect_absolute(i)
            x, y, w, h = r.rect_on_page(i)
            px, py = r.positions[i]
            if (ax, ay, aw, ah) != (x, y + r.page_of(i) * r.page_height, w, h):
                return False
            if (px, py) != (ax, ay) or (x, y) != r.local_positions[i]:
                return False
            if r.sizes[i] != r.tile_size:
                return False
    return True


check("page_occupied_rows 末页正确", _last_rows_ok())
check("rect_absolute / local_positions / sizes 三者自洽", _rects_consistent())
check("on_screen 在本页上等于 local_positions",
      all(L.compute_layout(ENTRIES_116, W, H, mk_settings()).on_screen(
          i) == L.compute_layout(ENTRIES_116, W, H, mk_settings()).local_positions[i]
          for i in range(116)))


# ── 汇总 ─────────────────────────────────────────────────
print()
print("=" * 72)
if _FAILED:
    print(f"结果: FAIL —— {len(_FAILED)}/{_TOTAL} 项失败")
    for name in _FAILED[:40]:
        print(f"  - {name}")
    if len(_FAILED) > 40:
        print(f"  ... 另有 {len(_FAILED) - 40} 项")
    print("=" * 72)
    sys.exit(1)

print(f"结果: PASS —— {_TOTAL} 项全部通过")
print("=" * 72)
sys.exit(0)
