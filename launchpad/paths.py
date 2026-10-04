# -*- coding: utf-8 -*-
"""
路径与配置位置。集中管理，避免各处硬编码字符串。
"""

import os
from pathlib import Path

APP_DIR_NAME = "Launchpad"
APP_TITLE = "Launchpad"


def _base(var: str) -> Path:
    p = Path(os.environ[var]) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def app_dir() -> Path:
    return _base("APPDATA")


def settings_file() -> Path:
    return app_dir() / "settings.json"


def shortcuts_file() -> Path:
    return app_dir() / "shortcuts.json"


def dismissed_file() -> Path:
    """
    被用户主动删掉的条目 uid 清单。

    为什么需要单独一个文件，而不是删了就算完：`__main__.main()` 在库为空时
    会自动扫描源目录导入，而「添加应用 → 浏览文件夹」也会整目录导入。
    没有这份清单的话，用户在界面上删掉一个图标，之后任何一次导入都会把它
    原样加回来 —— 表现为「我明明删了，它又出现了」。

    单独存而不是给 Entry 加 `hidden` 字段：Entry 是 dataclass，加字段会
    让它继续留在 library.entries 里，于是搜索、计数、去重全都还看得见它，
    只是界面上不画 —— 半吊子的删除比不删除更糟。
    """
    return app_dir() / "dismissed.json"


def renames_file() -> Path:
    """
    用户给条目起的自定义名字：``uid -> 显示名`` 的映射。

    为什么必须单独一个文件，而不是只改 ``Entry.name`` 就完事：

    ``Entry.name`` 是**从 .lnk 读出来的原始名**。直接改它有三个后果，

    1. **原名彻底找不回来了。** 搜索按 name 走，改完名字之后用户再也搜不到
       「微信」，除非他记得自己改成了什么。改名应该是「加一个显示层」，
       不是「覆盖原名」—— 原来的快捷方式在磁盘上一个字节都没动，凭什么
       它的名字也不能被搜到。
    2. **重新导入会丢。** 库为空时的首次自动导入、「添加应用 → 浏览文件夹」
       都会从 .lnk 重建条目，``name`` 会被刷回原始值。这和墓碑要解决的是
       **同一个问题**（改过的东西在导入后复活/丢失），所以用同一种解法。
    3. **uid 会跟着变。** 读不出 target 的条目 uid 是
       ``sha1(source_lnk|name)``，改了 name 就换了 uid —— 墓碑、去重、
       图标缓存键全部对不上。

    所以：``Entry.display`` 只作为**内存里的物化缓存**，磁盘上的真相是这份
    ``renames.json``。:meth:`Library.load` 和 :meth:`Library.import_folder`
    都会重新把它贴回去。
    """
    return app_dir() / "renames.json"


def icon_dir() -> Path:
    p = Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME / "icons"
    p.mkdir(parents=True, exist_ok=True)
    return p


def log_file() -> Path:
    p = Path(os.environ["LOCALAPPDATA"]) / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p / "launchpad.log"


def app_icon_file() -> Path:
    """
    自带图标的 .ico 位置。

    指向仓库内的 ``assets/``，所以整个目录拷走就能跑（portable），
    不用往 Program Files 或 %LOCALAPPDATA% 里装东西。图标由仓库根的
    ``make_icon.py`` 生成，缺失时调用方应降级而不是报错。
    """
    return install_root() / "assets" / "launchpad.ico"


# 默认导入目录。
#
# 原先这里硬编码了作者本人的快捷方式目录。
# 那不只是隐私问题，还是**功能问题**：别人 clone 下来这个常量指向一个
# 不存在的目录，首次自动导入静默失败（不报错，只是库里什么都没有），
# 看起来就像「程序坏了」。
#
# 现在按环境变量 → 用户目录的顺序找，并且**只挑真实存在的那个**。
# 找不到就返回空路径，由调用方决定怎么办，绝不假装成功。
DEFAULT_SOURCE_ENV = "LAUNCHPAD_SOURCE"


def _default_source() -> Path:
    env = os.environ.get(DEFAULT_SOURCE_ENV)
    if env:
        return Path(env)
    home = Path.home()
    for name in ("Desktop", "桌面", "OneDrive/Desktop", "OneDrive/桌面"):
        d = home / name
        if d.is_dir():
            return d
    return home


DEFAULT_SOURCE = _default_source()


def install_root() -> Path:
    return Path(__file__).resolve().parents[1]
