# -*- coding: utf-8 -*-
"""
「添加应用」：收集 → 预览 → 去重写入 → 明确反馈。

用户原话是"添加增加快捷方式的功能，又可能我有新的图表想要添加进来"。
背后有两件事：

1. **能把新的东西加进来** —— .lnk / .exe / 文件夹 / UWP 应用商店应用，
   拖拽或点选都行。
2. **加进来能启动** —— 尤其是 UWP。现有库里有读不出 TargetPath 的条目，
   ``window._launch`` 只会 Popen 一个空路径，界面上显示 ⚠，点了没反应。
   本模块负责把这类条目**修好**：从 .lnk 里把 AUMID 捞出来，改写成
   ``shell:AppsFolder\\<AUMID>`` 这种可以直接调起的形式。

UWP 目标为什么用 ``shell:AppsFolder\\...``
------------------------------------------
UWP 应用没有可执行文件路径，Windows 只认 ``AppsFolder`` 这个 shell 命名空间。
实测四种调起方式（``explorer.exe`` / ``os.startfile`` / ``cmd /c start`` /
``ShellExecuteW``）**全部**能真的启动（见 ``tests_add.py`` 的真机验证）。
本模块统一用 ``os.startfile``，失败再退回 ``explorer.exe``。

``Entry.target`` 里带 ``shell:`` 前缀就是"这不是文件路径"的信号，
``window.py`` 的 ``_launch`` 需要加一个分支 —— 见文件末尾的集成说明。

去重策略
--------
默认 SKIP，但有一条**例外**必须存在：库里已有的、target 读不出（missing）
条目，如果这次能把同一个 .lnk 解析成可启动目标，那不是"重复"而是**修复**，
就地升级而不是跳过。否则用户永远修不好那几个 ⚠ 条目 —— 而"我的快捷方式
都没添加上去"恰恰是最初要解决的问题。

依赖边界
--------
只 import ``library``（只读）。**不** import theme / grid / tile / window /
icons，所以并行修改那几个模块不会波及本文件；Qt 的 import 也延迟到真的
开对话框时，因此无界面环境下可以直接 import 本模块跑测试。
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from .library import Entry, Library

# ── 常量 ────────────────────────────────────────────────────────────────

SHELL_PREFIX = "shell:"
APPSFOLDER = "shell:AppsFolder\\"

#: 可直接 Popen 的目标类型后缀
LAUNCHABLE_SUFFIXES = (".exe", ".bat", ".cmd", ".com")

#: 会被扫描的快捷方式 / 程序后缀
LNK_SUFFIXES = (".lnk", ".url")

#: AUMID 形状：PackageFamilyName_<publisher hash>!AppId
AUMID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*_[a-z0-9]{8,13}![A-Za-z0-9][A-Za-z0-9._\-]*$")

#: 扫 .lnk 二进制时用的宽松版（同一段字节里可能出现前后缀）
AUMID_SCAN_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._\-]*_[a-z0-9]{8,13}![A-Za-z0-9][A-Za-z0-9._\-]*")

#: 文件系统扫描的闸门。开始菜单目录和 WindowsApps 都远超这个数，
#: 不设上限的话一次误拖就能卡死界面。
MAX_SCAN_FILES = 4000
MAX_SCAN_DEPTH = 8

#: 让 subprocess 不弹控制台窗口
CREATE_NO_WINDOW = 0x08000000

#: 扫目录时直接跳过的名字
_SKIP_DIRS = frozenset({
    ".git", "node_modules", "$recycle.bin", "system volume information",
    "__pycache__", ".svn", ".venv", "venv",
})


# ── 结果模型 ────────────────────────────────────────────────────────────

class AddPolicy:
    """重复项怎么处理。"""
    SKIP = "skip"            # 跳过（默认）
    REPLACE = "replace"      # 用新的覆盖旧的（保持原位置）
    KEEP_BOTH = "keep_both"  # 允许重复

    ALL = (SKIP, REPLACE, KEEP_BOTH)
    LABELS = {
        SKIP: "跳过重复项（推荐）",
        REPLACE: "覆盖已有项",
        KEEP_BOTH: "全部保留（允许重复）",
    }


class RowState:
    NEW = "new"
    DUPLICATE = "duplicate"
    UPGRADE = "upgrade"      # 修好一个已存在的失效条目
    REPLACE = "replace"
    FAILED = "failed"

    LABELS = {
        NEW: "新增",
        DUPLICATE: "重复·跳过",
        UPGRADE: "修复失效项",
        REPLACE: "覆盖",
        FAILED: "失败",
    }

    #: 预览表里"会被写入"的状态
    WRITING = (NEW, UPGRADE, REPLACE)


@dataclass
class UwpApp:
    """开始菜单里的一条可启动记录。"""
    name: str
    app_id: str
    kind: str                      # "appx" | "appsfolder" | "path"
    icon_hint: str = ""            # 已知的 .lnk，用作图标来源

    @property
    def label(self) -> str:
        return {"appx": "应用商店 / UWP",
                "appsfolder": "AppsFolder",
                "legacy": "经典程序（开始菜单注册项）",
                "path": "经典程序",
                "url": "网页链接"}.get(self.kind, self.kind)

    @property
    def shell_target(self) -> str:
        return shell_uri(self.app_id)

    @property
    def is_appsfolder(self) -> bool:
        """能不能包进 ``shell:AppsFolder\\...`` 直接调起。"""
        return self.kind in ("appx", "appsfolder", "legacy")


@dataclass
class Candidate:
    """一条待添加的条目。"""
    entry: Entry
    origin: str = ""               # 来自哪儿，给用户看
    note: str = ""


@dataclass
class ScanReport:
    candidates: list[Candidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    truncated: bool = False

    def merge(self, other: "ScanReport") -> "ScanReport":
        self.candidates.extend(other.candidates)
        self.errors.extend(other.errors)
        self.unsupported.extend(other.unsupported)
        self.truncated = self.truncated or other.truncated
        return self


@dataclass
class PreviewRow:
    entry: Entry
    state: str
    detail: str = ""
    existing: Entry | None = None

    @property
    def label(self) -> str:
        return RowState.LABELS.get(self.state, self.state)

    @property
    def selectable(self) -> bool:
        return self.state in RowState.WRITING


@dataclass
class AddResult:
    added: int = 0
    skipped: int = 0
    upgraded: int = 0
    replaced: int = 0
    failed: int = 0
    entries: list[Entry] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)   # (名称, 原因)

    @property
    def total_changed(self) -> int:
        return self.added + self.upgraded + self.replaced

    @property
    def ok(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        bits = [f"成功添加 {self.added} 项"]
        if self.upgraded:
            bits.append(f"修复失效 {self.upgraded} 项")
        if self.replaced:
            bits.append(f"覆盖 {self.replaced} 项")
        if self.skipped:
            bits.append(f"跳过重复 {self.skipped} 项")
        if self.failed:
            bits.append(f"【失败 {self.failed} 项】")
        if not self.total_changed:
            bits.append("本次没有改动任何条目")
        return " · ".join(bits)


# ── AUMID / AppsFolder ──────────────────────────────────────────────────

def shell_uri(app_id: str) -> str:
    """把 AUMID（或任意 AppsFolder 相对 ID）包成可启动的 shell: URI。"""
    return f"{APPSFOLDER}{app_id}"


def is_shell_target(target: str) -> bool:
    return (target or "").strip().lower().startswith(SHELL_PREFIX)


def is_appsfolder_target(target: str) -> bool:
    return (target or "").strip().lower().startswith(APPSFOLDER.lower())


def classify_app_id(app_id: str) -> str:
    """
    给开始菜单的 AppID 分类。

    ``Get-StartApps`` 返回的 AppID 实际有四种形态，全部实测过：

    - ``"appx"``       带 ``!`` 的 AUMID，例如
                       ``Microsoft.WindowsStore_8wekyb3d8bbwe!App``
    - ``"legacy"``     ``{GUID}\\子目录\\app.exe``，开始菜单给非 MSIX 程序
                       分配的注册项。**仍然只能用 AppsFolder 调起**
                       （实测 Notepad++、Acrobat 都这样起来过）
    - ``"path"``       ``C:\\...\\app.exe`` 这样的真实路径，可以直接 Popen
    - ``"url"``        ``https://...``，安装程序留下的文档链接
    - ``"appsfolder"`` 没有反斜杠的裸 ID：Electron 商店版、Chrome 扩展、
                       WSL 转发项（``Microsoft.AutoGenerated.{...}``）
    - ``"invalid"``    脏数据。``Get-StartApps`` 里确实混着两类：本机实测有
                       SOLIDWORKS 一堆二进制乱码（``.}4$t8Nr@9lHzHKwgKZ...``）
                       和 shell 命名空间项（``::{20D04FE0-...}`` 即"此电脑"）。
                       不过滤会生成一堆永远启动不了的条目。
    """
    s = (app_id or "").strip()
    if not s or len(s) > 256:
        return "invalid"
    # 盘符路径和 URL 必须**先**判：``C:\...`` 和 ``https://...`` 里都含
    # ``:``，落进下面的字符黑名单会被误杀（第一版就因为这个，431 条里
    # 被吃掉 192 条经典程序）。
    if re.match(r"^[A-Za-z]:[\\/]", s):
        return "path"
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", s):
        return "url"
    if any(ord(c) < 32 for c in s) or any(ch in s for ch in '<>|:"?*'):
        return "invalid"
    if "\\" in s or "/" in s:
        # {GUID}\... 这类 shell 注册项
        return "legacy" if re.match(r"^\{?[0-9A-Fa-f\-]{8,}\}?\\", s) \
            else "invalid"
    if "!" in s:
        return "appx" if AUMID_RE.match(s) else "invalid"
    return "appsfolder"


def aumid_from_lnk(path: Path | str,
                   known: Iterable[str] | None = None) -> str:
    """
    从 .lnk 的原始字节里捞出它指向的 AUMID。

    为什么不用正经 API
    -----------------
    ``WScript.Shell.CreateShortcut().TargetPath`` 对 UWP 链接返回空 ——
    这就是现有库里那几个 ⚠ 条目的成因。正解要走
    ``IShellLinkW::GetData(DLGTARGET)`` 或 ``IPropertyStore`` +
    ``PKEY_AppUserModel_ID``，但本机的 pywin32 **没有**导出
    ``CLSID_ShellLink`` / ``DLGTARGET``，``pythoncom`` 也没有
    ``IID_IPropertyStore``；纯 ctypes 手搓 vtable 调用实测直接
    access violation。``Get-AppxPackageManifest`` 更慢，且只在包里
    显式存了 AUMID 时可用。

    所以这里做字节扫描，并**用 Get-StartApps 的真实 ID 集合交叉验证** ——
    命中的一定是本机真实存在的应用，误报概率为零。实测
    ``某文件夹\\Codex.lnk`` 能正确读出
    ``OpenAI.Codex_2p2nqsd0c76g0!App``。

    :param known: 已知的合法 AppsFolder ID 集合。给了就只返回交集内的结果。
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    hits: set[str] = set()
    # UWP 链接把 AUMID 存成 UTF-16LE；少数工具写成单字节，一并扫。
    for enc in ("utf-16-le", "latin-1"):
        text = raw.decode(enc, errors="ignore")
        for m in AUMID_SCAN_RE.findall(text):
            if AUMID_RE.match(m):
                hits.add(m)
    if not hits:
        return ""
    if known is not None:
        known = set(known)
        hits &= known
        if not hits:
            return ""
    # 多个候选时取最短的：更可能是主应用 id 而不是某个子功能 id
    return sorted(hits, key=len)[0]


