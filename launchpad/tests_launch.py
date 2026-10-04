# -*- coding: utf-8 -*-
"""
验证「点图标启动后，启动器要不要收起」这条行为。

跑法::

    python -u -m launchpad.tests_launch

需要**真实屏幕**（要建全屏无边框窗口），不要设 offscreen。

## 为什么单独一个文件

这条行为牵涉三样东西，任一样错了症状都很难认：
- ``hide_me()`` 走的是 ``QVariantAnimation`` 驱动的淡出，**不是同步的**
- ``_launch`` 里 launch 是**函数内 import** 的，打桩要打在
  ``launchpad.addapp`` 上，打在 ``launchpad.window`` 上没用
- 窗口的「收起」状态是**降不透明度 + 鼠标穿透**，不是 ``hide()``，
  所以断言 ``isVisible()`` 永远为 True，判据要用 opacity 和属性
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

from PyQt5.QtCore import Qt                                      # noqa: E402
from PyQt5.QtWidgets import QApplication                        # noqa: E402

app = QApplication(sys.argv)

from launchpad import addapp as A                                # noqa: E402
from launchpad.library import Entry, Library                     # noqa: E402
from launchpad.settings import SCHEMA, Settings                  # noqa: E402
from launchpad.settingswin import GROUP_SPECS                    # noqa: E402
from launchpad.window import Launchpad                           # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def pump(ms=500):
    """
    把事件循环真正转 ms 毫秒。

    **必须真的转，不能只调一次 ``processEvents()``。**
    淡入淡出是 QVariantAnimation 驱动的，只转一帧的话量到的是动画的
    第一帧：明明 `hide_me()` 已经把 `_fade` 设成 0，opacity 却还是 1.0，
    于是「窗口有没有收起」这条断言会给出完全相反的结论。
    这个坑踩过一次：测试以为「不收起」的实现有 bug，实际是测试没等动画。
    """
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def head(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


TMP = Path(tempfile.mkdtemp(prefix="lp_launch_"))
NOTEPAD = r"C:\Windows\System32\notepad.exe"

lib = Library(TMP / "lib.json", TMP / "dismissed.json")
lib.entries = [Entry(name="记事本", target=NOTEPAD),
               Entry(name="计算器", target=NOTEPAD)]
lib.save()

_orig_launch_entry = A.launch_entry
_launched: list[str] = []


def fake_launch(entry):
    _launched.append(entry.name)
    return True, ""


def build(hide_after: bool, tag: str) -> Launchpad:
    st = Settings(TMP / f"settings_{tag}.json")
    st.set("columns", 4)
    st.set("rows", 3)
    st.set("hide_after_launch", hide_after)
    w = Launchpad(lib, 4, 3, settings=st)
    w.resize(1280, 800)
    w._resize()
    w.grid.relayout()
    app.processEvents()
    w.show_me()
    pump(120)
    return w


head("[1] 设置项本身")
check("hide_after_launch 在 SCHEMA 里", "hide_after_launch" in SCHEMA)
check("默认 False（= 启动后不收起，这是用户要的默认）",
      SCHEMA["hide_after_launch"][0] is False,
      str(SCHEMA["hide_after_launch"][0]))
check("wheel_gesture_ms 在 SCHEMA 里", "wheel_gesture_ms" in SCHEMA)
check("wheel_gesture_ms 默认 220ms / 范围 80~600",
      SCHEMA["wheel_gesture_ms"][0] == 220
      and SCHEMA["wheel_gesture_ms"][2:4] == (80, 600),
      str(SCHEMA["wheel_gesture_ms"]))

st = Settings(TMP / "coerce.json")
st.set("wheel_gesture_ms", 5)
check("过小的手势窗口被钳到 80", st.get("wheel_gesture_ms") == 80,
      str(st.get("wheel_gesture_ms")))
st.set("wheel_gesture_ms", 99999)
check("过大的手势窗口被钳到 600", st.get("wheel_gesture_ms") == 600,
      str(st.get("wheel_gesture_ms")))
st.set("wheel_gesture_ms", "不是数字")
check("垃圾值退回默认 220", st.get("wheel_gesture_ms") == 220,
      str(st.get("wheel_gesture_ms")))
st.set("hide_after_launch", "yes")
check("字符串 yes 认成 True", st.get("hide_after_launch") is True)
st.set("hide_after_launch", 0)
check("0 认成 False", st.get("hide_after_launch") is False)

_keys = [k for _g, items in GROUP_SPECS for k, *_ in items]
check("两项都在设置界面里", "hide_after_launch" in _keys
      and "wheel_gesture_ms" in _keys)
check("设置界面没有重复 key", len(_keys) == len(set(_keys)),
      f"{len(_keys)} 项")

head("[2] 启动后不收起（hide_after_launch=False）")
A.launch_entry = fake_launch
w = build(False, "keep")
_launched.clear()
w.grid._message = ""
# 记下启动前的窗口 flags。断言「一个位都没动过」而不是「最后还是置顶」
# ——后者对「改了再改回来」的实现也会通过，那正是要防的假通过。
_flags_before = int(w.windowFlags())
w._launch(lib.entries[0])
pump(200)

check("确实调到了启动器", _launched == ["记事本"], str(_launched))
check("窗口仍可见（没被收起）", w.windowOpacity() > 0.5,
      f"opacity={w.windowOpacity():.2f}")
check("窗口仍能收鼠标（没进入穿透状态）",
      not bool(w.testAttribute(Qt.WA_TransparentForMouseEvents)))
check("给了「已启动」的提示", "已启动" in w.grid._message,
      w.grid._message[:44])
check("提示里说明了怎么收起",
      "Esc" in w.grid._message or "空白" in w.grid._message)
check("提示里说明新应用在启动器后面（否则用户不知道去哪找）",
      "后面" in w.grid._message, w.grid._message[:44])
# 新契约：**窗口状态全程不动**。
#
# 之前的实现是「把窗口压到刚启动的应用之下」—— 临时清掉
# WindowStaysOnTopHint 再恢复。用户明确要求「不要把窗口消失，我自己
# 点击空白处再消失」，也就是别替他做切换。所以现在断言的是 flags
# 一个位都没动过，而不只是「最后还是置顶」（后者对「改了再改回来」
# 的实现也会通过，正是要防的那种假通过）。
check("置顶标志从头到尾没被动过（没改再改回来）",
      bool(w.windowFlags() & Qt.WindowStaysOnTopHint)
      and int(w.windowFlags()) == _flags_before,
      f"before=0x{_flags_before:x} after=0x{int(w.windowFlags()):x}")
check("仍然置顶（窗口停在原地，不被新应用压下去）",
      bool(w.windowFlags() & Qt.WindowStaysOnTopHint))
check("没有把 Launchpad 降权成普通窗口",
      bool(w.windowFlags() & Qt.FramelessWindowHint))
check("连点第二个应用也能启动（不锁死后续点击）",
      (w._launch(lib.entries[1]) or pump(120) or _launched) == ["记事本", "计算器"],
      str(_launched))
# 连点两次之后状态仍然没被动过。
check("连点两个应用之后置顶标志仍然没被动过",
      int(w.windowFlags()) == _flags_before,
      f"before=0x{_flags_before:x} after=0x{int(w.windowFlags()):x}")

head("[3] 启动后收起（hide_after_launch=True，macOS 行为）")
w2 = build(True, "hide")
w2._launch(lib.entries[0])
pump(700)          # 等淡出动画走完（FADE_MS 约 220ms，留足余量）
check("窗口已收起（不透明度降到 0）", w2.windowOpacity() <= 0.01,
      f"opacity={w2.windowOpacity():.3f}")
check("鼠标穿透已开",
      bool(w2.testAttribute(Qt.WA_TransparentForMouseEvents)))

head("[4] 启动失败时不能报「已启动」")


def fail_launch(entry):
    return False, "测试用的失败原因"


A.launch_entry = fail_launch
w.grid._message = ""
w._launch(lib.entries[0])
pump(120)
check("失败路径不说「已启动」", "已启动" not in w.grid._message,
      w.grid._message[:40])
check("失败路径给出了原因", "无法启动" in w.grid._message,
      w.grid._message[:40])
check("失败路径窗口保持可见（用户还能看见并重试）",
      w.windowOpacity() > 0.5, f"opacity={w.windowOpacity():.2f}")

head("[5] 两个窗口互不干扰")
# 先把 launch 桩复原！第 [4] 节把它换成了「总是失败」，不还原的话
# 这里 w3._launch() 必然走进失败路径、message 里没有「已启动」，
# 而断言会把它读成「两个窗口的 message 串了」—— 一个完全无关的结论。
A.launch_entry = fake_launch
w3 = build(False, "keep2")
w3._launch(lib.entries[0])
pump(120)
check("各自的 message 是各的",
      "已启动" in w3.grid._message and "已启动" not in w2.grid._message,
      f"w3={w3.grid._message[:20]!r}")

A.launch_entry = _orig_launch_entry
for x in (w, w2, w3):
    x.close()
    x.deleteLater()
app.processEvents()

head("[6] 全程只用临时目录")
real = Path.home() / "AppData" / "Roaming" / "Launchpad"
check("没碰用户真实库",
      str(lib.path).startswith(str(Path(tempfile.gettempdir()))),
      str(lib.path))

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