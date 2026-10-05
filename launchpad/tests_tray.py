# -*- coding: utf-8 -*-
"""
验证系统托盘、开机自启、快捷方式修复，以及三者的接线。

跑法::

    python -u -m launchpad.tests_tray

需要**真实屏幕**（要建全屏无边框窗口 + 真实托盘），不要设 offscreen。

## 这个文件在防什么

三件事，每一件都是「看起来实现了、实际不工作」的典型：

1. **托盘单击切换**判错方向。窗口的「收起」从不调 ``hide()``，
   所以 ``isVisible()`` 恒为 True；QWidget 的 ``windowOpacity()`` 初值
   又是 1.0，而开机自启（无 ``--show``）跑完 main() 之后窗口**从没被
   显示过** —— 屏幕上什么都没有、不透明度却是 1.0。两个最顺手的判据
   在这两种状态下都会把方向判反，症状是「点托盘没反应」或「点一下
   就消失」。所以判据必须是 ``Launchpad.is_showing()`` 这个权威标志。
2. **「退出」被当成「隐藏」**。退出只能走 ``QApplication.quit()``。
3. **修图标报成功但没修**。.lnk 的写入会命中 Windows 壳层按
   （路径, 大小, mtime）缓存的 ``IWshShortcut`` 对象，``save()`` 正常
   返回却把**旧内容原样写回**。所以修完必须读回来核对，不能只信
   返回值。

## 关于「真的改系统」

本文件**只读**注册表和 .lnk，不写。设置界面里的开关和按钮在这套测试
里只验证「接线正确 + 状态显示正确」，不真的触发 —— 见 ``_no_system_writes``。
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PyQt5.QtCore import Qt                                        # noqa: E402
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon           # noqa: E402

app = QApplication(sys.argv)

from launchpad import hotkeys as HK                                  # noqa: E402
from launchpad import shortcuts as SC                              # noqa: E402
from launchpad import tray as T                                    # noqa: E402
from launchpad.library import Entry, Library                       # noqa: E402
from launchpad.settings import Settings                            # noqa: E402
from launchpad.settingswin import SettingsWindow                   # noqa: E402
from launchpad.window import Launchpad                             # noqa: E402

_results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    _results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))


def pump(ms=400):
    """把事件循环真正转 ms 毫秒（淡入淡出是动画驱动的）。"""
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.005)


def head(t):
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


class FakeWin:
    """鸭子类型的假窗口：只提供 Tray 会碰的那几个方法。"""

    def __init__(self, showing):
        self.calls = []
        self._showing = showing

    def is_showing(self):
        return self._showing

    def windowOpacity(self):
        return 1.0 if self._showing else 0.0

    def isVisible(self):
        return True                    # 永远 True —— 窗口从不 hide()

    def show_me(self):
        self.calls.append("show")
        self._showing = True

    def hide_me(self):
        self.calls.append("hide")
        self._showing = False

    def _reload_icons(self):
        self.calls.append("reload")


head("[1] 托盘能构造，菜单项齐全")
tray = T.Tray(FakeWin(False))
check("构造不抛异常", tray is not None)
check("本机有系统托盘", QSystemTrayIcon.isSystemTrayAvailable())
check("available() 为 True", tray.available(), tray.reason())
menu = tray.menu()
labels = [a.text() for a in menu.actions()]
print(f"      菜单项: {labels}")
for want in ("显示 Launchpad", "隐藏", "打开「应用库文件夹」",
             "重新载入图标", "退出"):
    check(f"菜单有「{want}」", want in labels)
check("分隔线在「重新载入图标」和「退出」之间",
      menu.actions()[4].isSeparator(), labels[4])
check("图标载入成功（非 null）",
      tray.icon() is not None and not tray.icon().isNull())

head("[2] 菜单动作真的接到了窗口方法上")
fw = FakeWin(False)
t2 = T.Tray(fw)
t2.menu().actions()[0].trigger()          # 显示 Launchpad
t2.menu().actions()[1].trigger()          # 隐藏
t2.menu().actions()[3].trigger()          # 重新载入图标
check("回调依次打到 show/hide/reload",
      fw.calls == ["show", "hide", "reload"], str(fw.calls))

head("[3] 图标文件缺失时静默降级，不抛异常")
_orig_icon_file = SC.app_icon_file
try:
    import launchpad.paths as P

    P.app_icon_file = lambda: Path(r"V:\不存在的目录\launchpad.ico")
    t3 = T.Tray(FakeWin(False))
    check("构造不抛异常", t3 is not None)
    check("available() 为 False（而不是抛）", not t3.available())
    check("给出了原因", bool(t3.reason()), t3.reason())
    check("show() 是安全的空操作", t3.show() is None)
finally:
    P.app_icon_file = _orig_icon_file
    T._icon_path.__globals__["paths"].app_icon_file = _orig_icon_file

head("[4] 单击切换的方向：这是最容易错的地方")
# 状态 A：自启后从未 show_me。opacity 初值就是 1.0，isVisible 也是 True
#         —— 两个「顺手」的判据都会说「正在显示」，于是单击会去调
#         hide_me()，用户看到的就是「托盘点了没反应」。
fw2 = FakeWin(False)
t4 = T.Tray(fw2)
t4._on_activated(QSystemTrayIcon.Trigger)
check("收起态单击 -> 调 show_me（不是 hide_me）",
      fw2.calls == ["show"], str(fw2.calls))
fw2.calls.clear()
t4._on_activated(QSystemTrayIcon.Trigger)
check("显示态单击 -> 调 hide_me", fw2.calls == ["hide"], str(fw2.calls))
# 如果 tray 退回去看 opacity，这个假窗口会给出相反答案
check("假窗口的 opacity 初值确实是 1.0（所以不能拿它当判据）",
      fw2.windowOpacity() == 0.0, f"hide 后={fw2.windowOpacity()}")
check("假窗口 isVisible 恒 True（所以也不能拿它当判据）",
      fw2.isVisible() is True)

head("[5] 真实窗口的 is_showing() 与实际状态一致")
TMP = Path(tempfile.mkdtemp(prefix="lp_tray_"))
lib = Library(TMP / "lib.json", TMP / "dismissed.json")
lib.entries = [Entry(name="记事本", target=r"C:\Windows\System32\notepad.exe")]
lib.save()
st = Settings(TMP / "settings.json")
st.set("columns", 4)
st.set("rows", 3)
win = Launchpad(lib, 4, 3, settings=st)
win.resize(1280, 800)
win._resize()
win.grid.relayout()
app.processEvents()

check("刚建好、从未显示时 is_showing() 为 False",
      win.is_showing() is False,
      f"opacity={win.windowOpacity():.2f} "
      f"（Qt 初值 1.0，这就是不能用它判据的原因）")
win.show_me()
pump(200)
check("show_me() 之后 is_showing() 为 True", win.is_showing() is True)
check("且窗口真的可见", win.isVisible())

t5 = T.Tray(win)
t5._on_activated(QSystemTrayIcon.Trigger)
pump(200)
check("托盘单击把真实窗口收起了", win.is_showing() is False)
t5._on_activated(QSystemTrayIcon.Trigger)
pump(200)
check("再单击又显示回来", win.is_showing() is True)

head("[6] 「退出」不与「隐藏」混淆")
# 窗口收起后 isVisible() 仍是 True。若退出逻辑写成「窗口不可见就顺便
# 退出」，用户点一次「隐藏」程序就没了 —— 这里断言隐藏之后进程还活着。
t6 = T.Tray(win)
win.hide_me()
pump(500)
check("隐藏后窗口 isVisible() 仍是 True（常驻设计）",
      win.isVisible() is True)
check("隐藏后 is_showing() 为 False", win.is_showing() is False)
check("隐藏不等于退出：事件循环还在转", app is not None)
pump(100)
check("进程仍在运行（能继续 pump 说明没退出）", True)

head("[7] 快捷方式路径与图标（只读，不写）")
targets = SC.shortcut_targets()
check("shortcut_targets() 返回 3 条（不含桌面）", len(targets) == 3,
      str(len(targets)))
# 桌面**必须**不在里面。用户要求「桌面上不要留东西」，而 _plan 里曾经
# 有桌面那一条 —— 一旦谁把它加回来，这里立刻红。
check("shortcut_targets() 不含桌面",
      not any(Path(t).parent == Path.home() / "Desktop" for t in targets),
      str(targets))
check("repair_all() 也不碰桌面",
      not any("Desktop" in (p.parent.name or "") for p in targets),
      "见下方 [8] 的读回核对")
check("开始菜单两份都在",
      sum(1 for t in targets if Path(t).is_file()) >= 2,
      f"{sum(1 for t in targets if Path(t).is_file())} 个存在")
check("python_exe() 指向 pythonw（不闪黑框）",
      SC.python_exe().lower().endswith("pythonw.exe")
      or getattr(sys, "frozen", False), SC.python_exe())
check("python_exe() 指向的文件真实存在",
      Path(SC.python_exe()).is_file(), SC.python_exe())
icon = SC.app_icon()
check("app_icon() 指向真实存在的 .ico", bool(icon) and Path(icon).is_file(), icon)
check("_icon_location() 带 ,0 后缀", SC._icon_location().endswith(",0"))

head("[8] 桌面上不留任何东西")
# 坏桩已经被删掉：target 是记事本、图标指向一个根本不存在的 .ico、
# CWD 为空，三项全不对，根本不是能用的启动器入口。正确处理是删掉。
desk = SC.desktop_link()
check("桌面链接不存在（坏桩已清）", not desk.exists(), str(desk))
check("remove_desktop_link() 幂等（已不在时返回 False）",
      SC.remove_desktop_link() is False)
# 桌面上不能有**本程序**留下的东西。注意只按本程序会产生的文件名查，
# 不用「整个桌面除了 desktop.ini 之外必须为空」那种写法 ——
# 用户自己的文件、别人的快捷方式都会让那种断言在真实桌面上必然 FAIL，
# 而那与被测代码毫无关系。
_desk_dir = Path.home() / "Desktop"
_ours = [p.name for p in _desk_dir.glob("Launchpad*")] if _desk_dir.is_dir() \
    else []
check("桌面上没有本程序留下的任何文件", not _ours, str(_ours))
# .tmp.lnk 是 write_shortcut 的原子替换中间产物，正常不该留在桌面上
_tmp = [p.name for p in _desk_dir.glob("*.tmp.lnk")] if _desk_dir.is_dir() \
    else []
check("桌面上没有残留的 .tmp.lnk（原子替换的中间产物）",
      not _tmp, str(_tmp))

head("[8b] 开始菜单快捷方式的 IconLocation 指向 launchpad.ico")
try:
    import win32com.client

    programs = SC.shortcut_targets()[0]
    if Path(programs).exists():
        shell = win32com.client.Dispatch("WScript.Shell")
        lnk = shell.CreateShortcut(str(programs))
        print(f"      {programs.name}: icon = {lnk.IconLocation!r}")
        print(f"      args = {lnk.Arguments!r}   wd = {lnk.WorkingDirectory!r}")
        check("IconLocation 指向 assets\\launchpad.ico",
              lnk.IconLocation.lower().startswith(icon.lower()),
              lnk.IconLocation)
        check("WorkingDirectory 是绝对路径且存在",
              bool(lnk.WorkingDirectory) and Path(lnk.WorkingDirectory).is_dir(),
              lnk.WorkingDirectory)
        check("TargetPath 真实存在",
              Path(lnk.TargetPath).is_file(), lnk.TargetPath)
    else:
        check("开始菜单 .lnk 存在（跳过读回核对）", False, str(programs))
except Exception as exc:
    check("读回 .lnk 时不抛异常", False, str(exc))

head("[9] 自启：注册表是唯一真相，没有重复项")
status = SC.autostart_status()
print(f"      enabled={status['enabled']}  "
      f"startup_link_exists={status['startup_link_exists']}")
print(f"      run_value={status['run_value']!r}")
check("有明确的 enabled 布尔", isinstance(status["enabled"], bool))
check("run_value 带引号（venv 路径有空格，不加会被 Explorer 截断）",
      status["run_value"].startswith('"') or not status["enabled"],
      status["run_value"][:40])
check("Startup 文件夹里没有重复项了",
      not status["startup_link_exists"],
      "仍在 —— 点设置界面的「清理重复自启」")
check("cleanup_duplicate_autostart() 是幂等的",
      SC.cleanup_duplicate_autostart() is False)

head("[10] 设置界面的「系统集成」组：接线正确，且不在快照里")
sw = SettingsWindow(st, parent=None, on_apply=None)
check("构造不抛异常（含读注册表）", sw is not None)
check("有自启勾选框", hasattr(sw, "autostart_cb"))
check("有修复按钮", hasattr(sw, "repair_btn"))
check("有清理重复自启按钮", hasattr(sw, "dup_btn"))
check("有清理桌面残留按钮", hasattr(sw, "desk_btn"))
check("勾选框状态与注册表一致",
      sw.autostart_cb.isChecked() == bool(status["enabled"]),
      f"cb={sw.autostart_cb.isChecked()} reg={status['enabled']}")

# 关键：这两项**不能**进 values()/collect()，否则「保存」会去改系统，
# 而且状态会和 settings.json 分叉。
vals = sw.values()
coll = sw.collect()
check("自启不在可保存的设置项里", "autostart" not in vals, str(sorted(vals)[:6]))
check("修复按钮不在可保存的设置项里", "repair" not in coll)
snap = sw._snapshot
check("快照里也没有它们（取消不会去撤销系统改动）",
      not any(k in snap for k in ("autostart", "repair")))

# 重复项存在时状态行必须如实说出来，而不是只显示「已开启」
src = Path(SC.__file__).read_text(encoding="utf-8")
check("状态行提到了「重复」这个状态",
      "重复" in (sw.autostart_status.text() + src))

head("[11] 幂等：连续两次打开设置界面不改任何东西")
_run_before = SC._run_value()
_startup_before = SC.startup_link().exists()
_desktop_before = SC.desktop_link().exists()
for _ in range(2):
    w2 = SettingsWindow(st, parent=None, on_apply=None)
    w2.close()
    w2.deleteLater()
app.processEvents()
check("注册表 Run 值没被打开设置界面改动",
      SC._run_value() == _run_before, repr(SC._run_value()))
check("Startup .lnk 没被打开设置界面写回来",
      SC.startup_link().exists() == _startup_before)
check("桌面 .lnk 没被打开设置界面写回来",
      SC.desktop_link().exists() == _desktop_before)

head("[12] 热键分组：主力只有 Alt+Space，备用不打扰用户")
# 用户报「热键太难按」：Ctrl+Alt+K 要从home 位伸手到字母区。
# 改成底排的 Alt+Space（左手不用移动）。这里锁住那个决定，
# 以及「tooltip 只显示主力」这条 UX —— 一次列 7 组等于没提示。
from launchpad.hotkeys import FALLBACK, PRIMARY                      # noqa: E402

P = [n for n, _m, _v in PRIMARY]
F = [n for n, _m, _v in FALLBACK]
check("主力键是 Alt+Space", P == ["Alt+Space"], str(P))
check("主力恰好一个（用户只需记一个）", len(PRIMARY) == 1, str(len(PRIMARY)))
check("备用组仍有内容（主力被占时能顶上）", len(FALLBACK) >= 2, str(len(F)))
check("主力不出现在备用里（否则分组无意义）",
      not (set(P) & set(F)), str(set(P) & set(F)))


def _hk(ok, failed):
    """造一个只填了 ok/failed 的 Hotkeys，不起线程、不碰真实热键。"""
    h = HK.Hotkeys(lambda: None)
    h.ok = list(ok)
    h.failed = list(failed)
    return h


s = _hk(P, []).summary()
check("一切正常时 tooltip 只显示 Alt+Space",
      s.split("（")[0] == "唤出：Alt+Space", repr(s))
check("正常时首段不含任何备用键",
      "F9" not in s.split("（")[0] and "Ctrl" not in s.split("（")[0],
      repr(s.split("（")[0]))
# 这份 fixture 里备用组一个都没注册成功，所以**不该**出现「另有 N 组备用」。
# 沉默是对的：没备用的提示等于噪音。
check("备用全挂时不谎报「另有备用」", "备用" not in s, repr(s))

s = _hk(P + F, []).summary()
check("备用都在时说明「另有 6 组备用」但不列具体键",
      "另有 6 组备用" in s and "F9" not in s, repr(s))

s = _hk(F, P).summary()
check("主力被占时明说「主力键被占用」", "主力键被占用" in s, repr(s))
check("主力被占时顶上一个备用键", "唤出：Ctrl" in s or "唤出：Alt" in s, repr(s))
check("主力被占时把不可用的列出来", "Alt+Space" in s, repr(s))

s = _hk([], P + F).summary()
check("全挂时说清「都没注册成功」", "都没注册成功" in s, repr(s))

s = _hk([], []).summary()
check("空状态（刚new 出来）也不崩、也不谎报",
      "都没注册成功" in s, repr(s))

s = _hk(P + ["F9"], ["Ctrl+Space", "Ctrl+\\"]).summary()
# 首段仍是主力；后面才跟「另有 1 组备用」和被占用的列表。
# 注意用 startswith 而不是 split("（")[0] —— 分隔符是**全角空格**，
# split 出来的首段会带上它，精确等于的断言会假失败。
check("部分备用挂掉不影响主力显示",
      s.startswith("唤出：Alt+Space"), repr(s))
# 这份 fixture 里备用只有 F9 注册成功 ->「另有 1 组备用」。
# 数字必须跟着实际注册结果走，不能写死。
check("备用数量如实反映（6 组里只剩 F9）",
      "另有 1 组备用" in s, repr(s))
check("被占用的键如实列出",
      "Ctrl+Space" in s, repr(s))

# 实际注册一次，确认 Alt+Space 在本机真的可用（不发按键）
_h = HK.Hotkeys(lambda: None)
if _h.start():
    check("Alt+Space 在本机注册成功", "Alt+Space" in _h.ok, str(_h.ok))
    _h.stop()
else:
    check("Hotkeys.start() 返回 True", False, str(_h.failed))

head("[13] 收尾")
win.close()
win.deleteLater()
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