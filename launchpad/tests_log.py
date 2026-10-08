# -*- coding: utf-8 -*-
"""
验证日志安装（``launchpad/log.py``）—— 尤其是**打包成 exe 之后**还能不能写。

跑法::

    python -u -m launchpad.tests_log

## 为什么这个文件独立存在

这个功能的核心价值就是「把 pythonw 丢掉的 print 找回来」。而打包成
GUI exe（PyInstaller ``--windowed``）之后 ``sys.stdout`` / ``sys.stderr``
**是 None** —— 这是 PyInstaller 在无控制台子系统的既定行为，官方文档
明写「标准流不可用」。

于是出现了一个只有打包之后才会暴露、而且**完全静默**的故障：
``install()`` 原本写成 ``if sys.stdout is not None: 装 tee``，
在 windowed exe 里条件恒为假 -> 日志永远不装 -> 整个程序零输出。

实测（不是推断）：双击 ``dist\\Launchpad\\Launchpad.exe`` 起第二个实例，
它被单实例互斥体挡掉后立刻退出，而 ``main()`` 里 ``install_log()``
在 ``acquire_single_instance()`` **之前**，本该至少写一行
``[Main] 启动`` —— 日志 mtime **零变化**。此后两整天，程序里发生的
任何事都留不下痕迹，包括「手势被误触发」这种只有日志能回答的问题。

所以判据不是「源码模式下能写」，而是「**stdout 为 None 时也能写**」。
"""

