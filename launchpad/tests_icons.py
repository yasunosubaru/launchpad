# -*- coding: utf-8 -*-
"""
图标提取的真机测试。

跑法（必须用带 PyQt5 的解释器，QApplication 不能省）：

    python -u -m launchpad.tests_icons

它对真实目录 `%USERPROFILE%\Desktop` 里的每一个
.lnk 跑完整提取流程，报告：总数 / 真实图标数 / 占位图数及逐条原因 /
首次提取耗时 / 二次运行（命中磁盘缓存）耗时，以及每一种 icon_size 下的
表现（48 / 96 / 128 / 256）。

隔离：默认把 LOCALAPPDATA 指向临时目录，绝不碰用户真实的图标缓存；
`--real-cache` 才用真实目录（用于验证缓存失效时清理真实目录的行为）。

为什么必须在 Windows 上跑：本文件里每一项断言都依赖 Shell 的实际行为
（IShellItemImageFactory 对 .lnk / 应用商店链接 / 已删除目标的返回码、
资源里最大的图标尺寸、120 DPI 下 SM_CXICON=40）。这些在别的平台上
根本没有对应物，mock 出来的通过率没有意义。
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PyQt5.QtGui import QImage                                    # noqa: E402
# 这几个是 icons.py 的内部函数，不是 API。但判据本身就是「字符串形状」
# 和「像素统计」，要测它就必须直接调它 —— 包一层封装再测封装，
# 等于测的是封装而不是判据（而判据正是出 bug 的地方）。
from launchpad.icons import (                                     # noqa: E402
    _LOW_COLOR_MAX_BUCKETS, _color_buckets, _is_installer_product_icon,
    _is_low_color)

SOURCE = Path(os.environ.get("LAUNCHPAD_SOURCE", Path.home() / "Desktop"))

# 设置里允许的尺寸区间（settings.py: icon_size 默认 128，范围 48~256）
SIZES = (48, 96, 128, 256)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

_failures: list[str] = []
_skips: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  [{PASS if ok else FAIL}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        _failures.append(label)
    return ok


def skip(label: str, why: str) -> None:
    """
    环境不具备条件，跳过而不是判 FAIL。

    判 FAIL 是错的：它会被读成「图标提取坏了」，而实际上只是这台机器的
    快捷方式文件夹里没有那种条目。公开仓库里尤其重要 —— 别人 clone 下来
    装的东西和你不一样，硬要求「必须存在某种条目」就是必然报错。
    """
    _skips.append(label)
    print(f"  [{WARN}SKIP] {label}" + (f"  {why}" if why else ""))


def load_entries(folder: Path) -> list:
    from launchpad.library import Entry, scan_folder
    entries, errors = scan_folder(folder)
    print(f"扫描 {folder}")
    print(f"  条目 {len(entries)}，解析失败 {len(errors)}")
    for e in errors[:5]:
        print(f"    - {e}")
    if len(errors) > 5:
        print(f"    ...（另有 {len(errors) - 5} 条）")
    assert entries, "没有任何条目，源目录可能变了"
    return entries


def run_pass(size: int, entries: list, cache_dir: Path, label: str) -> dict:
    """在一个独立缓存目录上跑一遍冷提取 + 一遍热读取。"""
    from launchpad.icons import CACHE_SIZE, IconCache, co_initialize
    co_initialize()

    cache = IconCache(size)
    cache._dir = cache_dir
    cache._dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    for e in entries:
        cache.get(e)
    cold_ms = (time.perf_counter() - t0) * 1000.0

    # 按缓存键统计，而不是按 uid。uid 只由 target+args 构成，
    # 5 个应用商店链接（TargetPath 全为空）会共用一个 uid —— 用 uid
    # 统计会把它们算成 1 个，实际磁盘上是 5 个独立缓存。
    ph_keys = set(cache.placeholder_keys())
    placeholders = [(e.name, cache.reason(e.uid), cache.origin(e.uid))
                    for e in entries if cache.is_placeholder(e.uid)]
    # 只统计已经取过图标的条目的来源，未取的（探针只跑前 40 个）不参与
    origins: dict[str, int] = {}
    natives: dict[int, int] = {}
    for e in entries:
        o = cache.origin(e.uid)
        if not o:
            continue
        origins[o] = origins.get(o, 0) + 1
        n = cache.native_size(e.uid)
        if n:
            natives[n] = natives.get(n, 0) + 1

    files = sorted(cache_dir.glob("*.png"))
    disk_kb = sum(f.stat().st_size for f in files) / 1024.0
    n_real = len(cache._mem) - len(ph_keys)
    print(f"  缓存条目        {len(cache._mem)}"
          f"（uid 去重后 {len({e.uid for e in entries})}）")

    # 二次运行：全新 IconCache，同一目录 → 应该全部命中磁盘缓存。
    warm = IconCache(size)
    warm._dir = cache_dir
    t0 = time.perf_counter()
    for e in entries:
        warm.get(e)
    warm_ms = (time.perf_counter() - t0) * 1000.0

    # 命中缓存后仍然是有效图标吗？占位图在二次运行时不会被写盘
    # （只写真实图标），所以它必然重新提取 —— 这里顺便验证这一点。
    warm_ph = sum(1 for e in entries if warm.is_placeholder(e.uid))
    disk_hits = sum(1 for e in entries if warm.origin(e.uid) == "disk")

    print(f"\n[{label}] size={size}")
    print(f"  总数            {len(entries)}")
    print(f"  真实图标        {n_real}")
    print(f"  占位图          {len(ph_keys)}")
    print(f"  冷提取耗时      {cold_ms:.0f} ms  ({cold_ms / max(1, len(entries)):.1f} ms/个)")
    print(f"  二次运行耗时    {warm_ms:.0f} ms  ({warm_ms / max(1, len(entries)):.2f} ms/个)")
    print(f"  磁盘命中        {disk_hits}/{len(entries)}")
    print(f"  磁盘缓存        {len(files)} 个 PNG，{disk_kb:.0f} KB"
          f"（{CACHE_SIZE}px 单档）")
    print(f"  来源分布        {origins}")
    print(f"  原生尺寸分布    {natives}"
          f"{'  <- 小于请求尺寸的都是被放大的' if any(k < size for k in natives) else ''}")
    for name, reason, origin in placeholders:
        print(f"    占位：{name}  [{origin}]  {reason}")

    return {
        "size": size, "label": label, "total": len(entries),
        "real": n_real, "ph_keys": ph_keys,
        "placeholders": placeholders, "cold_ms": cold_ms, "warm_ms": warm_ms,
        "files": len(files), "disk_kb": disk_kb, "origins": origins,
        "natives": natives, "disk_hits": disk_hits, "warm_ph": warm_ph,
        "cache": cache,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(SOURCE))
    ap.add_argument("--real-cache", action="store_true",
                    help="用真实 LOCALAPPDATA（会删掉真实图标缓存里的 png）")
    ap.add_argument("--sizes", default=",".join(str(s) for s in SIZES))
    args = ap.parse_args()

    folder = Path(args.source)
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    if not folder.is_dir():
        print(f"源目录不存在：{folder}")
        return 2

    # QApplication 必须先于任何 QPixmap/QIcon/Font 使用存在。
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)

    tmp_root = Path(tempfile.mkdtemp(prefix="launchpad-icons-"))
    os.environ["LOCALAPPDATA"] = str(tmp_root / "Local")
    os.environ["APPDATA"] = str(tmp_root / "Roaming")
    (tmp_root / "Local").mkdir(parents=True, exist_ok=True)
    (tmp_root / "Roaming").mkdir(parents=True, exist_ok=True)

    from launchpad import paths
    from launchpad.icons import CACHE_SIZE, IconCache

    if args.real_cache:
        print("!! 使用真实缓存目录：clear_cache() 会删除其中的 png")
    print(f"图标缓存目录 {paths.icon_dir()}")
    print(f"磁盘缓存档位 {CACHE_SIZE}px（所有显示尺寸都从它缩放）")

    entries = load_entries(folder)
    print()

    # 每个档位用独立的子目录：否则第一个档位留下的缓存会让后面
    # 全部走「命中」路径，看不出提取耗时。
    results = []
    for size in sizes:
        cache_dir = tmp_root / f"cache_{size}"
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        results.append(run_pass(size, entries, cache_dir, "PASS"))

    main_res = next((r for r in results if r["size"] == 128), results[0])

    # ── 断言 ──
    print("\n=== 结论 ===")

    check(main_res["real"] + len(main_res["ph_keys"]) == len(entries),
      "真实图标数与占位图数互补",
      f"{main_res['real']} + {len(main_res['ph_keys'])} = {main_res['total']}")

    # 关键回归：目标为空的商店链接（Codex / OpenCode / Visual Studio）
    # 必须拿到真实图标。上一版这 3 个 + 某停止脚本 是占位图。
    by_name = {e.name: e for e in entries}
    for name in ("Codex", "OpenCode", "Visual Studio"):
        e = by_name.get(name)
        if e is None:
            continue
        got = main_res["cache"].is_placeholder(e.uid)
        check(not got, f"「{name}」（应用商店链接）拿到真实图标",
              main_res["cache"].reason(e.uid) or f"native={main_res['cache'].native_size(e.uid)}")

    # 真的一共减少了多少占位图
    ph_names = {n for n, _, _ in main_res["placeholders"]}
    check("Codex" not in ph_names, "Codex 不再是占位图")
    print(f"  当前占位图：{sorted(ph_names) or '无'}")

    # 每个占位图都必须有原因 —— 否则调用方无法区分，用户也看不到解释
    check(all(r for _, r, _ in main_res["placeholders"]),
          "每个占位图都有明确原因")

    # 「安装程序产品图标」那条断言要**现建**一个 IconCache，不能用
    # main_res["cache"] —— 那个的 _dir 已经被 256 档的 run_pass 覆盖过
    # （results 循环里每个档位都 shutil.rmtree + 重建成同一个对象属性），
    # 拿它提取会命中 256 的磁盘缓存，量到的 native/桶数都不是这条路径算的。
    from launchpad.icons import IconCache
    cache = IconCache(128)

    # ── 「安装程序产品图标」不该被当成应用图标 ──────────────
    # MSIX/Squirrel 安装的 .lnk 会把 IconLocation 指向
    # `%APPDATA%\Microsoft\Installer\{GUID}\ProductIcon` —— 那是安装程序的
    # 产品图标，对单 exe 应用只是一块纯灰白渐变（用户报「ccswitch 这个
    # 失效图标」）。必须跳过它、退到 target 拿真图标。
    cc = by_name.get("示例应用")
    if cc is not None:
        check(_is_installer_product_icon((cc.icon or "").split(",")[0]),
              "「示例应用」的 IconLocation 确实是 Installer 产品图标",
              (cc.icon or "")[-46:])
        cands = cache._candidates(cc)
        kinds = [k for k, _ in cands]
        check(kinds and kinds[0] == "icon",
              "icon 候选排在最前（所以必须靠筛掉它）", str(kinds))
        img, kind, native = cache._extract(cc, cands)
        check(kind != "icon", "「示例应用」最终不走 icon 候选",
              f"kind={kind!r}")
        buckets = _color_buckets(img) if img is not None else 0
        check(buckets > _LOW_COLOR_MAX_BUCKETS,
              "「示例应用」拿到了多色彩的真图标（不是灰白渐变）",
              f"桶={buckets} (阈值 {_LOW_COLOR_MAX_BUCKETS})")
        # 用上面那个新建的 cache 问，而不是 main_res["cache"] ——
        # main_res 那个对象的 _dir 被 256 档的 run_pass 覆盖过，
        # 它的占位图记账是另一轮提取的结果，对不上就是假失败。
        check(not cache.is_placeholder(cc.uid),
              "「示例应用」不再是占位图",
              cache.reason(cc.uid) or "有真实图标")

    # 路径判据本身：正例、反例、鲁棒性。这里不用 mock —— 用真实路径字符串，
    # 因为这条判据的**全部**内容就是字符串形状，mock 只会把被测对象替换掉。
    print("\n=== 安装程序产品图标：路径判据 ===")
    GUID = "{11111111-2222-3333-4444-555555555555}"
    for label, path, want in (   # noqa: E501
        ("真实形状（反斜杠）",
         rf"C:\Users\x\AppData\Roaming\Microsoft\Installer\{GUID}\ProductIcon",
         True),
        ("全小写",
         rf"c:\users\x\microsoft\installer\{GUID.lower()}\producticon", True),
        ("全正斜杠",
         rf"C:/Users/x/Microsoft/Installer/{GUID}/ProductIcon", True),
        ("文件名带空格（仍无扩展名）",
         rf"C:\x\Microsoft\Installer\{GUID}\Product Icon", True),
        ("Office VFS 下的真图标（有扩展名）",
         r"C:\Program Files\Microsoft Office\Root\VFS\Windows\Installer"
         r"\{90160000-0000-0000-0000-000000000000}\app.ico", False),
        ("Windows\\Installer 下的真图标（有扩展名）",
         r"C:\Windows\Installer\{AC76BA86-1033-FFFF-7760-0C0F074E4100}\setup.dll",
         False),
        ("目录名不是 GUID",
         r"C:\x\Microsoft\Installer\not-a-guid\ProductIcon", False),
        ("缺少 Microsoft 这一层",
         rf"C:\x\Installer\{GUID}\ProductIcon", False),
        ("普通 exe", r"C:\Program Files\App\app.exe", False),
        ("普通 ico", r"C:\Program Files\App\app.ico", False),
        ("GUID 位数不对", r"C:\x\Microsoft\Installer\{36BB69C6-1}\ProductIcon", False),
        ("空串", "", False),
    ):
        check(_is_installer_product_icon(path) == want,
              f"路径判据：{label}", path[-52:])

    # 像素兜底判据：全透明图不能被当成「占位图」
    # —— 那是「根本没取到」，该让调用方降级，不是「换下一个候选」。
    print("\n=== 低信息量图：像素兜底判据 ===")
    blank = QImage(64, 64, QImage.Format_ARGB32)
    blank.fill(0)
    check(not _is_low_color(blank), "全透明图不算低质量（交给调用方降级）")
    check(not _is_low_color(None), "None 不算低质量")
    check(not _is_low_color(QImage()), "空 QImage 不算低质量")

    # uid 冲突：TargetPath 都读不出来的链接 uid 相同。
    # 上一版拿 uid 当内存键，这几条会共用一张图标（谁先取到谁赢）。
    #
    # **「确实存在冲突」是环境性质，不是代码性质** —— 它取决于这台机器的
    # 快捷方式文件夹里有没有装商店应用。原来硬要求必须 >=1 组，换个目录
    # （比如只有几个普通 .lnk 的桌面）就必然 FAIL，而 FAIL 会被读成
    # 「图标缓存的 uid 处理坏了」。所以：有冲突就往下测（那几条才是真在
    # 验 uid 隔离），没有就明说没条件测。
    dup_uid: dict[str, list] = {}
    for e in entries:
        dup_uid.setdefault(e.uid, []).append(e.name)
    collided = {u: n for u, n in dup_uid.items() if len(n) > 1}
    if collided:
        check(True, "确实存在 uid 冲突的条目组（这是上一版图错的直接原因）",
              "; ".join(f"{u}: {len(n)} 个" for u, n in collided.items()))
    else:
        skip("uid 冲突组存在性", f"源目录 {SOURCE} 里没有这种条目（与代码无关）")
    if collided:
        # 取最大的那一组（5 个商店链接），那才是上一版图错得最明显的
        group = max(collided.values(), key=len)
        keys = set()
        distinct = []
        for e in entries:
            if e.name not in group:
                continue
            ic = main_res["cache"].get(e)
            keys.add(main_res["cache"]._cache_key(e, main_res["cache"]._candidates(e)))
            distinct.append(ic.cacheKey() if hasattr(ic, "cacheKey") else id(ic))
        check(len(keys) == len(group),
              "uid 相同但缓存键互不相同", f"{len(keys)} 个键 / {len(group)} 个条目")
        check(len(set(distinct)) == len(group),
              "uid 相同但拿到的是各自的图标（不是共用的同一张）")

    # 占位图不能写进磁盘缓存（否则下次启动读到缓存就再也重新尝试了）
    files = {f.name for f in (tmp_root / f"cache_{main_res['size']}").glob("*.png")}
    check(len(files) == main_res["real"],
          "磁盘只写真实图标（占位图不落盘）",
          f"{len(files)} 个文件 vs {main_res['real']} 个真实图标")

    # 二次运行必须全部命中
    check(main_res["disk_hits"] == main_res["total"] - main_res["warm_ph"],
          "二次运行命中磁盘缓存",
          f"命中 {main_res['disk_hits']}，未命中 {main_res['total'] - main_res['disk_hits']}"
          f"（= 重新提取的占位图数）")
    check(main_res["warm_ms"] < main_res["cold_ms"],
          "二次运行明显快于首次",
          f"{main_res['warm_ms']:.0f}ms < {main_res['cold_ms']:.0f}ms")

    # 任意尺寸都要能给出正确尺寸的有效图标。
    # 单独给每个探针一份缓存目录，否则它们会往共用的 icons/ 里写文件，
    # 让后面 clear_cache() 的计数对不上。
    for r in results:
        probe_dir = tmp_root / f"probe_{r['size']}"
        shutil.rmtree(probe_dir, ignore_errors=True)
        probe = IconCache(r["size"])
        probe._dir = probe_dir
        ok = True
        for e in entries[:40]:
            pm = probe.get(e).pixmap(r["size"], r["size"])
            if pm.isNull() or pm.width() != r["size"] or pm.height() != r["size"]:
                ok = False
                break
        check(ok, f"size={r['size']} 任意条目都能取到 {r['size']}x{r['size']} 图标")

    # 质量：内容既不该缩在角落里，也不该被非等比拉成正方形。
    # 只检查真实图标（占位图是自绘的，不参与）。
    from launchpad.icons import _content_box
    shrunk, stretched = [], []
    for e in entries:
        if main_res["cache"].is_placeholder(e.uid):
            continue
        img = main_res["cache"]._extract(
            e, main_res["cache"]._candidates(e))[0]
        if img is None or img.isNull():
            continue
        box = _content_box(img)
        if not box:
            continue
        x, y, w, h = box
        side = max(w, h)
        fill = side / float(img.width())
        off = max(abs((x + w / 2) - img.width() / 2),
                  abs((y + h / 2) - img.height() / 2)) / float(img.width())
        if fill < 0.75:
            shrunk.append((e.name, f"{fill:.0%}"))
        if off > 0.06:
            stretched.append((e.name, f"{off:.0%}"))
    check(not shrunk, "没有图标被缩在画布一角 / 留白过多", str(shrunk[:5]))
    check(not stretched, "没有图标偏心（Qt 的角落贴图毛病）", str(stretched[:5]))

    # 磁盘缓存不随尺寸膨胀：每个档位都是「每个真实图标一个文件」
    for r in results:
        check(r["files"] == main_res["real"],
              f"size={r['size']} 磁盘缓存只占一档",
              f"{r['files']} 个 PNG / {r['disk_kb']:.0f} KB")

    # 缓存失效：签名必须随源文件变动而变。
    # 用真实的临时文件做实验 —— 改 mtime 而不是 mock os.stat，
    # 因为要验证的是「键确实变了」，不是「mock 有没有被调用」。
    c = IconCache(128)
    tmpdir = tmp_root / "sigprobe"
    tmpdir.mkdir(parents=True, exist_ok=True)
    fake_exe = tmpdir / "fake_app.exe"
    fake_exe.write_bytes(b"MZ" + b"\0" * 64)
    fake_entry = type("E", (), {"uid": "probeuid", "target": str(fake_exe),
                                "icon": "", "args": "",
                                "source_lnk": ""})()
    k1 = c._cache_key(fake_entry, c._candidates(fake_entry))
    check(k1 == c._cache_key(fake_entry, c._candidates(fake_entry)),
          "同一份数据签名稳定（不会每次重启都重提取）")

    # 改内容 + 改 mtime -> 键必须变
    st = fake_exe.stat()
    fake_exe.write_bytes(b"MZ" + b"\1" * 128)
    os.utime(fake_exe, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    k2 = c._cache_key(fake_entry, c._candidates(fake_entry))
    check(k1 != k2, "源文件变了 -> 缓存键变（旧缓存自动失效）",
          f"{k1} -> {k2}")

    # 只改 mtime 也必须失效（用户换图标常常是原地覆盖）
    fake_exe.write_bytes(b"MZ" + b"\2" * 256)
    k3 = c._cache_key(fake_entry, c._candidates(fake_entry))
    st2 = fake_exe.stat()
    fake_exe.write_bytes(b"MZ" + b"\3" * 256)
    os.utime(fake_exe, ns=(st2.st_atime_ns, st2.st_mtime_ns + 5_000_000_000))
    k4 = c._cache_key(fake_entry, c._candidates(fake_entry))
    check(k3 != k4, "仅 mtime 变化也失效")

    # 提取策略版本也参与键：改版本号等于让全部旧缓存作废。
    import launchpad.icons as icons_mod
    cands = c._candidates(fake_entry)
    base_sig = c._signature(fake_entry, cands)
    old_ver = icons_mod.EXTRACTOR_VERSION
    try:
        icons_mod.EXTRACTOR_VERSION = old_ver + "+probe"
        check(c._signature(fake_entry, cands) != base_sig,
              "提取策略版本参与缓存键（改策略即全量重提取）")
    finally:
        icons_mod.EXTRACTOR_VERSION = old_ver

    # clear_cache() 必须真的把目录清空。默认 icons/ 目录此时可能是空的
    # （各档位用的是子目录），先往里塞两个文件再验证，避免「空目录上
    # 通过」这种没有意义的断言。
    c2 = IconCache(128)
    c2._dir.mkdir(parents=True, exist_ok=True)
    for nm in ("_probe_a.png", "_probe_b.png.tmp"):
        (c2._dir / nm).write_bytes(b"\x89PNG\r\n\x1a\n")
    keep = c2._dir / "_probe_keep.txt"
    keep.write_text("not a cache file", encoding="utf-8")
    n0 = len(list(c2._dir.glob("*.png"))) + len(list(c2._dir.glob("*.png.tmp")))
    n = c2.clear_cache()
    left = list(c2._dir.glob("*.png")) + list(c2._dir.glob("*.png.tmp"))
    check(n == n0 and not left,
          "clear_cache() 删光磁盘缓存", f"删了 {n} 个")
    check(keep.is_file(), "clear_cache() 不动目录里的非 png 文件")

    print()
    # SKIP 单独计数：它是「这台机器没条件测」，既不是通过也不是失败。
    # 不分开报的话，「全过」和「一大半没测」会显示成同一个数字。
    if _skips:
        print(f"{WARN} SKIP {len(_skips)} 项（环境不具备条件，与代码无关）：")
        for s in _skips:
            print("   -", s)
        print()
    if _failures:
        print(f"{FAIL} {len(_failures)} 项未通过：")
        for f in _failures:
            print("   -", f)
        return 1
    print(f"{PASS} 全部通过"
          + (f"（另有 {len(_skips)} 项跳过）" if _skips else ""))

    # 汇总表
    print("\n=== 各尺寸汇总 ===")
    print(f"{'size':>6}{'真实':>6}{'占位':>6}{'冷提取ms':>10}{'二次ms':>9}"
          f"{'文件':>6}{'KB':>8}{'最小原生':>10}")
    for r in results:
        mn = min(r["natives"]) if r["natives"] else 0
        print(f"{r['size']:>6}{r['real']:>6}{len(r['placeholders']):>6}"
              f"{r['cold_ms']:>10.0f}{r['warm_ms']:>9.0f}{r['files']:>6}"
              f"{r['disk_kb']:>8.0f}{mn:>10}")

    shutil.rmtree(tmp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
