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
    "paging_animation":   (True, bool, None, None),
    "paging_style":       ("smooth", str, None, None),
    "page_duration":      (320, int, 80, 900),
    "page_easing":        ("outQuint", str, None, None),
    "hover_anim":         (True, bool, None, None),
    "open_anim":          (True, bool, None, None),
    "open_duration":      (220, int, 60, 800),
    "reduce_motion":      (False, bool, None, None),
    # 搜索
    "pinyin_search":      (True, bool, None, None),
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