# -*- coding: utf-8 -*-
"""
应用库：读写 shortcuts.json，管理图标缓存。

设计原则（吸取前一版的教训）：
- 路径全部由环境变量推导，与进程工作目录无关。
- 写入原子化（临时文件 + os.replace），中途崩溃不会毁掉整个库。
- 解析失败时**保留原文件**并抛异常，绝不静默变成空列表。
- 首次启动若库不存在，直接扫描源目录导入，用户装完即用，不需要手动添加。
"""

import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List

APP_DIR = "Launchpad"


# 路径集中在 paths.py，其它模块一律从那里取，避免字符串散落各处。
from .paths import dismissed_file as _dismissed_file    # noqa: E402
from .paths import icon_dir as _icon_dir                # noqa: E402


# ─── 路径 ──────────────────────────────────────────────

def app_dir() -> Path:
    p = Path(os.environ["APPDATA"]) / APP_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_file() -> Path:
    return app_dir() / "shortcuts.json"


def dismissed_db_file() -> Path:
    return _dismissed_file()


def icon_dir() -> Path:
    return _icon_dir()


def log_file() -> Path:
    p = Path(os.environ["LOCALAPPDATA"]) / APP_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p / "launchpad.log"


class LibraryError(Exception):
    """应用库损坏。原始文件已备份，不会丢失。"""


# ─── 数据模型 ──────────────────────────────────────────

@dataclass
class Entry:
    name: str
    target: str                      # 启动目标（exe / bat / py）；空表示未知
    args: str = ""
    workdir: str = ""
    icon: str = ""                   # 图标来源路径（exe 本身或带 ,idx 的位置）
    source_lnk: str = ""             # 原始 .lnk 路径，可回溯
    keywords: List[str] = field(default_factory=list)   # 搜索用别名
    missing: bool = False            # 目标读不出或已不存在
    # 最近一次成功启动的 epoch 秒，0 = 从未用过。
    # 排序方式 recent 依赖它。to_dict/from_dict 都走 dataclass 字段反射，
    # 所以加了这个字段就自动往返，不需要改序列化代码。
    last_used: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        # 判据是"必需字段存在"，不是"值非空"。
        # 读不出 target 的失效条目必须能往返 —— 否则它保存时进得去、
        # 重新加载时被静默丢弃，用户重启一次就发现图标少了。
        required = {"name", "target"}
        if not required.issubset(d.keys()):
            missing = sorted(required - d.keys())
            raise ValueError(f"条目缺少必需字段 {missing}: {d!r}")
        if not str(d.get("name", "")).strip():
            raise ValueError(f"条目 name 为空: {d!r}")
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def uid(self) -> str:
        """
        稳定标识：用于图标缓存键 + 去重。

        **读不出 target 时必须换字段参与哈希。**
        原实现是 sha1("target|args")，而应用商店/损坏的 .lnk 的 TargetPath
        和 args 全是空串，5 个不同的应用于是算出**完全相同**的 uid。
        去重按 uid 走，于是它们被当成彼此的重复项而丢弃 ——
        实测源目录里大部分 .lnk 都进来了，丢的是少数几个，丢掉的 6 个里有 4 个是误丢
        （Codex / OpenCode / Visual Studio / Intel® 处理器识别工具 /
        某 NAS 工具），这正是用户报的「我的快捷方式都没添加上去」。

        退回用 .lnk 自身路径 + 名字参与哈希：既能保住这些应用，
        又仍然能识别「同一个 .lnk 被导入两次」——那才是真重复。
        有 target 时行为不变，真重复（Blackhawk、GitHub 各两份）照旧合并。
        """
        target = (self.target or "").strip()
        args = (self.args or "").strip()
        if target:
            raw = f"{target}|{args}".lower()
        else:
            raw = f"{self.source_lnk}|{self.name}".lower()
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


# ─── 快捷方式解析 ──────────────────────────────────────

def read_lnk(path: Path) -> dict:
    """
    读 .lnk，返回 name/target/args/workdir/icon。

    用 WScript.Shell 而不是自己解析二进制格式 —— .lnk 结构复杂，
    自解析只能覆盖一半情况（相对路径、参数、特殊 flag）。
    """
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(path))
    return {
        "name": path.stem,
        "target": sc.TargetPath or "",
        "args": sc.Arguments or "",
        "workdir": sc.WorkingDirectory or "",
        "icon": sc.IconLocation or "",
    }


