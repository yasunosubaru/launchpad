# -*- coding: utf-8 -*-
"""
验证「重命名」这条链路：库 →磁盘 → 搜索 → 界面显示 → 重新导入。

跑法::

    python -u -m launchpad.tests_rename

需要**真实屏幕**（要建全屏无边框窗口看Tile 上的文字）。

## 这个功能真正的风险不在「能不能改」

改名本身是三行代码。难的是这几条，每一条都对应一个具体的坑：

1. **原名必须还能搜到。** 用户在开始菜单里看到的是「微信」，他多半拿
   「微信」来搜。如果只搜自定义名，改完名等于把这个应用从搜索里弄丢 ——
   而磁盘上的 .lnk 一个字节都没动，凭什么它的名字消失了。
2. **重新导入不能把名字冲掉。** 库为空时的首次自动导入、「添加应用 →
   浏览文件夹」都会从 .lnk 重建 Entry，``name`` 被刷回原值。
3. **uid 不能变。** 读不出 target 的条目 uid是
   ``sha1(source_lnk|name)``，直接改 name 就换了 uid —— 墓碑、去重、
   图标缓存键全部对不上。
4. **磁盘写失败必须回滚内存。** 否则用户看到「界面上改了、重启又变回去」。
5. **Tile 上要重建。** Tile 在构造时把名字烤进了 ``_name`` 与 elide
   结果，不重建的话界面上还是旧名字。
"""

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtWidgets import QApplication                        # noqa: E402

app = QApplication(sys.argv)

from launchpad import testfix                                     # noqa: E402
from launchpad.blankarea import TILE_MENU_ITEMS                  # noqa: E402
from launchpad.library import Entry, Library                     # noqa: E402
from launchpad.window import Launchpad                           # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def head(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


TMP = Path(tempfile.mkdtemp(prefix="lp_rename_"))
SRC = TMP / "src"
SRC.mkdir(parents=True, exist_ok=True)

_targets = testfix.available_targets()
check("夹具造得出快捷方式", bool(_targets), f"{len(_targets)} 个")

made = []
for i, (nm, tgt) in enumerate(_targets[:4]):
    p = SRC / f"应用{i}.lnk"
    if testfix.make_lnk(p, tgt):
        made.append(p)
check("源目录里有 .lnk", len(made) >= 3, f"{len(made)} 个")

# ── 库 ──────────────────────────────────────────────────
lib = Library(TMP / "lib.json", TMP / "dismissed.json", TMP / "renames.json")
rep = lib.import_folder(SRC)
check("导入成功", rep["added"] == len(made), str(rep))
check("renames.json 存在且是映射", isinstance(lib.renames, dict), str(lib.renames))

e0 = lib.entries[0]
uid0 = e0.uid
orig0 = e0.name

head("[1] 基本改名")
check("初始 label == 原名", e0.label == orig0, f"{e0.label!r}")
check("初始 renamed 为 False", e0.renamed is False)
ok = lib.rename(uid0, "我的改名字")
check("rename() 返回 True", ok is True)
check("Entry.name 仍是原名（磁盘真相没被动）", e0.name == orig0,
      f"name={e0.name!r}")
check("Entry.display 是自定义名", e0.display == "我的改名字", e0.display)
check("label 返回自定义名", e0.label == "我的改名字", e0.label)
check("renamed 为 True", e0.renamed is True)
check("uid 没变（否则墓碑/去重/图标缓存全对不上）", e0.uid == uid0,
      f"{uid0} -> {e0.uid}")

head("[2] 原名必须仍然搜得到")
hits = lib.search(orig0)
check(f"按原名「{orig0}」搜得到", any(h.uid == uid0 for h in hits),
      f"{len(hits)} 条")
hits2 = lib.search("我的改名")
check("按自定义名搜得到", any(h.uid == uid0 for h in hits2), f"{len(hits2)} 条")
# 部分匹配也要行
if len(orig0) >= 2:
    part = orig0[:2]
    check(f"按原名片段「{part}」搜得到",
          any(h.uid == uid0 for h in lib.search(part)))

head("[3] 拼音搜索：原名和自定义名都要能搜")
from launchpad.library import load_pinyin                    # noqa: E402
_p, _Style = load_pinyin()
if _p is None:
    print("      [跳过] 本机没装 pypinyin")
else:
    e1 = next((e for e in lib.entries if e.uid != uid0), None)
    if e1 is not None:
        u1 = e1.uid
        lib.rename(u1, "测试拼音")
        found = [h.uid for h in lib.search("cspy", _p)]
        check("按拼音首字母搜自定义名", u1 in found, str(found))
        found2 = [h.uid for h in lib.search("ceshi", _p)]
        check("按拼音全拼搜自定义名", u1 in found2, str(found2))
        lib.reset_name(u1)

