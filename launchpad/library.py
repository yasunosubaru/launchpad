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
from .paths import renames_file as _renames_file        # noqa: E402
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


def renames_db_file() -> Path:
    return _renames_file()


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
    # 用户自定义的显示名。**空串 = 用原名 name。**
    # 磁盘上的真相是 renames.json（uid -> 显示名），这里只是物化缓存：
    # load() 和 import_folder() 会重新贴回来。见 paths.renames_file 的说明。
    display: str = ""
    # 最近一次成功启动的 epoch 秒，0 = 从未用过。
    # 排序方式 recent 依赖它。to_dict/from_dict 都走 dataclass 字段反射，
    # 所以加了这个字段就自动往返，不需要改序列化代码。
    last_used: float = 0.0

    @property
    def label(self) -> str:
        """
        **界面上要显示的名字**：自定义名优先，没有就用原名。

        这是显示的唯一真相来源。直接读 ``entry.name`` 的地方一律要改成
        读这个 —— 否则改名对某些路径不生效，而那种「有的地方改了有的地方
        没改」的半吊子状态最难排查。
        """
        return self.display.strip() or self.name

    @property
    def renamed(self) -> bool:
        """是否被用户改过名（决定要不要在菜单里给「还原原名」）。"""
        return bool(self.display.strip()) and self.display.strip() != self.name

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
                 dismissed_path: Path | None = None,
                 renames_path: Path | None = None):
        self.path = Path(path) if path else db_file()
        # 墓碑清单。测试里会传临时路径，否则会污染用户真实的 dismissed.json。
        self.dismissed_path = (Path(dismissed_path) if dismissed_path
                               else dismissed_db_file())
        # 自定义名清单。理由同上：测试必须能传临时路径。
        self.renames_path = (Path(renames_path) if renames_path
                             else renames_db_file())
        self.entries: List[Entry] = []
        #: 用户主动删掉的条目 uid。见 paths.dismissed_file 的说明。
        self.dismissed: set[str] = set()
        #: uid -> 自定义显示名。见 paths.renames_file 的说明。
        self.renames: dict[str, str] = {}
        #: 两张表是否已经从磁盘读过。没读过就导入是**危险**的：
        #: ``import_folder`` 会拿着空的墓碑/改名表去判断，于是用户
        #: 删掉的条目复活、改过的名字被冲掉 —— 正好是这两张表
        #: 存在的理由。见 :meth:`_ensure_side_tables`。
        self._side_tables_loaded = False

    # ── 读 ──
    def load(self) -> None:
        self.dismissed = self._load_dismissed()
        self.renames = self._load_renames()
        self._side_tables_loaded = True
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
        # 名字覆盖要在条目都读完之后贴：entry.load 走的是磁盘上的 name，
        # 自定义名是另一份文件里的第二真相。
        self.apply_renames()

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

    # ── 自定义名 ──────────────────────────────────────────
    #
    # 纪律与 remove() 完全一致：**先落盘、再改内存**，落盘失败就回滚内存并
    # 返回 False。理由是内存和磁盘分家之后，用户看到「界面上改了、重启又
    # 变回去」—— 而那正好是这个功能要解决的问题本身。

    def _load_renames(self) -> dict:
        try:
            if not self.renames_path.exists():
                return {}
            raw = json.loads(self.renames_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            print(f"[Library] 自定义名清单读不了，按空处理: {exc}")
            return {}
        if not isinstance(raw, dict):
            print(f"[Library] 自定义名清单顶层应为对象，实际 "
                  f"{type(raw).__name__}，按空处理")
            return {}
        return {str(k): str(v).strip()
                for k, v in raw.items()
                if isinstance(k, str) and k and isinstance(v, str)
                and v.strip()}

    def _save_renames(self) -> bool:
        """落盘。返回是否成功 —— 调用方据此决定要不要回滚内存。"""
        try:
            self.renames_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.renames, ensure_ascii=False, indent=2)
            tmp = self.renames_path.with_suffix(".json.tmp")
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self.renames_path)
            return True
        except OSError as exc:
            print(f"[Library] 自定义名清单写入失败: {exc}")
            return False

    def apply_renames(self) -> None:
        """把 ``renames`` 贴到内存条目上（``display`` 字段）。

        load() 与 import_folder() 都要调 —— 这两个是「重建了 Entry 对象」
        的地方，也就是自定义名唯一会丢的地方。
        """
        if not self.renames:
            return
        for e in self.entries:
            new = self.renames.get(e.uid)
            if new:
                e.display = new

    def rename(self, uid: str, new_name: str) -> bool:
        """
        给 ``uid`` 起自定义名。``new_name`` 为空 = 还原原名。

        返回是否成功。**磁盘上的 ``.lnk`` 一个字节都不动** —— 与「移除」
        同一套语义：那个 .lnk 可能同时出现在开始菜单和其它地方，改它是
        越权，而且几乎肯定不是用户的意思。

        uid 查不到、或落盘失败，都返回 False 且**不改内存**。
        """
        # 落盘前必须确保读过：renames 若是空字典，_save_renames 会把磁盘上
        # 已有的改名记录**整个覆盖掉** —— 那些名字于是全部恢复原样，而
        # 文件本身也只剩这一条。
        self._ensure_side_tables()
        target = None
        for e in self.entries:
            if e.uid == uid:
                target = e
                break
        if target is None:
            print(f"[Rename] 库里没有 uid={uid} 的条目，忽略")
            return False

        new_name = (new_name or "").strip()
        old_display = target.display
        was_renamed = uid in self.renames
        old_saved = self.renames.get(uid)

        if new_name:
            self.renames[uid] = new_name
        else:
            self.renames.pop(uid, None)
        if not self._save_renames():
            # 回滚：磁盘没变，内存也不能变
            if was_renamed:
                self.renames[uid] = old_saved
            else:
                self.renames.pop(uid, None)
            return False

        target.display = new_name
        return True

    def reset_name(self, uid: str) -> bool:
        """还原成 .lnk 里的原名。"""
        return self.rename(uid, "")

    def custom_name(self, uid: str) -> str:
        """当前的自定义名（没有就返回空串）。给「还原原名」菜单项判断用。"""
        return self.renames.get(uid, "")

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
        self._ensure_side_tables()
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

    def _ensure_side_tables(self) -> None:
        """没从磁盘读过就补读一次（幂等）。

        **这个洞是真实踩出来的。** ``import_folder`` 靠 ``self.dismissed``
        和 ``self.renames`` 判断，但这两张表默认是**空字典** ——
        只有 ``load()`` 才会去磁盘读。于是一个刚 ``Library()`` 出来、
        直接就 ``import_folder`` 的调用方（测试、脚本、将来可能的任何
        入口）拿到的是空的表，于是：

        * 墓碑失效 -> 用户在界面上删掉的图标，在下一次导入后**全部回来**；
        * 改名失效 -> 用户改过的名字，在重新导入后**被冲回原名**。

        两个都是这两张表要解决的核心问题，却从另一个门又漏进来了。
        所以判断之前先确保读过。
        """
        if self._side_tables_loaded:
            return
        self.dismissed = self._load_dismissed()
        self.renames = self._load_renames()
        self._side_tables_loaded = True

    # ── 导入 ──
    def import_folder(self, folder: Path) -> dict:
        """
        导入文件夹内所有快捷方式。

        三种不导入的情况，各有各的原因：
          * uid 已在库里          → 真重复
          * uid 在墓碑里          → 用户主动删过，别再塞回来
          * 上一批导入的同 uid     → 同一次扫描里的重复文件
        """
        self._ensure_side_tables()
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
            # 新建的 Entry 是从 .lnk 现读的，display 一定是空 —— 不贴回
            # 的话，用户改过的名在「添加应用 → 浏览文件夹」之后就消失了。
            self.apply_renames()
            self.save()
        return {"added": added, "skipped": skipped,
                "dismissed": dismissed,
                "failed": len(errors), "errors": errors}

    # ── 搜索 ──
    def search(self, query: str, pinyin=None) -> List[Entry]:
        """
        名称 / 别名 / 拼音 首字母或全拼，全部小写比较。

        **原名和自定义名都要搜。** 改名是「加一层显示名」，不是「覆盖原名」
        ——磁盘上的 .lnk 一个字节都没动，它的原名也就还在。用户在开始菜单
        里看到的是「微信」，他多半会拿「微信」来搜；只搜自定义名的话，
        改完名就等于把这个应用从搜索里弄丢了。
        """
        if not query:
            return list(self.entries)
        q = query.strip().lower()
        if not q:
            return list(self.entries)

        hits = []
        for e in self.entries:
            hay = [e.name.lower(), e.display.strip().lower()]
            hay += [k.lower() for k in e.keywords]
            if any(q in h for h in hay if h):
                hits.append(e)
                continue
            if pinyin is None:
                continue
            # 拼音对「原名 / 自定义名 / 别名」都算一遍
            for field_text in (e.name, e.display.strip(),
                               " ".join(e.keywords)):
                if not field_text:
                    continue
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