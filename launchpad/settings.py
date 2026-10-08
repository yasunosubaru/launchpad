# -*- coding: utf-8 -*-
"""
用户设置持久化。

之前所有布局参数都写死在代码里，改一次要动多个文件，而且用户
没法调。这里把可调项集中到一个 JSON，存 %APPDATA%\\Launchpad\\settings.json。

设计要点：
- 原子写入（临时文件 + os.replace），中途断电不会毁掉设置
- 读取时对每个键做类型校验和范围钳制：设置文件被手改坏过时
  不能导致程序起不来，退回默认值即可
- 提供 reset() 供设置界面"恢复默认"
"""

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .paths import APP_DIR_NAME


def settings_dir() -> Path:
    p = Path(os.environ["APPDATA"]) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return settings_dir() / "settings.json"


# ── 默认值与校验规则 ────────────────────────────────────
# (默认值, 类型, 最小值, 最大值)
SCHEMA: dict[str, tuple] = {
    # 布局
    "columns":            (7, int, 3, 16),
    "rows":               (5, int, 1, 10),
    "icon_size":          (128, int, 48, 256),
    "icon_gap":           (22, int, 0, 80),      # 图标到文字的间距
    "label_lines":        (1, int, 1, 2),       # 文字行数
    # 排列方式：grid=规则网格, flow=按名称长度流式, compact=紧凑
    "layout_mode":        ("grid", str, None, None),
    "sort_mode":          ("name", str, None, None),
    "cell_ratio":         (1.0, float, 0.6, 2.0),   # 单元格宽高比
    "margin":             (48, int, 0, 200),
    # 行为
    "hide_labels":        (False, bool, None, None),
    "click_through_empty":(True, bool, None, None),  # 左键空白退出
    # 点图标启动后是否收起启动器。
    # False（默认）= 启动后**留在原地**，可以接着点别的应用 —— 用户要的是这个：
    # 「点开一个应用后不要自动退出，我有可能还要开启别的应用」。
    # True = macOS Launchpad 的行为，点一下就走，下一个应用等下次热键再唤。
    # 做成设置而不是写死，是因为两种都有人要；而写死的话改主意就得改代码。
    "hide_after_launch":  (False, bool, None, None),
    "paging_animation":   (True, bool, None, None),
    "paging_style":       ("smooth", str, None, None),
    "page_duration":      (320, int, 80, 900),
    "page_easing":        ("outQuint", str, None, None),
    "hover_anim":         (True, bool, None, None),
    "open_anim":          (True, bool, None, None),
    "open_duration":      (220, int, 60, 800),
    "reduce_motion":      (False, bool, None, None),
    # 滚轮手势节流窗口（ms）。一次手势 = 一次翻页。
    # 调大 = 划快一点才翻第二页；调小 = 连划容易一次滑两页。
    "wheel_gesture_ms":   (220, int, 80, 600),
    # 搜索
    "pinyin_search":      (True, bool, None, None),
    # 触控板手势唤出总开关。**默认 False**。
    #
    # 为什么默认**关**（反复开关过两次，两次都是被用户报「鼠标滚轮一划
    # 就弹出来」推翻的）：这次终于想明白了 —— 它不是阈值调错了，是
    # **判据测不到它想测的东西**。
    #
    # Windows 上**两指滑动不移动光标**。手势是通过 ``WH_MOUSE_LL`` 的
    # ``WM_MOUSEWHEEL`` 观察到的，而那条消息里只有 delta 和当时的
    # ``pt``（光标位置）—— 没有任何字段描述「手指往哪个方向动了」。
    # 于是「起点在屏幕边缘」实际测的是「光标碰巧停在边上」，而不是
    # 「从边缘滑了进来」。而经典滚动条就占屏幕最右 17px，任何 >= 20px
    # 的边缘带都必然把它包进去。
    #
    # 更细的信号在这台机器上也拿不到：``MULTITOUCH_AVAILABLE`` 未置位、
    # ``GetPointerDevices`` 返回 0、无厂商触控板驱动、
    # ``RegisterRawInputDevices`` 对每一种usage（含标准鼠标）都返
    # FALSE / err=87 —— RawInput 多点触控这条路是封死的。
    #
    # 结论：**没有可靠的办法把「触控板轻弹」和「鼠标滚轮快滚两下」
    # 区分开**。那就不要赌它。热键 ``Alt+Space`` 零误触、两只手都在
    # 底排，是可靠入口；手势保留为显式开启的可选项。
    "wake_gesture":       (False, bool, None, None),
    # 上面开着时才生效：手势起点离屏幕侧边多少像素内才算数。0 = 不限。
    #
    # 120 而不是原来的 40：40px 只有屏宽的 2.3%，等于要求指针精确停在
    # 屏幕最边缘，实操上几乎瞄不准 —— 这才是「手势没反应」的真正原因
    # （不是 DPI 换算问题，那个结论已被实测推翻，见 wheelhook._edge_ok）。
    # 120px 约等于屏宽 7%，手不用刻意伸到边缘。
    #
    # 120px 同时也是上面那个「赌」的具体代价：实测 5 分钟、2974 个光标
    # 采样点里，有 1.0% 的时间指针停在这个带内。这条数据还说明
    # **误触并不频繁** —— 用户抱怨的不是「太频繁」而是「会」。所以选择
    # 默认关掉，而不是把边缘带收窄（收窄会把「瞄不准」的老问题带回来）。
    "wake_edge_px":       (120, int, 0, 400),
    # 手势需要累积多少滚轮刻度才唤出（120 = 一格）。调大 = 要滑更快更多。
    # 保持 2：这台机器实测 delta 规整 ±120，两格= 一次两指快滑两下，
    # 而正常滚动每格间隔远大于 250ms，两者分得开。
    "wake_notches":       (2, int, 1, 10),
}