def launch_shell_uri(uri: str) -> tuple[bool, str]:
    """
    调起一个 ``shell:`` URI（主要是 ``shell:AppsFolder\\...``）。

    实测四种方式都能启动（``tests_add.py`` 里有真机验证）。首选
    ``os.startfile``：不额外起进程，语义就是"用默认方式打开"；
    失败时退回 ``explorer.exe``。

    返回 ``(是否成功, 原因)``。
    """
    if not is_shell_target(uri):
        return False, f"不是 shell: URI：{uri}"
    try:
        os.startfile(uri)          # noqa: S606  (Windows 专用)
        return True, ""
    except Exception as exc:
        first = exc
    try:
        subprocess.Popen(["explorer.exe", uri],
                         creationflags=CREATE_NO_WINDOW)
        return True, ""
    except Exception as exc:
        return False, f"{first}（退回 explorer.exe 也失败：{exc}）"


def launch_entry(entry: Entry) -> tuple[bool, str]:
    """
    启动一个条目。**已经包含 UWP 分支**，所以 ``window.py`` 的 ``_launch``
    可以整个函数体换成调用它（见文件末尾"集成方式"）。

    返回 ``(是否成功, 原因)``。
    """
    target = (entry.target or "").strip()

    if is_shell_target(target):
        return launch_shell_uri(target)

    if not target:
        return False, "目标为空（快捷方式指向的应用商店应用无法解析）"

    # http(s) / mailto 快捷方式（.url）
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", target) or \
            target.lower().startswith("mailto:"):
        try:
            os.startfile(target)     # noqa: S606
            return True, ""
        except Exception as exc:
            return False, str(exc)

    try:
        # 下面四处 Popen **每一处**都必须显式带 CREATE_NO_WINDOW，漏一处
        # 就漏一类黑框。
        #
        # 机制（不是"美化"，是 Win32 的分配规则）：CreateProcess 时如果
        # 调用方自己**没有控制台**，而新进程是**控制台子系统**（PE 头里
        # Subsystem = CONSOLE），Windows 就会给它**新分配一个控制台** ——
        # 那是一个真实存在、可见的控制台窗口，等进程退出就消失。Launchpad
        # 由 pythonw.exe 启动（GUI 子系统，自己就没有控制台），所以
        # cmd.exe、python.exe、控制台型 .exe 全部命中这条规则，用户看到的
        # 就是"点一下闪一个黑框"。只有显式给 CREATE_NO_WINDOW（或
        # DETACHED_PROCESS）才能让 Windows 跳过这次分配；什么都不传就是
        # 默认行为，闪框是必然而不是偶发。
        #
        # 注意方向：这条只针对**控制台子系统**的目标。真正的 GUI 程序
        # （pythonw.exe、绝大多数 GUI .exe）默认就不分配控制台，本来不闪。
        # 但"目标恰好是 GUI 程序"不能作为省略标志的理由 —— 那取决于用户
        # 往库里拖了什么，四条分支各自都会被走到。
        if target.lower().endswith((".bat", ".cmd")):
            # .bat / .cmd 必须经 cmd.exe 才能执行，而 cmd.exe 一定是控制台
            # 子系统 —— 这一条是黑框的**主要**来源，标志位在这里最要紧。
            subprocess.Popen(["cmd", "/c", target],
                             cwd=entry.workdir or None,
                             creationflags=CREATE_NO_WINDOW)
        elif target.lower().endswith(".py"):
            # sys.executable 在本项目里可能是 python.exe（控制台）也可能是
            # pythonw.exe（GUI）。是 python.exe 时它就是控制台子系统目标，
            # CREATE_NO_WINDOW 从"可选优化"变成**必需**，否则每个 .py 条目
            # 都会闪一帧黑框。
            #
            # 这里**刻意不动它、继续用 sys.executable**，而不是换成同目录
            # 的 pythonw.exe：pythonw.exe 编译进 GUI 子系统，从根上就不分配
            # 控制台，确实更彻底，但代价是脚本的 stdout/stderr 被彻底丢弃
            # —— 脚本崩了用户只看到"点了没反应"，没有 traceback；
            # ``python -u x.py``、输出重定向、attach 调试器这些手段也全部
            # 失效。当前要解决的是"闪一下"，不是"静默跑脚本"，所以保留
            # 可调试性、只压掉窗口。想要真正静默启动的用户，应该在条目的
            # 参数里显式写 pythonw.exe，由他自己决定，而不是在这里替他决定。
            subprocess.Popen([sys.executable, target],
                             cwd=entry.workdir or None,
                             creationflags=CREATE_NO_WINDOW)
        elif entry.args:
            # 带参数的通用分支（.exe / .com / .bat 都可能落到这里）。
            subprocess.Popen([target, entry.args],
                             cwd=entry.workdir or None,
                             creationflags=CREATE_NO_WINDOW)
        else:
            # 无参数的通用分支。理由同上一条：目标是控制台型 .exe 时，
            # 不给标志就会分配一个可见控制台。
            subprocess.Popen([target],
                             cwd=entry.workdir or None,
                             creationflags=CREATE_NO_WINDOW)
        return True, ""
    except Exception as exc:
        return False, str(exc)


# ── 开始菜单 / 应用商店枚举 ─────────────────────────────────────────────

_PS_CACHE: dict = {}


def _run_ps(script: str, timeout: float = 25.0) -> str:
    """跑一段 PowerShell 并拿 UTF-8 结果。绝不抛异常。"""
    prelude = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "$OutputEncoding=[System.Text.Encoding]::UTF8;")
    try:
        p = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", prelude + script],
            capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return ""
    if p.returncode != 0:
        return ""
    return p.stdout.decode("utf-8-sig", errors="replace")


def _load_ps_json(raw: str) -> list:
    try:
        data = json.loads(raw) if raw.strip() else []
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    return [d for d in data if isinstance(d, dict)]


