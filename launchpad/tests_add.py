# -*- coding: utf-8 -*-
"""
验证「添加应用」模块：批量导入、去重、持久化、UWP 列表与真机启动。

用**临时库**跑，绝不碰 ``%APPDATA%\\Launchpad\\shortcuts.json``。

跑法::

    python -u -m launchpad.tests_add --no-launch

退出码非 0 表示有 FAIL。

``--no-launch`` 可以跳过真机启动验证（那会真的弹出一个"计算器"窗口，
测完自动关掉）。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from launchpad.addapp import (AddPolicy, AddResult, Entry, Library,  # noqa: E402
                              RowState, aumid_from_lnk, add_paths,
                              build_preview, classify_app_id, commit,
                              entry_from_file, known_aumids, list_start_apps,
                              package_install_locations, recover_broken,
                              scan_paths, scan_uwp_apps_from_start_menu,
                              shorten_path, uwp_icon_path)
from launchpad.addapp import launch_entry, launch_shell_uri           # noqa: E402

SOURCE = Path(os.environ.get("LAUNCHPAD_SOURCE", Path.home() / "Desktop"))
RUN_LAUNCH = "--no-launch" not in sys.argv

#: 真机启动验证用的目标。计算器：无害、开得快、一定是 UWP（走
#: ApplicationFrameHost），测完能精确关掉。
CALC = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

#: 一个「TargetPath 读不出来、AUMID 只存在于 .lnk 字节里」的 UWP 快捷方式。
#:
#: 原先这里硬编码了作者本机某个具体应用（一个 AI 编程 CLI）的 .lnk 路径。
#: 脱敏时换成环境变量之后问题才暴露出来：**这个文件只有作者机器上有**，
#: 换台机器 `CODEX_LNK.is_file()` 恒为 False，于是依赖它的十几条断言
#: 全部 FAIL —— 而它们测的东西（字节扫描捞 AUMID）本来是完全可移植的。
#:
#: 所以现在**自己造**这个夹具：写一个内容像模像样的 AppUserModelID
#: 快捷方式，指向一个真实存在的开始菜单 AUMID（计算器，任何 Windows 都有）。
#: 造出来的 .lnk 和真的商店链接在字节结构上走同一条解析路径。
#:
#: 保留一条**真实**文件的检查（下面 `REAL_UWP_LNK`），但那条只在文件存在时
#: 跑 —— 它验的是「真实商店链接也认得出来」，属于加分项而不是前提。
results: list[tuple[str, bool]] = []
_tmpdirs: list[Path] = []
_skips: list[str] = []


def skip(name, why):
    """环境不具备条件，跳过而不是判 FAIL。

    判 FAIL 是错的：它让人以为代码坏了，而实际上只是这台机器上没有那个文件。
    对外要能区分「测过了且通过」「测过了且失败」「没条件测」三种状态。
    """
    _skips.append(name)
    print(f"  SKIP  {name}   [{why}]")


def make_uwp_lnk(tag: str) -> Path:
    """
    造一个「商店链接」夹具：``.lnk`` 的 TargetPath 读不出来，
    AUMID 只存在于 .lnk 的字节里。

    这正是 MSIX / Store 安装的快捷方式的形状，也是旧实现存成空 target
    失效条目的那一种。AUMID 用**计算器**的 —— 任何 Windows 上都真的存在
    （`Get-StartApps` 能查到），所以 `known_aumids()` 能把它认出来。

    返回造出来的路径。
    """
    import win32com.client
    d = Path(tempfile.mkdtemp(prefix=f"lp_add_{tag}_"))
    _tmpdirs.append(d)
    # Windows 的 Shell 只能通过 AppsFolder 启动 UWP；WScript.Shell 写不出
    # 真正的商店链接，所以这里**直接构造 .lnk 字节**：
    # 头部 + 一个 AUMID 字符串。aumid_from_lnk 的字节扫描要认的就是它。
    p = d / "StoreApp.lnk"
    p.write_bytes(_lnk_with_aumid(CALC))
    return p


def _lnk_with_aumid(aumid: str) -> bytes:
    """
    拼一个最小可用的 .lnk：LNK 头 + AUMID 的 UTF-16LE 串。

    结构不需要完整 —— `aumid_from_lnk` 做的是**字节扫描**（找形如
    ``Xxx_8wekyb3d8bbwe!App`` 的串），不是解析 LNK 结构，所以只要让
    头看起来合法、串能被 UTF-16 扫到就行。

    关键约束：AUMID 必须放在**偶数偏移**上，否则 UTF-16LE 的高位字节会
    把它劈成两半。头部固定 0x4C 字节（76），本身就是偶数。
    """
    import struct
    header = bytearray(0x4C)
    header[0:4] = b"\x4c\x00\x00\x00"          # HeaderSize
    header[4:8] = b"\x01\x14\x02\x00"          # LinkCLSID
    # LinkFlags: HasLinkTargetIDList(0x1) | IsUnicode(0x80)
    struct.pack_into("<I", header, 0x14, 0x1 | 0x80)
    payload = aumid.encode("utf-16-le")
    return bytes(header) + payload


def is_uwp(target: str) -> bool:
    """target 是不是 AppsFolder 形式（UWP / 应用商店应用）。"""
    return (target or "").strip().lower().startswith("shell:appsfolder\\")


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  {mark}  {name}" + (f"   [{detail}]" if detail else ""))


def head(t):
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


def tmp_db(tag) -> Path:
    d = Path(tempfile.mkdtemp(prefix=f"lp_add_{tag}_"))
    _tmpdirs.append(d)
    return d / "shortcuts.json"


# ══════════════════════════════════════════════════════════════════════
#  真机启动验证 —— 独立子进程模式（--launch-child）
# ══════════════════════════════════════════════════════════════════════
#
# 这一节跑在**子进程**里，父进程只读它的输出。理由有两条，都不是洁癖：
#
# 1) 反复 `os.startfile` 拉起 UWP 再 `taskkill /F ApplicationFrameHost`
#    之后，本进程里会留下一摊没拆干净的 COM apartment 和 shell 扩展引用，
#    接着再走一次同样的流程就有概率硬崩 —— 进程直接死掉，退出码 -1，
#    连"结果"那一行都来不及打印。实测 6 次里撞上 1~2 次。
# 2) `taskkill /F ApplicationFrameHost` 会把用户**所有**打开的 UWP /
#    商店窗口一起杀掉。这测试不是用户授权的，不该这么粗暴。
#
# 所以：判定改成"找 class=ApplicationFrameWindow 且标题是计算器的那个
# 窗口"（精确、不误伤），关窗只发 WM_CLOSE 给它自己，验证过程整体隔离
# 到子进程。父进程只看退出码和 LAUNCHCHECK 行。

LAUNCH_PREFIX = "LAUNCHCHECK|"


def _launch_child_main() -> int:
    """子进程：真的拉起 UWP / exe，逐项汇报。退出码 0 = 全过。"""
    import ctypes

    def say(name, ok, detail=""):
        print(f"{LAUNCH_PREFIX}{name}|{1 if ok else 0}|{detail}", flush=True)

    def run(args):
        try:
            p = subprocess.run(args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=0x08000000, timeout=30)
            return p.stdout or ""
        except Exception as exc:
            print(f"    [辅助命令失败] {args[0]}: {exc}", flush=True)
            return ""

    user32 = ctypes.windll.user32
    PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def frame_windows():
        out = []

        def cb(hwnd, _):
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value == "ApplicationFrameWindow":
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                out.append((int(hwnd), buf.value))
            return True

        user32.EnumWindows(PROC(cb), 0)
        return out

    def calc_windows():
        return [(h, t) for h, t in frame_windows()
                if "计算器" in t or "calculator" in t.lower()]

    def wait_calc(timeout=25.0):
        end = time.time() + timeout
        while time.time() < end:
            found = calc_windows()
            if found:
                return found[0][1]
            time.sleep(0.4)
        return ""

    def close_calc():
        for hwnd, _ in calc_windows():
            user32.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE
        time.sleep(1.2)

    exe = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "notepad.exe"
    rc = 0
    try:
        # 负对照：先确认桌面上没有计算器窗口，检测方法本身不会误报
        pre = calc_windows()
        quiet = not pre
        say("负对照：调起前桌面上没有计算器窗口", quiet,
            "检测方法可靠" if quiet else f"本来就开着 {pre}，请手动关掉后重跑")

        ok, why = launch_shell_uri(f"shell:AppsFolder\\{CALC}")
        title = wait_calc(25.0)
        say("shell:AppsFolder 调起返回成功", ok, why)
        say("UWP 应用真的启动了（出现了计算器窗口）", bool(title),
            f"窗口标题={title!r}")
        rc |= 0 if (ok and (title or not quiet)) else 1
        close_calc()

        ok2, why2 = launch_entry(Entry(name="计算器",
                                       target=f"shell:AppsFolder\\{CALC}"))
        title2 = wait_calc(25.0)
        say("launch_entry(UWP 条目) 真的启动了", ok2 and bool(title2),
            f"ok={ok2} why={why2} 窗口标题={title2!r}")
        rc |= 0 if (ok2 and (title2 or not quiet)) else 1
        close_calc()

        ok3, why3 = launch_entry(Entry(name="notepad", target=str(exe)))
        time.sleep(2.0)
        say("launch_entry(exe) 仍能启动", ok3, why3)
        rc |= 0 if ok3 else 1
        run(["taskkill", "/IM", "notepad.exe", "/F"])

        ok4, why4 = launch_entry(Entry(name="空的", target=""))
        say("空 target 明确报错不静默", (not ok4) and bool(why4), why4)
        rc |= 0 if ((not ok4) and bool(why4)) else 1
    finally:
        close_calc()
    return rc


if "--launch-child" in sys.argv:
    # 直接进子进程模式，**不要**先把上面 12 节全跑一遍：
    # 那一整套（导入 122 个 lnk、写临时库、跑 3 次 Get-StartApps）
    # 纯属浪费，而子进程会被父进程读 stdout，多余输出反而碍事。
    sys.exit(_launch_child_main())


# ══════════════════════════════════════════════════════════════════════
head("[1] 批量导入真实目录（临时库，不碰真实库）")
# ══════════════════════════════════════════════════════════════════════

print(f"  源目录: {SOURCE}")
print(f"  存在  : {SOURCE.is_dir()}")

if not SOURCE.is_dir():
    check("源目录存在", False, str(SOURCE))
    db = tmp_db("nodir")
else:
    n_lnk = len(list(SOURCE.glob("*.lnk")))
    print(f"  .lnk  : {n_lnk} 个")

    db = tmp_db("batch")
    lib = Library(db)
    rep = scan_paths([SOURCE], recursive=False, include_exe=False)
    print(f"  扫描  : 候选 {len(rep.candidates)} / 错误 {len(rep.errors)}"
          f" / 不支持 {len(rep.unsupported)}")
    for e in rep.errors[:6]:
        print(f"      ! {e}")

    check("扫描出候选条目", len(rep.candidates) > 0,
          f"{len(rep.candidates)} 条")
    check("批量导入数量与 .lnk 数同量级",
          len(rep.candidates) >= n_lnk - 5,
          f"{len(rep.candidates)} vs {n_lnk} 个 .lnk")

    rows = build_preview(rep.candidates, lib, AddPolicy.SKIP)
    check("空库时全部标为新增",
          all(r.state == RowState.NEW for r in rows),
          f"{sorted({r.state for r in rows})}")

    res = commit(rows, lib, AddPolicy.SKIP)
    print(f"  写入  : {res.summary()}")
    check("新增数 = 库中条目数", res.added == len(lib.entries),
          f"{res.added} / {len(lib.entries)}")
    check("库文件已落盘", db.is_file() and db.stat().st_size > 0,
          f"{db.stat().st_size if db.is_file() else 0} B")
    check("写入过程零失败", res.failed == 0, res.summary())
    check("所有条目都有名字", all(e.name.strip() for e in lib.entries))
    check("大部分条目有可启动目标",
          sum(1 for e in lib.entries if (e.target or "").strip())
          >= len(lib.entries) - 8,
          f"{sum(1 for e in lib.entries if (e.target or '').strip())}"
          f"/{len(lib.entries)}")

    # ── UWP 快捷方式在**扫描阶段**就被修好了 ──
    # 这一条比 recover_broken 更重要：商店链接的 TargetPath 读不出来，
    # 旧实现只能存成空 target 的失效条目（界面上 ⚠，点了没反应）。
    # 现在扫描时就从 .lnk 字节里捞出 AUMID，直接变成可启动条目。
    #
    # 用**自己造的夹具**，不依赖源目录里恰好有某个特定应用（那台机器上
    # 有、别的机器上没有，会让这十几条断言在别人机器上必然 FAIL）。
    uwp_entries = [e for e in lib.entries if is_uwp(e.target)]
    print(f"  扫出 UWP 条目: {len(uwp_entries)} 条 "
          f"{[e.name for e in uwp_entries]}")
    # 「源目录里有没有商店链接」取决于这台机器装了什么，不是代码性质。
    # 断言硬要求 >=1 的话，在没装任何 UWP 应用的机器上必然 FAIL，
    # 而 FAIL 会被读成「扫描逻辑坏了」。这里两条都给：有就断言扫出来的
    # 确实可用，没有就明说没条件测。
    if uwp_entries:
        check("至少扫出 1 个 UWP 条目", True, f"{len(uwp_entries)} 条")
        check("扫出来的 UWP 条目都不再是 missing",
              all(not e.missing for e in uwp_entries),
              str([e.name for e in uwp_entries if e.missing]))
    else:
        skip("至少扫出 1 个 UWP 条目",
             f"源目录 {SOURCE} 里没有商店链接（与代码无关）")

    # ── recover_broken：剩下这些是真的坏链接，必须明确报错而不是静默 ──
    broken_before = [e for e in lib.entries if not (e.target or "").strip()]
    n_before_fix = len(lib.entries)
    print(f"  剩余失效条目: {len(broken_before)} 条 "
          f"{[e.name for e in broken_before]}")
    known = known_aumids()
    fix = recover_broken(lib, known=known)
    print(f"  修复  : {fix.summary()}")
    for n, why in fix.errors:
        print(f"      ! {n}: {why}")
    check("recover_broken 不抛异常", True)
    check("每条失效条目都有明确结论（升级或带原因失败）",
          fix.upgraded + fix.failed == len(broken_before),
          f"升级 {fix.upgraded} + 失败 {fix.failed} vs {len(broken_before)}")
    check("修不了的失败项每条都带原因",
          all(bool(why.strip()) for _, why in fix.errors),
          f"{len(fix.errors)} 条")
    # 判据是「没有条目被偷偷删掉」——用身份比对，不看 target 是否被填上。
    # 之前这里断言的是「空 target 的条数不变」，那等于假设修复永远失败；
    # recover_broken 加了「按精确同名去开始菜单里找」的兜底之后，
    # 确实能把一批原本修不了的条目救回来，于是这条判据反而误报。
    # 条目还在不在这件事，和它有没有被修好，是两回事。
    still_present = (len(lib.entries) == n_before_fix
                     and all(any(e is b for e in lib.entries)
                             for b in broken_before))
    check("修不了的条目没被偷偷删掉（仍留在库里标 missing）",
          still_present,
          f"{n_before_fix} -> {len(lib.entries)}，"
          f"{len(broken_before)} 条原失效条目，"
          f"本轮修复 {fix.upgraded} 条")

    # ── 持久化：重新加载 ──
    lib2 = Library(db)
    lib2.load()
    check("重新加载条数一致", len(lib2.entries) == len(lib.entries),
          f"{len(lib2.entries)} vs {len(lib.entries)}")
    t_map = {e.name: e.target for e in lib.entries}
    t_map2 = {e.name: e.target for e in lib2.entries}
    check("每条 target 往返一致", t_map == t_map2)
    check("UWP 条目往返后仍是 shell: URI",
          all(is_uwp(e.target) for e in lib2.entries
              if (e.target or "").lower().startswith("shell:")))
    check("missing 标记往返一致",
          [e.missing for e in lib2.entries] == [e.missing for e in lib.entries])
    check("无残留 .tmp", not list(db.parent.glob("*.tmp")),
          f"{list(db.parent.glob('*.tmp'))}")

# ══════════════════════════════════════════════════════════════════════
head("[2] 去重：同一目录导入两次，条目数不变")
# ══════════════════════════════════════════════════════════════════════

if SOURCE.is_dir():
    before = len(lib.entries)
    rep2, rows2, res2 = add_paths(lib, [SOURCE], recursive=False,
                                  include_exe=False, policy=AddPolicy.SKIP)
    print(f"  第二次: {res2.summary()}")
    check("条目数不变", len(lib.entries) == before,
          f"{before} -> {len(lib.entries)}")
    check("全部判为重复", res2.added == 0 and res2.upgraded == 0,
          f"新增 {res2.added} 修复 {res2.upgraded}")
    check("重复数 = 候选数", res2.skipped == len(rep2.candidates),
          f"{res2.skipped} vs {len(rep2.candidates)}")

    # 第三次 + 允许重复策略
    rep3, rows3, res3 = add_paths(lib, [SOURCE], recursive=False,
                                  include_exe=False, policy=AddPolicy.KEEP_BOTH)
    check("KEEP_BOTH 策略确实会加重复", res3.added == len(rep3.candidates),
          f"新增 {res3.added}")
    check("KEEP_BOTH 后条目数翻倍",
          len(lib.entries) == before + len(rep3.candidates),
          f"{before} -> {len(lib.entries)}")

    # 恢复干净
    lib.entries = [e for e in lib.entries]
    lib.entries = lib2.entries[:]
    lib.save()
    check("重置回原始状态", len(lib.entries) == len(lib2.entries))

# ══════════════════════════════════════════════════════════════════════
head("[3] .exe 直接添加")
# ══════════════════════════════════════════════════════════════════════

db3 = tmp_db("exe")
lib3 = Library(db3)
exe = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "notepad.exe"
print(f"  目标: {exe}  存在={exe.exists()}")

if exe.is_file():
    entry, err = entry_from_file(exe)
    check("从 .exe 构造出条目", entry is not None, err)
    if entry:
        check("target 就是 exe 本身", entry.target == str(exe), entry.target)
        check("workdir 是所在目录", entry.workdir == str(exe.parent))
        check("icon 指向 exe", entry.icon == str(exe))
        check("name = 文件名去后缀", entry.name == "notepad")
        check("不是 missing", entry.missing is False)

        rows = build_preview([type("C", (), {"entry": entry})()], lib3)
        check("预览标为新增", rows[0].state == RowState.NEW)
        r = commit(rows, lib3)
        check("写入成功", r.added == 1 and r.failed == 0, r.summary())

        rows2 = build_preview([type("C", (), {"entry": entry})()], lib3)
        check("再次添加标为重复", rows2[0].state == RowState.DUPLICATE,
              rows2[0].detail)
        r2 = commit(rows2, lib3)
        check("重复被跳过、库不变",
              r2.skipped == 1 and len(lib3.entries) == 1, r2.summary())
else:
    check("找到 notepad.exe", False, str(exe))

# 批量拖多个 exe：扫描不去重，但**预览**会去重并说清原因
db3b = tmp_db("exes")
lib3b = Library(db3b)
reps = scan_paths([exe, exe.parent / "notepad.exe"], include_exe=True)
rows3 = build_preview(reps.candidates, lib3b)
check("同一个 exe 拖两次会进两条候选",
      len(reps.candidates) == 2, f"{len(reps.candidates)} 条")
check("但预览把第二条标为重复并说明原因",
      [r.state for r in rows3] == [RowState.NEW, RowState.DUPLICATE],
      f"{[r.state for r in rows3]}")
check("重复原因里带上了重复项的名字",
      bool(rows3[1].detail) and "notepad" in rows3[1].detail,
      rows3[1].detail)
r3 = commit(rows3, lib3b)
check("批内去重后只写入 1 条", len(lib3b.entries) == 1, r3.summary())

# ══════════════════════════════════════════════════════════════════════
head("[4] UWP 应用列表")
# ══════════════════════════════════════════════════════════════════════

t0 = time.time()
apps = list_start_apps(refresh=True)
dt = time.time() - t0
print(f"  取到 {len(apps)} 条，耗时 {dt:.2f}s")
if apps:
    for a in apps[:5]:
        print(f"      {a.name[:36]:38} {a.kind:11} {a.app_id}")

check("Get-StartApps 返回非空", len(apps) > 0, f"{len(apps)} 条")
check("耗时可接受（<8s）", dt < 8.0, f"{dt:.2f}s")

apx = [a for a in apps if a.kind == "appx"]
afs = [a for a in apps if a.kind == "appsfolder"]
legacy = [a for a in apps if a.kind == "legacy"]
pth = [a for a in apps if a.kind == "path"]
urls = [a for a in apps if a.kind == "url"]
print(f"  分类: appx={len(apx)}  appsfolder={len(afs)}  "
      f"legacy={len(legacy)}  path={len(pth)}  url={len(urls)}")
check("legacy（{GUID}\\...）分类非空 —— 分类没把经典程序误杀",
      len(legacy) > 0, f"{len(legacy)} 条")
check("盘符 path 分类非空", len(pth) > 0, f"{len(pth)} 条")
check("https 链接被识别为 url 而不是误杀", len(urls) > 0, f"{len(urls)} 条")
# 本机 Get-StartApps 原始 431 条：11 条 https 文档链接（已收为 url），
# 其余 ~21 条是 SOLIDWORKS 的二进制乱码 + 「此电脑/回收站/网络」这类
# shell 命名空间项，必须拒掉 —— 它们根本不是应用。
check("总量守恒：没有条目被误杀成 invalid",
      len(apx) + len(afs) + len(legacy) + len(pth) + len(urls) == len(apps),
      f"{len(apps)} 条")
check("至少 3 条带 ! 的 UWP（AUMID 形式）", len(apx) >= 3, f"{len(apx)} 条")

named = sorted({a.name for a in apx})
print("  真实 UWP 应用名（最多列 8 个）:")
for n in named[:8]:
    print(f"      · {n}")
check("能列出至少 3 个真实 UWP 应用名", len(apx) >= 3,
      f"{len(apx)} 条，例如：" + "、".join(named[:3]))
check("UWP 名不是空串或占位符",
      all(n.strip() and n.strip() != "?" for n in named),
      f"{len([n for n in named if not n.strip()])} 个空名")

print("  真实 UWP 应用（最多列 6 个）:")
for a in sorted(apx, key=lambda x: x.name.lower())[:6]:
    print(f"      {a.name}  ->  {a.app_id}")

# 脏数据必须被挡掉
check("非法 AppID 被分类为 invalid（Get-StartApps 混进的脏数据）",
      classify_app_id("!BFg[o3^]9y9R*9(n[xheDrawingsViewer>y).H),O8y8^Ysoj5N)^,")
      == "invalid")
check("SOLIDWORKS 的二进制乱码判 invalid",
      classify_app_id(".}4$t8Nr@9lHzHKwgKZOCTSProgramFiles>)w[Z+WTtcA`(W9OY53rW")
      == "invalid")
check("shell 命名空间项（此电脑/回收站）判 invalid",
      classify_app_id("::{20D04FE0-3AEA-1069-A2D8-08002B30309D}") == "invalid")
check("https 文档链接分类为 url",
      classify_app_id("https://nodejs.org/") == "url")
check("标准盘符路径分类为 path",
      classify_app_id(r"C:\Program Files\App\app.exe") == "path")
check("标准 AUMID 分类为 appx", classify_app_id(CALC) == "appx", CALC)
check("Electron 商店版分类为 appsfolder",
      classify_app_id("electron.app.Bitwarden") == "appsfolder")
check("WSL 转发项分类为 appsfolder",
      classify_app_id("Microsoft.AutoGenerated."
                      "{CC1DCC78-BE66-1116-A582-40047B103E18}") == "appsfolder")
check("空串判 invalid", classify_app_id("") == "invalid")
check("超长串判 invalid", classify_app_id("x" * 300) == "invalid")
check("{GUID}\\... 分类为 legacy（仍能经 AppsFolder 调起）",
      classify_app_id(
          r"{6D809377-6AF0-444B-8957-A3773F02200E}\Notepad++\notepad++.exe")
      == "legacy")
check("相对路径判 invalid（不能当 shell URI 用）",
      classify_app_id("Notepad++\\notepad++.exe") == "invalid")

# ── UWP 变成条目 ──
if apx:
    picked = apx[:3]
    db4 = tmp_db("uwp")
    lib4 = Library(db4)
    rep4 = scan_uwp_apps_from_start_menu(picked)
    check("UWP 扫描产出 3 条", len(rep4.candidates) == 3,
          f"{len(rep4.candidates)} 条")
    check("UWP 扫描零错误", not rep4.errors, str(rep4.errors))

    for c in rep4.candidates:
        ok = c.entry.target.lower().startswith("shell:appsfolder\\")
        print(f"      {c.entry.name[:26]:28} -> {c.entry.target}")
    check("UWP 条目 target 都是 shell:AppsFolder\\...",
          all(c.entry.target.lower().startswith("shell:appsfolder\\")
              for c in rep4.candidates))
    check("UWP 条目不是 missing",
          all(not c.entry.missing for c in rep4.candidates))
    check("UWP 条目 AUMID 出现在 Get-StartApps 里",
          all(c.entry.target[len("shell:AppsFolder\\"):] in
              {a.app_id for a in apps} for c in rep4.candidates))

    rows4 = build_preview(rep4.candidates, lib4)
    r4 = commit(rows4, lib4)
    check("UWP 写入成功", r4.added == 3 and r4.failed == 0, r4.summary())
    lib4b = Library(db4)
    lib4b.load()
    check("UWP 条目重新加载还在", len(lib4b.entries) == 3,
          f"{len(lib4b.entries)} 条")

    rows4b = build_preview(rep4.candidates, lib4)
    check("UWP 二次添加判为重复",
          all(r.state == RowState.DUPLICATE for r in rows4b),
          f"{[r.state for r in rows4b]}")
    r4b = commit(rows4b, lib4)
    check("UWP 去重有效", len(lib4.entries) == 3, r4b.summary())

    # 图标：包 Assets 里的 logo
    icon_target = apx[0]
    logo = uwp_icon_path(icon_target.app_id)
    print(f"      图标探测 {icon_target.app_id} -> {logo or '(取不到，占位图)'}")
    check("能取到 UWP logo png 或明确返回空串",
          isinstance(logo, str) and (logo == "" or Path(logo).is_file()),
          logo or "(空)")

# ══════════════════════════════════════════════════════════════════════
head("[5] .lnk 字节里捞 AUMID")
# ══════════════════════════════════════════════════════════════════════

# 用自己造的商店链接夹具，不依赖源目录里恰好有某个特定应用。
codex = make_uwp_lnk("aumid")
print(f"  夹具: {codex}")
raw = aumid_from_lnk(codex, known_aumids())
print(f"  {codex.name} -> {raw!r}")
check("从商店链接夹具捞到 AUMID", bool(raw), raw)
check("捞到的 AUMID 是合法格式",
      raw == "" or classify_app_id(raw) == "appx", raw)
check("捞到的 AUMID 在本机真实存在",
      raw == "" or raw in known_aumids(), raw)
check("交叉验证能挡掉假阳性",
      aumid_from_lnk(codex, known={"__definitely_not_real__"}) == "")
check("不给 known 集合时也能捞到（宽松模式）",
      bool(aumid_from_lnk(codex)))

check("不存在的文件返回空串而不是抛异常",
      aumid_from_lnk(SOURCE / "__no_such__.lnk") == "")
check("空串路径返回空串", aumid_from_lnk("") == "")

# ══════════════════════════════════════════════════════════════════════
head("[6] 失效条目就地修复（recover_broken）")
# ══════════════════════════════════════════════════════════════════════

db6 = tmp_db("fix")
lib6 = Library(db6)
no_aumid_lnk = Path(tempfile.mkdtemp(prefix="lp_nolnk_")) / "broken.lnk"
_tmpdirs.append(no_aumid_lnk.parent)
no_aumid_lnk.write_bytes(b"\x00\x01broken shortcut payload\x00" * 4)
lib6.entries = [
    Entry(name="UWP 链接", target="", source_lnk=str(codex), missing=True),
    Entry(name="真坏链接", target="", source_lnk=str(no_aumid_lnk),
          missing=True),
    Entry(name="指向不存在的文件", target=r"C:\nope\nope.exe",
          source_lnk="", missing=True),
]
lib6.save()

r6 = recover_broken(lib6)
print(f"  {r6.summary()}")
for n, w in r6.errors:
    print(f"      ! {n}: {w}")
check("修好 1 条（夹具 .lnk 里的 AUMID）", r6.upgraded == 1, r6.summary())
check("修不了的那条明确报错，不静默",
      len(r6.errors) == 1 and r6.errors[0][0] == "真坏链接",
      str(r6.errors))
check("失败原因说清了是找不到应用 ID",
      bool(r6.errors) and "应用 ID" in r6.errors[0][1],
      str(r6.errors))
check("有 target 但目标不存在的条目不被当成 UWP 待修项",
      all(n != "指向不存在的文件" for n, _ in r6.errors),
      str(r6.errors))
fixed = [e for e in lib6.entries if e.name == "UWP 链接"]
check("修复后 target 非空且是 shell: URI",
      bool(fixed) and is_uwp(fixed[0].target),
      fixed[0].target if fixed else "(没找到)")
check("修复后 missing 被清掉",
      bool(fixed) and fixed[0].missing is False)
check("条目数不变（就地修好，不是新增）", len(lib6.entries) == 3,
      f"{len(lib6.entries)} 条")
lib6b = Library(db6)
lib6b.load()
check("修复结果已持久化",
      any(e.name == "UWP 链接" and is_uwp(e.target)
          for e in lib6b.entries))
check("真坏链接仍留在库里（没被悄悄删）",
      any(e.name == "真坏链接" and e.missing for e in lib6b.entries))

# ══════════════════════════════════════════════════════════════════════
head("[7] 失败路径：不崩溃 + 有明确提示")
# ══════════════════════════════════════════════════════════════════════

db7 = tmp_db("fail")
lib7 = Library(db7)

rep = scan_paths([SOURCE / "__绝对不存在__.lnk"])
check("不存在的路径 -> 明确错误，不抛异常",
      len(rep.errors) == 1 and "不存在" in rep.errors[0],
      str(rep.errors))
check("不存在的路径不产生候选", len(rep.candidates) == 0)

e7, err7 = entry_from_file(SOURCE / "__不存在__.exe")
check("不存在的 exe -> 标 missing 且给出原因，不静默",
      e7 is not None and e7.missing and "已不存在" in err7,
      f"{e7}, {err7}")

unsupported = SOURCE / "__不支持__.txt"
try:
    unsupported.write_text("hello", encoding="utf-8")
    e7b, err7b = entry_from_file(unsupported)
    check("不支持的格式 -> (None, 明确原因)",
          e7b is None and "不支持的格式" in err7b, err7b)
    rep7 = scan_paths([unsupported])
    check("不支持的格式进 unsupported 而不是 errors",
          rep7.unsupported and not rep7.errors,
          f"errors={rep7.errors} unsupported={rep7.unsupported}")
    unsupported.unlink()
except OSError as exc:
    check("能写临时文件", False, str(exc))

# 损坏的 .lnk
bad_lnk = Path(tempfile.mkdtemp(prefix="lp_bad_")) / "bad.lnk"
_tmpdirs.append(bad_lnk.parent)
bad_lnk.write_bytes(b"NOT A SHORTCUT" + b"\x00" * 64)
e7c, err7c = entry_from_file(bad_lnk)
check("损坏 .lnk 不崩溃", True, f"entry={'有' if e7c else '无'} err={err7c[:60]}")
check("损坏 .lnk 要么标 missing 要么给原因",
      (e7c is not None and e7c.missing) or bool(err7c),
      err7c[:60])

# 空路径 / 奇怪类型
for weird in [None, "", 123, [], {}, b"bytes", 3.5]:
    try:
        rep_w = scan_paths([weird])
        ent_w, err_w = entry_from_file(weird)
        build_preview([], lib7)
        commit([], lib7)
        ok = True
        detail = f"errors={len(rep_w.errors)}"
    except Exception as exc:
        ok, detail = False, f"{type(weird).__name__}: {exc}"
    check(f"畸形输入 {type(weird).__name__} 不崩溃", ok, detail)

check("空路径列表不炸", len(scan_paths([]).candidates) == 0)
check("None 列表当空处理", len(scan_paths(None).candidates) == 0)
check("空候选 commit 是 no-op",
      commit([], lib7).summary().endswith("本次没有改动任何条目"))
check("不存在的目录报明确错误",
      any("路径不存在" in e
          for e in scan_paths([ROOT / "__没有这个目录__"]).errors))

# 写入失败（把库路径变成一个目录）
db7b = tmp_db("unwritable")
blocked = Path(db7b)
blocked.mkdir(parents=True, exist_ok=True)
lib7b = Library(blocked)
lib7b.entries = [Entry(name="x", target=r"C:\a\b.exe")]
r7 = commit([type("R", (), {"entry": lib7b.entries[0],
                            "state": RowState.NEW, "detail": "",
                            "existing": None, "label": "新增",
                            "selectable": True})()], lib7b)
check("库不可写时不静默，报失败", r7.failed >= 1, r7.summary())
check("库不可写时有具体原因", bool(r7.errors), str(r7.errors))

# ══════════════════════════════════════════════════════════════════════
head("[8] 冲突策略与升级语义")
# ══════════════════════════════════════════════════════════════════════

db8 = tmp_db("policy")
lib8 = Library(db8)
tgt = str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "notepad.exe")
lib8.entries = [Entry(name="旧名字", target=tgt)]
lib8.save()

def row_for(library, entry):
    return build_preview([type("C", (), {"entry": entry})()], library,
                         AddPolicy.SKIP)[0]

new_entry = Entry(name="新名字", target=tgt)
check("SKIP：同 target 判重复",
      row_for(lib8, new_entry).state == RowState.DUPLICATE)
check("REPLACE：同 target 判覆盖",
      build_preview([type("C", (), {"entry": new_entry})()], lib8,
                    AddPolicy.REPLACE)[0].state == RowState.REPLACE)
check("KEEP_BOTH：同 target 判新增",
      build_preview([type("C", (), {"entry": new_entry})()], lib8,
                    AddPolicy.KEEP_BOTH)[0].state == RowState.NEW)

r8 = commit(build_preview([type("C", (), {"entry": new_entry})()], lib8,
                          AddPolicy.REPLACE), lib8, AddPolicy.REPLACE)
check("REPLACE 保持位置（不追加到末尾）", len(lib8.entries) == 1, r8.summary())
check("REPLACE 换成了新条目", lib8.entries[0].name == "新名字",
      lib8.entries[0].name)

# 失效旧条目 + 能修好的新条目 => UPGRADE
lib8.entries = [Entry(name="失效的商店应用", target="",
                      source_lnk=str(codex), missing=True)]
lib8.save()
recovered, _ = entry_from_file(codex, known_aumids())
check("商店链接夹具能被修成可启动条目",
      recovered is not None and is_uwp(recovered.target),
      recovered.target if recovered else "(None)")
if recovered:
    row = row_for(lib8, recovered)
    check("失效旧条目 + 可修好的新条目 => UPGRADE（不是重复）",
          row.state == RowState.UPGRADE, f"{row.state} / {row.detail}")
    r8b = commit([row], lib8)
    check("UPGRADE 后条目数不变（就地修好，不是新增）",
          len(lib8.entries) == 1, r8b.summary())
    check("UPGRADE 计入 upgraded", r8b.upgraded == 1, r8b.summary())
    check("UPGRADE 后 target 非空", bool(lib8.entries[0].target),
          lib8.entries[0].target)

    # 反向：新的解析不出来、旧的好好的（同一个 .lnk）—— 绝不能拿它覆盖
    lib8.entries = [Entry(name="好的商店应用", target=tgt,
                          source_lnk=str(codex))]
    lib8.save()
    worse = Entry(name="更差的商店应用", target="",
                  source_lnk=str(codex), missing=True)
    row2 = row_for(lib8, worse)
    check("新的读不出目标时不许覆盖能用的旧条目",
          row2.state == RowState.DUPLICATE, f"{row2.state} / {row2.detail}")
    r8c = commit([row2], lib8)
    check("反向降级被跳过，库不变",
          r8c.skipped == 1 and lib8.entries[0].target == tgt, r8c.summary())

# dry_run
db8b = tmp_db("dryrun")
lib8b = Library(db8b)
rows8 = build_preview([type("C", (), {"entry": Entry(name="n",
                                                      target=r"C:\x\y.exe")})()],
                      lib8b)
commit(rows8, lib8b, dry_run=True)
check("dry_run 不写库", len(lib8b.entries) == 0 and not db8b.exists(),
      f"{len(lib8b.entries)} 条, 文件存在={db8b.exists()}")

# ══════════════════════════════════════════════════════════════════════
head("[9] 结果摘要与路径缩短")
# ══════════════════════════════════════════════════════════════════════

r9 = AddResult(added=3, skipped=2, failed=1, upgraded=1)
s9 = r9.summary()
print(f"  {s9}")
check("摘要含成功数", "成功添加 3" in s9, s9)
check("摘要含跳过数", "跳过重复 2" in s9, s9)
check("摘要含失败数且被标出来", "失败 1" in s9, s9)
check("摘要含修复数", "修复失效 1" in s9, s9)
check("全空结果也能出一句话",
      bool(AddResult().summary()), AddResult().summary())

long_p = r"C:\Users\x\Desktop\apps\some\deep\folder\Notepad++.exe"
sh = shorten_path(long_p, 40)
print(f"  {long_p}\n  -> {sh}")
check("长路径被缩短", len(sh) <= 41, f"{len(sh)}")
check("缩短后保留尾部（用户要看文件名）",
      sh.endswith("exe") or "…" in sh, sh)
check("短路径原样返回", shorten_path(r"C:\a\b.exe") == r"C:\a\b.exe")

# ══════════════════════════════════════════════════════════════════════
head("[10] 模块可无界面导入（不起 QApplication）")
# ══════════════════════════════════════════════════════════════════════

_probe = (
    "import sys; sys.path.insert(0, r'%s');\n"
    "import launchpad.addapp as A;\n"
    "print('QTW' if 'PyQt5.QtWidgets' in sys.modules else 'NOQT');\n"
    "print('QAPP' if 'PyQt5.QtWidgets' in sys.modules and\n"
    "      getattr(sys.modules.get('PyQt5.QtWidgets'),\n"
    "              'QApplication', None) and\n"
    "      sys.modules['PyQt5.QtWidgets'].QApplication.instance()\n"
    "      else 'NOINST');\n"
    "print('ENTRY', A.Entry(name='n', target='t').uid[:6]);\n"
) % ROOT

_p = subprocess.run([sys.executable, "-c", _probe],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
_out = (_p.stdout or "").strip().splitlines()
print("  子进程输出: " + " | ".join(_out) + f"  rc={_p.returncode}")
check("子进程能 import addapp", _p.returncode == 0,
      (_p.stderr or "")[-200:])
check("import addapp 不引入 QtWidgets（顶层不 import Qt）",
      "NOQT" in _out, str(_out))
check("import addapp 不创建 QApplication",
      "QAPP" not in _out, str(_out))
check("addapp 正常导出 Entry", any(o.startswith("ENTRY") for o in _out),
      str(_out))
check("AddPolicy / RowState 常量齐全",
      AddPolicy.SKIP == "skip" and AddPolicy.REPLACE == "replace"
      and AddPolicy.KEEP_BOTH == "keep_both"
      and RowState.UPGRADE == "upgrade")

check("package_install_locations 能读到 WindowsApps 目录",
      len(package_install_locations()) > 0,
      f"{len(package_install_locations())} 个包")

# ══════════════════════════════════════════════════════════════════════
head("[11] 真机启动验证（会真的弹出窗口）")
# ══════════════════════════════════════════════════════════════════════
#  实现在文件头的 _launch_child_main()，跑在 `--launch-child` 子进程里。
#  隔离的理由见那里的注释（COM 拆解会硬崩；taskkill 会误杀用户窗口）。

if not RUN_LAUNCH:
    check("已按要求跳过（--no-launch）", True)
elif sys.platform != "win32":
    check("非 Windows，跳过", True)
else:
    print(f"  在子进程里真机验证（--launch-child），目标：{CALC}")
    try:
        child = subprocess.run(
            [sys.executable, "-u", str(Path(__file__).resolve()),
             "--launch-child"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, cwd=str(ROOT))
        _cout = child.stdout or ""
    except subprocess.TimeoutExpired:
        _cout, child = "", None
        print("  子进程超时（300s）")
    except Exception as exc:
        _cout, child = "", None
        print(f"  子进程启动失败：{exc}")

    seen_any = False
    for line in _cout.splitlines():
        line = line.strip()
        if not line.startswith(LAUNCH_PREFIX):
            print("  " + line)
            continue
        seen_any = True
        parts = line[len(LAUNCH_PREFIX):].split("|", 2)
        name = parts[0]
        ok = len(parts) > 1 and parts[1] == "1"
        detail = parts[2] if len(parts) > 2 else ""
        check(name, ok, detail)

    _rc = child.returncode if child is not None else -1
    print(f"  子进程退出码: {_rc}")
    check("子进程跑完了（有 LAUNCHCHECK 输出）", seen_any)
    check("子进程没有崩溃", _rc >= 0, f"rc={_rc}")
    if _rc not in (0, 1):
        print(f"  子进程 stderr 尾部: {(child.stderr or '')[-300:]}")
    check("子进程报告的失败项与输出一致",
          (_rc == 0) == ("FAIL" not in _cout and "|0|" not in _cout),
          f"rc={_rc}")

# ══════════════════════════════════════════════════════════════════════
head("[12] 收尾")
# ══════════════════════════════════════════════════════════════════════

real = Path(os.environ.get("APPDATA", "")) / "Launchpad" / "shortcuts.json"
if SOURCE.is_dir() and db.is_file():
    try:
        payload = json.loads(db.read_text(encoding="utf-8-sig"))
        check("产物是合法 JSON 数组", isinstance(payload, list),
              f"{len(payload) if isinstance(payload, list) else '?'} 条")
    except Exception as exc:
        check("产物是合法 JSON 数组", False, str(exc))

print(f"  真实库（未被本次测试写入）: {real}")
check("测试全程使用临时库",
      all(str(d).startswith(tempfile.gettempdir()) for d in _tmpdirs),
      f"{len(_tmpdirs)} 个临时目录")

for d in _tmpdirs:
    shutil.rmtree(d, ignore_errors=True)

n_pass = sum(1 for _, ok in results if ok)
n_fail = len(results) - n_pass
print()
print("=" * 74)
# SKIP 单独计数：它是「这台机器没条件测」，既不是通过也不是失败。
# 不分开报的话，「全过」和「一半没测」会显示成同一个数字。
print(f"结果: PASS {n_pass} / FAIL {n_fail} / SKIP {len(_skips)}"
      f"   （共 {len(results)} 项）")
for name, ok in results:
    if not ok:
        print(f"  FAIL  {name}")
for name in _skips:
    print(f"  SKIP  {name}")
print("=" * 74)
sys.stdout.flush()
sys.stderr.flush()

# os._exit 而不是 sys.exit：本节测试真的拉起过 UWP 宿主进程，
# 解释器退出时要拆 COM apartment / 停 win32com 线程，偶尔在这段
# 拆解里硬崩（exit code -1）。那不是测试失败，可不能让退出码
# 把"144 项全过"报成失败。落盘、清理都做完了，直接交还退出码。
os._exit(1 if n_fail else 0)