LAYOUT_MODES = ("grid", "flow", "compact")
SORT_MODES = ("name", "name_rev", "recent", "manual")
EASINGS = ("outQuint", "outCubic", "inOutCubic", "outExpo", "spring")


def _coerce(key: str, value: Any) -> Any:
    """把任意值转成该键的合法值，非法则返回默认值。"""
    default, typ, lo, hi = SCHEMA[key]
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        if isinstance(value, (int, float)):
            return bool(value)
        return default

    if typ is int:
        try:
            v = int(round(float(value)))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    if typ is float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    # 字符串：白名单校验
    if not isinstance(value, str):
        return default
    if key == "layout_mode" and value not in LAYOUT_MODES:
        return default
    if key == "sort_mode" and value not in SORT_MODES:
        return default
    if key == "paging_style" and value not in ("smooth", "instant"):
        return default
    if key == "page_easing" and value not in EASINGS:
        return default
    return value


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else settings_file()
        self._data: dict[str, Any] = {k: v[0] for k, v in SCHEMA.items()}
        self.load_error = ""

    # ── 读写 ──────────────────────────────────────────────
    def load(self) -> None:
        self._data = {k: v[0] for k, v in SCHEMA.items()}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                shutil.copy2(self.path,
                             self.path.with_name(f"settings.corrupt-{stamp}.json"))
            except OSError:
                pass
            self.load_error = f"设置文件损坏，已用默认值: {exc}"
            return

        if not isinstance(raw, dict):
            self.load_error = "设置文件顶层应为对象，已用默认值"
            return

        unknown = [k for k in raw if k not in SCHEMA]
        if unknown:
            self.load_error = f"忽略未知设置项: {', '.join(sorted(unknown))}"

        for k, v in raw.items():
            if k in SCHEMA:
                self._data[k] = _coerce(k, v)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2,
                             sort_keys=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)

    # ── 访问 ──────────────────────────────────────────────
    def get(self, key: str) -> Any:
        if key not in self._data:
            raise KeyError(f"未知设置项: {key}")
        return self._data[key]

    def set(self, key: str, value: Any) -> None:
        if key not in SCHEMA:
            raise KeyError(f"未知设置项: {key}")
        self._data[key] = _coerce(key, value)

    def update(self, values: dict) -> None:
        for k, v in values.items():
            if k in SCHEMA:
                self._data[k] = _coerce(k, v)

    def reset(self) -> None:
        self._data = {k: v[0] for k, v in SCHEMA.items()}
        self.save()

    def as_dict(self) -> dict:
        return dict(self._data)

    # ── 便捷属性 ──────────────────────────────────────────
    @property
    def columns(self) -> int:
        return self._data["columns"]

    @property
    def rows(self) -> int:
        return self._data["rows"]

    @property
    def icon_size(self) -> int:
        return self._data["icon_size"]

    @property
    def reduce_motion(self) -> bool:
        return self._data["reduce_motion"]