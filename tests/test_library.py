# -*- coding: utf-8 -*-
"""验证 library 层：导入真实目录、搜索、去重、原子写入、损坏保护。"""

import sys
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from launchpad.library import (Entry, Library, LibraryError,
                               load_pinyin, db_file)

SOURCE = Path(os.environ.get("LAUNCHPAD_SOURCE", Path.home() / "Desktop"))

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f"  ({detail})" if detail else ""))


print("=" * 72)
print("[1] 导入真实目录")
print("=" * 72)

tmp = Path(tempfile.mkdtemp()) / "shortcuts.json"
lib = Library(tmp)
report = lib.import_folder(SOURCE)

print(f"  源目录: {SOURCE}")
print(f"  报告  : {report['added']} 新增 / {report['skipped']} 重复 / "
      f"{report['failed']} 失败")
for e in report["errors"][:5]:
    print(f"      {e}")

check("导入数量 > 0", report["added"] > 0, f"{report['added']} 条")
check("导入数量接近源文件数",
      report["added"] >= len(list(SOURCE.glob("*.lnk"))) - 10,
      f"{report['added']} vs {len(list(SOURCE.glob('*.lnk')))} 个 lnk")

print()
print("=" * 72)
print("[2] 搜索")
print("=" * 72)

py, style = load_pinyin()
check("pypinyin 可用", py is not None)

# 抽查一条真实条目
if lib.entries:
    sample = lib.entries[0]
    print(f"  抽查: {sample.name!r} -> {sample.target}")
    check("条目有 target", bool(sample.target))
    check("条目有 source_lnk", bool(sample.source_lnk))

r_text = lib.search("code", py)
check("英文搜索 'code' 有结果", len(r_text) > 0, f"{len(r_text)} 条")

r_cn = lib.search("微信", py)
check("中文搜索 '微信' 有结果", len(r_cn) > 0, f"{len(r_cn)} 条")

# 拼音：找一个含中文的条目，试首字母
cn_entries = [e for e in lib.entries if any("\u4e00" <= c <= "\u9fff" for c in e.name)]
if cn_entries and py:
    probe = cn_entries[0]
    initials = "".join(py.lazy_pinyin(probe.name, style=style.FIRST_LETTER)).lower()
    r_py = lib.search(initials[:2], py)
    check(f"拼音首字母 '{initials[:2]}' -> {probe.name}",
          any(e.name == probe.name for e in r_py),
          f"{len(r_py)} 条命中")

check("空查询返回全部", len(lib.search("", py)) == len(lib.entries))
check("无结果查询返回空", len(lib.search("zzzznotexist", py)) == 0)

print()
print("=" * 72)
print("[3] 去重")
print("=" * 72)

before = len(lib.entries)
lib.import_folder(SOURCE)
check("重复导入不增加条目", len(lib.entries) == before,
      f"{before} -> {len(lib.entries)}")

print()
print("=" * 72)
print("[4] 持久化")
print("=" * 72)

lib2 = Library(tmp)
lib2.load()
check("重新加载条数一致", len(lib2.entries) == len(lib.entries),
      f"{len(lib2.entries)} vs {len(lib.entries)}")
check("字段完整往返",
      lib2.entries[0].name == lib.entries[0].name and
      lib2.entries[0].target == lib.entries[0].target)
check("无残留 .tmp 文件",
      not list(tmp.parent.glob("*.tmp")), f"{list(tmp.parent.glob('*.tmp'))}")

# 回归：读不出 target 的失效条目必须能完整往返。
# 曾经的 bug 是 from_dict 判 target 非空，于是这些条目保存成功、
# 重新加载时被静默跳过 —— 用户重启一次就发现图标少了。
lost = [e for e in lib2.entries if e.missing]
check("失效条目被保留并标记", len(lost) > 0, f"{len(lost)} 条")
check("失效条目 name 全部非空",
      all(e.name.strip() for e in lib2.entries), "有条目 name 为空")
check("missing 标记往返一致",
      [e.missing for e in lib2.entries] == [e.missing for e in lib.entries],
      "载入后标记与保存前不一致")

print()
print("=" * 72)
print("[5] 损坏保护")
print("=" * 72)

bad = tmp.parent / "bad.json"
bad.write_text("{ this is not json", encoding="utf-8")
badlib = Library(bad)
try:
    badlib.load()
    check("损坏时抛 LibraryError", False, "竟然没抛异常")
except LibraryError as exc:
    check("损坏时抛 LibraryError", True)
    check("损坏时保留原文件", bad.exists())
    # 备份名固定为 shortcuts.corrupt-<时间戳>.json（不跟随原文件名）
    baks = list(tmp.parent.glob("shortcuts.corrupt-*.json"))
    check("损坏时生成备份", len(baks) == 1, f"{[b.name for b in baks]}")

# 顶层不是数组
bad2 = tmp.parent / "bad2.json"
bad2.write_text('{"not": "a list"}', encoding="utf-8")
try:
    Library(bad2).load()
    check("非数组顶层抛错", False)
except LibraryError:
    check("非数组顶层抛错", True)

print()
print("=" * 72)
print("[6] 真实数据库位置")
print("=" * 72)
print(f"  {db_file()}")
print(f"  存在: {db_file().exists()}")

shutil.rmtree(tmp.parent, ignore_errors=True)

print()
print("=" * 72)
n_pass = sum(1 for _, ok in results if ok)
n_fail = sum(1 for _, ok in results if not ok)
print(f"结果: PASS {n_pass} / FAIL {n_fail}")
for name, ok in results:
    if not ok:
        print(f"  FAIL {name}")
print("=" * 72)
sys.exit(1 if n_fail else 0)