def list_start_apps(refresh: bool = False,
                    cache_seconds: float = 300.0) -> list[UwpApp]:
    """
    列出开始菜单里所有可启动的应用。

    **为什么用 ``Get-StartApps`` 而不是扫开始菜单下的 .lnk**

    - .lnk 里存的是 ``TargetPath``，UWP 链接这一栏是空的 —— 这正是库里
      那些 ⚠ 条目的来源，扫出来还是启动不了。开始菜单里的数百个 .lnk
      里，字节扫描能捞到 AUMID 的数量是 **0**。
    - ``Get-StartApps`` 直接读开始菜单自己维护的索引，UWP 条目带的是
      **AUMID**（``Microsoft.WindowsStore_8wekyb3d8bbwe!App``），
      包进 ``shell:AppsFolder\\`` 就能调起。
    - 它还顺手给出非 UWP 但同样只能用 AppsFolder 调起的东西（Electron、
      Chrome 扩展、WSL 转发项），这些的 .lnk 同样没有可执行路径。

    实测本机 431 条 / 约 0.7 秒，其中 45 条带 ``!``、271 条不带反斜杠。
    结果按名字缓存，连开对话框不会反复起 PowerShell。
    """
    now = time.time()
    hit = _PS_CACHE.get("apps")
    if not refresh and hit and now - hit[0] < cache_seconds:
        return list(hit[1])

    data = _load_ps_json(_run_ps(
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"))

    out: list[UwpApp] = []
    seen: set[tuple[str, str]] = set()
    for item in data:
        name = str(item.get("Name") or "").strip()
        app_id = str(item.get("AppID") or "").strip()
        kind = classify_app_id(app_id)
        if kind == "invalid" or not name:
            continue
        key = (name.lower(), app_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(UwpApp(name=name, app_id=app_id, kind=kind))

    out.sort(key=lambda a: a.name.lower())
    _PS_CACHE["apps"] = (now, list(out))
    return out


def known_aumids(refresh: bool = False) -> set[str]:
    """本机真实存在的全部 AppsFolder ID（appx + appsfolder）。"""
    return {a.app_id for a in list_start_apps(refresh=refresh)
            if a.is_appsfolder}


def package_install_locations(refresh: bool = False) -> dict[str, str]:
    """PackageFamilyName -> 安装目录。给 UWP 图标用。失败返回空表。"""
    now = time.time()
    hit = _PS_CACHE.get("pkgs")
    if not refresh and hit and now - hit[0] < 3600.0:
        return dict(hit[1])

    data = _load_ps_json(_run_ps(
        "Get-AppxPackage | Select-Object PackageFamilyName,InstallLocation | "
        "ConvertTo-Json -Compress", timeout=40.0))
    out = {str(d.get("PackageFamilyName") or "").strip():
           str(d.get("InstallLocation") or "").strip()
           for d in data
           if str(d.get("PackageFamilyName") or "").strip()
           and str(d.get("InstallLocation") or "").strip()}
    _PS_CACHE["pkgs"] = (now, dict(out))
    return out


#: logo 文件名里越靠前越像"启动器图标"
_LOGO_TIERS = ("square44x44logo", "square150x150logo", "square310x310logo",
               "squarelogo", "storelogo", "appicon", "logo")
#: 一律不要：启动画面、宽图、非方形磁贴、深浅色适配版（用错会看不清）
_LOGO_REJECT = ("splashscreen", "widelogo", "widetile", "largetile", "medtile",
                "smalltile", "altform", "unplated", "storehero", "screenshot")


def _pick_logo(assets: Path) -> Path | None:
    """从包的 Assets 里挑一张最像"启动器图标"的 png。取不到返回 None。"""
    if not assets.is_dir():
        return None
    best: tuple[tuple[int, int], Path] | None = None
    try:
        files = list(assets.glob("*.png"))
    except OSError:
        return None
    for f in files:
        low = f.name.lower()
        if any(bad in low for bad in _LOGO_REJECT):
            continue
        tiers = [i for i, t in enumerate(_LOGO_TIERS) if t in low]
        if not tiers:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        key = (tiers[0], -size)
        if best is None or key < best[0]:
            best = (key, f)
    return best[1] if best else None


def uwp_icon_path(app_id: str) -> str:
    """
    UWP 应用的 logo png 路径。取不到返回空串（调用方走占位图）。

    ``C:\\Program Files\\WindowsApps`` 里的安装目录实测**可读**，所以
    不需要额外提权。UWP 应用本身没有 exe 路径，``QFileIconProvider``
    对 ``shell:AppsFolder\\...`` 一律返回空 icon（实测），只能走这里。
    """
    fam = (app_id or "").split("!")[0].strip()
    if not fam:
        return ""
    loc = package_install_locations().get(fam)
    if not loc:
        return ""
    logo = _pick_logo(Path(loc) / "Assets")
    return str(logo) if logo else ""


# ── 从文件 / 应用构造条目 ───────────────────────────────────────────────

def entry_from_file(path: Path | str,
                    known: Iterable[str] | None = None) -> tuple[Entry | None, str]:
    """
    单个文件 -> 条目。返回 ``(条目或 None, 错误原因)``。

    读不出 TargetPath 的 .lnk 会**自动尝试**从字节里捞 AUMID 并改写成
    ``shell:AppsFolder\\...``；捞到了就是可启动的正常条目，捞不到才标
    missing。用户不需要知道 UWP 这回事。
    """
    try:
        p = Path(path)
    except TypeError as exc:
        return None, f"{path!r}：不是合法的路径（{exc}）"
    name = p.stem.strip()
    if not name:
        return None, f"{p.name}：文件名不含可用名称"

    suf = p.suffix.lower()

    if suf in LNK_SUFFIXES:
        from .library import read_lnk            # 复用既有解析，别重写一遍
        try:
            info = read_lnk(p)
        except Exception as exc:
            return None, f"{p.name}：无法读取快捷方式（{exc}）"

        target = (info.get("target") or "").strip()
        args = info.get("args") or ""
        workdir = info.get("workdir") or ""
        icon = info.get("icon") or ""

        if not target:
            app_id = aumid_from_lnk(p, known)
            if app_id:
                return (Entry(name=name, target=shell_uri(app_id),
                              args="", workdir="", icon=uwp_icon_path(app_id),
                              source_lnk=str(p), keywords=[], missing=False), "")
            return (Entry(name=name, target="", args=args, workdir=workdir,
                          icon=icon, source_lnk=str(p), keywords=[],
                          missing=True),
                    f"{p.name}：读不出启动目标，链接里也没有应用 ID"
                    f"（链接损坏，或它指向的东西没装在这台机器上）")

        if suf == ".url" or _looks_like_uri(target):
            # .url / 网页快捷方式：直接交给 os.startfile，不做存在性检查
            return Entry(name=name, target=target, args=args, workdir=workdir,
                         icon=icon or target, source_lnk=str(p),
                         keywords=[], missing=False), ""

        if not Path(target).exists():
            return (Entry(name=name, target=target, args=args, workdir=workdir,
                          icon=icon, source_lnk=str(p), keywords=[],
                          missing=True),
                    f"{p.name}：目标已不存在（{target}）")

        return Entry(name=name, target=target, args=args, workdir=workdir,
                     icon=icon, source_lnk=str(p), keywords=[],
                     missing=False), ""

    if suf in LAUNCHABLE_SUFFIXES:
        # .exe 也查存在性：拖进来一个已经被删掉的可执行，生成一个永远
        # 启动不了的 ⚠ 条目比明确报错更糟。与 library.scan_folder 的
        # 判据保持一致。
        if not p.exists():
            return (Entry(name=name, target=str(p), args="",
                          workdir=str(p.parent), icon=str(p), source_lnk="",
                          keywords=[], missing=True),
                    f"{p.name}：文件已不存在（{p}）")
        return Entry(name=name, target=str(p), args="", workdir=str(p.parent),
                     icon=str(p), source_lnk="", keywords=[],
                     missing=False), ""

    return None, f"{p.name}：不支持的格式{suf or '（无后缀）'}"


def entry_from_app(app: UwpApp) -> tuple[Entry | None, str]:
    """开始菜单 / 应用商店记录 -> 条目。"""
    if app.kind == "url":
        return Entry(name=app.name, target=app.app_id, icon="",
                     source_lnk="", keywords=[], missing=False), ""

    if app.kind == "path":
        if not Path(app.app_id).exists():
            return (Entry(name=app.name, target=app.app_id, icon=app.app_id,
                          source_lnk="", keywords=[], missing=True),
                    f"{app.name}：目标已不存在（{app.app_id}）")
        return Entry(name=app.name, target=app.app_id, icon=app.app_id,
                     source_lnk="", keywords=[], missing=False), ""

    if app.is_appsfolder:
        # legacy（{GUID}\...\exe.exe）的图标取不到 —— AppsFolder 不暴露它，
        # 只能走占位图。用户要真图标的话从对应 .lnk 拖进来。
        icon = uwp_icon_path(app.app_id) if app.kind == "appx" else ""
        return Entry(name=app.name, target=shell_uri(app.app_id),
                     icon=icon, source_lnk=app.icon_hint,
                     keywords=[], missing=False), ""

    return None, f"{app.name}：无法识别的应用 ID（{app.app_id!r}）"


def _looks_like_uri(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", target)) and \
        not re.match(r"^[A-Za-z]:\\", target)


# ── 扫描 ────────────────────────────────────────────────────────────────

def scan_paths(paths: Sequence[str | Path], recursive: bool = False,
               include_exe: bool = True,
               known: Iterable[str] | None = None) -> ScanReport:
    """
    扫描一批路径（拖进来的一堆，或多选的文件）。

    目录按 ``recursive`` 决定是否下钻，``include_exe`` 决定是否收
    .exe/.bat/.cmd/.com。单个文件失败只记一条错误，不影响其它。
    """
    rep = ScanReport()
    known = set(known) if known is not None else None
    for raw in paths or []:
        # Path() 本身就会对 None/int/list 抛异常，所以构造放在 try 里 ——
        # 拖放 API 返回的东西类型不受控，不能让一个畸形条目炸掉整批。
        try:
            p = Path(raw)
            if p.is_dir():
                rep.merge(_scan_dir(p, recursive=recursive,
                                    include_exe=include_exe, known=known))
            elif p.exists():
                _absorb(rep, p, known)
            else:
                rep.errors.append(f"{p}：路径不存在（可能已被移动或删除）")
        except TypeError as exc:
            rep.errors.append(f"{raw!r}：不是合法的路径（{exc}）")
        except PermissionError as exc:
            rep.errors.append(f"{raw}：无权访问（{exc}）")
        except OSError as exc:
            rep.errors.append(f"{raw}：访问失败（{exc}）")
        except Exception as exc:                       # 任何意外都不许崩
            rep.errors.append(f"{raw}：扫描失败（{exc}）")
    return rep


def scan_folder(folder: str | Path, recursive: bool = False,
                include_exe: bool = True,
                known: Iterable[str] | None = None) -> ScanReport:
    """扫描一个文件夹（点「浏览文件夹」用）。"""
    return scan_paths([folder], recursive=recursive,
                      include_exe=include_exe, known=known)


def _absorb(rep: ScanReport, p: Path, known: set[str] | None) -> None:
    suf = p.suffix.lower()
    if suf in LNK_SUFFIXES or suf in LAUNCHABLE_SUFFIXES:
        entry, err = entry_from_file(p, known)
        if entry is not None:
            if is_appsfolder_target(entry.target):
                note = "应用商店 / UWP 应用"
            elif entry.missing:
                note = "无法解析，会以失效条目保留"
            else:
                note = ""
            rep.candidates.append(Candidate(entry=entry, origin=str(p),
                                            note=note))
        if err:
            rep.errors.append(err)
        return
    rep.unsupported.append(f"{p.name}：不支持的格式{suf or '（无后缀）'}")


def _scan_dir(folder: Path, recursive: bool, include_exe: bool,
              known: set[str] | None) -> ScanReport:
    rep = ScanReport()
    if not folder.is_dir():
        rep.errors.append(f"{folder}：目录不存在")
        return rep

    seen_files = 0
    depth_limit = MAX_SCAN_DEPTH if recursive else 1
    try:
        for depth, (root, dirs, files) in enumerate(os.walk(folder)):
            if depth >= depth_limit:
                dirs[:] = []
                rep.truncated = True
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
            stop = False
            for fn in sorted(files):
                if seen_files >= MAX_SCAN_FILES:
                    rep.truncated = True
                    dirs[:] = []
                    stop = True
                    break
                suf = Path(fn).suffix.lower()
                if suf in LNK_SUFFIXES or (include_exe
                                           and suf in LAUNCHABLE_SUFFIXES):
                    seen_files += 1
                    _absorb(rep, Path(root) / fn, known)
            if stop:
                break
            if rep.truncated:
                break
    except PermissionError as exc:
        rep.errors.append(f"{folder}：无权访问（{exc}）")
    except Exception as exc:
        rep.errors.append(f"{folder}：扫描失败（{exc}）")

    if rep.truncated:
        rep.errors.append(
            f"{folder}：文件过多或层级过深，只扫了前 {seen_files} 个；"
            f"请缩小范围，或取消勾选「包含子文件夹」")
    return rep


def scan_uwp_apps_from_start_menu(selected: Iterable[UwpApp]) -> ScanReport:
    """把用户勾选的开始菜单记录变成候选条目。"""
    rep = ScanReport()
    for app in selected:
        try:
            entry, err = entry_from_app(app)
            if entry is not None:
                rep.candidates.append(
                    Candidate(entry=entry, origin="开始菜单", note=app.label))
            if err:
                rep.errors.append(err)
        except Exception as exc:
            rep.errors.append(f"{app.name}：处理失败（{exc}）")
    return rep


# ── 去重 ────────────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    return (s or "").strip().strip('"').lower()


def target_key(entry: Entry) -> tuple[str, str, str]:
    """有 target 时的键：归一化 (target, args, 链接)。"""
    return (_norm(entry.target).rstrip("\\/"), _norm(entry.args),
            _norm(entry.source_lnk))


def lnk_key(entry: Entry) -> tuple[str, str] | None:
    """有 .lnk 时的键。没有返回 None。"""
    return ("lnk", _norm(entry.source_lnk)) if entry.source_lnk else None


def _is_broken(entry: Entry | None) -> bool:
    """现有条目是不是"读不出目标"的那种。"""
    return entry is None or not (entry.target or "").strip()


def build_preview(candidates: Sequence[Candidate],
                  library: Library,
                  policy: str = AddPolicy.SKIP) -> list[PreviewRow]:
    """
    生成预览表。每行标明：新增 / 重复 / 覆盖 / 修复失效 / 失败。

    批量添加时这一步不能省 —— 用户要在写库之前看到"哪些会被跳过、
    哪些会和现有项打架"。

    **为什么不用 ``Entry.uid``**：
    它内部是 ``sha1(target|args)``，对 **target 为空** 的条目全部塌缩成
    ``sha1("|")`` —— 库里那几个读不出目标的 ⚠ 条目会互相撞车，
    结果是"第一个当重复、后面全部当新条目"。这里自己算两套索引：
    按目标匹配 + 按 .lnk 路径匹配，后者正好用来把"失效旧条目"和
    "从同一个 .lnk 修好的新条目"对上，从而做出**升级**而不是重复。
    """
    rows: list[PreviewRow] = []
    entries = list(library.entries)
    by_target: dict[tuple[str, str, str], Entry] = {}
    by_lnk: dict[tuple[str, str], Entry] = {}
    for e in entries:
        if (e.target or "").strip():
            by_target.setdefault(target_key(e), e)
        k = lnk_key(e)
        if k:
            by_lnk.setdefault(k, e)

    batch_seen: dict[object, PreviewRow] = {}

    def _find_old(entry: Entry) -> Entry | None:
        if (entry.target or "").strip():
            hit = by_target.get(target_key(entry))
            if hit is not None:
                return hit
        k = lnk_key(entry)
        return by_lnk.get(k) if k else None

    for cand in candidates:
        entry = cand.entry
        k = target_key(entry) if (entry.target or "").strip() else lnk_key(entry)
        k = k or ("n", _norm(entry.name))

        # 批内重复（同一个文件被拖了两遍 / 同一批里出现两次）。
        # KEEP_BOTH 的语义是"什么都别拦"，批内也一样 —— 否则用户选了
        # "全部保留"却仍有 122 行标着"重复·跳过"，那这个选项就是骗人的。
        # REPLACE 下仍然折叠：同一个库下标只能被覆盖一次，让最后一条赢
        # 等于悄悄丢掉中间几条，不如老实标成批内重复。
        if k in batch_seen and policy != AddPolicy.KEEP_BOTH:
            first = batch_seen[k]
            rows.append(PreviewRow(
                entry=entry, state=RowState.DUPLICATE,
                detail=f"与本次列表里的「{first.entry.name}」重复",
                existing=first.entry))
            continue

        old = _find_old(entry)

        if old is None:
            row = PreviewRow(entry=entry, state=RowState.NEW, detail="")
        elif policy == AddPolicy.KEEP_BOTH:
            row = PreviewRow(entry=entry, state=RowState.NEW,
                             detail=f"允许重复：与现有「{old.name}」同源",
                             existing=old)
        elif _is_broken(entry) and not _is_broken(old):
            # 反向：新条目解析不出来，旧的好好的 —— 绝不能拿它覆盖旧的
            row = PreviewRow(entry=entry, state=RowState.DUPLICATE,
                             detail=f"现有「{old.name}」已可启动，保留现有的",
                             existing=old)
        elif _is_broken(old) and not _is_broken(entry):
            row = PreviewRow(entry=entry, state=RowState.UPGRADE,
                             detail=f"修好现有失效条目「{old.name}」",
                             existing=old)
        elif policy == AddPolicy.REPLACE:
            row = PreviewRow(entry=entry, state=RowState.REPLACE,
                             detail=f"覆盖现有「{old.name}」", existing=old)
        else:
            row = PreviewRow(entry=entry, state=RowState.DUPLICATE,
                             detail=f"现有已有「{old.name}」", existing=old)

        rows.append(row)
        batch_seen[k] = row
    return rows


def _index_of_entry(entries: Sequence[Entry], old: Entry | None) -> int | None:
    if old is None:
        return None
    for i, e in enumerate(entries):
        if e is old:
            return i
    k = target_key(old) if (old.target or "").strip() else lnk_key(old)
    if k is None:
        return None
    for i, e in enumerate(entries):
        same = (target_key(e) if (e.target or "").strip() else lnk_key(e)) == k
        if same:
            return i
    return None


def commit(rows: Sequence[PreviewRow], library: Library,
           policy: str = AddPolicy.SKIP,
           allowed: Iterable[int] | None = None,
           dry_run: bool = False) -> AddResult:
    """
    按预览结果写库。

    - 立刻 ``library.save()``，不重启就生效。
    - 每一条失败都进 ``AddResult.errors``，**绝不静默吞掉**。
    - ``allowed`` 给定时只有这些下标的行会被处理（对话框"只添加勾选"用）。
    - ``dry_run`` 时只统计不落盘。

    写入顺序上有个刻意的选择：**先全部改内存、最后一次 save**。
    ``Library.save()`` 是原子替换（临时文件 + ``os.replace``），中途崩溃
    不会毁库；但逐条 save 会让 1000 条的批量导入做 1000 次全量序列化。
    升级/覆盖保持原位置不动，新增的追加到末尾，避免用户已有的排版全变。
    """
    res = AddResult()
    entries = list(library.entries)
    keep = set(allowed) if allowed is not None else None

    # 先算出完整计划，再动计数器 —— 中途发现找不到老条目时可以安全退化
    plan: list[tuple[int, str, int | None]] = []      # (行号, 状态, 老条目下标)
    for i, row in enumerate(rows):
        if keep is not None and i not in keep:
            continue
        if row.state == RowState.FAILED:
            res.failed += 1
            res.errors.append((row.entry.name or "?", row.detail or "未知原因"))
            continue
        if row.state == RowState.DUPLICATE:
            res.skipped += 1
            continue
        if row.state in (RowState.UPGRADE, RowState.REPLACE):
            plan.append((i, row.state,
                         _index_of_entry(entries, row.existing)))
            continue
        plan.append((i, RowState.NEW, None))

    for i, state, _ in plan:
        if state == RowState.NEW:
            res.added += 1
        elif state == RowState.UPGRADE:
            res.upgraded += 1
        else:
            res.replaced += 1
    res.entries = [rows[i].entry for i, _, _ in plan]

    if dry_run or not plan:
        if dry_run:
            res.entries = []
        return res

    replaced: dict[int, Entry] = {
        j: rows[i].entry for i, _, j in plan if j is not None}
    new_list: list[Entry] = [replaced.get(j, e) for j, e in enumerate(entries)]
    new_list.extend(rows[i].entry for i, _, j in plan if j is None)

    library.entries = new_list
    try:
        library.save()
    except Exception as exc:
        res.errors.append(("<整个库>", f"写入失败：{exc}"))
        res.failed += 1
    return res


def _norm_name(s: str) -> str:
    """名字归一化：压空白、去不间断空格、大小写无关。

    .lnk 里的名字常带 `\\xa0`（不间断空格）——「某 NAS 工具 - Synology NAS」
    就是这样，而 Get-StartApps 给的是普通空格。不归一化就精确匹配不上。
    """
    s = (s or "").replace("\\xa0", " ").replace("\\u00a0", " ")
    return " ".join(s.split()).casefold()


def _match_by_name(name: str, known: set | None = None) -> str:
    """按精确同名去开始菜单里找 app_id。找不到返回空串。

    只接受精确匹配。多个同名时优先 appsfolder / apx（真正能经 shell: 调起），
    其次 legacy，最后放弃 —— 宁可不做，也不要把「Visual Studio」
    错配成「Visual Studio Code」。
    """
    want = _norm_name(name)
    if not want:
        return ""
    try:
        apps = list_start_apps()
    except Exception:
        return ""
    hits = [a for a in apps if _norm_name(a.name) == want]
    if not hits:
        return ""
    order = {"appsfolder": 0, "appx": 0, "path": 1, "legacy": 2, "url": 3}
    hits.sort(key=lambda a: order.get(getattr(a, "kind", ""), 9))
    app_id = (getattr(hits[0], "app_id", "") or "").strip()
    if known and app_id not in known:
        return ""
    return app_id


def recover_broken(library: Library,
                   known: Iterable[str] | None = None) -> AddResult:
    """
    把库里"读不出目标"的条目就地修好（UWP 链接）。

    这直接对上用户最初的抱怨：那几个 ⚠ 条目不是"加不进去"，是加进去了
    但存了个空 target，永远启动不了。这里扫描它们各自的 .lnk，把 AUMID
    捞出来改写成可调起的 ``shell:AppsFolder\\...``。
    """
    res = AddResult()
    if known is None:
        try:
            known = known_aumids()
        except Exception:
            known = set()
    known = set(known)

    broken = [e for e in library.entries
              if not (e.target or "").strip() and e.source_lnk]
    if not broken:
        return res

    changed = False
    for e in broken:
        try:
            app_id = aumid_from_lnk(Path(e.source_lnk), known)
        except Exception as exc:
            res.failed += 1
            res.errors.append((e.name, f"解析失败：{exc}"))
            continue
        if not app_id:
            # 兜底：按**精确同名**去开始菜单列表里找。
            #
            # aumid_from_lnk 只在 .lnk 字节里找 AUMID，但并不是所有读不出
            # TargetPath 的链接都带 AUMID —— 实测 4 个（OpenCode /
            # 两个应用商店链接）
            # 都没有，而它们在 Get-StartApps 里全都**精确同名**存在。
            # 只接受精确匹配：模糊匹配会把「Visual Studio」配到
            # 「Visual Studio Code」上去，那比不配更糟。
            app_id = _match_by_name(e.name, known)
        if not app_id:
            res.failed += 1
            res.errors.append((e.name, "链接里找不到可用的应用 ID，"
                                     "开始菜单里也没有同名项，仍无法启动"))
            continue
        try:
            icon = uwp_icon_path(app_id)
        except Exception:
            icon = ""
        if icon:
            e.icon = icon
        e.target = shell_uri(app_id)
        e.missing = False
        res.upgraded += 1
        res.entries.append(e)
        changed = True

    if changed:
        try:
            library.save()
        except Exception as exc:
            res.errors.append(("<整个库>", f"写入失败：{exc}"))
            res.failed += 1
    return res


def add_paths(library: Library, paths: Sequence[str | Path],
              recursive: bool = False, include_exe: bool = True,
              policy: str = AddPolicy.SKIP,
              known: Iterable[str] | None = None,
              dry_run: bool = False) -> tuple[ScanReport, list[PreviewRow], AddResult]:
    """
    无界面一条龙：扫描 → 预览 → 写入。测试和脚本用这个。

    返回 ``(扫描报告, 预览行, 写入结果)``。
    """
    rep = scan_paths(paths, recursive=recursive, include_exe=include_exe,
                     known=known)
    rows = build_preview(rep.candidates, library, policy)
    res = commit(rows, library, policy, dry_run=dry_run)
    return rep, rows, res


# ── 路径缩短（预览和结果里都要显示，太长的省略） ────────────────────────

def shorten_path(text: str, max_chars: int = 58) -> str:
    """
    路径太长时**中间**省略，保留开头和结尾。

    截掉尾部没用 —— 用户要看的恰恰是最后那个 exe 文件名。
    """
    s = (text or "").strip()
    if len(s) <= max_chars:
        return s
    if max_chars < 12:
        return s[:max(1, max_chars - 1)] + "…"
    head = (max_chars - 3) // 2
    tail = max_chars - 3 - head
    return s[:head] + " … " + s[-tail:]


# ══════════════════════════════════════════════════════════════════════
#  Qt 部分
#  只在真的开对话框时才 import Qt，所以无界面环境下本模块可以直接导入
#  跑测试（收集 → 预览 → 写入 → 重载），不起 QApplication。
# ══════════════════════════════════════════════════════════════════════

def _qt():
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets      # noqa: F401
    except Exception as exc:                            # pragma: no cover
        raise RuntimeError(f"需要 PyQt5 才能打开添加窗口：{exc}") from exc
    return QtCore, QtGui, QtWidgets


def add_dialog(library: Library, parent=None, icons=None):
    """
    构造「添加应用」对话框实例。

    ::

        dlg = add_dialog(window.library, parent=window)
        dlg.added.connect(window._after_add)
        dlg.exec_()
        print(dlg.result_info.summary())

    等价于 ``AddAppDialog(library, parent)`` —— 后者是同一个类。
    """
    # 注意不能直接写 AddAppDialog(...)：那个名字要等模块级 __getattr__
    # 触发后才存在于 globals 里，直接引用会 NameError。
    return _dialog_class()(library, parent, icons)


_DIALOG_CLASS: type | None = None


def _dialog_class() -> type:
    """
    构造并缓存对话框类。

    类只建一次：`_build_dialog` 里的 `class _Dialog(QDialog)` 是闭包里的
    局部类，每次调用都会造出一个**新类型**，因此 `isinstance` /
    `AddAppDialog.Accepted` 之类的跨次比较全都不成立。缓存起来就稳定了。
    """
    global _DIALOG_CLASS
    if _DIALOG_CLASS is None:
        QtCore, QtGui, QtWidgets = _qt()
        _DIALOG_CLASS = _build_dialog(QtCore, QtGui, QtWidgets)
        globals()["AddAppDialog"] = _DIALOG_CLASS
    return _DIALOG_CLASS


def __getattr__(name):
    """
    PEP 562 惰性导出。

    window.py 里已经写了 ``from .addapp import AddAppDialog``，但本模块
    刻意不在顶层 import PyQt5（无界面环境下要能直接 import 跑测试）。
    所以这里在**第一次访问这个名字时**才建类并塞进模块全局，之后
    ``AddAppDialog`` 就是普通的模块级名字了。

    这样两种写法都成立::

        from .addapp import AddAppDialog   # 惰性
        from .addapp import add_dialog     # 函数式
    """
    if name == "AddAppDialog":
        return _dialog_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _paths_from_mime(mime) -> list[str]:
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]


def _make_drop_forwarder(QtCore, handler):
    """
    把子控件上的拖放事件转发给对话框。

    Qt 的拖放管理器只把事件发给**第一个 ``acceptDrops()`` 为真的控件**
    （沿 parent 链向上找）。整棵对话框都接了拖放，所以正常拖放落到对话框
    自己的 ``dropEvent``；但 ``QLineEdit`` 之类默认接受文本拖放的子控件一旦
    将来也 acceptDrops，就会把事件吃掉 —— 用户明明拖到了预览表上、松手了，
    界面毫无反应。这个过滤器强制"我不处理，交给祖先"。

    必须是 ``QObject`` 子类：``installEventFilter`` 只接受 QObject，
    传普通对象直接 ``TypeError``。

    实测（别再重复踩）：装到 ``QTreeWidget.viewport()`` 上是**无效**的 ——
    事件根本不会送到 viewport，滚动区域的拖放一律派发给滚动区域本身。
    """

    class _DropForwarder(QtCore.QObject):
        def eventFilter(self, obj, ev):
            if ev.type() in (QtCore.QEvent.DragEnter,
                             QtCore.QEvent.DragMove,
                             QtCore.QEvent.Drop):
                if ev.mimeData().hasUrls():
                    handler(ev)
                    return True                # 吃掉，交给 handler 处理
            return False

    return _DropForwarder()


#: 会直接当图标用的图片后缀（UWP 的 logo 就是 png）
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".gif",
                             ".ico", ".svg"})


class _IconLoader:
    """QFileIconProvider + 首字母占位图。自包含，不依赖 icons.py。"""

    BADGE = {
        RowState.NEW: "#7ee2a8",
        RowState.DUPLICATE: "#8b96a5",
        RowState.UPGRADE: "#79b8ff",
        RowState.REPLACE: "#f0c674",
        RowState.FAILED: "#ff8080",
    }

    def __init__(self, size: int = 40):
        from PyQt5.QtGui import QIcon
        from PyQt5.QtWidgets import QFileIconProvider
        self.size = size
        self._QIcon = QIcon
        self._provider = QFileIconProvider()
        self._cache: dict[str, object] = {}

    def get(self, entry: Entry):
        from PyQt5.QtCore import QFileInfo
        key = entry.uid
        if key in self._cache:
            return self._cache[key]
        icon = self._QIcon()
        for src in (entry.icon, entry.source_lnk, entry.target):
            if not src or is_shell_target(src):
                continue          # Qt 对 shell: URI 一律返回空 icon（实测）
            if Path(src).suffix.lower() in _IMAGE_SUFFIXES:
                # 图片直接当图标用。**不能**丢给 QFileIconProvider：
                # 它对 png/jpg 返回的是"图片文件类型"的通用小图，
                # 而且非空 —— 于是 UWP 的 logo 全部显示成同一个
                # "PNG 图标"，看起来像所有应用长得一样。
                icon = self._QIcon(src)
            else:
                info = QFileInfo(src)
                if not info.exists():
                    continue
                icon = self._provider.icon(info)
            if not icon.isNull():
                break
        if icon.isNull():
            icon = self._placeholder(entry.name)
        self._cache[key] = icon
        return icon

    def _placeholder(self, name: str):
        from PyQt5.QtCore import QRect, Qt
        from PyQt5.QtGui import (QBrush, QColor, QFont, QLinearGradient,
                                 QPainter, QPixmap)
        pm = QPixmap(self.size, self.size)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, self.size, self.size)
        g.setColorAt(0.0, QColor(88, 96, 112))
        g.setColorAt(1.0, QColor(52, 58, 70))
        p.setBrush(QBrush(g))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRect(0, 0, self.size, self.size),
                          max(1, self.size // 6), max(1, self.size // 6))
        f = QFont("Segoe UI", max(6, int(self.size * 0.44)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(238, 242, 248))
        p.drawText(pm.rect(), Qt.AlignCenter, (name.strip() or "?")[0].upper())
        p.end()
        return self._QIcon(pm)


def _build_dialog(QtCore, QtGui, QtWidgets):
    """
    造出「添加应用」对话框的类。

    参数刻意只有 Qt 三件套：`library` / `parent` / `icons` 是**实例**
    参数，所以类可以缓存复用（见 :func:`_dialog_class`）。
    """
    from PyQt5.QtCore import Qt, pyqtSignal
    from PyQt5.QtGui import QBrush, QColor
    from PyQt5.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox,
                                 QDialogButtonBox, QFileDialog, QHBoxLayout,
                                 QHeaderView, QLabel, QLineEdit, QListWidget,
                                 QListWidgetItem, QMessageBox, QPushButton,
                                 QTreeWidget, QTreeWidgetItem, QVBoxLayout)

    def _fake_entry(app: UwpApp) -> Entry:
        return Entry(name=app.name, target=app.app_id, icon=app.icon_hint)

    # 预览表列下标。定义在 _build_dialog 里（闭包）而不是类体里 ——
    # 方法体只看得到模块/闭包作用域，类体里的常量必须写 self.C_ICON。
    COL_CHECK, COL_ICON, COL_NAME, COL_TARGET, COL_STATE = range(5)

    class _UwpPicker(QtWidgets.QDialog):
        """勾选应用商店 / 开始菜单里用 AppsFolder 调起的应用。"""

        def __init__(self, apps, parent=None, on_paths=None):
            super().__init__(parent)
            self.setWindowTitle("选择要添加的应用")
            self.resize(600, 560)
            self.apps = apps
            self._map: dict[str, UwpApp] = {}
            self._loader = _IconLoader(32)
            self._on_paths = on_paths
            # 筛选框默认接受文本拖放：拖文件进来会被当成 URL 往里插文字，
            # 看着像"没反应"。关掉它，让事件冒到本对话框。
            self.setAcceptDrops(True)

            lay = QVBoxLayout(self)

            filt = QLineEdit(self)
            filt.setPlaceholderText("按名称筛选…")
            lay.addWidget(filt)

            row = QHBoxLayout()
            self.only_uwp = QCheckBox("只显示应用商店 / UWP 应用", self)
            self.only_uwp.setChecked(True)
            self.show_classic = QCheckBox("也显示经典程序和网页链接", self)
            self.show_classic.setChecked(False)
            self.show_classic.setToolTip(
                "经典程序 = 开始菜单里的 exe 快捷方式和 {GUID} 注册项；\n"
                "网页链接 = 安装程序留下的 https 文档链接")
            row.addWidget(self.only_uwp)
            row.addStretch(1)
            row.addWidget(self.show_classic)
            lay.addLayout(row)

            self.lst = QListWidget(self)
            self.lst.setSelectionMode(QAbstractItemView.ExtendedSelection)
            lay.addWidget(self.lst, 1)

            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
                                  parent=self)
            bb.button(QDialogButtonBox.Ok).setText("添加所选")
            bb.accepted.connect(self.accept)
            bb.rejected.connect(self.reject)
            lay.addWidget(bb)

            def rebuild():
                self.lst.clear()
                self._map.clear()
                q = filt.text().strip().lower()
                for a in self.apps:
                    if a.kind in ("path", "url") \
                            and not self.show_classic.isChecked():
                        continue
                    if self.only_uwp.isChecked() and a.kind != "appx":
                        continue
                    if q and q not in a.name.lower():
                        continue
                    key = f"{a.name}|{a.app_id}"
                    it = QListWidgetItem(a.name)
                    it.setData(Qt.UserRole, key)
                    it.setIcon(self._loader.get(_fake_entry(a)))
                    it.setToolTip(f"{a.name}\n{a.app_id}\n类型：{a.label}")
                    self._map[key] = a
                    self.lst.addItem(it)

            filt.textChanged.connect(rebuild)
            self.only_uwp.toggled.connect(rebuild)
            self.show_classic.toggled.connect(rebuild)
            rebuild()

            # 在这个对话框里拖文件 -> 直接交给主对话框的添加逻辑
            self._fwd = _make_drop_forwarder(QtCore, self._forward_drag)
            self._fwd.setParent(self)
            filt.installEventFilter(self._fwd)
            self.lst.installEventFilter(self._fwd)

        # ── 拖放：在这里拖进来的文件同样能加 ──
        def _mime_ok(self, ev) -> bool:
            return (self._on_paths is not None
                    and ev.mimeData().hasUrls()
                    and bool(_paths_from_mime(ev.mimeData())))

        def dragEnterEvent(self, ev):
            ev.acceptProposedAction() if self._mime_ok(ev) else ev.ignore()

        def dragMoveEvent(self, ev):
            ev.acceptProposedAction() if self._mime_ok(ev) else ev.ignore()

        def dropEvent(self, ev):
            if self._mime_ok(ev):
                self._on_paths(_paths_from_mime(ev.mimeData()))
                ev.acceptProposedAction()
            else:
                ev.ignore()

        def _forward_drag(self, ev):
            if ev.type() == QtCore.QEvent.Drop:
                self.dropEvent(ev)
            else:
                self.dragEnterEvent(ev)

        def selected(self) -> list[UwpApp]:
            return [self._map[i.data(Qt.UserRole)]
                    for i in self.lst.selectedItems()
                    if i.data(Qt.UserRole) in self._map]

    class _DropZone(QtWidgets.QLabel):
        dropped = pyqtSignal(list)

        def __init__(self, parent=None):
            super().__init__(
                "把 .lnk / .exe / 文件夹拖到这里\n"
                "（可直接从桌面、开始菜单、文件资源管理器拖进来）", parent)
            self.setAcceptDrops(True)
            self.setAlignment(Qt.AlignCenter)
            self.setWordWrap(True)
            self.setMinimumHeight(84)
            self.setStyleSheet(
                "QLabel{ border:2px dashed #555; border-radius:10px;"
                " color:#9aa4b2; background:#14171d; padding:8px; }")

        def dragEnterEvent(self, ev):
            (ev.acceptProposedAction() if ev.mimeData().hasUrls()
             else ev.ignore())

        def dragMoveEvent(self, ev):
            (ev.acceptProposedAction() if ev.mimeData().hasUrls()
             else ev.ignore())

        def dropEvent(self, ev):
            paths = _paths_from_mime(ev.mimeData())
            if paths:
                self.dropped.emit(paths)
                ev.acceptProposedAction()
            else:
                ev.ignore()

    class _Dialog(QtWidgets.QDialog):
        """「添加应用」主对话框。"""

        added = pyqtSignal(object)              # AddResult

        def __init__(self, library, parent=None, icons=None):
            super().__init__(parent)
            self.library = library
            self.icons = icons
            self.result_info = AddResult()
            self._rows: list[PreviewRow] = []
            self._scan_errors: list[str] = []
            self._result_info: AddResult | None = None

            self.setWindowTitle("添加应用")
            self.resize(900, 660)
            self.setAcceptDrops(True)

            root = QVBoxLayout(self)

            src = QHBoxLayout()
            b_files = QPushButton("浏览文件…")
            b_files.setToolTip("可多选。.lnk / .url / .exe / .bat / .cmd")
            b_files.clicked.connect(self._browse_files)
            b_folder = QPushButton("浏览文件夹…")
            b_folder.setToolTip("扫描一个文件夹里的所有快捷方式")
            b_folder.clicked.connect(self._browse_folder)
            b_uwp = QPushButton("应用商店 / UWP 应用…")
            b_uwp.setToolTip("列出开始菜单里用 AppsFolder 调起的应用"
                             "（UWP 应用、Electron 商店版等）")
            b_uwp.clicked.connect(self._browse_uwp)
            b_fix = QPushButton("修复失效条目…")
            b_fix.setToolTip("把读不出启动目标的快捷方式就地修好")
            b_fix.clicked.connect(self._fix_broken)
            for b in (b_files, b_folder, b_uwp, b_fix):
                src.addWidget(b)
            src.addStretch(1)
            root.addLayout(src)

            opts = QHBoxLayout()
            self.cb_recursive = QCheckBox("包含子文件夹")
            self.cb_recursive.setChecked(True)
            self.cb_exe = QCheckBox("同时导入 .exe / .bat / .cmd")
            self.cb_exe.setChecked(False)
            opts.addWidget(self.cb_recursive)
            opts.addWidget(self.cb_exe)
            opts.addSpacing(16)
            opts.addWidget(QLabel("重复项："))
            self.cb_policy = QComboBox()
            for p in AddPolicy.ALL:
                self.cb_policy.addItem(AddPolicy.LABELS[p], p)
            self.cb_policy.currentIndexChanged.connect(self._rebuild_preview)
            opts.addWidget(self.cb_policy)
            opts.addStretch(1)
            root.addLayout(opts)

            self.drop = _DropZone()
            self.drop.dropped.connect(self._add_paths)
            root.addWidget(self.drop)

            self.tree = QTreeWidget(self)
            # 5 列而不是 4 列：勾选框和图标必须**各占一列**。
            # 早先把图标塞进第 0 列又把第 0 列 hide 掉（想藏表头），
            # 结果 122 个图标一个都看不见 —— 用户预览时完全没有"这是
            # 哪个程序"的直觉，只能对着路径猜。
            # 注意 `setSectionHidden(True)` 是把整列**移出视图**（宽度归零），
            # 不是"只藏表头文字"。想藏表头就留空标题，别用 hide。
            self.tree.setColumnCount(5)
            self.tree.setHeaderLabels(["", "", "名称", "目标", "状态"])
            self.tree.setRootIsDecorated(False)
            self.tree.setAlternatingRowColors(True)
            self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.tree.setIconSize(QtCore.QSize(32, 32))
            hh = self.tree.header()
            hh.setSectionResizeMode(COL_CHECK, QHeaderView.Fixed)
            hh.setSectionResizeMode(COL_ICON, QHeaderView.Fixed)
            self.tree.setColumnWidth(COL_CHECK, 36)
            self.tree.setColumnWidth(COL_ICON, 44)
            hh.setSectionResizeMode(COL_NAME, QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(COL_TARGET, QHeaderView.Stretch)
            hh.setSectionResizeMode(COL_STATE, QHeaderView.ResizeToContents)
            root.addWidget(self.tree, 1)

            self.summary = QLabel("还没有待添加的条目。")
            self.summary.setWordWrap(True)
            root.addWidget(self.summary)

            self.detail = QTreeWidget(self)
            self.detail.setColumnCount(2)
            self.detail.setHeaderLabels(["条目", "说明"])
            self.detail.setRootIsDecorated(False)
            self.detail.hide()
            root.addWidget(self.detail, 1)

            bb = QDialogButtonBox(parent=self)
            self.b_add = QPushButton("添加所选")
            self.b_add.setDefault(True)
            self.b_clear = QPushButton("清空列表")
            self.b_close = QPushButton("完成")
            bb.addButton(self.b_add, QDialogButtonBox.AcceptRole)
            bb.addButton(self.b_clear, QDialogButtonBox.ActionRole)
            bb.addButton(self.b_close, QDialogButtonBox.RejectRole)
            root.addWidget(bb)

            self.b_add.clicked.connect(self._do_add)
            self.b_clear.clicked.connect(self._clear)
            self.b_close.clicked.connect(self.close)

            # 子控件的拖放拦截。
            # 注意：**不要**装到 QTreeWidget.viewport() 上 —— 实测事件根本
            # 不会送到那里。Qt 的拖放管理器沿 parent 链向上找第一个
            # acceptDrops() 为真的控件，整棵对话框都接了，所以真实拖放一律
            # 落到对话框自己身上；这里装的过滤器只是兜底（比如哪天某个子控件
            # 自己 acceptDrops 了，把事件吃掉）。
            self._fwd = _make_drop_forwarder(QtCore, self._forward_drag)
            self._fwd.setParent(self)
            for w in (self.tree, self.detail, self.summary,
                      self.cb_recursive, self.cb_exe, self.cb_policy):
                w.installEventFilter(self._fwd)

        # ── 拖放 ──
        def _mime_ok(self, ev) -> bool:
            return ev.mimeData().hasUrls() and bool(
                _paths_from_mime(ev.mimeData()))

        def dragEnterEvent(self, ev):
            ev.acceptProposedAction() if self._mime_ok(ev) else ev.ignore()

        def dragMoveEvent(self, ev):
            ev.acceptProposedAction() if self._mime_ok(ev) else ev.ignore()

        def dropEvent(self, ev):
            if self._mime_ok(ev):
                self._add_paths(_paths_from_mime(ev.mimeData()))
                ev.acceptProposedAction()
            else:
                ev.ignore()

        def _forward_drag(self, ev):
            """事件过滤器转发用。"""
            if ev.type() == QtCore.QEvent.Drop:
                self.dropEvent(ev)
            else:
                self.dragEnterEvent(ev)

        # ── 来源 ──
        def _browse_files(self):
            files, _ = QFileDialog.getOpenFileNames(
                self, "选择要添加的文件", str(Path.home()),
                "程序和快捷方式 (*.lnk *.url *.exe *.bat *.cmd *.com);;"
                "所有文件 (*)")
            if files:
                self._add_paths(files)

        def _browse_folder(self):
            d = QFileDialog.getExistingDirectory(
                self, "选择包含快捷方式的文件夹")
            if d:
                self._add_paths([d])

        def _browse_uwp(self):
            apps = list_start_apps()
            if not apps:
                QMessageBox.warning(
                    self, "读不到应用列表",
                    "没能从开始菜单读到应用列表（Get-StartApps 返回空或超时）。\n"
                    "你仍然可以用「浏览文件夹」选开始菜单目录下的快捷方式。")
                return
            dlg = _UwpPicker(apps, self, on_paths=self._add_paths)
            if dlg.exec_() != QtWidgets.QDialog.Accepted:
                return
            picked = dlg.selected()
            if picked:
                self._merge(scan_uwp_apps_from_start_menu(picked))

        def _add_paths(self, paths):
            try:
                known = known_aumids()
            except Exception:
                known = None
            self._merge(scan_paths(paths,
                                   recursive=self.cb_recursive.isChecked(),
                                   include_exe=self.cb_exe.isChecked(),
                                   known=known))

        def _merge(self, rep: ScanReport):
            if rep.candidates:
                self._rows.extend(build_preview(rep.candidates, self.library,
                                                self.cb_policy.currentData()))
            for msg in rep.errors:
                self._scan_errors.append(msg)
            for msg in rep.unsupported:
                self._scan_errors.append("（格式不支持）" + msg)
            if rep.truncated:
                self._scan_errors.append("扫描范围过大，结果不完整")
            self._render()
            self._render_detail()

        # ── 预览 ──
        def _render(self):
            self.tree.clear()
            loader = self.icons or _IconLoader(32)
            for row in self._rows:
                it = QTreeWidgetItem(self.tree)
                it.setIcon(COL_ICON, loader.get(row.entry))
                it.setText(COL_NAME, row.entry.name)
                it.setText(COL_TARGET, shorten_path(
                    row.entry.target or row.entry.source_lnk or "—"))
                it.setText(COL_STATE, row.label + (f"（{row.detail}）"
                                                  if row.detail else ""))
                color = QColor(_IconLoader.BADGE.get(row.state, "#cbd5e0"))
                it.setForeground(COL_NAME, QBrush(
                    color if row.state == RowState.FAILED else QColor("#e6eaf0")))
                it.setForeground(COL_STATE, QBrush(color))
                it.setToolTip(COL_NAME, "".join((
                    f"名称：{row.entry.name}\n",
                    f"目标：{row.entry.target or '（无）'}\n",
                    f"参数：{row.entry.args or '（无）'}\n",
                    f"工作目录：{row.entry.workdir or '（无）'}\n",
                    f"图标来源：{row.entry.icon or '（自动）'}\n",
                    f"来源链接：{row.entry.source_lnk or '（无）'}")))
                it.setData(COL_CHECK, Qt.UserRole, row.selectable)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(COL_CHECK, Qt.Checked if row.selectable
                                 else Qt.Unchecked)

            n = {s: sum(1 for r in self._rows if r.state == s)
                 for s in (RowState.NEW, RowState.DUPLICATE, RowState.UPGRADE,
                           RowState.REPLACE, RowState.FAILED)}
            bits = [f"待添加 {len(self._rows)} 项", f"将新增 {n[RowState.NEW]}"]
            if n[RowState.UPGRADE]:
                bits.append(f"将修复 {n[RowState.UPGRADE]}")
            if n[RowState.REPLACE]:
                bits.append(f"将覆盖 {n[RowState.REPLACE]}")
            if n[RowState.DUPLICATE]:
                bits.append(f"将跳过重复 {n[RowState.DUPLICATE]}")
            if n[RowState.FAILED]:
                bits.append(f"失败 {n[RowState.FAILED]}")
            if self._scan_errors:
                bits.append(f"另有 {len(self._scan_errors)} 条读取错误")
            self.summary.setText(" · ".join(bits))
            self.b_add.setEnabled(
                bool(self._rows)
                and bool(n[RowState.NEW] or n[RowState.UPGRADE]
                         or n[RowState.REPLACE]))

        def _rebuild_preview(self, *_):
            """
            按当前库状态 + 当前策略重算所有行的状态。

            切换「重复项」策略时**必须**重算：SKIP 下显示"重复·跳过"的行，
            切到 KEEP_BOTH 后应该变成"新增"。不重算的话下拉框就是个摆设 ——
            用户选了"全部保留"，点添加还是什么都没发生。

            写入之后也必须重算：不然刚加进去的 122 项在列表里还写着"新增"，
            用户会以为没生效（正是"我的快捷方式都没添加上去"那种观感）。
            """
            if not self._rows:
                return
            self._rows = build_preview(
                [Candidate(entry=r.entry) for r in self._rows],
                self.library, self.cb_policy.currentData())
            self._render()
            self._render_detail()

        def _clear(self):
            self._rows.clear()
            self._scan_errors.clear()
            self._result_info = None
            self.detail.clear()
            self.detail.hide()
            self._render()

        def _render_detail(self):
            """
            底部详情面板：失败项、扫描错误、被跳过的重复项，逐条列出来。

            **不弹 QMessageBox** —— 第一版每次扫描完都弹一个模态框，
            连着加三批东西要点掉三次确认，而且 QMessageBox 是嵌套事件循环，
            在自动化脚本里会直接把进程挂死。所有信息都在对话框里呈现：
            摘要行给计数，详情面板给逐条原因。
            """
            self.detail.clear()
            res = self._result_info
            if res is not None:
                for name, reason in res.errors:
                    it = QTreeWidgetItem(self.detail)
                    it.setText(0, name)
                    it.setText(1, reason)
            for msg in self._scan_errors:
                it = QTreeWidgetItem(self.detail)
                it.setText(0, "（读取）")
                it.setText(1, msg)
            for row in self._rows:
                if row.state == RowState.DUPLICATE:
                    it = QTreeWidgetItem(self.detail)
                    it.setText(0, row.entry.name)
                    it.setText(1, f"已跳过：{row.detail}")
            self.detail.setVisible(bool(self.detail.topLevelItemCount()))

        # ── 写入 ──
        def _checked_indices(self) -> list[int]:
            idx = []
            for i in range(self.tree.topLevelItemCount()):
                it = self.tree.topLevelItem(i)
                if it.checkState(0) == Qt.Checked and it.data(0, Qt.UserRole):
                    idx.append(i)
            return idx

        def _do_add(self):
            allowed = self._checked_indices()
            if not allowed:
                self._scan_errors.append("没有勾选任何项，未做任何改动")
                self._render()
                self._render_detail()
                return
            # 提交前按当前库状态重算一次 —— 对话框开着期间库可能变了
            policy = self.cb_policy.currentData()
            rows = build_preview([Candidate(entry=r.entry) for r in self._rows],
                                 self.library, policy)
            res = commit(rows, self.library, policy, allowed=allowed)

            # 未勾选的行留在列表里，方便接着添加别的
            self._rows = ([self._rows[i] for i in range(len(self._rows))
                           if i not in allowed] + [rows[i] for i in allowed])
            self.result_info = res
            self._result_info = res
            # 写完之后必须整体重算：刚加进去的 122 项如果还挂着"新增"，
            # 用户会觉得"点了没反应" —— 这正是最初那句"我的快捷方式都没
            # 添加上去"的观感来源。
            self._rebuild_preview()
            # **不清空 _scan_errors**：那几个解析失败的文件根本没有变成候选，
            # 清掉就等于把失败信息丢了 —— 用户恰好是靠这一栏知道"我的快捷方式
            # 没能全部添加上去"的。要到点「清空列表」才重算。
            tail = (f" · 另有 {len(self._scan_errors)} 条读取失败"
                    if self._scan_errors else "")
            self.summary.setText(f"添加结果 — {res.summary()}{tail}")
            self.added.emit(res)

        def _fix_broken(self):
            res = recover_broken(self.library)
            self.result_info = res
            self._result_info = res
            if res.upgraded:
                title = "修复完成"
            elif res.failed:
                title = "没能修好：这些快捷方式指向的应用解析不出来，" \
                        "请重新创建一次快捷方式"
            else:
                title = "没有可修复的条目：库里没有读不出启动目标的快捷方式"
            self._rebuild_preview()
            self.summary.setText(f"{title} — {res.summary()}")
            if res.upgraded:
                self.added.emit(res)

        # ── 关闭语义 ──────────────────────────────────────────────
        # window.py 的接法是 ``if dlg.exec_() == AddAppDialog.Accepted:
        # 刷新网格``，所以"加没加东西"必须体现在 exec_() 的返回值上。
        # 三条关闭路径（完成 / Esc / 右上角 ×）都要一致，
        # 否则用户加了东西再按 Esc，界面不会刷新 —— 又变成"没添加上去"。
        def _changed(self) -> bool:
            return (self._result_info is not None
                    and self._result_info.total_changed > 0)

        def accept(self):
            if not self._changed():
                self.setResult(QtWidgets.QDialog.Rejected)
            super().accept()

        def reject(self):
            # 不能写成 setResult(Accepted) + super().reject()：
            # QDialog.reject() 内部走 done(Rejected)，会把刚设的结果
            # 又覆盖回去 —— 实测 Esc 关闭后 result() 仍是 0。
            if self._changed():
                self.done(QtWidgets.QDialog.Accepted)
            else:
                super().reject()

        def closeEvent(self, ev):
            self.setResult(QtWidgets.QDialog.Accepted if self._changed()
                           else QtWidgets.QDialog.Rejected)
            super().closeEvent(ev)

    _Dialog.__name__ = "AddAppDialog"
    _Dialog.__qualname__ = "AddAppDialog"
    return _Dialog


# ══════════════════════════════════════════════════════════════════════
#  window.py 集成方式
#
#  window.py 里已经这么写了，本模块按此对齐（不用再改 window.py）::
#
#      from .addapp import AddAppDialog
#      dlg = AddAppDialog(self.library, self)
#      if dlg.exec_() == AddAppDialog.Accepted:
#          self._on_search(self.search.text())
#
#  `AddAppDialog` 是通过 PEP 562 的模块级 `__getattr__` **惰性**导出的：
#  第一次访问时才 import PyQt5 并建类，这样无界面环境下
#  ``import launchpad.addapp`` 依然不需要 QApplication（tests_add.py 靠这个）。
#  `AddAppDialog.Accepted` / `.Rejected` 是从 QDialog 继承来的，
#  语义已按下面第 1 条调好。
#
#  ── 1) exec_() 的返回值 = 有没有真的改动库 ──────────────────────────
#  完成 / Esc / 右上角 × 三条关闭路径都会 setResult：加了东西一律
#  Accepted，什么都没加则 Rejected。上层据此决定要不要刷新网格。
#  走 `added` 信号也行（每写入一批就 emit 一次 AddResult）。
#
#  ── 2) _launch 必须加 shell: 分支（UWP 目标不是文件路径）──────────
#  现在库里可能有 ``target = "shell:AppsFolder\\..."`` 的条目，
#  直接丢给 ``subprocess.Popen`` 一定失败。最小改动::
#
#      from .addapp import is_shell_target, launch_shell_uri
#      ...
#      if is_shell_target(target):
#          ok, why = launch_shell_uri(target)
#          if not ok:
#              self.grid._message = f"「{entry.name}」启动失败：{why}"
#              self.grid.update()
#              return
#          self._mark_used(entry)
#          self.hide_me()
#          return
#
#  更省事：把 _launch 的函数体整个换成 ``ok, why = launch_entry(entry)``。
#  它已经把 shell: / http(s) / .bat / .cmd / .py 都分支好了，
#  返回 (是否成功, 失败原因)。
#
#  ── 3) 启动时顺手修一遍失效条目（可选，但强烈建议）────────────────
#  能直接解决那几个 ⚠ —— 它们不是"加不进去"，是加进去了但存了空 target::
#
#      from .addapp import recover_broken
#      rep = recover_broken(lib)
#      if rep.upgraded or rep.errors:
#          print(f"[Add] {rep.summary()}")
#          for name, why in rep.errors:              # 失败必须有人看
#              print(f"[Add][失败] {name}: {why}")
#
#  注意 `recover_broken` 会就地改 Entry 并 save，所以要在
#  `Launchpad.__init__` 建网格**之前**调用。
