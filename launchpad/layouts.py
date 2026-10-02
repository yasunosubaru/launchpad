# -*- coding: utf-8 -*-
"""
排列与分页计算：把「一堆条目 + 一个可视区 + 一份设置」变成一组坐标。

## 为什么单独一个模块

grid.py 里原来把排布写死在 _margins / _cell / _tile_size 三个私有方法里，
想加一种排列方式就得改那个类，而那个类同时还管翻页动画、搜索过滤、
右键菜单 —— 每次试一种新排布都要冒着重启 GUI 的风险。这里把排布拆成
纯函数：不吃 Qt、不 import grid/library/settings（只用鸭子类型读设置），
给定相同输入永远得到相同输出，于是排列方式可以单独穷举测试。

## 索引约定

`compute_layout` 返回的一切，索引一律是**显示顺序**（排序之后）：

    result.entries[k]        第 k 个**显示**出来的条目
    result.positions[k]      第 k 个显示条目的左上角
    result.page_of(k)        第 k 个显示条目在第几页

调用方如果需要回到自己的原始列表，用 `result.source_index[k]`。
这一层映射必须显式存在：排序之后「输入下标」和「显示下标」不再相同，
而 grid.py 手上有的是自己那份 _tiles 列表。

## 分页不泄漏的充分条件（全部排列方式共用）

    page_height == 可视区高度                      # 不是"网格高度"近似值
    第 k 页的**页内**坐标 y 满足 0 <= y 且 y + tile_h <= page_height
    第 k 页的**绝对**坐标 y_abs = k * page_height + y

三条一起成立时，第 k+1 页第一个图标的绝对 y 必然 >= (k+1) * page_height；
翻页把整条长轴平移 -k * page_height 之后，它的屏上 y 仍 >= page_height ——
**顶边正好压在下边缘上，一个像素都露不出来**。反向同理：第 k-1 页的
底边 <= -tile_h，整个在视口上方。

所以"下一页第一个图标从底边漏出来"只可能来自两个地方，本模块都堵死了：
  1. page_height 取了比可视区小的值（近似、减了个 margin）
  2. 页内 y 没有被夹到 [0, page_height - tile_h]
后者由 _place() 的夹取兜底，并且 `result.clamp_count` 会记录兜底触发了几次
（正常情况恒为 0 —— 任何一次都是排布算错了，测试会当场抓到）。

## 排列方式

| mode      | 纵向节拍            | 列/行数            | 定位              |
|-----------|---------------------|--------------------|-------------------|
| `grid`    | 铺满（均分可用高度） | 用设置里的值       | 整块居中          |
| `flow`    | 按内容紧凑，不拉伸  | 用设置里的值       | 整块居中，底部留白 |
| `compact` | 按内容紧凑          | **按可用空间自动填满** | 整块居中      |

`cell_ratio` 的含义随 mode 变化（已写进 LayoutResult.notes）：
  - `grid`：不参与（规则网格按定义就是均分，比例由窗口尺寸决定）
  - `flow` / `compact`：纵向节拍 = 内容高 * cell_ratio，1.0 = 紧凑无留白
节拍被夹在 [内容高, 可用高 // rows] 之间，所以这个旋钮有物理上下限
（内容装不下就是装不下），notes 里会报出实际生效值。

## 排序方式

`name` / `name_rev` 走中文排序：**先按"首字符是不是汉字"分成两组，
拉丁/数字组在前按自然序（7-Zip < Adobe，item2 < item10），汉字组在后按
拼音**。没有拼音库时降级到 GBK 字节序（GB2312 一级汉字本身就是按拼音
排的，Windows 中文排序也是这个原理），结果略有差异但同样稳定。
两种排序器的具体输出都写死在 tests_layout.py 里当断言。

`recent` 和 `manual` 需要调用方提供数据，本模块只定义接口，不 import
library.py，也不改它 —— 见 `register_usage_provider` / `set_manual_order`
的文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "LAYOUT_MODES", "SORT_MODES",
    "LayoutResult", "compute_layout", "positions_for_order",
    "recompute_after_reorder",
    "uid_of", "name_of", "usage_stamp", "record_use",
    "register_usage_provider", "get_usage_provider",
    "set_manual_order", "get_manual_order", "merge_manual_order",
    "apply_manual_order", "manual_order_from_entries",
    "sort_key", "transliterate", "set_collator", "collator_name",
]


# ─── 常量 ────────────────────────────────────────────────
# LABEL_H 必须与 theme.Theme.LABEL_H 一致，否则排出来的 tile 高度
# 和 tile.py 实际画文字的区域对不上，文字会被挤出控件。
LABEL_H = 34
ICON_PAD = 24            # grid 模式里图标与单元格宽度的最小余量
TILE_VPAD = 8            # grid 模式里 tile 高度相对单元格的收缩
MIN_EDGE = 4             # 任何模式下上下左右留白的下限

COMPACT_ICON_RATIO = 0.70    # 紧凑模式图标相对设置值的比例
COMPACT_HGAP_RATIO = 0.09    # 图标到文字的间距相对图标边长
COMPACT_WGAP_RATIO = 0.45    # 单元格宽度相对图标边长的余量
COMPACT_MIN_LABEL_W = 24     # 留给省略号文字的最小宽度

# 硬上限与 settings.SCHEMA 的 columns/rows 上限保持一致：紧凑模式
# 最多填到 16 列 10 行，再多字就小到认不出了。
COLS_HARD_MAX = 16
ROWS_HARD_MAX = 10

LAYOUT_MODES = ("grid", "flow", "compact")
SORT_MODES = ("name", "name_rev", "recent", "manual")
COLLATORS = ("pinyin", "gbk")

_UNSET = object()


# ─── 设置读取（鸭子类型） ─────────────────────────────────
def _setting(src: Any, key: str, default: Any) -> Any:
    """
    从任意设置容器里取值。

    接受 launchpad.settings.Settings、dict、或者任何有同名属性的对象。
    为什么不直接 import settings：布局计算必须能在没有 %APPDATA% 的
    环境（单测、headless CI）里跑，而且 settings.py 未来加字段时
    这里不会因为 import 时序被牵连。
    """
    if src is None:
        return default
    getter = getattr(src, "get", None)
    if callable(getter):
        try:
            v = getter(key)
        except (KeyError, TypeError, AttributeError):
            v = None
        if v is not None:
            return v
    v = getattr(src, key, None)
    if v is not None:
        return v
    return default


@dataclass(frozen=True)
class _Cfg:
    """把设置摊平成算布局真正需要的几个数。"""
    layout_mode: str
    sort_mode: str
    columns: int
    rows: int
    icon_size: int
    icon_gap: int
    label_lines: int
    cell_ratio: float
    margin: int
    hide_labels: bool

    @classmethod
    def from_settings(cls, settings: Any) -> "_Cfg":
        mode = _setting(settings, "layout_mode", "grid")
        if mode not in LAYOUT_MODES:
            mode = "grid"
        sort = _setting(settings, "sort_mode", "name")
        if sort not in SORT_MODES:
            sort = "name"
        return cls(
            layout_mode=mode,
            sort_mode=sort,
            columns=max(1, int(_setting(settings, "columns", 7))),
            rows=max(1, int(_setting(settings, "rows", 5))),
            icon_size=max(8, int(_setting(settings, "icon_size", 128))),
            icon_gap=max(0, int(_setting(settings, "icon_gap", 22))),
            label_lines=max(1, int(_setting(settings, "label_lines", 1))),
            cell_ratio=float(_setting(settings, "cell_ratio", 1.0)),
            margin=max(0, int(_setting(settings, "margin", 48))),
            hide_labels=bool(_setting(settings, "hide_labels", False)),
        )


# ─── 条目读取 ─────────────────────────────────────────────
def name_of(entry: Any) -> str:
    """条目的显示名。拿不到就退化成字符串本身，绝不抛异常。"""
    v = getattr(entry, "name", None)
    if isinstance(v, str) and v:
        return v
    if v is not None:
        try:
            return str(v)
        except Exception:
            pass
    try:
        return str(entry)
    except Exception:
        return ""


def uid_of(entry: Any) -> str:
    """
    条目的稳定标识。优先用 library.Entry 自带的 uid；没有就按
    target|args 兜底（和 Entry.uid 同一口径），再不行退回名字。

    必须是**稳定**的：manual 顺序表和 recent 时间表都以它为键，
    一次运行内重算出同样的值，否则手动排序会在重排后自己乱掉。
    """
    v = getattr(entry, "uid", None)
    if isinstance(v, str) and v:
        return v
    parts = []
    for attr in ("target", "args"):
        x = getattr(entry, attr, None)
        if isinstance(x, str) and x:
            parts.append(x)
    if parts:
        return "|".join(parts).lower().strip()
    return name_of(entry)


# ─── 中文排序 ─────────────────────────────────────────────
_PY: Any = _UNSET
_collator: str | None = None


def _pinyin_module():
    global _PY
    if _PY is _UNSET:
        try:
            import pypinyin
            from pypinyin import Style
            _PY = (pypinyin, Style)
        except Exception:          # 缺依赖 / 导入出错都降级，不影响启动
            _PY = None
    return _PY


def set_collator(name: str | None) -> None:
    """
    强制指定中文排序器："pinyin" / "gbk" / None（自动）。

    测试需要它：两种排序器对个别名字的先后不一致，必须分别断言。
    """
    global _collator
    if name is not None and name not in COLLATORS:
        raise ValueError(f"未知排序器 {name!r}，可选 {COLLATORS}")
    _collator = name


def collator_name() -> str:
    """当前实际生效的排序器名（写进 LayoutResult.notes 便于排查）。"""
    if _collator is not None:
        return _collator
    return "pinyin" if _pinyin_module() else "gbk"


def _gbk_key(s: str) -> bytes:
    """
    GBK **字节序**。GB2312 一级汉字（3755 个常用字）本身就是按拼音排的，
    二级汉字按部首排。常用字足够日常应用名用，代价是零依赖。
    编码不出来的字符（生僻字、emoji）直接丢掉；整个都丢光时退回 UTF-8
    字节，至少还是确定的。

    关键：顺序信息只在**字节**上，"编码后再用 gbk 解码回 str"是错的 ——
    那样就退化成按 Unicode 码点排，而码点序正是这个函数要避免的
    （"微" U+5FAE 会排到 "汉" U+6C49 后面，拼音却是 han < wei）。
    字节序要保住，但排序键必须是 str（要和其它 str 键放进同一个元组比较），
    所以用 latin-1 往返：把每个字节映射成同码位的字符，字节序 == 码位序，
    且不丢信息、不会和真正的排序键类型打架。
    """
    try:
        raw = s.encode("gbk", "ignore")
    except Exception:
        raw = b""
    if not raw:
        raw = s.encode("utf-8", "ignore")
    return raw.decode("latin-1")


def transliterate(name: str) -> tuple[str, str]:
    """
    返回 (全拼, 首字母拼)。非汉字原样保留，所以 "VS Code" -> ("VS Code", "VS Code")。

    降级到 GBK 时没有可用的全拼，第二个返回值给一个不会被误当作拼音的
    占位（低字节序的全角空格不影响分组比较，组内本来就靠第一个分量）。
    """
    mod = _pinyin_module()
    if collator_name() == "pinyin" and mod is not None:
        pypinyin, Style = mod
        try:
            full = "".join(pypinyin.lazy_pinyin(name))
            init = "".join(pypinyin.lazy_pinyin(name, style=Style.FIRST_LETTER))
            return full.casefold(), init.casefold()
        except Exception:
            pass
    k = _gbk_key(name)
    return k, k


def _cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x2E80 <= cp <= 0x9FFF        # 部首扩展 + 中日韩统一表意
        or 0x3400 <= cp <= 0x4DBF     # 扩展 A
        or 0xF900 <= cp <= 0xFAFF     # 兼容表意
        or 0xFF01 <= cp <= 0xFF60     # 全角标点/字母
    )


_NAT_SPLIT = re.compile(r"(\d+)")


def _natural(s: str) -> tuple:
    """
    自然序：数字段按数值比，字母段按字符串比。
    于是 "item2" < "item10"，纯 str 比较会反过来。
    每段统一成 (类型, 数值, 文本) 三元组，避免 int/str 互相比炸掉。
    """
    out: list[tuple[int, int, str]] = []
    for part in _NAT_SPLIT.split(s.casefold()):
        if not part:
            continue
        if part.isdigit():
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part))
    return tuple(out)


def sort_key(name: str, uid: str = "", index: int = 0) -> tuple:
    """
    名称排序键。**全序**：任何两个不同的 (name, uid, index) 都不会撞键，
    因此同一次调用的结果与输入顺序无关（排序稳定性的前提）。

    分组：首字符是汉字的排在拉丁/数字之后。这不是"按 Unicode 码点" ——
    那样 "微信"(U+5FAE) 会排在 "汉语"(U+6C49) 前，拼音却是 han < wei，
    用户看到的顺序跟读出来的顺序对不上。
    """
    if name and _cjk(name[0]):
        full, init = transliterate(name)
        # 最后一层用原始字节兜底：GBK 排序器下 latin-1 往返后的串在
        # 视觉上不可读，出问题时要能看出是哪个名字。
        return (1, full, init, name.casefold(), uid, index)
    return (0, _natural(name), "", name.casefold(), uid, index)


# ─── recent：最近使用 ─────────────────────────────────────
_USAGE_HOOK: Callable[[Any], float | None] | None = None
_USAGE_TABLE: dict[str, float] = {}


def register_usage_provider(fn: Callable[[Any], float | None] | None) -> None:
    """
    注册"某条目最近一次被使用的时间"（epoch 秒，没有就返回 None）。

    ## library 侧需要配套什么

    Entry 现在只有 name/target/args/workdir/icon/source_lnk/keywords/missing，
    **没有任何使用记录**，所以 `recent` 目前只能退化成"按名称排"。

    最小改动（推荐）：在 Entry 上加一个字段

        last_used: float = 0.0        # epoch 秒；0 / 缺省 = 从没用过

    连带要做的三件事：
      1. `Entry.from_dict` 用 `known = set(cls.__dataclass_fields__)` 过滤，
         加了字段它就会自动往返，**这一处不用改**。
      2. 落盘：Library.save() 走的是 `asdict()`，自动带上，**不用改**。
      3. 写入时机：window.Launchpad._launch() 里启动成功后
         `e.last_used = time.time()` 然后 `library.save()`；
         建议加个节流（同一 uid 60 秒内不重复写盘），否则每启一个
         应用就重写一次 shortcuts.json。

    在 1~3 落地之前，有两条**零改动**的路子可以先用：
      - `register_usage_provider(fn)`：自己拿 Library 挂上去，
        从别处（比如一个 stats.json）读时间。
      - `record_use(entry)` + 把 `layouts.get_usage_table()` 的内容
        序列化到任何地方：完全绕开 Entry。
    """
    global _USAGE_HOOK
    _USAGE_HOOK = fn


def get_usage_provider() -> Callable[[Any], float | None] | None:
    return _USAGE_HOOK


def get_usage_table() -> dict[str, float]:
    """内置时间表（uid -> epoch 秒）。可直接序列化持久化。"""
    return _USAGE_TABLE


def record_use(entry: Any, when: float | None = None,
               table: dict[str, float] | None = None) -> float:
    """记一次使用。返回写入的时间戳。"""
    import time as _time
    ts = float(when) if when is not None else _time.time()
    t = _USAGE_TABLE if table is None else table
    t[uid_of(entry)] = ts
    return ts


def usage_stamp(entry: Any, table: dict[str, float] | None = None) -> float | None:
    """
    取某条目的最近使用时间，取不到返回 None。

    查找顺序：注册的 provider -> entry.last_used / used_at / last_launched
    -> 时间表。前两条任一命中就够了，所以只要 library 侧补了
    `last_used` 字段，这里立刻就能用上，不用改本文件。
    """
    v: Any = None
    if _USAGE_HOOK is not None:
        try:
            v = _USAGE_HOOK(entry)
        except Exception:
            v = None
    if v is None:
        for attr in ("last_used", "used_at", "last_launched"):
            cand = getattr(entry, attr, None)
            # 「没设过」有两种形态：属性不存在（None）和从未使用（0.0）。
            # 只判 None 是不够的 —— library.Entry 现在带了 last_used=0.0
            # 默认值，循环会在它上面 break，然后 f > 0 为假直接返回 None，
            # 永远轮不到下面那张使用时间表。实测会让 2 项排序测试失败。
            # 所以这里把「取不到有效时间戳」一律当作未设，继续往下找。
            if cand is None or isinstance(cand, bool):
                continue
            try:
                if float(cand) > 0.0:
                    v = cand
                    break
            except (TypeError, ValueError):
                continue
    if v is None:
        t = _USAGE_TABLE if table is None else table
        try:
            v = t.get(uid_of(entry))
        except Exception:
            v = None
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


# ─── manual：手动顺序 ─────────────────────────────────────
_MANUAL_ORDER: list[str] = []

# 手动顺序合并时给"新增 uid"排序用的名字表。名字拿不到就退回 uid 字典序，
# 顺序照样是确定的。手工重排路径上会先由调用方/本模块填好。
_uid_to_name: dict[str, str] = {}


def set_manual_order(uids: Iterable[str] | None) -> list[str]:
    """设置手动顺序（uid 列表）。返回规范化后的副本。"""
    global _MANUAL_ORDER
    _MANUAL_ORDER = [u for u in (uids or []) if isinstance(u, str) and u]
    return list(_MANUAL_ORDER)


def get_manual_order() -> list[str]:
    return list(_MANUAL_ORDER)


def merge_manual_order(uids: Sequence[str],
                       current: Sequence[str] | None = None) -> list[str]:
    """
    把「已存的手动顺序」对齐到「当前实际存在的 uid 集合」。

    两个方向都要处理，否则手动排序会随着库的变化悄悄坏掉：
      - **已删除的 uid** 从顺序里剔除（留在那儿会白白吃掉一个位置）
      - **新增的 uid** 追加到末尾，且按名称排（补进去的顺序不能每次
        运行都变，否则用户刚拖好的顺序会被后来导入的图标搅乱）
    """
    src = list(get_manual_order() if current is None else current)
    have = list(dict.fromkeys(uids))          # 去重且保序
    present = set(have)
    kept = [u for u in src if u in present]
    known = set(kept)
    extra = sorted((u for u in have if u not in known),
                   key=lambda u: (sort_key(_uid_to_name.get(u, ""), u, 0), u))
    return kept + extra


def apply_manual_order(entries: Sequence[Any],
                       order: Sequence[str] | None = None) -> list[Any]:
    """
    按手动顺序重排条目。

    顺序表里没有的条目按名称追加到末尾（和 merge_manual_order 同一
    套规则），保证"存了顺序之后新导入的应用"不会凭空消失。
    """
    items = list(entries)
    for e in items:
        _uid_to_name[uid_of(e)] = name_of(e)
    order = merge_manual_order([uid_of(e) for e in items], order)
    rank = {u: i for i, u in enumerate(order)}
    return sorted(
        items,
        key=lambda e: (rank.get(uid_of(e), len(order)),
                       sort_key(name_of(e), uid_of(e), 0)),
    )


def manual_order_from_entries(entries: Sequence[Any]) -> list[str]:
    """把当前条目列表固化成一份手动顺序（拖拽重排后调用它来落库）。"""
    return [uid_of(e) for e in entries]


# ─── 结果对象 ─────────────────────────────────────────────
@dataclass(frozen=True)
class LayoutResult:
    """
    一次排布的全部结果。不可变 —— 坐标算完就不该再被谁改掉，
    否则翻页动画会把基准坐标污染掉（上一版 tile._base 就是这么坏的）。
    """
    mode: str
    sort_mode: str
    viewport: tuple[int, int]
    page_height: int
    columns: int
    rows: int
    per_page: int
    pages: int
    cell: tuple[int, int]                # 单元格步距 (w, h)
    origin: tuple[int, int]              # 第 0 页第 0 格的左上角（页内）
    tile_size: tuple[int, int]           # 控件尺寸，全局统一
    icon_size: int
    label_lines: int
    labels_visible: bool

    entries: tuple[Any, ...]             # 显示顺序
    source_index: tuple[int, ...]        # 显示位置 -> 输入列表下标
    positions: tuple[tuple[int, int], ...]    # **绝对**坐标（虚拟长轴）
    local_positions: tuple[tuple[int, int], ...]   # 页内坐标
    sizes: tuple[tuple[int, int], ...]   # 逐项控件尺寸（默认与 tile_size 同）

    clamp_count: int = 0
    notes: tuple[str, ...] = ()

    # ── 索引 ──────────────────────────────────────────
    def __len__(self) -> int:
        return len(self.entries)

    def page_of(self, i: int) -> int:
        """第 i 个**显示**条目在第几页（0 起）。"""
        if i < 0 or i >= len(self.entries):
            raise IndexError(f"下标越界: {i}（共 {len(self.entries)} 个）")
        return i // self.per_page if self.per_page else 0

    def slot_of(self, i: int) -> int:
        """第 i 个显示条目在本页内是第几格（0 起）。"""
        self.page_of(i)
        return i % self.per_page if self.per_page else 0

    def page_range(self, page: int) -> range:
        """第 page 页覆盖的显示下标区间。越界夹到合法范围。"""
        if self.per_page <= 0:
            return range(0, 0)
        n = len(self.entries)
        last = max(0, (n + self.per_page - 1) // self.per_page - 1)
        p = max(0, min(int(page), last))
        return range(p * self.per_page, min(n, (p + 1) * self.per_page))

    def page_indices(self, page: int) -> tuple[int, ...]:
        return tuple(self.page_range(page))

    def page_occupied_rows(self, page: int) -> int:
        """第 page 页实际占了几行。最后一页不满时用得上。"""
        n = len(self.page_range(page))
        if self.columns <= 0:
            return 0
        return (n + self.columns - 1) // self.columns

    # ── 坐标 ──────────────────────────────────────────
    def on_screen(self, i: int, page: int | None = None) -> tuple[int, int]:
        """
        翻到 page 页时第 i 个条目的屏上坐标。

        翻页动画每帧都是"绝对坐标 - page * page_height"，把这个函数
        接到 positionChanged/valueChanged 上就不会再出现符号写反的 bug
        （写反的现象是第 2 页图标飞到 y=2900，屏幕全空）。
        """
        self.page_of(i)
        p = self.page_of(i) if page is None else int(page)
        x, y_abs = self.positions[i]
        return (x, y_abs - p * self.page_height)

    def rect_on_page(self, i: int) -> tuple[int, int, int, int]:
        """第 i 个条目在它自己那页里的矩形 (x, y, w, h)，y 是页内坐标。"""
        x, y = self.local_positions[i]
        w, h = self.sizes[i]
        return (x, y, w, h)

    def rect_absolute(self, i: int) -> tuple[int, int, int, int]:
        x, y = self.positions[i]
        w, h = self.sizes[i]
        return (x, y, w, h)

    def entry_at(self, i: int) -> Any:
        return self.entries[i]

    def describe(self) -> str:
        """人读的一行摘要，给自检和日志用。"""
        tw, th = self.tile_size
        cw, ch = self.cell
        return (f"{self.mode}/{self.sort_mode} {self.viewport[0]}x{self.viewport[1]} "
                f"cell={cw}x{ch} tile={tw}x{th} icon={self.icon_size} "
                f"{self.columns}x{self.rows}={self.per_page}/页 "
                f"共 {len(self.entries)} 个 / {self.pages} 页 "
                f"page_height={self.page_height} "
                f"夹取={self.clamp_count} 排序器={collator_name()}")


# ─── 排布 ─────────────────────────────────────────────────
def _place(cols: int, rows: int, per_page: int, cell: tuple[int, int],
           origin: tuple[int, int], tile: tuple[int, int],
           page_height: int, viewport: tuple[int, int], n: int,
           ) -> tuple[list, list, int]:
    """
    逐项算坐标。**所有排列方式共用这一段**，所以分页规则只有一处实现，
    不存在"grid 算对了、compact 忘了夹"这类漏洞。

    返回 (绝对坐标, 页内坐标, 夹取次数)。
    """
    vw, vh = viewport
    cw, ch = cell
    tw, th = tile
    ox, oy = origin

    # 兜底夹取：任何一项都不得越出可视区。这是"不能被切掉"的最后一道
    # 防线，正常情况下 count 恒为 0（排布已经保证装得下）。
    max_x = max(0, vw - tw)
    max_y = max(0, page_height - th)

    abs_pos: list[tuple[int, int]] = []
    loc_pos: list[tuple[int, int]] = []
    clamped = 0
    for i in range(n):
        page, slot = divmod(i, per_page)
        row, col = divmod(slot, cols)
        x = ox + col * cw + (cw - tw) // 2
        y = oy + row * ch + (ch - th) // 2
        cx, cy = min(max(0, x), max_x), min(max(0, y), max_y)
        if (cx, cy) != (x, y):
            clamped += 1
        loc_pos.append((cx, cy))
        abs_pos.append((cx, page * page_height + cy))
    return abs_pos, loc_pos, clamped


def _label_block(cfg: _Cfg) -> tuple[bool, int, int]:
    """返回 (是否显示文字, 行数, 文字块高度)。"""
    if cfg.hide_labels:
        return False, 0, 0
    lines = max(1, cfg.label_lines)
    return True, lines, LABEL_H * lines


def _plan_grid(cfg: _Cfg, vw: int, vh: int) -> tuple:
    """
    规则网格：可用空间按行列均分，纵向节拍 = 可视高度/行数。

    这是基线模式，参数选择刻意做成能**逐像素复现 grid.py 原来的
    _margins/_cell/_tile_size**：margin=48 时上下留白 48//6=8，
    图标 128、格 352x281、控件 352x184，全部与旧实现一致。
    """
    mt = mb = max(MIN_EDGE, cfg.margin // 6)
    ml = mr = max(MIN_EDGE, cfg.margin)
    avail_w = max(1, vw - ml - mr)
    avail_h = max(1, vh - mt - mb)
    cols, rows = cfg.columns, cfg.rows
    cw = max(1, avail_w // cols)
    ch = max(1, avail_h // rows)

    labels, lines, block = _label_block(cfg)
    icon = max(1, min(cfg.icon_size, cw - ICON_PAD))
    gap = cfg.icon_gap if labels else 0
    content_h = icon + (gap + block if labels else 0)
    tile = (max(1, cw), max(1, min(ch - TILE_VPAD, content_h)))
    origin = (ml + (avail_w - cw * cols) // 2, mt + (avail_h - ch * rows) // 2)
    return cols, rows, (cw, ch), origin, tile, icon, labels, lines, ()


def _plan_flow(cfg: _Cfg, vw: int, vh: int) -> tuple:
    """
    弹性网格：横向照旧铺满（macOS 观感的关键），**纵向节拍由内容决定**。

    单元格高度 = 内容高 * cell_ratio，再夹到 [内容高, 可用高//行数]。
    整块因此是紧凑的，多出来的纵向空间变成上下留白而不是被拉伸的
    行间距 —— 这正是 flow 与 grid 的区别：grid 永远把纵向铺满，
    flow 只在 cell_ratio 调大时才逐渐接近 grid。

    上下居中而不是顶对齐：内容块居中在 Launchpad 这种满屏界面里
    视觉重心才对，页与页之间的节拍也保持一致（不会因为某页少一个
    图标就整页往上跳）。
    """
    mt = mb = max(MIN_EDGE, cfg.margin // 6)
    ml = mr = max(MIN_EDGE, cfg.margin)
    avail_w = max(1, vw - ml - mr)
    avail_h = max(1, vh - mt - mb)
    cols, rows = cfg.columns, cfg.rows
    cw = max(1, avail_w // cols)

    labels, lines, block = _label_block(cfg)
    icon = max(1, min(cfg.icon_size, cw - ICON_PAD))
    gap = cfg.icon_gap if labels else 0
    content_h = icon + (gap + block if labels else 0)

    want = max(1, round(content_h * cfg.cell_ratio))
    ch = min(max(1, avail_h // rows), max(content_h, want))
    block_h = rows * ch

    tile = (max(1, cw), max(1, content_h))
    origin = (ml + (avail_w - cw * cols) // 2,
              mt + max(0, (avail_h - block_h) // 2))
    note = f"flow 纵向节拍 = 内容高 {content_h} * {cfg.cell_ratio:g} = {want}" \
           f"，夹取后 {ch}（上限 {max(1, avail_h // rows)}）"
    return cols, rows, (cw, ch), origin, tile, icon, labels, lines, (note,)


def _plan_compact(cfg: _Cfg, vw: int, vh: int) -> tuple:
    """
    紧凑：一屏塞尽可能多的图标。

    与另外两种模式的根本差别是**列数行数不再由设置决定**，而是按
    可用空间自动填满（硬上限 16x10，与 settings.SCHEMA 的上限一致）：

        图标     = icon_size * 0.70
        单元格宽 = 图标 + max(24, 图标*0.45)      # 余量留给省略号文字
        列数     = 可用宽 // 单元格宽，夹到 3..16
        节拍     = 内容高 * cell_ratio
        行数     = 可用高 // 节拍，夹到 1..10

    2560x1422 上默认设置：图标 90、格 151x140、16 列 10 行 = 每页 160 个
    （grid 是 7x5 = 35），所以 116 个应用一屏就能看完。
    """
    mt = mb = max(MIN_EDGE, cfg.margin // 8)
    ml = mr = max(MIN_EDGE, cfg.margin // 2)
    avail_w = max(1, vw - ml - mr)
    avail_h = max(1, vh - mt - mb)

    icon = max(1, min(round(cfg.icon_size * COMPACT_ICON_RATIO), cfg.icon_size))
    labels, lines, block = _label_block(cfg)
    lines = 1 if labels else 0                     # 紧凑模式固定单行
    gap = max(MIN_EDGE, round(icon * COMPACT_HGAP_RATIO)) if labels else 0
    content_h = icon + (gap + block if labels else 0)
    content_w = icon + max(COMPACT_MIN_LABEL_W, round(icon * COMPACT_WGAP_RATIO))

    cols = min(COLS_HARD_MAX, max(3, avail_w // max(1, content_w)))
    pitch = max(1, round(content_h * cfg.cell_ratio))
    rows = min(ROWS_HARD_MAX, max(1, avail_h // pitch))

    cw = max(1, avail_w // cols)
    ch = max(1, avail_h // rows)
    tile = (max(1, min(cw, content_w)), max(1, content_h))
    origin = (ml + (avail_w - cw * cols) // 2, mt + (avail_h - ch * rows) // 2)
    note = (f"compact 自动密度 {cols}x{rows}（设置里的 columns/rows 不参与，"
            f"硬上限 {COLS_HARD_MAX}x{ROWS_HARD_MAX}），图标 {icon}")
    return cols, rows, (cw, ch), origin, tile, icon, labels, lines, (note,)


_PLANNERS = {"grid": _plan_grid, "flow": _plan_flow, "compact": _plan_compact}


# ─── 排序 ─────────────────────────────────────────────────
def _sorted_indices(entries: Sequence[Any], cfg: _Cfg,
                    order: Sequence[int] | None,
                    usage_table: dict[str, float] | None) -> tuple[list[int], list[str]]:
    """
    算出显示顺序（元素是**输入列表下标**）。

    返回 (顺序, 提示信息)。每条分支都以 (name_key, uid, index) 收尾，
    保证键是全序：任何两条同名条目、或时间戳相同的条目，顺序都由
    uid/下标决定，重复调用结果一致。
    """
    n = len(entries)
    if order is not None:
        seen: set[int] = set()
        idx = []
        for v in order:
            i = int(v)
            if 0 <= i < n and i not in seen:
                seen.add(i)
                idx.append(i)
        idx.extend(i for i in range(n) if i not in seen)
        return idx, ["显式指定顺序，未给出的条目按名称追加到末尾"]

    names = [name_of(e) for e in entries]
    uids = [uid_of(e) for e in entries]
    notes: list[str] = []

    if cfg.sort_mode == "manual":
        for u, nm in zip(uids, names):
            _uid_to_name[u] = nm
        merged = merge_manual_order(uids)
        rank = {u: i for i, u in enumerate(merged)}
        idx = sorted(range(n), key=lambda i: (rank.get(uids[i], len(merged)),
                                              sort_key(names[i], uids[i], i)))
        # 有多少条目**不在调用方给的顺序表里**。必须跟 merge 前的表比：
        # merge 之后 rank 里每个 uid 都有了，拿 rank 比永远得到 0。
        given = set(get_manual_order())
        extra = sum(1 for u in uids if u not in given)
        if extra:
            notes.append(f"手动顺序表里没有 {extra} 个条目，已按名称追加到末尾")
        return idx, notes

    if cfg.sort_mode == "recent":
        stamps = [usage_stamp(entries[i], usage_table) for i in range(n)]
        missing = sum(1 for s in stamps if s is None)
        if missing:
            notes.append(f"{missing}/{n} 个条目没有使用记录，已排到最后（按名称）")
        idx = sorted(range(n),
                     key=lambda i: (stamps[i] is None,
                                    -(stamps[i] if stamps[i] is not None else 0.0),
                                    sort_key(names[i], uids[i], i)))
        return idx, notes

    idx = sorted(range(n), key=lambda i: sort_key(names[i], uids[i], i))
    if cfg.sort_mode == "name_rev":
        idx.reverse()
    return idx, notes


# ─── 主入口 ───────────────────────────────────────────────
def compute_layout(entries: Sequence[Any], width: int, height: int,
                   settings: Any = None, *,
                   sort_mode: str | None = None,
                   layout_mode: str | None = None,
                   usage_table: dict[str, float] | None = None,
                   order: Sequence[int] | None = None,
                   **overrides: Any) -> LayoutResult:
    """
    算排布。**纯函数**：不改 entries、不改 settings、不读文件、不读时钟，
    相同输入必然得到完全相同的结果（包括对象相等意义上的可重复）。

    entries : 任何有 .name（可选 .uid）的东西 —— library.Entry、
              测试里的假对象、dict 包装都算。
    width/height : **可视区**尺寸（不是窗口尺寸）。grid.py 里就是
              self.width() / self.height()，也就是已经扣掉搜索框和
              页码条之后的那块。
    settings : launchpad.settings.Settings / dict / None。

    可选关键字参数都是给集成方留的钩子，正常调用用不到：
      sort_mode / layout_mode : 临时覆盖设置（设置界面预览用）
      usage_table            : recent 排序的时间表
      order                  : 直接指定显示顺序（下标列表），
                               传了就完全跳过排序
      **overrides            : 逐项覆盖设置，如 icon_size=96 / margin=0。
                               只接受 _Cfg 的字段名，写错的键直接报错 ——
                               悄悄忽略一个打错的设置名，比报错难查得多。
    """
    items = list(entries)
    cfg = _Cfg.from_settings(settings)
    if sort_mode is not None:
        if sort_mode not in SORT_MODES:
            raise ValueError(f"未知排序方式 {sort_mode!r}，可选 {SORT_MODES}")
        cfg = replace(cfg, sort_mode=sort_mode)
    if layout_mode is not None:
        if layout_mode not in LAYOUT_MODES:
            raise ValueError(f"未知排列方式 {layout_mode!r}，可选 {LAYOUT_MODES}")
        cfg = replace(cfg, layout_mode=layout_mode)
    if overrides:
        fields = {f.name for f in _Cfg.__dataclass_fields__.values()}
        unknown = sorted(set(overrides) - fields)
        if unknown:
            raise TypeError(f"未知的布局参数: {unknown}；可用 {sorted(fields)}")
        cfg = replace(cfg, **overrides)

    vw = max(1, int(width))
    vh = max(1, int(height))

    idx, notes = _sorted_indices(items, cfg, order, usage_table)
    disp = [items[i] for i in idx]

    (cols, rows, cell, origin, tile, icon, labels, lines,
     extra) = _PLANNERS[cfg.layout_mode](cfg, vw, vh)

    # 归一化：控件不得大于所在单元格，也不得大于可视区。
    # 空间紧张时（窗口很小、cell_ratio 调大、rows 调多）内容块会超过节拍，
    # 不收一下就会和邻行叠在一起 —— 这个之前没人管是因为默认尺寸下
    # 内容块一直比节拍小。单元格也跟着抬到不小于控件，于是同页不重叠。
    _raw_tile = tile
    cw, ch = cell
    tw = max(1, min(tile[0], cw, vw))
    th = max(1, min(tile[1], ch, vh))
    cell = (max(cw, tw), max(ch, th))
    tile = (tw, th)
    # 起点也要收进可视区。可用空间被 margin 吃光时（60x40 这种）
    # 起点会跑到 48 以外，整张网格横着溢出屏幕。夹进来之后控件会
    # 叠在原点，但至少没有半个图标挂在屏幕外 —— 病态尺寸下这是
    # 唯一能表达"装不下"的方式。
    ox, oy = origin
    origin = (min(max(0, ox), max(0, vw - tw)), min(max(0, oy), max(0, vh - th)))

    per_page = max(1, cols * rows)
    # 页高严格等于可视区高度。取小了下一页第一个图标就会从底边漏出来，
    # 取大了翻页时中间会空一段。这个值只能是 height 本身。
    page_height = vh
    n = len(disp)
    pages = max(1, (n + per_page - 1) // per_page) if n else 1

    abs_pos, loc_pos, clamped = _place(cols, rows, per_page, cell, origin, tile,
                                       page_height, (vw, vh), n)

    all_notes = list(notes) + list(extra)
    if cfg.layout_mode == "grid":
        all_notes.append("grid 按定义均分可用空间，cell_ratio 不参与")
    all_notes.append(f"中文排序器: {collator_name()}")
    if (tw, th) != _raw_tile:
        all_notes.append(
            f"空间不足：控件从 {_raw_tile[0]}x{_raw_tile[1]} 缩到 {tw}x{th}"
            "（单元格或可视区容不下，文字可能走省略号）")

    return LayoutResult(
        mode=cfg.layout_mode,
        sort_mode=cfg.sort_mode,
        viewport=(vw, vh),
        page_height=page_height,
        columns=cols,
        rows=rows,
        per_page=per_page,
        pages=pages,
        cell=cell,
        origin=origin,
        tile_size=tile,
        icon_size=icon,
        label_lines=lines,
        labels_visible=labels,
        entries=tuple(disp),
        source_index=tuple(idx),
        positions=tuple(abs_pos),
        local_positions=tuple(loc_pos),
        sizes=tuple([tile] * n),
        clamp_count=clamped,
        notes=tuple(all_notes),
    )


def positions_for_order(entries: Sequence[Any], width: int, height: int,
                        settings: Any = None,
                        order: Sequence[Any] | None = None) -> LayoutResult:
    """
    按**调用方给定的显示顺序**算坐标，不经过任何排序。

    拖拽重排的落点：grid.py 拿到新的条目顺序（含刚被拖动的那个），
    调这个函数就能立刻算出新坐标去摆控件，连设置里的 sort_mode 都不用
    碰。order 里给出的是条目对象（按 uid 自动转成下标），也可以直接给
    下标。
    """
    items = list(entries)
    if order is None:
        idx = None
    else:
        pos = {id(e): i for i, e in enumerate(items)}
        uid_pos = {uid_of(e): i for i, e in enumerate(items)}
        idx = []
        for v in order:
            if isinstance(v, int) and 0 <= v < len(items):
                idx.append(v)
            else:
                i = pos.get(id(v))
                if i is None:
                    i = uid_pos.get(uid_of(v))
                if i is not None:
                    idx.append(i)
    return compute_layout(items, width, height, settings, order=idx)


def recompute_after_reorder(entries: Sequence[Any], width: int, height: int,
                            settings: Any = None,
                            order: Sequence[str] | None = None,
                            sort_mode: str | None = None) -> LayoutResult:
    """
    「重排之后重新算位置」的完整入口。

    做三件事：
      1. 把顺序表对齐到当前条目集合（剔除已删的、补上新增的）
      2. 写进模块级的顺序表（之后 sort_mode="manual" 就会用它）
      3. 按新顺序重算坐标

    order 省略时沿用已存的顺序表。sort_mode 省略时用设置里的值 ——
    如果调用方在 grid+name 模式下拖拽，传 sort_mode="manual" 才不会
    下一次 rebuild 又被排回名称序。
    """
    items = list(entries)
    for e in items:
        _uid_to_name[uid_of(e)] = name_of(e)
    merged = merge_manual_order([uid_of(e) for e in items], order)
    set_manual_order(merged)
    return compute_layout(items, width, height, settings, sort_mode=sort_mode)


# ─── 给 grid.py 的接入说明（示例，不执行） ────────────────
"""
    from launchpad.layouts import compute_layout, set_manual_order

    class Grid:
        def _compute_base(self):
            r = compute_layout(self._entries, self.width(), self.height(),
                               self.settings)
            self._page = 0
            self._pages = r.pages
            for t, (x, y) in zip(self._tiles, r.positions):
                t.setGeometry(x, y, *r.tile_size)
                t._base = QPoint(x, y)          # positions 已经是绝对坐标
            self._page_h = r.page_height

        def _page_height(self):
            return self._page_h

        def _apply(self):
            off = self._offset               # 负数
            for t in self._tiles:
                t.move(t._base.x(), t._base.y() + off)
"""