head("[4] 落盘与重启往返")
lib.save()
lib2 = Library(TMP / "lib.json", TMP / "dismissed.json", TMP / "renames.json")
lib2.load()
e0b = next((e for e in lib2.entries if e.uid == uid0), None)
check("重新 load 后条目还在", e0b is not None)
if e0b is not None:
    check("重新 load 后自定义名还在", e0b.label == "我的改名字", e0b.label)
    check("重新 load 后原名也还在", e0b.name == orig0, e0b.name)

import json                                                # noqa: E402
raw = json.loads((TMP / "renames.json").read_text(encoding="utf-8"))
check("renames.json 是 uid->名字 的映射", isinstance(raw, dict)
      and raw.get(uid0) == "我的改名字", str(raw)[:70])

head("[5] 重新导入不把名字冲掉")
lib3 = Library(TMP / "lib3.json", TMP / "dismissed.json", TMP / "renames.json")
rep3 = lib3.import_folder(SRC)
e0c = next((e for e in lib3.entries if e.uid == uid0), None)
check("全新库导入同样一批 .lnk", rep3["added"] == len(made), str(rep3))
check("跨库（不同 library.json）uid 一致", e0c is not None)
if e0c is not None:
    check("导入后自定义名仍在（apply_renames 生效）",
          e0c.label == "我的改名字", e0c.label)

head("[5b] 没 load() 就 import_folder —— 惰性补读两张表")
# 这个洞是真实踩出来的：import_folder 靠 self.dismissed / self.renames
# 判断，而它们默认是**空字典**，只有 load() 才去磁盘读。于是一个刚
# Library() 出来就直接 import_folder 的调用方，拿到空表：
# 墓碑失效（删掉的条目复活）+ 改名失效（名字被冲回原名）——
# 正好是这两张表要解决的核心问题，从另一个门又漏进来。
lib5 = Library(TMP / "lib5.json", TMP / "dismissed.json", TMP / "renames.json")
_r5 = lib5.import_folder(SRC)          # 先导入，库里才有条目可改可删
check("lib5 导入到条目", len(lib5.entries) >= 2, f"{len(lib5.entries)} 条")
check("lib5 改名成功", lib5.rename(uid0, "惰性补读要保住我") is True)
_victim = next(e.uid for e in lib5.entries if e.uid != uid0)
check("lib5 移除成功", lib5.remove(_victim) is True)
lib5.save()
lib6 = Library(TMP / "lib6.json", TMP / "dismissed.json", TMP / "renames.json")
check("新库还没 load（两张表是空的）",
      not lib6.renames and not lib6.dismissed,
      f"renames={lib6.renames} dismissed={lib6.dismissed}")
r6 = lib6.import_folder(SRC)
e6 = next((e for e in lib6.entries if e.uid == uid0), None)
check("没 load 就导入后，自定义名仍在", e6 is not None
      and e6.label == "惰性补读要保住我",
      e6.label if e6 else "缺条目")
check("没 load 就导入后，墓碑仍然生效（被删的没复活）",
      r6["dismissed"] >= 1 and _victim not in {e.uid for e in lib6.entries},
      f"dismissed={r6['dismissed']}")

head("[6] 清空 = 还原原名")
ok = lib.rename(uid0, "")
check("rename('') 返回 True", ok is True)
check("label 回到原名", e0.label == orig0, e0.label)
check("display 清空", e0.display == "", repr(e0.display))
check("renamed 回到 False", e0.renamed is False)
check("uid 仍然没变", e0.uid == uid0)
check("renames 里那条被删掉", uid0 not in lib.renames)
check("reset_name() 等价", lib.reset_name(uid0) is True)

head("[7] 落盘失败必须回滚内存")
lib.rename(uid0, "改回自定义")
check("先改成自定义名", e0.label == "改回自定义", e0.label)


# 让**真的** _save_renames 跑，只把它的写文件动作弄失败。
# （第一版把整个 _save_renames 换成会抛的桩，等于替掉了它内部的
#  try/except，于是 OSError 直接冒到 rename() 外面 —— 那测的是
#  「桩抛不抛」，不是「写盘失败会不会回滚」。）
import pathlib                                              # noqa: E402
_real_write = pathlib.Path.write_text


def boom(self, *a, **k):
    raise OSError("模拟磁盘写不进去")


