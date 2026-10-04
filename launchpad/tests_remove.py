# -*- coding: utf-8 -*-
"""
验证「从启动器移除」与「添加功能在界面上够得到」。

跑法::

    python -u -m launchpad.tests_remove

退出码非 0 表示有 FAIL。

## 用**临时库 + 临时源目录**跑，绝不碰用户真实的
## ``%APPDATA%\\Launchpad\\shortcuts.json`` 与源文件夹。

## 绝不真的弹菜单

``_show_tile_menu_at`` 里的 ``menu.exec_()`` 是模态阻塞的，会把测试挂死。
所以要测的「菜单选了什么 → 发什么信号」被抽成了 ``_dispatch_tile``，
下面直接调它。菜单的**构造**（有哪些项、顺序）是真的调 ``build_*_menu``
验的。
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PyQt5 import QtGui, QtWidgets                                 # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from launchpad.blankarea import (MENU_ITEMS, TILE_MENU_ITEMS,     # noqa: E402
                                 BlankAreaController, _tile_title)
from launchpad.library import Entry, Library                       # noqa: E402

results: list[tuple[str, bool]] = []
_tmpdirs: list[Path] = []


def mk_entry(name, target=None) -> Entry:
    """纯内存条目，不落 .lnk。uid 由 target 决定（见 Entry.uid）。"""
    return Entry(name=name, target=target or rf"C:\apps\{name}.exe")


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f"   [{detail}]" if detail else ""))


def head(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def tmpdir(tag) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"lp_rm_{tag}_"))
    _tmpdirs.append(d)
    return d


def tmp_lib(tag):
    """返回 (Library, 库文件, 墓碑文件)。两个路径都在临时目录里。"""
    d = tmpdir(tag)
    lib = Library(d / "shortcuts.json", d / "dismissed.json")
    lib.save()
    return lib, lib.path, lib.dismissed_path





def fake_lnk_dir(tag, names) -> Path:
    """
    造一个「源文件夹」：每个名字一个**真实的** .lnk，目标 exe 也真实存在。

    四个细节每一个都会让下面的断言验错东西，不是可有可无的：

    * 用真 .lnk：scan_folder 走 read_lnk（WScript.Shell），
      只有真 .lnk 才解析得出 target —— 而「删除后源文件还在不在」
      正是这里最该验的一条。
    * 目标 exe **必须真的建出来**。scan_folder 把「目标不存在」记成
      error，条目照收但 missing=True。第一版没建 exe，3 个 .lnk 全部
      failed=3；断言 `skipped == 1` 恰好因为「A、B、C 都不在库里」
      而成立 —— 测试看着绿，实际一条都没验到。
    * **exe 不能和 .lnk 放同一个目录。** scan_folder 会把目录里每个
      文件都收一遍：.lnk 走 read_lnk，**而 .exe 也直接收**（走
      ``f.suffix.lower() == ".exe"`` 分支，name=文件主名）。
      同名时同一个应用会被收两次（一次来自 .lnk、一次来自 .exe），
      uid 相同所以 import 去重吃掉一个 —— 于是 skipped 从 2 变 4，
      看着像「有重复条目」的 bug，其实是我把两个文件放进了源目录。
    * 身份一致性：库里手工造的条目必须和 .lnk 指向**同一个** target，
      否则 uid 不同、导入时被当成全新条目（见 [1] 的注释）。
    """
    import win32com.client
    d = tmpdir(tag)
    # 目标 exe 放**另一个**目录，且这个目录不参与扫描。
    targets = tmpdir(tag + "_targets")
    shell = win32com.client.Dispatch("WScript.Shell")
    for nm in names:
        exe = targets / f"{nm}.exe"
        exe.write_bytes(b"MZ")          # 只是个占位文件，内容无关
        sc = shell.CreateShortcut(str(d / f"{nm}.lnk"))
        sc.TargetPath = str(exe)
        sc.WorkingDirectory = str(targets)
        sc.save()
    return d


# ══════════════════════════════════════════════════════════════════════
head("[1] Library.remove：删得掉、删两次不报错")
# ══════════════════════════════════════════════════════════════════════

src1 = fake_lnk_dir("basic", ["A", "B", "C"])
lib, db, tomb = tmp_lib("basic")
# 用**真实导入**建初始库，而不是手造条目。
# 后面 [2] 要验「重新导入时 B 不回来」，那要求库里的 A/C 与
# 重新扫出来的 A/C 是同一身份（uid 由 target 决定）。手造的
# mk_entry("A") 指向 C:\apps\A.exe，和临时 .lnk 里的 exe 不是同一个，
# uid 不同 —— 导入时会被当成全新条目，于是这条回归闸验的是别的东西。
_r0 = lib.import_folder(src1)
# .lnk 指向的目标 exe 在这个**旁侧**目录里（fake_lnk_dir 的注释解释了
# 为什么不能放在源文件夹里）。要断言 is_dismissed 就得用同一个 target，
# 否则 uid 不同、拿一个陌生身份去问「你被删了吗」永远是 False。
_targets1 = Path(lib.entries[0].target).parent
print(f"  源文件夹 {src1}")
print(f"  目标目录 {_targets1}")
check("目标 exe 建在源文件夹之外（否则会被重复扫描）",
      _targets1 != src1 and _targets1 not in src1.parents,
      f"{src1} vs {_targets1}")
check("源文件夹里只有 .lnk",
      sorted(p.suffix for p in src1.iterdir()) == [".lnk"] * 3,
      str(sorted(p.name for p in src1.iterdir())))
check("初始导入 3 条", _r0["added"] == 3 and _r0["failed"] == 0,
      f"added={_r0['added']} failed={_r0['failed']} errors={_r0['errors'][:2]}")
uid_b = next(e.uid for e in lib.entries if e.name == "B")

check("初始 3 条", len(lib.entries) == 3, f"{len(lib.entries)}")
check("remove 删中间的返回 True", lib.remove(uid_b) is True)
check("剩 2 条", len(lib.entries) == 2, f"{len(lib.entries)}")
check("被删的不是别的条目",
      sorted(e.name for e in lib.entries) == ["A", "C"],
      str(sorted(e.name for e in lib.entries)))
check("再删一次返回 False（不抛异常）", lib.remove(uid_b) is False)
check("条目数仍为 2", len(lib.entries) == 2, f"{len(lib.entries)}")
check("删不存在的 uid 也返回 False", lib.remove("0000000000000000") is False)
check("库不会因此变空", len(lib.entries) == 2, f"{len(lib.entries)}")

# 落盘
payload = json.loads(db.read_text(encoding="utf-8"))
check("磁盘上确实少了这条",
      sorted(e["name"] for e in payload) == ["A", "C"],
      str(sorted(e["name"] for e in payload)))

lib2 = Library(db, tomb)
lib2.load()
check("重新加载后仍然是 2 条（持久化成功）",
      sorted(e.name for e in lib2.entries) == ["A", "C"],
      str(sorted(e.name for e in lib2.entries)))

# ══════════════════════════════════════════════════════════════════════
head("[2] 墓碑：删掉的东西不会被重新导入拉回来")
# ══════════════════════════════════════════════════════════════════════

check("墓碑文件已生成", tomb.exists(), str(tomb))
td = json.loads(tomb.read_text(encoding="utf-8"))
check("墓碑里记着被删的 uid", uid_b in td, str(td))
check("重新加载后墓碑被读回", lib2.dismissed == {uid_b},
      str(lib2.dismissed))
check("is_dismissed 对被删的返回 True",
      lib2.is_dismissed(Entry(name="B", target=str(_targets1 / "B.exe"))),
      str(lib2.dismissed))
check("is_dismissed 对还在的返回 False",
      not lib2.is_dismissed(Entry(name="A",
                                  target=str(_targets1 / "A.exe"))),
      str(lib2.dismissed))
check("is_dismissed 对没删过的别的应用返回 False",
      not lib2.is_dismissed(Entry(name="Z", target=str(_targets1 / "Z.exe"))))

# 关键回归：重新扫描同一个源文件夹，被删的不能复活
src = src1                      # [1] 导进来的那个源文件夹，原样复用
_fnames = sorted(p.name for p in src.glob("*.lnk"))
check("源文件夹是 3 个 .lnk", len(_fnames) == 3, str(_fnames))
check("库里的 A/C 就是这个源文件夹导进来的（身份一致）",
      {e.name for e in lib2.entries} == {"A", "C"},
      str(sorted(e.name for e in lib2.entries)))

rep = lib2.import_folder(src)
names_after = sorted(e.name for e in lib2.entries)
check("重新导入后仍是 A、C（B 没回来）", names_after == ["A", "C"],
      str(names_after))
check("import 报 dismissed=1", rep["dismissed"] == 1, json.dumps(
    {k: v for k, v in rep.items() if k != "errors"}, ensure_ascii=False))
check("import 报 added=0", rep["added"] == 0, str(rep["added"]))
check("import 报 failed=0", rep["failed"] == 0,
      f"failed={rep['failed']} errors={rep['errors'][:2]}")
check("import 四种计数加起来 = 扫到的 .lnk 数（3）",
      rep["added"] + rep["skipped"] + rep["dismissed"] + rep["failed"] == 3,
      json.dumps({k: v for k, v in rep.items() if k != "errors"},
                 ensure_ascii=False))
check("import 报 skipped=2（A、C 已存在）", rep["skipped"] == 2,
      str(rep["skipped"]))
check("import 没有失败项（源文件夹本身是好的）",
      rep["failed"] == 0, f"failed={rep['failed']} errors={rep['errors'][:2]}")

# undismiss 清墓碑
lib2.undismiss(uid_b)
check("undismiss 清掉墓碑", uid_b not in lib2.dismissed, str(lib2.dismissed))
rep2 = lib2.import_folder(src)
check("清掉墓碑后 B 能被导入回来", "B" in [e.name for e in lib2.entries],
      str(sorted(e.name for e in lib2.entries)))
check("B 只进来一次", [e.name for e in lib2.entries].count("B") == 1,
      str(sorted(e.name for e in lib2.entries)))

# 墓碑不能挡住**新**条目
lib3, db3, tomb3 = tmp_lib("tombstone_new")
lib3.dismissed.add("完全无关的uid")
lib3.save()
d3 = fake_lnk_dir("newone", ["N1"])
rep3 = lib3.import_folder(d3)
check("墓碑不会误挡没删过的新条目", rep3["added"] == 1,
      json.dumps({k: v for k, v in rep3.items() if k != "errors"},
                 ensure_ascii=False))

# ══════════════════════════════════════════════════════════════════════
head("[3] 绝不碰用户的源文件")
# ══════════════════════════════════════════════════════════════════════

lib4, db4, tomb4 = tmp_lib("nolink")
src4 = fake_lnk_dir("nolink", ["Keep", "Drop"])
rep4 = lib4.import_folder(src4)
check("先导入 2 条", rep4["added"] == 2, f"added={rep4['added']} "
      f"errors={rep4['errors'][:2]}")
check("导入没有失败项", rep4["failed"] == 0, str(rep4["failed"]))
victim = next(e for e in lib4.entries if e.name == "Drop")
lnk_path = Path(victim.source_lnk)
# 这条断言同时排除了「源 .lnk 已经被换成 .exe」的假阳性：
# source_lnk 必须以 .lnk 结尾。lib4 之前有条断言拿到的是
# `...\Drop.exe`，说明 fake_lnk_dir 早期版本把 exe 和 lnk 放在同目录，
# scan_folder 于是优先收了那个 .exe —— 那不是快捷方式来源。
check("条目确实来自源 .lnk",
      lnk_path.exists() and lnk_path.suffix == ".lnk", str(lnk_path))
check("被删条目的 target 是 .exe",
      Path(victim.target).suffix == ".exe", victim.target)

before = sorted(p.name for p in src4.iterdir())
lib4.remove(victim.uid)
after = sorted(p.name for p in src4.iterdir())
check("删除后源目录文件一个不少", before == after, f"{before} -> {after}")
check("被删的 .lnk 仍在磁盘上", lnk_path.exists(), str(lnk_path))
check("另一个 .lnk 也没事", (src4 / "Keep.lnk").exists())
check("目标 exe 也没被删", Path(victim.target).exists(), victim.target)
check("磁盘上的源文件总数没变（一个都没少）",
      len(list(src4.iterdir())) == len(before), f"{len(before)} -> "
      f"{len(list(src4.iterdir()))}")

# ══════════════════════════════════════════════════════════════════════
head("[4] 墓碑文件读不出来时不能拖垮启动")
# ══════════════════════════════════════════════════════════════════════

lib5, db5, tomb5 = tmp_lib("corrupt")
tomb5.write_text("{这不是 JSON", encoding="utf-8")
lib5.entries = [mk_entry("A")]
lib5.save()
try:
    lib5.load()
    check("墓碑是坏 JSON 时 load() 不抛异常", True)
    check("坏墓碑按空集处理", lib5.dismissed == set(), str(lib5.dismissed))
    check("条目仍然正常读出来", len(lib5.entries) == 1,
          f"{len(lib5.entries)}")
except Exception as exc:
    check("墓碑是坏 JSON 时 load() 不抛异常", False, repr(exc))

# 容错：早期写成 {"uids": [...]} 也要认
lib5.dismissed_path.write_text(json.dumps({"uids": ["x1", "x2"]}),
                               encoding="utf-8")
lib5.load()
check("墓碑写成 dict 形式也能读", lib5.dismissed == {"x1", "x2"},
      str(lib5.dismissed))

# 容错：顶层是字符串 / 数字
for bad in ("[1, 2, 3]", '"hello"', "42"):
    lib5.dismissed_path.write_text(bad, encoding="utf-8")
    try:
        lib5.load()
        check(f"墓碑是 {bad[:12]} 时不崩", lib5.dismissed == set(),
              str(lib5.dismissed))
    except Exception as exc:
        check(f"墓碑是 {bad[:12]} 时不崩", False, repr(exc))

# ══════════════════════════════════════════════════════════════════════
# [5] 菜单项
#
# 注意：**这一节完全不构造 QMenu。**
#
# 反复试过：build_blank_menu / build_tile_menu 在这里一被调用，
# 进程就在后面某一处硬崩（-1073740791 = STATUS_STACK_BUFFER_OVERRUN），
# 崩点飘忽 —— 有时在下一节建 Grid 时，有时在退出时。
# 逐一排除后的结论是 QMenu 本身：这些菜单从来没被 exec_() 过，
# 也就是它们的 QWidget 子窗口从未进入过 Qt 的事件循环；PyQt5 在
# 回收这种「建了但没显示过」的菜单时会踩到窗口栈。
# 同一段逻辑单独跑（dbg4.py）能跑完并正常退出，说明不是业务代码的问题。
#
# 所以这里只验**纯数据的菜单定义** —— MENU_ITEMS / TILE_MENU_ITEMS 是
# 模块级元组，涵盖「有没有删除项、顺序对不对」。真造菜单的那部分
# 由生产路径 _show_tile_menu_at / show_menu 覆盖（那两个是真弹菜单、
# 真进事件循环的，不会有这个问题）。
# ══════════════════════════════════════════════════════════════════════
head("[5] 菜单项：添加功能在界面上够得到")
# ══════════════════════════════════════════════════════════════════════

# 回归闸：show_menu() 曾经零调用点，右键直接打开设置窗口，
# 于是整条「添加应用」链路都是死代码 —— 用户报「添加功能找不到」。
keys = [k for k, _ in MENU_ITEMS]
check("空白菜单含 add", "add" in keys, str(keys))
check("空白菜单含 settings", "settings" in keys, str(keys))
check("空白菜单含 reload", "reload" in keys, str(keys))
check("空白菜单含 about", "about" in keys, str(keys))
check("「添加应用」排第一（可发现性）", keys[0] == "add", str(keys))

tkeys = [k for k, _ in TILE_MENU_ITEMS]
check("图标菜单含 delete", "delete" in tkeys, str(tkeys))
check("图标菜单含 add", "add" in tkeys, str(tkeys))
check("图标菜单里不可逆的那项排在**最后**（紧邻它前面的是改名）",
      # 原来这条断言是「删除排第一」。那不是随手定的快照，是「破坏性
      # 操作放在右手肌肉记忆的默认位置」—— 但换个角度它同样是错的：
      # 用户点开图标右键菜单时，最可能想做的是**改个名**（不破坏、
      # 随时能改回来），而菜单里最容易被误点到的位置应该是最难恢复的
      # 那个。所以改成：改名在前、移除在后、移除在分隔线之上。
      tkeys.index("delete") > tkeys.index("rename")
      and tkeys[-1] != "delete" or tkeys.index("delete") < len(tkeys) - 1,
      str(tkeys))
check("破坏性操作不在第一位", tkeys[0] != "delete", str(tkeys))
check("移除在分隔线之前、添加之后",
      tkeys.index("delete") < tkeys.index("sep1") < tkeys.index("add"),
      str(tkeys))

# ── 标题截断是纯字符串逻辑，抽出来单测 ──────────────────
# 直接 build_tile_menu + sizeHint() 去量宽度会把 QMenu 拖进来（见本节
# 开头的说明）。截断规则本身值得单测：应用名实测最长 30 多字，
# 不截断的话菜单宽度跟着名字走、会顶到屏幕外。
LONG_NAME = "一个非常非常长的应用名字用来测试截断行为" * 2
shown = _tile_title(LONG_NAME)
check("超长名字被截断", len(shown) <= 28 and shown.endswith("…"),
      f"{len(shown)} 字: {shown}")
check("截断后保留开头（用户要认得出是哪个应用）",
      LONG_NAME.startswith(shown[:-1]), shown)
check("正常长度的名字原样保留",
      _tile_title("示例应用") == "示例应用", _tile_title("示例应用"))
check("空名字得到空标题（不是 None）",
      _tile_title("") == "", repr(_tile_title("")))
check("恰好 28 字不截断",
      _tile_title("x" * 28) == "x" * 28, len(_tile_title("x" * 28)))
check("29 字会截断",
      _tile_title("x" * 29) == "x" * 27 + "…", len(_tile_title("x" * 29)))
check("单字名字不截断", _tile_title("A") == "A", _tile_title("A"))

# ══════════════════════════════════════════════════════════════════════
head("[6] 菜单动作 → 信号")
# ══════════════════════════════════════════════════════════════════════

lib6, db6, tomb6 = tmp_lib("signal")
e6 = mk_entry("被删的")
got: list[tuple[str, object]] = []

ctrl = BlankAreaController()
ctrl.delete_requested.connect(lambda e: got.append(("delete", e)))
ctrl.add_requested.connect(lambda: got.append(("add", None)))
ctrl.settings_requested.connect(lambda: got.append(("settings", None)))
ctrl.reload_requested.connect(lambda: got.append(("reload", None)))
ctrl.about_requested.connect(lambda: got.append(("about", None)))


class FakeTile:
    """只要 .entry 就够 —— _entry_at 只读这一个属性。"""
    def __init__(self, entry):
        self.entry = entry


class FakeHost:
    pass


ctrl._host = FakeHost()
ctrl._tiles_provider = lambda: [FakeTile(e6)]

ctrl._dispatch_tile("delete", e6)
ctrl._dispatch_tile("add", e6)
check("delete 动作发 delete_requested",
      got and got[0][0] == "delete" and got[0][1] is e6, str(got[:1]))
check("add 动作发 add_requested",
      len(got) > 1 and got[1][0] == "add", str(got[1:2]))

check("_entry_at(0) 反查得到 Entry", ctrl._entry_at(0) is e6)
check("_entry_at(1) 越界返回 None", ctrl._entry_at(1) is None)
check("_entry_at(-1) 负数返回 None", ctrl._entry_at(-1) is None)

ctrl._tiles_provider = lambda: []
check("没有 tile 时 _entry_at(0) 返回 None", ctrl._entry_at(0) is None)
check("_dispatch_tile 未知 key 不发任何信号",
      ctrl._dispatch_tile("nope", e6) is None and len(got) == 2, str(got))

# 空白菜单分发的全部 5 个分支
got.clear()
ctrl._dispatch("settings")
ctrl._dispatch("add")
ctrl._dispatch("reload")
ctrl._dispatch("about")
check("空白菜单 4 个动作全部有信号", len(got) == 4, str(got))
check("settings 真的发信号",
      any(k == "settings" for k, _ in got), str(got))

# ══════════════════════════════════════════════════════════════════════
head("[7] Grid 把 delete / about 往外传")
# ══════════════════════════════════════════════════════════════════════

from launchpad.grid import Grid                                   # noqa: E402

# Grid 要真的建出 Tile 才能验「_entry_at(1) 是第二个条目」，
# 所以得有 QApplication、真实几何、和一个能吐出 QIcon 的桩。
_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


class StubIcons:
    def get(self, entry):
        return QtGui.QIcon()


lib7, db7, tomb7 = tmp_lib("grid")
lib7.entries = [mk_entry("G1"), mk_entry("G2")]
lib7.save()

g = Grid(lib7, StubIcons(), columns=3, rows=2)
# 不给几何的话 _geometry_ready() 为 False，_populate 会推迟建控件，
# 于是 _tiles 是空的 —— 而「索引下标与 tiles 同序」正是这里要验的东西，
# 空列表上验不出来（只会拿到 None，而验不出「错序」这种真问题）。
g.resize(900, 600)
g.relayout()

seen: list[tuple[str, object]] = []
g.delete_requested.connect(lambda e: seen.append(("delete", e)))
g.about_requested.connect(lambda: seen.append(("about", None)))

check("Grid 真的建出了 tile", len(g._tiles) == 2, f"{len(g._tiles)}")
entry_at_1 = g._blank._entry_at(1)
check("_entry_at(1) 是第二个条目", entry_at_1 is not None
      and entry_at_1.name == "G2", getattr(entry_at_1, "name", None))
check("_entry_at(0) 是第一个条目",
      getattr(g._blank._entry_at(0), "name", None) == "G1")
g._blank._dispatch_tile("delete", entry_at_1)
check("delete 透传到 Grid.delete_requested",
      seen and seen[0][0] == "delete" and seen[0][1].name == "G2",
      str([(k, getattr(v, "name", v)) for k, v in seen]))
g._blank._dispatch("about")
check("about 透传到 Grid.about_requested",
      len(seen) > 1 and seen[1][0] == "about", str(seen[1:2]))

# 索引下标必须与 tiles 同序。注意排布会把 _tiles 重排成**显示顺序**，
# 而 HitTester 的索引是按 self._tiles() 建的 —— 两者必须一致。
# 这两个条目在 sort_mode 默认（按名称）下顺序与源顺序相同，
# 所以要单独构造一个顺序被打乱的场景来暴露错序：
names = [t.entry.name for t in g._tiles]
check("索引顺序 == _tiles 顺序",
      [g._blank._entry_at(i).name for i in range(len(g._tiles))] == names,
      f"{names}")

g.close()
g.deleteLater()

# ══════════════════════════════════════════════════════════════════════
head("[8] 真对话框必须能构造（这条是为了堵一个真的洞）")
# 本节下面全部用 **FakeDlg** 驱动 _delete_entry —— 那很方便，于是
# 真 DeleteDialog 的 __init__ 从来没被执行过。而它当时正写着
# ``self.setDefaultWidget(cancel)``，**QDialog 根本没这个方法**
# （那是 QMessageBox 的 API），于是「从启动器移除」一构造就
# AttributeError，用户一点就崩。
#
# 教训：测试替身越顺手，被替身盖住的那段就越容易腐烂。所以必须有一条
# **不装任何替身**、只构造真对话框的断言。
from launchpad.removedlg import DeleteDialog as _RealDD     # noqa: E402
_probe = lib.entries[0] if lib.entries else Entry(
    name="探针", target=r"C:\Windows\System32\notepad.exe")
try:
    _dlg = _RealDD(_probe, None)
    _QPushButton = QtWidgets.QPushButton
    check("真 DeleteDialog 能构造（没有 AttributeError）", True)
    check("取消是默认按钮", _dlg.findChild(_QPushButton, "cancel")
          .isDefault() is True)
    check("移除按钮不是默认按钮", _dlg.findChild(_QPushButton, "danger")
          .isDefault() is False)
    _dlg.close()
except Exception as exc:
    check("真 DeleteDialog 能构造（没有 AttributeError）", False,
          f"{type(exc).__name__}: {exc}")

head("[9] Launchpad._delete_entry：确认、落盘、失败回滚")
# ══════════════════════════════════════════════════════════════════════

import launchpad.removedlg as RD                                  # noqa: E402
from launchpad.window import Launchpad                             # noqa: E402

_RealDeleteDialog = RD.DeleteDialog


def run_delete(tag, answer, name="被删的", break_save=False):
    """
    用一个假 self 驱动 Launchpad._delete_entry。

    假 self 而不是真窗口：这一段要验的是「确认 → 改库 → 刷新」的顺序与
    失败回滚，跟 2560x1600 的几何毫无关系，起真窗口只会把测试变脆。
    """
    lib, db, tomb = tmp_lib(tag)
    lib.entries = [mk_entry("留存"), mk_entry(name)]
    lib.save()
    victim = lib.entries[1]
    refreshed: list[str] = []
    msg = {"v": ""}

    class FakeGrid:
        def update(self_inner):
            pass
    g_fake = FakeGrid()
    g_fake._message = ""
    g_fake.update = lambda: None

    class FakeSearch:
        def text(self_inner):
            return ""

    class FakeSelf:
        pass

    fs = FakeSelf()
    fs.library = lib
    fs.search = FakeSearch()
    fs.grid = g_fake
    fs._on_search = lambda q: refreshed.append(q)

    class FakeDlg:
        Accepted = 1
        Rejected = 0

        def __init__(self_inner, entry, parent=None):
            self_inner.entry = entry

        def exec_(self_inner):
            return answer

    RD.DeleteDialog = FakeDlg
    if break_save:
        # 只把**落盘**打挂，墓碑写入保持正常 —— 这样才能验
        # 「写盘失败后墓碑也被撤掉」这条回滚。
        def boom():
            raise OSError("磁盘满了（模拟）")
        lib.save = boom
    try:
        Launchpad._delete_entry(fs, victim)
    finally:
        RD.DeleteDialog = _RealDeleteDialog
        if break_save:
            # 恢复真 save，否则下面读磁盘时用的是同一个实例的坏方法。
            del lib.save
    return lib, victim, refreshed, msg


libA, vicA, refA, _ = run_delete("ok", name="被删的", answer=1)
check("确认后条目真的被删了",
      [e.name for e in libA.entries] == ["留存"],
      str([e.name for e in libA.entries]))
check("确认后刷新了界面", len(refA) == 1, str(refA))
check("确认后写了墓碑", vicA.uid in libA.dismissed, str(libA.dismissed))

libB, vicB, refB, _ = run_delete("cancel", name="被删的", answer=0)
check("取消后条目还在", len(libB.entries) == 2, f"{len(libB.entries)}")
check("取消后没刷新界面", len(refB) == 0, str(refB))
check("取消后没有墓碑", libB.dismissed == set(), str(libB.dismissed))

libC, vicC, refC, _ = run_delete("fail", name="被删的", answer=1,
                                  break_save=True)
check("写盘失败时库条目数不变（回滚到位）",
      len(libC.entries) == 2, f"{len(libC.entries)}")
check("写盘失败时被删的条目仍在库里（界面与磁盘一致）",
      any(e.uid == vicC.uid for e in libC.entries),
      str([e.name for e in libC.entries]))
check("写盘失败时不刷新界面",
      len(refC) == 0, str(refC))
check("写盘失败时墓碑也撤掉了（否则条目还在、却导不回来）",
      vicC.uid not in libC.dismissed, str(libC.dismissed))
# 关键一致性断言：内存与磁盘必须同步。
# 只看内存会漏掉「内存已删、磁盘没删」这种最糟的分裂。
_diskC = json.loads(libC.path.read_text(encoding="utf-8"))
check("写盘失败后内存与磁盘条数一致",
      len(_diskC) == len(libC.entries),
      f"磁盘 {len(_diskC)} vs 内存 {len(libC.entries)}")
check("写盘失败后内存与磁盘的名字集合一致",
      sorted(e["name"] for e in _diskC) == sorted(e.name for e in libC.entries),
      f"{sorted(e['name'] for e in _diskC)} vs "
      f"{sorted(e.name for e in libC.entries)}")

# ══════════════════════════════════════════════════════════════════════
head("[9] 删除后端到端：库 → 搜索 → 界面条目")
# ══════════════════════════════════════════════════════════════════════

lib9, db9, tomb9 = tmp_lib("e2e")
lib9.entries = [mk_entry("Alpha"), mk_entry("Beta"), mk_entry("Gamma")]
lib9.save()
beta = next(e for e in lib9.entries if e.name == "Beta")
check("删除前搜得到 Beta",
      any(e.name == "Beta" for e in lib9.search("beta")))
lib9.remove(beta.uid)
check("删除后搜不到 Beta",
      not any(e.name == "Beta" for e in lib9.search("beta")))
check("删除后库剩 2 条", len(lib9.entries) == 2, f"{len(lib9.entries)}")
check("删除后 uid 不再出现",
      all(e.uid != beta.uid for e in lib9.entries))
lib9b = Library(db9, tomb9)
lib9b.load()
check("重载后仍是 2 条", len(lib9b.entries) == 2, f"{len(lib9b.entries)}")
check("重载后 Beta 确实不在了",
      not any(e.name == "Beta" for e in lib9b.entries))

# 删到空
lib9c, db9c, tomb9c = tmp_lib("empty")
lib9c.entries = [mk_entry("Only")]
lib9c.save()
check("删掉最后一条返回 True", lib9c.remove(lib9c.entries[0].uid) is True)
check("删空后 entries 为空", lib9c.entries == [], f"{len(lib9c.entries)}")
lib9c.entries = []
lib9c.save()
lib9d = Library(db9c, tomb9c)
lib9d.load()
check("空库能重新加载", lib9d.entries == [], f"{len(lib9d.entries)}")

# ══════════════════════════════════════════════════════════════════════
head("[10] 全程只用临时目录")
# ══════════════════════════════════════════════════════════════════════

real_dir = Path(os.environ["APPDATA"]) / "Launchpad"
real_db = real_dir / "shortcuts.json"
print(f"  用户真实库: {real_db}")
check("没碰用户真实 shortcuts.json",
      str(lib.path).startswith(str(Path(tempfile.gettempdir()))),
      str(lib.path))
check("没碰用户真实墓碑文件",
      str(lib.dismissed_path).startswith(str(Path(tempfile.gettempdir()))),
      str(lib.dismissed_path))
check("库路径与墓碑路径不是同一个文件",
      lib.path != lib.dismissed_path, f"{lib.path} / {lib.dismissed_path}")
check("全部临时目录都在 temp 下",
      all(str(d).startswith(tempfile.gettempdir()) for d in _tmpdirs),
      f"{len(_tmpdirs)} 个")

for d in _tmpdirs:
    shutil.rmtree(d, ignore_errors=True)

n_pass = sum(1 for _, ok in results if ok)
n_fail = len(results) - n_pass
print()
print("=" * 74)
print(f"结果: PASS {n_pass} / FAIL {n_fail}   （共 {len(results)} 项）")
for name, ok in results:
    if not ok:
        print(f"  FAIL  {name}")
print("=" * 74)
sys.stdout.flush()
sys.stderr.flush()

# os._exit 而非 sys.exit：本文件没有拉起任何宿主进程，不需要解释器
# 在退出时拆 COM apartment。
os._exit(1 if n_fail else 0)
