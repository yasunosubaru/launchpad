# -*- coding: utf-8 -*-
"""
打包成真正的 GUI 程序（单个 exe，双击即用，不依赖 python.exe）。

跑法::

    python build.py            # 打包
    python build.py --clean    # 先清 build/dist 再打包

## 产物长什么样

::

    dist/Launchpad/Launchpad.exe     <- 双击这个
    dist/Launchpad/assets/            <- 必须跟着：图标、开始菜单 IconLocation

``--windowed``（等价 ``-w``）让 exe 进 GUI 子系统：**双击不闪黑框**。
这是必须的 —— 从开始菜单点一下就出黑窗口是不能接受的。

## 为什么必须打包，而不是「pythonw.exe -m launchpad」

1. **开机自启会静默失败。** 注册表 Run 项由 ``CreateProcess`` 解析、
   **没有当前工作目录**，而 ``-m launchpad`` 靠 cwd 找包。实测把
   ``"<pythonw>" -m launchpad`` 写进 Run 键后，开机后**一个进程都没起来、
   日志零新增写入** —— Python 拿 ``%WINDIR%\\System32`` 当基准找不到包，
   而 ``pythonw.exe`` 没有控制台，于是这件事**没有任何报错**。
   注册表里读回来是「有一条 Launchpad 值」，看起来一切正常。
2. **要求机器上装有 Python、且装在同一个路径。** 换个解释器版本就废。
3. ``python.exe``/``pythonw.exe`` 在 ``Scripts\\`` 下，用户很容易误删。

打包之后这三条依赖全部消失：exe 是自包含的，``sys.executable`` 就是它
自己，``sys.frozen`` 为真。

## 打包时踩过的坑

* **``assets/`` 不能用 ``--add-data``。** 它是**数据**，运行时必须留在
  exe 旁边（托盘图标、开始菜单 ``IconLocation`` 都要指向真实文件路径）。
  ``--add-data`` 会把文件塞进 PyInstaller 的临时解包目录，程序退出即删 ——
  于是快捷方式的 ``IconLocation`` 指向一个消失的文件，壳层取不到就显示
  空白图标，**且不报任何错**。所以改成打完包把 ``assets/`` 拷过去。
  （代码侧由 ``paths.install_root()`` 的 frozen 分支负责回到 exe 目录。）
* **别用 ``--onefile``。** onefile 每次启动都要解包到 ``%TEMP%``，冷启动
  明显变慢，而本程序的目标就是「秒开」。

## 想要单文件 exe

用 ``--onefile``，但要接受两条代价：启动时先解包（慢 1~2s），以及
``assets/`` 仍然必须留在 exe 旁边（因为 ``IconLocation`` 指向真实路径）。
所以「单文件 + 有图标」这两件事在 Windows 上无法同时满足 —— 除非把图标
base64 进 exe 再运行时释放，那要多写一套自解压和缓存逻辑。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "Launchpad"

# 控制台默认编码在中文 Windows 上是 GBK，打包过程里的报错会变成问号 ——
# 而打包失败的原因往往就在那行报错里。
for _s in (sys.stdout, sys.stderr):
    try:
        if _s is not None:
            _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass

# PyQt5 的 QtWebEngine 之类的大依赖不在这里，但 QWindows 平台插件和
# styles 这些必须有，否则「打包成功、启动就崩」。
EXCLUDES = [
    "PyQt5.QtQml", "PyQt5.QtQuick", "PyQt5.QtQuick3D", "PyQt5.QtWebEngine",
    "PyQt5.QtWebEngineWidgets", "PyQt5.QtMultimedia", "PyQt5.Qt3DCore",
    "PyQt5.QtBluetooth", "PyQt5.QtNetworkAuth", "PyQt5.QtPositioning",
    "PyQt5.QtSql", "PyQt5.QtTest", "PyQt5.QtDesigner", "PyQt5.QtHelp",
    "tkinter", "unittest", "pydoc_data", "test",
]

HIDDEN = [
    "pypinyin",          # 拼音搜索要用
    "pywintypes",
    "win32com",
    "win32api",
]


def log(msg: str) -> None:
    # Windows 控制台默认 GBK，中文会变成一串问号；强制 UTF-8 才能看清
    # 打包过程里那些真正的错误信息。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    print(f"  {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"\n打包失败：{msg}", flush=True)
    sys.exit(1)


def clean() -> None:
    for d in ("build", "dist"):
        p = ROOT / d
        if p.exists():
            log(f"删除 {d}/")
            shutil.rmtree(p, ignore_errors=True)
    spec = ROOT / f"{NAME}.spec"
    if spec.exists():
        log(f"删除 {NAME}.spec")
        spec.unlink()


def build() -> None:
    t0 = time.monotonic()
    log(f"解释器：{sys.executable}")

    # 必须在 PyInstaller **之前**清场，不是只在 smoke_test 里清。
    # COLLECT 阶段要 rmtree 整个 dist/Launchpad/，而正在运行的 exe 被
    # Windows 锁着 -> PermissionError: [WinError 5] 拒绝访问，打包直接失败。
    # 这个失败信息还指向 shutil/rmtree，跟「有个旧实例没退」毫无关系，
    # 光看栈根本想不到要先杀进程。
    _kill_all_instances()

    # PyInstaller 在自己 import 时
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        fail("没装 PyInstaller。先跑：\n"
             f"    {sys.executable} -m pip install pyinstaller\n"
             "或者用 uv：\n"
             "    uv pip install --python .venv pyinstaller")

    assets = ROOT / "assets"
    if not assets.is_dir():
        fail("找不到 assets/ —— 图标和快捷方式都要用它")
    ico = assets / "launchpad.ico"
    if not ico.is_file():
        fail("assets/launchpad.ico 不存在。先跑 python make_icon.py 生成")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        # GUI 子系统：双击不闪黑框
        "--windowed",
        "--name", NAME,
        # 一个 exe + 一个文件夹，而不是 --onefile（见模块 docstring）
        "--contents-directory", "_internal",
        # 图标：影响 exe 本身在资源管理器里的显示
        "--icon", str(ico),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT),
        # 让分析器能找到仓库根下的 launchpad
        "--paths", str(ROOT),
        # ★ 必须有这一条：``launchpad`` 是**没有 ``__init__.py`` 的命名空间包**，
        # PyInstaller 的静态分析会把它整个漏掉 —— 表现为「exe 双击没反应、
        # 连日志都不写」。因为 GUI 子系统没有 stdout/stderr，import 期抛的
        # 异常直接消失，没有任何报错。
        #
        # 实测：不加这条时 34 个模块里 **30 个** ``ModuleNotFoundError``；
        # 加上之后 34/34 全过。这不是「可能有用的保险」，是必需的。
        "--collect-submodules", "launchpad",
        "--hidden-import", "launchpad",
        # 重复的 Qt 插件能省好几 MB
        "--exclude-module", "tkinter",
    ]
    for m in EXCLUDES:
        cmd += ["--exclude-module", m]
    for m in HIDDEN:
        cmd += ["--hidden-import", m]
    cmd.append(str(ROOT / "launchpad" / "__main__.py"))

    print("\n运行 PyInstaller…\n", flush=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        fail(f"PyInstaller 返回 {r.returncode}")

    dist = ROOT / "dist" / NAME
    exe = dist / f"{NAME}.exe"
    if not exe.is_file():
        fail(f"没有产出 {exe}")

    # assets 必须拷到 exe 旁边 —— 不是 --add-data，理由见模块 docstring。
    log("拷贝 assets/ 到 exe 旁边（--add-data 会让 IconLocation 指向临时目录）")
    dst_assets = dist / "assets"
    if dst_assets.exists():
        shutil.rmtree(dst_assets, ignore_errors=True)
    shutil.copytree(assets, dst_assets)

    # ── 产出后自检 ────────────────────────────────────────
    print("\n产出自检：", flush=True)
    problems = []

    size_mb = exe.stat().st_size / 1024 / 1024
    log(f"exe = {size_mb:.1f} MB")

    # PyInstaller 的 bootloader 决定「有没有控制台」；用 PE 头里
    # Subsystem 字段验证 —— 2 = GUI，3 = console。
    import struct
    with open(exe, "rb") as f:
        f.seek(0x3C)
        pe_off = struct.unpack("<I", f.read(4))[0]
        f.seek(pe_off + 0x5C)
        subsystem = struct.unpack("<H", f.read(2))[0]
    if subsystem == 2:
        log("PE Subsystem = 2 (Windows GUI) —— 双击不闪黑框")
    elif subsystem == 3:
        problems.append("PE Subsystem = 3 (console) ——双击会闪黑框，"
                        "说明 --windowed 没生效")
    else:
        problems.append(f"PE Subsystem = {subsystem}（既不是 2 也不是 3）")

    if not (dist / "assets" / "launchpad.ico").is_file():
        problems.append("assets/launchpad.ico 没落到 exe 旁边")

    total = sum(f.stat().st_size for f in dist.rglob("*") if f.is_file())
    log(f"整个目录 = {total / 1024 / 1024:.1f} MB")

    if problems:
        print("\n自检发现问题：", flush=True)
        for p in problems:
            print(f"  FAIL  {p}", flush=True)
        sys.exit(1)

    log("自检通过")
    print(f"\n完成（{time.monotonic() - t0:.0f}s）")
    print(f"\n  程序：{exe}")
    print(f"  分发：把整个 {dist} 目录一起拷走（exe 依赖旁边的 _internal/ "
          f"和 assets/）")

    # ── 真的跑一次 ────────────────────────────────────────
    # 「打包成功」不等于「能启动」。GUI 子系统没有 stdout/stderr，
    # import 期抛的异常直接消失，症状是「双击没反应、连日志都不写」，
    # 而打包过程一路绿灯。所以必须真的跑它，看日志有没有动。
    if not smoke_test(exe, log_path()):
        print("\n  **启动自检失败** —— 上面那个 exe 不能用，别拿去分发。",
              flush=True)
        sys.exit(1)

    print(f"\n  装好之后：把 {dist} 加进 PATH，或用开始菜单快捷方式")
    print(f"  说明：开机自启请在「设置 → 系统集成」里打开，程序会写")
    print(f"        指向 **exe 本身** 的注册表 Run 项")


def log_path() -> Path:
    return Path(os.environ.get("APPDATA", ".")) / "Launchpad" / "launchpad.log"


def _kill_all_instances() -> None:
    """
    停掉**所有形态**的 Launchpad：打包的 exe + 源码模式的 pythonw。

    为什么必须连源码模式一起杀：单实例互斥体是跨形态共享的，任何一个
    实例占着它，新起的那个就会被挡掉、立刻退出 —— 于是启动自检看不到
    任何日志变化，看起来像「exe 起不来」，其实是被自己人挡住了。
    只杀 ``Launchpad.exe`` 不够：开发时后台常留着一个
    ``pythonw.exe -m launchpad``。

    不用 ``taskkill /IM pythonw.exe``：那会连带杀掉机器上所有 pythonw
    （别的项目的后台服务也在里面），代价太大。

    ## 判据必须匹配「真正的入口」，不能只匹配 launchpad 这个词

    第一版写的是 ``$_.CommandLine -match 'launchpad'``，结果是
    **build.py 把自己杀了**（实测 rc=-1、输出只剩「解释器：…」一行）：
    它由 ``.venv\\Scripts\\python.exe -u build.py`` 启动，PowerShell
    记进 WMI 的是**解析后的绝对路径**，里面就含
    ``…\\apps\\Launchpad\\.venv\\…`` —— 于是这个正则命中了打包脚本自己。

    所以只认三种**确实在跑启动器**的形状，另外无条件排除自己：

    * ``-m launchpad``
    * ``launchpad\\__main__.py``（含 ``launchpad/__main__.py``）
    * ``Launchpad.exe``（由上面那条 taskkill 覆盖，这里不重复）
    """
    import os
    import subprocess

    subprocess.run(["taskkill", "/F", "/IM", f"{NAME}.exe"],
                   capture_output=True)
    # [^\s] 是为了不匹配 `-m launchpad.tests_wheelhook` 这种测试进程 ——
    # 它们只是 import 了一下包，不是运行中的启动器实例。
    pattern = r"-m\s+launchpad(\s|$)|launchpad[\\/]__main__"
    ps = ("Get-CimInstance Win32_Process -Filter "
          "\"Name='python.exe' OR Name='pythonw.exe'\" | "
          f"Where-Object {{ $_.ProcessId -ne {os.getpid()} -and "
          f"$_.CommandLine -match '{pattern}' }} | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                   capture_output=True)


def _start_lines(logp: Path) -> int:
    """日志里 ``[Main] 启动`` 出现的次数（读不出来当 0）。"""
    try:
        body = logp.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return body.count("[Main] 启动")


def smoke_test(exe: Path, logp: Path) -> bool:
    """
    真起一次 exe，看它有没有**自己写出启动日志**。

    ## 为什么必须有这一步

    第一版 build.py 只做静态检查（文件在不在、PE Subsystem 对不对），
    然后**打包一路绿灯、exe 双击没反应**。原因是 ``launchpad`` 是没有
    ``__init__.py`` 的命名空间包，PyInstaller 静态分析整个漏掉它，30 个
    模块 import 全部失败 —— 而 GUI 子系统 exe 没有 stdout/stderr，
    那些异常**一个字都看不见**。

    静态检查证明不了「能启动」。启动器第一件事就是 ``install_log()``，
    所以判据是：日志里**新增**了一条 ``[Main] 启动``。

    ## 为什么数行数而不是比 mtime

    第一版比 mtime，结果**假阳性过一次**：那一次 exe 压根没写日志
    （``log.install()`` 见到 windowed 的 ``sys.stdout is None`` 就跳过
    安装），可就在那 30 秒的等待窗口里，一个还没退干净的源码实例往同一
    个日志文件里写了一行 —— mtime 变了 -> 自检报「启动成功」-> 我据此
    宣布打包没问题，实际交付了一个零输出的程序，还把它当成基线沿用了
    两天。

    mtime 会被**任何**写这个文件的进程推动；「新增了一条启动记录」不会。
    所以这里数行数，并且先把两种形态的实例全部清干净。
    """
    import subprocess
    import time

    _kill_all_instances()
    # 互斥体随进程释放，得给它一点时间
    for _ in range(6):
        time.sleep(0.5)
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {NAME}.exe"],
                             capture_output=True).stdout.decode(
                                 "utf-8", "replace")
        if NAME not in out:
            break

    before = _start_lines(logp)
    try:
        subprocess.Popen([str(exe)], cwd=r"C:\Windows\System32",
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except OSError as exc:
        log(f"    启动失败: {exc}")
        return False

    # 等它初始化（导入 Qt + 读图标缓存，冷启动可能要几秒）
    grew = False
    for _ in range(30):
        time.sleep(1.0)
        if _start_lines(logp) > before:
            grew = True
            break

    subprocess.run(["taskkill", "/F", "/IM", f"{NAME}.exe"],
                   capture_output=True)

    if not grew:
        log(f"    日志里没有新增 [Main] 启动 行（现有 {before} 条）")
        log("    逐条排查：")
        log("      1) --collect-submodules launchpad 没加？"
            "（命名空间包会被静态分析漏掉，import 全炸）")
        log("      2) log.install() 退回成「stdout 为 None 就不装」？"
            "（PyInstaller --windowed 就是把 stdout 设成 None）")
        log("      3) 单实例互斥体被别的实例占着")
        return False

    try:
        tail = logp.read_text(encoding="utf-8", errors="replace")
        lines = [x for x in tail.splitlines() if x.strip()]
    except OSError:
        lines = []
    if any("[Main] 启动" in x for x in lines[-20:]):
        log("    日志确认启动成功")
        for x in lines[-3:]:
            log(f"      {x}")
        return True
    log("    日志有变化但没有 [Main] 启动行 —— 可能停在 import 阶段")
    for x in lines[-8:]:
        log(f"      {x}")
    return False


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="把 Launchpad 打包成 GUI exe")
    ap.add_argument("--clean", action="store_true",
                    help="先删掉 build/ dist/ *.spec")
    ns = ap.parse_args()
    if ns.clean:
        clean()
    build()