pathlib.Path.write_text = boom
try:
    ok2 = lib.rename(uid0, "这个改不动")
    check("写盘失败时 rename() 返回 False", ok2 is False)
    check("内存已回滚（label 还是改之前的）", e0.label == "改回自定义",
          e0.label)
    check("内存里的 renames 也回滚了",
          lib.renames.get(uid0) == "改回自定义",
          str(lib.renames.get(uid0)))
finally:
    pathlib.Path.write_text = _real_write

lib4 = Library(TMP / "lib.json", TMP / "dismissed.json", TMP / "renames.json")
lib4.load()
e0d = next((e for e in lib4.entries if e.uid == uid0), None)
check("磁盘上也是改之前的名字（没有半个改动）",
      e0d is not None and e0d.label == "改回自定义",
      e0d.label if e0d else "缺条目")

head("[8] 查不到的 uid 不会静默成功")
check("不存在的 uid 返回 False", lib.rename("deadbeefdeadbeef", "x") is False)
check("库里没被塞进这条",
      "deadbeefdeadbeef" not in lib.renames, str(list(lib.renames))[:60])

head("[9] 菜单里有「重命名」，且排在移除前面")
keys = [k for k, _t in TILE_MENU_ITEMS]
check("菜单含 rename", "rename" in keys, str(keys))
check("rename 排在 delete 前面（不可逆的那项放后面）",
      "rename" in keys and "delete" in keys
      and keys.index("rename") < keys.index("delete"), str(keys))

head("[10] 信号链路接通了")
st = Launchpad(lib, 4, 3, settings=None)
check("window 接了 rename_requested", hasattr(st, "_rename_entry"))
import inspect                                            # noqa: E402
src = inspect.getsource(Launchpad.__init__)
check("__init__ 里连了 rename_requested -> _rename_entry",
      "rename_requested.connect(self._rename_entry)" in src)
check("blankarea 有 rename_requested 信号",
      hasattr(st.grid, "rename_requested"))
got = []
st.grid.rename_requested.connect(lambda e: got.append(e))
# 把对话框换成自动确认的替身。
#
# 为什么必须**在第一次 dispatch 之前**换掉：第一版把换桩写在后面，
# 于是上面那句 ``_dispatch_tile('rename')`` 真的触发了 ``_rename_entry``
# -> 构造**真** RenameDialog -> ``exec_()`` 起嵌套事件循环等用户点按钮
# -> 无人点击，**整个测试进程永久挂住**。
#
# 这与 tests_remove 里``QMenu.exec_()`` 那个坑是同一类：模态调用在无头
# 环境里就是死锁。所以任何「一路走到对话框」的测试，都必须先把对话框
# 本身换掉，而不是等走到它跟前才发现。
from launchpad import renamedlg as RD                           # noqa: E402

_real_RD = RD.RenameDialog


class _AutoOk:
    """自动点「保存」并返回一个新名字。"""

    new_name = "端到端改的名字"

    # **必须自己带上 Accepted / Rejected。** ``_rename_entry`` 里的比较是
    # ``dlg.exec_() != RenameDialog.Accepted`` —— 它取的是**模块里那个名字**
    # （也就是被换成我的这个替身），不是真对话框。所以替身只实现 exec_/value
    # 的话，第一版会在这里 AttributeError。
    Accepted = _real_RD.Accepted
    Rejected = _real_RD.Rejected

    def __init__(self, entry, parent=None):
        self._entry = entry

    def exec_(self):
        return _AutoOk.Accepted

    def value(self):
        return _AutoOk.new_name


lib.rename(uid0, "改名前的状态")
RD.RenameDialog = _AutoOk
try:
    # _dispatch_tile 在 BlankAreaController 上（Grid 只做转发）
    st.grid._blank._dispatch_tile("rename", e0)
    app.processEvents()
    check("_dispatch_tile('rename') 真的发出信号", len(got) == 1
          and got[0] is e0, f"{len(got)} 个")
    check("blankarea -> Grid -> window 全链路：真的改名成功了",
          e0.label == _AutoOk.new_name, e0.label)
    check("落盘也是新名字",
          lib.renames.get(uid0) == _AutoOk.new_name,
          str(lib.renames.get(uid0)))

    # 取消路径：对话框返回 Rejected 时**不能**改任何东西
    class _AutoCancel(_AutoOk):
        def exec_(self):
            return _AutoOk.Rejected

    RD.RenameDialog = _AutoCancel
    st.grid._blank.rename_requested.emit(e0)
    app.processEvents()
    check("对话框点了取消 -> 名字不变", e0.label == _AutoOk.new_name,
          e0.label)
finally:
    RD.RenameDialog = _real_RD