def scan_folder(folder: Path) -> tuple[List[Entry], List[str]]:
    """
    扫描一个文件夹里的所有快捷方式。

    返回 (条目, 失败列表)。单个文件坏掉不影响其它文件。
    """
    entries: List[Entry] = []
    errors: List[str] = []
    if not folder.is_dir():
        return entries, [f"目录不存在: {folder}"]

    for f in sorted(folder.iterdir()):
        if not f.is_file():
            continue
        try:
            if f.suffix.lower() == ".lnk":
                info = read_lnk(f)
            elif f.suffix.lower() == ".exe":
                info = {"name": f.stem, "target": str(f),
                        "args": "", "workdir": str(f.parent), "icon": str(f)}
            else:
                continue

            # 目标读不出（UWP/AppX 链接、损坏的 lnk）或指向已删除文件：
            # 仍然保留条目并标记 missing。绝不能静默丢弃 —— 用户会以为
            # 程序吞了他的快捷方式，而界面上根本看不到它存在过。
            target = (info.get("target") or "").strip()
            if not target:
                entries.append(Entry(**{**info, "target": "",
                                        "missing": True}, source_lnk=str(f)))
                errors.append(f"{f.name}: 无法读取目标（可能是应用商店应用或损坏链接）")
                continue
            if not Path(target).exists():
                entries.append(Entry(**{**info, "target": target,
                                        "missing": True}, source_lnk=str(f)))
                errors.append(f"{f.name}: 目标已不存在 ({target})")
                continue

            entries.append(Entry(source_lnk=str(f), **info))
        except Exception as exc:
            errors.append(f"{f.name}: {exc}")
    return entries, errors


# ─── 应用库 ────────────────────────────────────────────

