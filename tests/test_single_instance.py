# -*- coding: utf-8 -*-
"""
单实例互斥体回归测试。

这个 bug 很隐蔽：重复点桌面快捷方式会开出第二个进程，
第二个实例去抢已被第一个占住的全局热键，于是启动日志里
四个热键全部报"被其它程序占用"，用户以为热键坏了。
实际是单实例检测形同虚设。

根因：判断"已存在"时用了 ctypes.windll.kernel32.GetLastError()，
那是一个没开 use_last_error 的独立 WinDLL 对象，读到的是 0。
必须用 ctypes.get_last_error()（它读的是 ctypes 为
use_last_error=True 包装保存的那份错误码）。
"""

import ctypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\LaunchpadSingleInstance"

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def create_mutex(name=MUTEX_NAME):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    h = k.CreateMutexW(None, False, name)
    return h, ctypes.get_last_error()


print("=" * 70)
print("第一次创建（用唯一名称，不该报已存在）")
print("=" * 70)
# 必须用随机唯一名称：跑这个测试时后台很可能已经有实例占着
# MUTEX_NAME，那样"第一次"创建也会返回 183，测不出东西。
import uuid
UNIQUE = f"Local\\LaunchpadTest_{uuid.uuid4().hex}"
h1, err1 = create_mutex(UNIQUE)
print(f"  名称={UNIQUE}")
print(f"  handle={h1}  last_error={err1}")
check("首次创建返回有效句柄", bool(h1), f"handle={h1}")
check("首次创建不报 ERROR_ALREADY_EXISTS", err1 != ERROR_ALREADY_EXISTS,
      f"err={err1}")

print()
print("=" * 70)
print("第二次创建（同一名称，必须报已存在）")
print("=" * 70)
# 用刚才那个唯一名称的互斥体（h1 还持有）。
# 不能用 MUTEX_NAME：后面的 main.acquire_single_instance() 也会创建它，
# 谁先跑谁就改变了状态，断言会随执行顺序翻转 —— 那是在测顺序不是测行为。
h2, err2 = create_mutex(UNIQUE)
print(f"  名称={UNIQUE}")
print(f"  handle={h2}  last_error={err2}")
check("第二次创建返回有效句柄", bool(h2), f"handle={h2}")
check("第二次创建报 ERROR_ALREADY_EXISTS(183)",
      err2 == ERROR_ALREADY_EXISTS, f"err={err2}")

print()
print("=" * 70)
print("两种取错误码方式的对比（这正是 bug 所在）")
print("=" * 70)
h3, err3 = create_mutex(UNIQUE)
windll_err = ctypes.windll.kernel32.GetLastError()
check("ctypes.get_last_error() 能读到 183",
      err3 == ERROR_ALREADY_EXISTS, f"err={err3}")
check("windll.kernel32.GetLastError() 读不到（证明它不可用）",
      windll_err != ERROR_ALREADY_EXISTS,
      f"windll 读到 {windll_err}，ctypes 读到 {err3}")

print()
print("=" * 70)
print("main.acquire_single_instance 的真实行为")
print("=" * 70)
from launchpad.__main__ import acquire_single_instance

# 语义：already=True 表示"已经有实例在跑"。所以期望值取决于当前
# 是否真有实例 —— 两种情况都算正确，关键是它必须和实际一致，
# 而不是永远返回 False（那正是原 bug）。
from launchpad.hotkeys import WND_CLASS_NAME, _apis
u, _ = _apis()
u.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
u.FindWindowW.restype = ctypes.c_void_p
app_running = bool(u.FindWindowW(WND_CLASS_NAME, WND_CLASS_NAME))

handle, already = acquire_single_instance()
check("返回的句柄有效", bool(handle), f"handle={handle}")
check(f"检测结果与实际一致（应用{'在跑' if app_running else '未运行'}"
      f" -> already={already}）",
      already == app_running,
      f"already={already}, 期望={app_running}")
if app_running:
    check("检测到已有实例（关键回归：原来永远返回 False）",
          already is True, f"already={already}")

print()
print("=" * 70)
n_pass = sum(1 for _, ok in results if ok)
n_fail = sum(1 for _, ok in results if not ok)
print(f"结果: PASS {n_pass} / FAIL {n_fail}")
for name, ok in results:
    if not ok:
        print(f"  FAIL {name}")
print("=" * 70)
sys.exit(1 if n_fail else 0)