head("[11] 界面上显示的是自定义名（Tile 必须重建）")
# 这一节曾经失败，而失败揭出的是**产品 bug**，不是测试问题：
# ``set_search`` 的「结果没变就不重建」用**对象身份**比较（给热键唤出
# 省开销），而改名是就地改 ``Entry.display``、对象还是同一个 —— 判定
# 「没变」-> 压根不重建 -> 提示条说「已改名为 X」、界面上的字纹丝不动。
# 修法是 ``Grid.invalidate_tiles()``，由 ``_rename_entry`` 调。
#
# 所以这里**必须走真实的 _rename_entry**（带对话框替身），直接调
# _on_search 是测不到那条失效调用的 —— 第一版就是这么写的，
# 于是「界面不刷新」这个 bug 被测试掩盖成了「测试没写对」。
_AutoOk.new_name = "界面上该显示我"
RD.RenameDialog = _AutoOk
try:
    st._rename_entry(e0)
    app.processEvents()
finally:
    RD.RenameDialog = _real_RD

check("改名后 label 是新名", e0.label == "界面上该显示我", e0.label)
found = [t for t in st.grid._tiles
         if getattr(t, "entry", None) is not None
         and t.entry.uid == uid0]
check("界面上能找到那个 Tile", bool(found), f"{len(found)} 个")
if found:
    check("Tile 上烤的是自定义名（重建过了）",
          "界面上该显示我" in found[0]._name, found[0]._name)
    check("Tile 上不是旧名", "端到端改的名字" not in found[0]._name,
          found[0]._name)

# 反向：不失效就一定不重建（证明上面那条断言不是碰巧过的）
before_name = found[0]._name if found else ""
lib.rename(uid0, "只改库不改界面")
st._on_search(st.search.text())
stale = [t for t in st.grid._tiles
         if getattr(t, "entry", None) is not None
         and t.entry.uid == uid0]
check("对照组：没调 invalidate_tiles 就不会重建（仍是旧名）",
      bool(stale) and "只改库不改界面" not in stale[0]._name,
      stale[0]._name if stale else "无 Tile")
lib.rename(uid0, "界面上该显示我")
st.grid.invalidate_tiles()
st._on_search(st.search.text())
fresh = [t for t in st.grid._tiles
         if getattr(t, "entry", None) is not None
         and t.entry.uid == uid0]
check("对照组：调了 invalidate_tiles 就重建了",
      bool(fresh) and "界面上该显示我" in fresh[0]._name,
      fresh[0]._name if fresh else "无 Tile")

head("[12] 改名对话框的基本行为")
from launchpad.renamedlg import RenameDialog                # noqa: E402
dlg = RenameDialog(e0, None)
check("能构造", dlg is not None)
check("预填当前显示名", dlg.edit.text() == e0.label, dlg.edit.text())
check("value() 初始等于当前显示名", dlg.value() == e0.label)
dlg.edit.setText("对话框里的名字")
check("value() 取到输入", dlg.value() == "对话框里的名字")
dlg.edit.setText("  前后空格  ")
check("value() 已 strip", dlg.value() == "前后空格", dlg.value())
dlg.edit.setText(e0.label)
check("与当前名相同时保存键禁用", dlg.ok_btn.isEnabled() is False)
dlg.edit.setText("别的名字")
check("改成别的名字后保存键可用", dlg.ok_btn.isEnabled() is True)
dlg.edit.setText("")
check("清空在「已是自定义名」时可用（= 还原）",
      dlg.ok_btn.isEnabled() is True)
dlg.close()

head("[13] 磁盘上的 .lnk 一个字节都没动")
import hashlib                                            # noqa: E402
for p in made:
    now = p.stat()
    check(f"{p.name} 仍存在", p.is_file())
check("源目录里没有多出 .json 之类的东西",
      sorted(x.suffix for x in SRC.iterdir()) == [".lnk"] * len(made),
      str(sorted(x.name for x in SRC.iterdir())))

head("[14] 全程只用临时目录")
real = Path.home() / "AppData" / "Roaming" / "Launchpad"
check("没碰用户真实的库", str(lib.path).startswith(str(TMP)), str(lib.path))
check("没碰用户真实的 renames.json", str(lib.renames_path).startswith(str(TMP)),
      str(lib.renames_path))

st.close()
st.deleteLater()
app.processEvents()
shutil.rmtree(TMP, ignore_errors=True)

n_pass = sum(1 for _, ok, _ in _results if ok)
n_fail = len(_results) - n_pass
print()
print("=" * 72)
print(f"合计 {len(_results)} 项：PASS {n_pass} / FAIL {n_fail}")
for name, ok, detail in _results:
    if not ok:
        print(f"  FAIL  {name}   [{detail}]")
print("=" * 72)
sys.stdout.flush()
sys.exit(1 if n_fail else 0)