class Library:
    def __init__(self, path: Path | None = None,
                 dismissed_path: Path | None = None):
        self.path = Path(path) if path else db_file()
        # 墓碑清单。测试里会传临时路径，否则会污染用户真实的 dismissed.json。
        self.dismissed_path = (Path(dismissed_path) if dismissed_path
                               else dismissed_db_file())
        self.entries: List[Entry] = []
        #: 用户主动删掉的条目 uid。见 paths.dismissed_file 的说明。
        self.dismissed: set[str] = set()

    # ── 读 ──
    def load(self) -> None:
        self.dismissed = self._load_dismissed()
        if not self.path.exists():
            self.entries = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"shortcuts.corrupt-{stamp}.json")
            shutil.copy2(self.path, backup)
            raise LibraryError(
                f"应用库损坏: {exc}\n已备份到 {backup}\n"
                f"原文件未被删除，修复后可手动恢复。"
            ) from exc

        if not isinstance(data, list):
            raise LibraryError(f"应用库顶层应为数组，实际 {type(data).__name__}")

        self.entries = []
        for item in data:
            try:
                self.entries.append(Entry.from_dict(item))
            except (TypeError, ValueError) as exc:
                # 单条坏掉就跳过，不让整库报废
                print(f"[Library] 跳过无法解析的条目: {exc}")

    # ── 墓碑 ──────────────────────────────────────────────
    def _load_dismissed(self) -> set[str]:
        """
        读墓碑清单。

        **读不出来就当空集，绝不抛异常。** 这份文件只影响「重新导入时要不要
        再加回来」，丢一次的代价仅仅是某个被删过的图标在下次导入时复活；
        而在这里抛异常会让整个库加载失败、启动器直接打不开 ——
        为一个可丢的缓存文件牺牲可用性，比例完全不对。
        """
        try:
            if not self.dismissed_path.exists():
                return set()
            raw = json.loads(self.dismissed_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            print(f"[Library] 墓碑清单读不了，按空处理: {exc}")
            return set()
        if isinstance(raw, dict):          # 容错：早期版本写成 {"uids": [...]}
            raw = raw.get("uids", [])
        if not isinstance(raw, list):
            return set()
        return {str(x) for x in raw if isinstance(x, str) and x}

    def _save_dismissed(self) -> None:
        try:
            self.dismissed_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(sorted(self.dismissed),
                                 ensure_ascii=False, indent=2)
            tmp = self.dismissed_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self.dismissed_path)
        except OSError as exc:
            # 写不进去只是「下次导入会复活」，不能让删除操作整体失败。
            print(f"[Library] 墓碑清单写入失败: {exc}")

    def is_dismissed(self, entry: Entry) -> bool:
        return entry.uid in self.dismissed

    # ── 写 ──
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([e.to_dict() for e in self.entries],
                             ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)

    # ── 删除 ──────────────────────────────────────────────
    def remove(self, uid: str) -> bool:
        """
        从库里删掉一个条目，并记下墓碑。

        返回是否真的删了东西（uid 不存在时返回 False，不报错）。

        ## 绝不碰用户的源文件

        条目大多来自 `%APPDATA%` 之外的用户目录（这里就是
        `%USERPROFILE%` 下的快捷方式文件夹），它们是**用户自己的
        .lnk**。删除启动器里的一个图标是「我不想在启动器里看到它」，
        不是「把这个程序卸载了」。真去 unlink 用户的快捷方式是不可逆的
        破坏，而且几乎肯定不是用户要的 —— 那个 .lnk 可能同时出现在桌面和
        开始菜单里。所以这里只改启动器自己的库。

        ## 为什么要记墓碑

        `import_folder`（以及库为空时的首次自动导入）会重新扫源目录。
        不记的话，用户删掉的图标在**任何一次导入**后都会原样回来 ——
        表现为「我明明删了它又出现了」，比不能删还糟。

        ## 顺序：写盘 → 才改内存

        内存里的 `entries` 是**最后**才动的，而且只在落盘成功之后。
        反过来（先从 entries 剔掉、再 save）时，一旦 save 抛异常，
        磁盘上那条还在、内存里已经没了 —— 界面上少一个图标，
        重启一次它又回来，中途退出则删除等于没发生。
        实测（注入 save 抛 OSError）：先改内存的写法让库条目数从 2
        掉到 1，而磁盘仍是 2 条，两边永久不一致。

        墓碑同样在 save() **之前**写，且写失败只记日志不抛 ——
        墓碑只是「别再自动导回来」的优化，丢了它最坏结果是某个
        被删过的图标在下次导入时回来；为了这个可选优化让整个删除失败，
        比例不对。反过来（save 成功、墓碑失败）也只是少一层保险，
        不会出现「条目消失了又出现」的状态不确定。

        ## 异常语义

        落盘失败时**向上抛**，由调用方（window._delete_entry）处理。
        这里吞掉异常返回 True 会让界面以为删成功了。
        """
        original = self.entries
        kept = [e for e in original if e.uid != uid]
        if len(kept) == len(original):
            return False                       # uid 不存在，不是错误

        added_tomb = uid not in self.dismissed
        if added_tomb:
            self.dismissed.add(uid)
            self._save_dismissed()             # 失败只记日志，不抛
        try:
            self.entries = kept
            self.save()
        except Exception:
            # 恢复**原来的列表对象内容**，而不是拿 kept 再拼 ——
            # self.entries 在上面已经被换成 kept 了，从它里面找不回
            # 被删的那条（第一版就是这么写的，回滚等于没做）。
            self.entries = original
            if added_tomb:
                self.dismissed.discard(uid)
                self._save_dismissed()
            raise
        return True

    def undismiss(self, uid: str) -> None:
        """
        撤销一次删除（清掉墓碑）。条目本身**不会**回到库里 ——
        那是 import_folder 的活。这里只提供「取消隐藏」的语义，
        供将来做撤销删除时用。
        """
        if uid in self.dismissed:
            self.dismissed.discard(uid)
            self._save_dismissed()

    # ── 导入 ──
    def import_folder(self, folder: Path) -> dict:
        """
        导入文件夹内所有快捷方式。

        三种不导入的情况，各有各的原因：
          * uid 已在库里          → 真重复
          * uid 在墓碑里          → 用户主动删过，别再塞回来
          * 上一批导入的同 uid     → 同一次扫描里的重复文件
        """
        found, errors = scan_folder(folder)
        existing = {e.uid for e in self.entries}
        added = skipped = dismissed = 0
        for e in found:
            if e.uid in existing:
                skipped += 1
                continue
            if e.uid in self.dismissed:
                dismissed += 1
                continue
            existing.add(e.uid)
            self.entries.append(e)
            added += 1
        if added:
            self.save()
        return {"added": added, "skipped": skipped,
                "dismissed": dismissed,
                "failed": len(errors), "errors": errors}

    # ── 搜索 ──
    def search(self, query: str, pinyin=None) -> List[Entry]:
        """名称 / 别名 / 拼音 首字母或全拼，全部小写比较。"""
        if not query:
            return list(self.entries)
        q = query.strip().lower()
        if not q:
            return list(self.entries)

        hits = []
        for e in self.entries:
            hay = [e.name.lower()]
            hay += [k.lower() for k in e.keywords]
            if any(q in h for h in hay):
                hits.append(e)
                continue
            if pinyin is None:
                continue
            for field_text in (e.name, " ".join(e.keywords)):
                initials = "".join(
                    pinyin.lazy_pinyin(field_text, style=pinyin.Style.FIRST_LETTER)
                ).lower()
                full = "".join(pinyin.lazy_pinyin(field_text)).lower()
                if q in initials or q in full:
                    hits.append(e)
                    break
        return hits


def load_pinyin():
    """返回 (module, Style) 或 (None, None)。缺依赖时降级为纯文本搜索。"""
    try:
        import pypinyin
        from pypinyin import Style
        return pypinyin, Style
    except ImportError as exc:
        print(f"[Library][警告] pypinyin 未装 ({exc})，拼音搜索不可用")
        return None, None