import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def head(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


def _fresh_log_module():
    """
    拿一份**状态干净**的 log 模块。

    必须重新 import：``log._installed`` 是模块级全局，一旦装过就再也
    装不了第二次 —— 而「重复 install」本身是要测的性质之一，所以测试
    必须在干净状态下跑，否则测的是上一轮留下的残骸。
    """
    import importlib
    import launchpad.log as log
    return importlib.reload(log)


def _swap_path(logmod, tmp: Path) -> str:
    """把 log 文件路径指到临时目录，绝不碰用户真实的 %APPDATA% 日志。"""
    p = tmp / "t.log"
    logmod.log_file = lambda: str(p)
    return str(p)


# ── [1] stdout 为 None（打包后的 GUI 子系统）─────────────
head("[1] sys.stdout / sys.stderr 为 None 时必须仍然能写日志\n"
     "    这就是 PyInstaller --windowed 的真实运行环境")

tmp = Path(tempfile.mkdtemp(prefix="lplog_"))
logmod = _fresh_log_module()
path = _swap_path(logmod, tmp)

real_out, real_err = sys.stdout, sys.stderr
tee_out = None
try:
    sys.stdout = None          # ← 关键：模拟 windowed exe
    sys.stderr = None
    logmod.install()
    # 局部存一份：sys.stdout 可能仍然是 None，那句 flush 会把测试自己
    # 炸掉 —— 那样看到的是一个 AttributeError，而不是「日志没装」这个
    # 真正的结论。测试失败要以结论的形式报出来，不是以崩溃的形式。
    tee_out = sys.stdout
    check("install() 之后 sys.stdout 不再是 None",
          tee_out is not None,
          f"实际是 {tee_out!r}")
    check("install() 之后 sys.stderr 不再是 None",
          sys.stderr is not None,
          f"实际是 {sys.stderr!r}")

    # 真打一行 —— 这正是 hook 线程里那句「手势触发：…」
    print("[Probe] windowed 下的一行")
    if tee_out is not None:
        tee_out.flush()
    wrote = Path(path).is_file() and "[Probe] windowed 下的一行" in \
        Path(path).read_text(encoding="utf-8", errors="replace")
    check("stdout=None 时 print 仍然落盘", wrote)
finally:
    sys.stdout, sys.stderr = real_out, real_err

# ── [2] stderr 也要能写 ──────────────────────────────────
head("[2] stderr 独立可写")

tmp2 = Path(tempfile.mkdtemp(prefix="lplog_"))
logmod2 = _fresh_log_module()
path2 = _swap_path(logmod2, tmp2)

real_out, real_err = sys.stdout, sys.stderr
buf_out, buf_err = io.StringIO(), io.StringIO()
try:
    sys.stdout = buf_out
    sys.stderr = buf_err
    logmod2.install()
    print("out-line")
    print("err-line", file=sys.stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    body = Path(path2).read_text(encoding="utf-8", errors="replace") \
        if Path(path2).is_file() else ""
    check("stdout 的内容进了日志", "out-line" in body)
    check("stderr 的内容进了日志", "err-line" in body)
    check("原 stdout 仍然收到内容（不吞掉终端输出）",
          "out-line" in buf_out.getvalue())
    check("原 stderr 仍然收到内容", "err-line" in buf_err.getvalue())
finally:
    sys.stdout, sys.stderr = real_out, real_err

# ── [3] 重复 install 不套两层 ────────────────────────────
head("[3] 幂等：重复 install 只装一次，不套两层")

tmp3 = Path(tempfile.mkdtemp(prefix="lplog_"))
logmod3 = _fresh_log_module()
path3 = _swap_path(logmod3, tmp3)

real_out = sys.stdout
try:
    sys.stdout = io.StringIO()
    logmod3.install()
    first = sys.stdout
    logmod3.install()
    check("第二次 install 不再包装", sys.stdout is first)
finally:
    sys.stdout = real_out

# ── [4] 原流是 None 时 print 不能炸 ──────────────────────
head("[4] 原流为 None 时 print 不抛异常")

tmp4 = Path(tempfile.mkdtemp(prefix="lplog_"))
logmod4 = _fresh_log_module()
path4 = _swap_path(logmod4, tmp4)

real_out, real_err = sys.stdout, sys.stderr
tee4 = None
try:
    sys.stdout = None
    sys.stderr = None
    logmod4.install()
    tee4 = sys.stdout
    ok = True
    err = ""
    try:
        print("no-console 情况下的 print")
    except Exception as exc:
        ok = False
        err = repr(exc)
    check("print 不抛异常", ok, err)
    if tee4 is not None:
        tee4.flush()
finally:
    sys.stdout, sys.stderr = real_out, real_err

# ── [5] 写不进去时静默退化，不能把程序带崩 ────────────────
head("[5] 日志文件打不开时必须静默退化")

logmod5 = _fresh_log_module()
# 指到一个必定开不上的路径：把文件名放在一个已存在的**文件**下面
blocked = Path(tempfile.mkdtemp(prefix="lplog_")) / "blocker"
blocked.write_text("x", encoding="utf-8")
logmod5.log_file = lambda: str(blocked / "sub" / "t.log")

real_out, real_err = sys.stdout, sys.stderr
try:
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    logmod5.install()
    ok = True
    err = ""
    try:
        print("写不进日志也要照常跑")
    except Exception as exc:
        ok = False
        err = repr(exc)
    check("日志打不开时 print 不抛异常", ok, err)
finally:
    sys.stdout, sys.stderr = real_out, real_err

# ── [6] log.note() 不依赖 stdout ─────────────────────────
head("[6] log.note() 直写文件，与 stdout 是否被替换无关")

tmp6 = Path(tempfile.mkdtemp(prefix="lplog_"))
logmod6 = _fresh_log_module()
path6 = _swap_path(logmod6, tmp6)
logmod6.note("直写一行")
body6 = Path(path6).read_text(encoding="utf-8", errors="replace") \
    if Path(path6).is_file() else ""
check("note() 的内容进了文件", "直写一行" in body6)

# ── 汇总 ────────────────────────────────────────────────
print()
print("=" * 72)
n_ok = sum(1 for _, ok, _ in _results if ok)
n = len(_results)
print(f"合计 {n} 项：PASS {n_ok} / FAIL {n - n_ok}")
if n_ok != n:
    print()
    print("失败项：")
    for name, ok, detail in _results:
        if not ok:
            print(f"  FAIL  {name}   [{detail}]")
print("=" * 72)

sys.exit(0 if n_ok == n else 1)