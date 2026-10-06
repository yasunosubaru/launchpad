# -*- coding: utf-8 -*-
"""
快捷方式与开机自启：.lnk 的创建/修复，自启状态的读写与去重。

── 要解决的三个问题（都实测过，不是假想） ────────────────

1) **桌面图标是 Python 的蛇，不是我们画的那个图标。**
   实测四份 .lnk：两份的 IconLocation 指向 venv 的 ``pythonw.exe``，
   另两份是空的 ``,0``。而 .lnk 在不显式设 IconLocation 时就是取 target
   的图标 —— target 恰恰是 ``pythonw.exe``。于是「不设」和「设成空的」
   两种写法**得到同一个错误结果**，这就是用户抱怨「桌面快捷方式图标
   没改好」的根因：``assets/launchpad.ico`` 早就画好了，问题是**没有任何
   一处引用它**。IconLocation 是 .lnk 里唯一能覆盖 target 图标的字段，
   所以「修图标」= 显式写 ``<assets\\launchpad.ico>,0``，没有别的办法。

2) **开机自启开了两遍。**
   ``HKCU\\...\\CurrentVersion\\Run`` 里有一个 ``Launchpad`` 值，Startup
   文件夹里又有一份 ``Launchpad.lnk``，两处都生效。症状看着轻
   （单实例互斥体会让输的那个立刻退出），真正坏掉的是**状态**：
   只关注册表的话 Startup 那份照样拉起进程，用户只会认为「开关坏了」；
   任务管理器的「启动」标签里会出现两个 Launchpad，其中一条的
   TargetPath 一旦过期（venv 挪了位置）就成了只有任务管理器能删的
   幽灵条目。所以本模块**只把注册表 Run 键当作自启的唯一真相**，
   Startup 文件夹里那份只作为重复项被清理。

3) **为什么自启走注册表而不是 Startup 文件夹。**
   两者都在登录时执行，但只有注册表这一侧适合被程序自己管理：

   * **状态可查、可判、可重复执行。** ``autostart_status()`` 能当场读出
     「现在到底开没开」，从而在设置界面里如实显示；Startup 文件夹只能
     靠「文件在不在」推断，而且用户从任务管理器里禁用某个启动项时，
     那个文件夹里留下的是一个**被改名或挪走的 .lnk**，程序会误判成
     「还开着」。
   * **开关不依赖用户的文件管理器。** 关自启要去 Explorer 里找文件、
     认名字、删文件；注册表是一次 DeleteValue，幂等且不误伤别人的条目。
   * **不用管理员。** Run 键在 HKCU，普通用户可写。放 HKLM 的话每台
     机器上非管理员都改不了，与「portable、整个目录拷走就能跑」的
     前提直接冲突。

   唯一代价：注册表里的条目在「任务管理器 → 启动」里同样可见可禁用，
   这不是缺点 —— 恰恰是需要的，用户总要有后路。

── 全局约定 ────────────────────────────────────────────

* **只 import** sys / os / 标准库 / winreg / win32com.client / .paths。
  COM 是**函数内延迟 import** 的：本模块的路径计算和注册表部分在没装
  pywin32 的机器上也必须能 import 进来（library.py 的 read_lnk 同样
  在函数体内 import，不在模块级拉 COM）。
* **COM 对象不缓存。** 每个用到 COM 的公开函数自己 Dispatch 一次、
  用完即弃。模块级常驻的 WScript.Shell 在解释器退出时被拆解会让进程
  直接硬崩（本项目其它测试脚本踩过这个坑），而 WScript.Shell 本身
  无状态，每次新建也就一次 Dispatch 的开销。
* **所有会改系统的函数都幂等、不抛异常**，只返回 bool 或结构化 dict。
  它们会被设置界面在主线程直接调用，一次 COM 抖动不该把设置窗口带崩。
"""

import os
import sys
import winreg
from pathlib import Path

from .paths import app_icon_file, install_root

# ── 常量 ────────────────────────────────────────────────

#: 自启只写 HKCU，不碰 HKLM（后者需要管理员权限）。
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: Run 键里的值名。与 Startup 文件夹里的文件名保持一致，
#: 这样用户在两处看到的是同一个名字，不会以为是两个程序。
_RUN_VALUE = "Launchpad"

#: 快捷方式文件名 / 开始菜单里的文件夹名。
_LNK_NAME = "Launchpad.lnk"
_APP_DIR = "Launchpad"

#: 自启条目与快捷方式统一的描述文字。
_DESCRIPTION = "macOS Launchpad 风格的 Windows 启动器"

#: 桌面位置。优先读注册表而不是拼 %USERPROFILE%\\Desktop：
#: 开了 OneDrive「已知文件夹」重定向的机器上，那个 Desktop 可能只是个
#: 空壳目录，真实的桌面在 OneDrive 里 —— 拼路径会把快捷方式写到没人看
#: 得到的地方，症状是「我明明生成了，桌面却没有」。
_SHELL_FOLDERS_KEY = (r"Software\Microsoft\Windows\CurrentVersion"
                      r"\Explorer\User Shell Folders")


# ── 路径计算（纯读，不创建任何东西） ──────────────────────

def package_root() -> Path | None:
    """
    ``launchpad`` 包的父目录（也就是仓库根），**找不到就返回 None**。

    只在**源码运行**时用来生成一条带绝对路径的启动命令；打包成 exe 之后
    用不到（exe 自己就是入口）。

    判据用「这个文件存在吗」而不是「有没有 ``__init__.py``」：
    ``launchpad`` 目录本身没有 ``__init__.py``（命名空间包），拿它当
    判据会永远返回 None —— 而那正是「以为配好了其实没配」的形态。
    """
    try:
        f = Path(__file__).resolve()
    except OSError:
        return None
    for parent in f.parents:
        if (parent / "launchpad" / "__main__.py").is_file():
            return parent
    return None


def bundled_exe() -> Path | None:
    """
    打包好的 ``Launchpad.exe``，**没打包就返回 None**。

    源码运行时也应该优先用它：exe 是自包含的，不依赖``python.exe``、
    不依赖解释器版本、不依赖工作目录 —— 而源码模式依赖这三样。
    找不到（没跑 ``build.py``）才退回源码模式。

    存在性判据用「路径就是文件」，并且顺手确认它**旁边有 assets/**——
    缺了assets 的话托盘图标和快捷方式 ``IconLocation`` 都会指向不存在的
    文件，那种情况下用一个残缺的 exe 比用源码更糟。
    """
    root = package_root()
    if root is None:
        return None
    for cand in (root / "dist" / "Launchpad" / "Launchpad.exe",
                 root / "dist" / "Launchpad.exe"):
        try:
            if cand.is_file() and (cand.parent / "assets").is_dir():
                return cand
        except OSError:
            pass
    return None


def launch_command() -> str:
    """
    能从**任意工作目录**启动本程序的完整命令（给注册表 Run / .lnk 用）。

    ## 打包成 exe（正常情况，也是优先选的）

    直接返回 exe 的绝对路径。exe 是自包含的：不依赖 ``python.exe``、
    不依赖 ``PYTHONPATH``、**也不依赖当前工作目录** —— 这三个依赖正是
    下面那个 bug 的全部成因。

    即使当前是源码运行（``sys.frozen`` 为假），只要 ``dist/`` 下有打包
    好的 exe，也优先用它 —— 用户要的是「双击一个程序」，不是「装好
    Python 才能跑」。

    ## 源码运行（没打包时的退路）

    ``-m launchpad`` 靠**当前工作目录**找包，而注册表 Run 项没有 cwd
    （由 ``CreateProcess`` 解析，不是 cmd）。实测：把
    ``"<pythonw>" -m launchpad`` 写进Run 键后，开机自启**一个进程都没
    起来、日志零写入** —— Python 拿 ``%WINDIR%\\System32`` 当基准，
    找不到包；``pythonw.exe`` 又没有控制台，于是**静默失败**：注册表里
    明明有一条 ``Launchpad`` 值，程序也「装了」，但按热键永远没反应。

    这里用 ``-c`` 把包路径写死在代码里，而不是靠 cwd：

    .. code-block:: python

        "<pythonw>" -c "import sys; sys.path.insert(0, r'<root>'); \\
                        import runpy; runpy.run_module('launchpad', run_name='__main__')"

    为什么不用 ``set "PYTHONPATH=..." && ...``：Run 项的值**只有命令行、
    没有环境变量**，由 ``CreateProcess`` 解析；``set`` 是 cmd 的内建命令，
    没人会执行它 —— 写进去就是一条永远启动失败、读回来却「看起来正常」
    的死条目。``cd /d`` 同理。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'

    # 源码运行，但 dist/ 下有打包好的 exe —— 用 exe。
    bundled = bundled_exe()
    if bundled is not None:
        return f'"{bundled}"'

    root = package_root()
    exe = python_exe()
    if root is None:
        # 找不到仓库根（如被装成 site-packages 里的包）。如实降级成
        # 原来的写法并说明，而不是假装这条能用。
        return f'"{exe}" -m launchpad'
    code = (f"import sys; sys.path.insert(0, r'{root}'); "
            f"import runpy; runpy.run_module('launchpad', "
            f"run_name='__main__')")
    return f'"{exe}" -c "{code}"'


def _split_command(cmd: str) -> tuple[str, str]:
    """
    把 ``launch_command()`` 拆成 ``(TargetPath, Arguments)``。

    只按**第一个**引号对拆，因为 ``launch_command()`` 只会生成两种形状：

    * 打包后的 exe —— ``"C:\\...\\Launchpad.exe"``（没有参数）
    * 源码运行 —— ``"C:\\...\\pythonw.exe" -c "import sys; ..."``

    后者的 ``-c`` 参数**自身含引号和空格**，用 ``shlex`` 之类的通用拆分器
    会把它切碎、还得处理转义。这里按结构拆更简单也更准：Target 是第一段
    引号内的内容，其余原样作为 Arguments。引号缺失时（理论上不该发生，
    但别让一条坏命令把快捷方式写崩）退回「第一个空格」。
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return python_exe(), ""
    if cmd.startswith('"'):
        end = cmd.find('"', 1)
        if end > 0:
            return cmd[1:end], cmd[end + 1:].strip()
    sp = cmd.find(" ")
    if sp < 0:
        return cmd, ""
    return cmd[:sp], cmd[sp + 1:].strip()


def python_exe() -> str:
    """
    快捷方式要指向的解释器。返回绝对路径。

    固定优先 ``pythonw.exe``：它编译进 GUI 子系统，双击时不会先闪一个
    黑框再消失。``python.exe`` 是控制台子系统，从快捷方式启动会在窗口
    面板出现的瞬间弹出一个黑底窗口并挂在那儿 —— 这对「点一下就出
    启动器」的入口是不能接受的。

    （需求里写的是「解释器是 winw 就用 pythonw、否则用 python」。照字面
    实现的话，在 ``python.exe`` 下跑一次修复就会把四份 .lnk 的
    TargetPath 从 pythonw 换成 python，等于顺手引入控制台闪烁。
    所以这里取的是「永远 GUI、找不到再退回」：效果与既有四份 .lnk 一致，
    ``repair_all`` 因此不会改动 TargetPath —— 修复只动该动的。）

    打包成 exe（PyInstaller 等）时 sys.executable 本身就是 GUI 程序，
    原样返回。
    """
    if getattr(sys, "frozen", False):
        return sys.executable

    scripts = Path(sys.executable).parent
    for name in ("pythonw.exe", "python.exe"):
        cand = scripts / name
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            pass
    return sys.executable


def app_icon() -> str:
    """
    自带图标 ``assets/launchpad.ico`` 的绝对路径；**不存在时返回空串**。

    ## 打包后要指向 **exe 旁边**那份，不是仓库里那份

    ``IconLocation`` 是一个**写进快捷方式的绝对路径**，会被持久化到磁盘。
    指向仓库里的 ``assets/`` 有两个问题：

    1. 把 ``dist/Launchpad/`` 整个目录拷给别人（或装到另一台机器）时，
       快捷方式指向的路径根本不存在 —— 壳层取不到图标就显示**空白图标**，
       而且**不报任何错**（这就是当初为什么必须显式设 ``IconLocation``）。
    2. 仓库移动/重命名之后，已有的快捷方式全部失效。

    所以打包后优先用 exe 旁边的 ``assets/`` —— 那一份跟着程序走。

    返回空串而不是抛异常或返回一条坏路径，是因为调用方要靠它决定
    「要不要设 IconLocation」。图标可能没生成（``make_icon.py`` 没跑），
    也可能整个 ``assets/`` 没跟着目录拷过来 —— 这两种都不该让
    「修图标」这件事整体失败。
    """
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent
                          / "assets" / "launchpad.ico")
    exe = bundled_exe()
    if exe is not None:
        candidates.append(exe.parent / "assets" / "launchpad.ico")
    candidates.append(app_icon_file())

    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            pass
    return ""


def _icon_location() -> str:
    """
    能安全写进 IconLocation 的值，形如 ``<...\\launchpad.ico>,0``；
    不可用时返回空串。

    为什么不直接用 :func:`app_icon` 的结果：那个函数用「文件在不在」
    来决定返回空串，但 ``write_shortcut`` 是公开函数，调用方可以替换
    ``app_icon``，.ico 也可能在检查与写入之间被删掉。写坏 IconLocation
    的代价不是报错，而是用户看到一个空白图标、且只能手工重建 .lnk
    才能恢复 —— 所以「文件真的在」这一下必须在**真正写入的那一层**
    再确认一遍，让「绝不写出坏 IconLocation」成为写操作的固有性质，
    而不是调用方的自觉。
    """
    path = app_icon()
    if not path:
        return ""
    try:
        if Path(path).is_file():
            return f"{path},0"
    except OSError:
        pass
    return ""


def _programs_dir() -> Path:
    """开始菜单「程序」目录（用户级，在 %APPDATA% 下）。"""
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows"
            / "Start Menu" / "Programs")


def _desktop_dir() -> Path:
    """桌面目录。注册表优先，探测目录次之，最后退回 %USERPROFILE%\\Desktop。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_FOLDERS_KEY,
                            0, winreg.KEY_READ) as key:
            raw, _ = winreg.QueryValueEx(key, "Desktop")
        p = Path(os.path.expandvars(raw))
        if p.is_dir():
            return p
    except OSError:
        pass

    home = Path.home()
    for name in ("Desktop", "桌面", "OneDrive/Desktop", "OneDrive/桌面"):
        d = home / name
        if d.is_dir():
            return d
    return home / "Desktop"


def _startup_link() -> Path:
    """Startup 文件夹里那份自启快捷方式（**遗留重复项**）。"""
    return _programs_dir() / "Startup" / _LNK_NAME


def startup_link() -> Path:
    """Startup 文件夹里那份 Launchpad.lnk 的路径。

    自启已统一到注册表，这个路径现在只用于两件事：
    ``autostart_status()`` 报告重复状态，以及 ``cleanup_duplicate_autostart()``
    把它删掉。
    """
    return _startup_link()


def _plan() -> tuple[tuple[str, Path, bool], ...]:
    """
    快捷方式的唯一真相：``(用途, 路径, 是否带 --show)``。

    ``shortcut_targets()`` 与 ``repair_all()`` 都从这里取，
    避免两处各写一份列表而悄悄错位。

    **为什么带一个「用途」字段**：原来的实现返回裸元组，``repair_all``
    用 ``plan[:3]`` / ``plan[3]`` 这种**下标切片**来区分「用户点的入口」
    和「开机自启」。加一份、删一份、换个顺序，这个下标就静默指错到
    别的文件上 —— 而且不报错，只是悄悄修/删错了东西。名字比下标难错。

    ## 桌面那份为什么不在这里

    用户明确要求「桌面上不要留东西」。所以桌面**不生成**快捷方式，
    启动器只从开始菜单和托盘进。

    顺带说明那个「坏桩」是什么：桌面上原来有一个 ``Launchpad.lnk``，
    但它的 ``TargetPath`` 是记事本、``IconLocation`` 指向一个**不存在**
    一个根本不存在的 ``.ico`` 路径、``WorkingDirectory`` 为空 —— 三个字段全不对，
    根本不是能用的启动器快捷方式（更像是早期某个探针脚本留下的）。
    本模块曾经把它当自己的入口重写过，那是错的：对着一个不属于本程序
    的文件做「修复」，修得越对越说明认错了对象。现在它已被删除
    （备份留在 temp），且不再被自动重建。

    为什么带不带 ``--show`` 不一样：

    * **带**（开始菜单 \\Launchpad\\ 那份）：用户主动点的入口，点完就该
      看到面板。``__main__.main()`` 见到 ``--show`` 才会 ``show_me()``。
    * **不带**（开始菜单根目录那份、Startup 那份）：启动器本来就该常驻、
      靠热键/手势唤出，SelfStart 在登录时弹一整屏界面既抢焦点又挡住
      用户登录后的第一件事。开始菜单根目录那份历史上就没有 ``--show``，
      本模块保持原样 —— 修图标的顺带把启动语义改了，是把两件事混在
      一次不可回退的写入里。

    另外，开始菜单的 ``Programs\\Launchpad.lnk`` 与
    ``Programs\\Launchpad\\Launchpad.lnk`` 是同一个快捷方式的两份拷贝，
    搜索「Launchpad」会出两个结果。两份都在这里返回、都修复；
    **删不删是用户的决定，本模块只重写不删除**。
    """
    programs = _programs_dir()
    return (
        ("programs", programs / _LNK_NAME, False),
        ("programs-subdir", programs / _APP_DIR / _LNK_NAME, True),
        ("startup", _startup_link(), False),
    )


def shortcut_targets() -> list[Path]:
    """
    快捷方式的路径，**不管存不存在都返回**（开始菜单两份 + Startup 那份）。
    **桌面不在其中** —— 见 :func:`_plan` 里「桌面那份为什么不在这里」。

    不按存在性过滤：调用方（设置界面、修复流程）需要的是「应该在哪里」，
    而不是「现在在哪里」。过滤掉不存在的会让「少了一个」和「本来就该
    有」两种情况长得一模一样。
    """
    return [path for _role, path, _show in _plan()]


def desktop_link() -> Path:
    """
    桌面上那份 ``Launchpad.lnk`` 的路径。

    单独提供是为了让「清理桌面残留」这件事有明确的落点，而不是靠
    ``shortcut_targets()`` 顺带扫到（它已经不扫桌面了）。
    """
    return _desktop_dir() / _LNK_NAME


def remove_desktop_link() -> bool:
    """
    删掉桌面上那份 ``Launchpad.lnk``（如果还在）。返回是否真的删了。

    只按这一个精确路径删，绝不 glob 桌面：桌面上还有用户自己的东西。
    文件本来就不在时返回 False —— 幂等，不是失败。

    为什么需要它：本模块曾经往桌面写过一个快捷方式，而那个位置原本
    已经有一个坏桩（target 是记事本、图标指向不存在的文件）。修坏了它，
    又把它改成了一个能用的启动器 —— 但用户要的是「桌面上什么都别留」，
    所以正确处理是删掉而不是修好。
    """
    path = desktop_link()
    try:
        if not path.exists():
            return False
        path.unlink()
        return True
    except OSError:
        # 被占用 / 权限不足 —— 留到下次再清，不抛。
        return False


# ── 写快捷方式 ──────────────────────────────────────────

def write_shortcut(path: Path, show: bool) -> bool:
    """
    创建或覆盖一份 .lnk。成功返回 True，任何异常都吞掉并返回 False。

    字段的含义：

    * ``TargetPath``      = :func:`python_exe`（GUI 子系统，不闪黑框）
    * ``Arguments``       = ``-m launchpad [--show]``
    * ``WorkingDirectory``= 仓库根（``launchpad/`` 的上一级）
      **必须写绝对路径**：开机自启与从开始菜单启动时 CWD 都是
      ``C:\\Windows\\System32``，留着空 CWD 会让 ``-m launchpad`` 找不到包。
    * ``IconLocation``    = ``<assets\\launchpad.ico>,0`` —— **这才是修图标
      的地方**。逗号后的 0 是图标组内的序号；.ico 只有一张时固定是 0。
    * ``Description``     = 悬停提示

    为什么用 WScript.Shell 而不是自己拼 .lnk 二进制：.lnk 头部带 CLSID、
    环境块、tracker data 等一堆变长结构，自己拼出来的文件 Explorer
    能显示图标但丢一堆属性（任务栏分组、最近使用……），而且没有系统
    公开的写入 API —— WScript.Shell 是唯一受支持的通道。
    反向的读取也是同一套：``library.read_lnk`` 用 WScript.Shell 读回
    target/args/workdir/icon，本模块的校验步骤与之同源。

    幂等：同一组参数写两次得到同一个文件。

    注意这里**不会留下中间状态**：目标是 os.replace 覆盖上去的，不是
    先删后建，中途失败时旧快捷方式还在（先删再建的话，删完崩了就变成
    「图标没了的快捷方式」比原来更糟）。
    """
    path = Path(path)
    tmp = None
    try:
        # 开始菜单里 Programs\\Launchpad\\ 这一层可能还不存在
        path.parent.mkdir(parents=True, exist_ok=True)

        # 先写到同目录下的临时文件，校验通过后再 os.replace 覆盖目标。
        #
        # 这么绕一层是为两件事，都跟「别把好的东西弄坏」有关：
        #
        # 1) **原子性。** COM 的 save() 是「先写后改名」还是「就地截断」不受
        #    我们控制；进程在写到一半被杀，桌面上就剩一个 0 字节或半截的
        #    .lnk，资源管理器里表现为「打不开的快捷方式」，比图标没修更
        #    糟。先落临时文件、成功才 replace，中途失败时旧快捷方式原样
        #    还在。
        # 2) **先校验再发布。** 见下面 save() 之后那一步。
        #
        # 用同目录而不是 %TEMP%：os.replace 要求同卷，跨卷会抛 EXDEV。
        # 文件名必须以 .lnk 结尾 —— CreateShortcut 对扩展名有硬性要求
        # （实测传 `.new` 会直接抛 com_error「快捷方式路径名称需以 .lnk 或
        # .url 结尾」）。代价是修的那一瞬间，桌面上会多出一个
        # Launchpad.tmp.lnk，只存在几毫秒就被 replace 换掉。
        tmp = path.with_name(path.stem + ".tmp.lnk")
        tmp.unlink(missing_ok=True)

        # 延迟 import + 不缓存：见模块 docstring 的「全局约定」。
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")

        sc = shell.CreateShortcut(str(tmp))
        # Target/Arguments 从 launch_command() 拆出来，而不是各写各的 ——
        # 两处各写一遍必然漂移：改了命令格式就只改了一边，产出一个
        # Target 是新版、Arguments 是旧版的快捷方式。
        cmd = launch_command()
        sc.TargetPath, sc.Arguments = _split_command(cmd)
        if show:
            sc.Arguments = (sc.Arguments + " --show").strip()
        sc.WorkingDirectory = str(install_root())
        sc.Description = _DESCRIPTION

        # 图标不在就**一个字段都不写**。写一条指向不存在文件的
        # IconLocation 不会当场报错，Explorer 会拿它当权威来源去取图标，
        # 取不到就落到通用空白图标，还要手工重建 .lnk 才能恢复 ——
        # 比「不设、退回 target 图标」更糟。所以图标缺失时交给调用方
        # 报告，而不是硬写一条坏引用。判断用 _icon_location()，它会在
        # 这里再 stat 一次文件，不依赖调用方是否检查过。
        icon_loc = _icon_location()
        if icon_loc:
            sc.IconLocation = icon_loc

        sc.save()

        # 发布之前先确认「写出来的确实是我们要的」。修图标这个动作如果
        # 失败却报告成功，用户看到的还是原来那只 Python 图标 —— 而这正是
        # 本模块要修的那个现象本身。所以宁可返回 False 让界面报出来，
        # 也不要一个「看起来成功」的假修复。
        #
        # 读回用的是**全新的 Dispatch**：同一个 WScript.Shell 实例里刚
        # CreateShortcut 过的路径，会把内存里那个对象直接交回来（实测
        # 同实例读回拿到的是设进去的值，不是磁盘上的文件），那样等于
        # 拿自己刚写的东西验证自己，永远通过。
        if icon_loc:
            verify = (win32com.client.Dispatch("WScript.Shell")
                      .CreateShortcut(str(tmp)))
            if (verify.IconLocation or "") != icon_loc:
                return False

        # 同卷原子替换：写到一半被断电/被杀也不会留下半个坏快捷方式，
        # 而半个 .lnk 在资源管理器里表现为「打不开的快捷方式」。
        os.replace(str(tmp), str(path))
        return True
    except Exception:
        # 磁盘满、目录被占用、COM 被别的线程拆了……都在这里兜住。
        # 这类调用发生在设置界面的主线程上，抛出去等于把窗口带崩。
        return False
    finally:
        # 成功时 os.replace 已经把 tmp 搬走了，这里只清理失败残留。
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def repair_all(shortcut: bool = True, startup_link: bool = False) -> dict:
    """
    重写快捷方式，返回 ``{'ok': n, 'fail': [...], 'skipped_icon': [...]}``。

    * ``shortcut=True``    → 开始菜单那两份
    * ``startup_link=True``→ **另外**再重写 Startup 那份（默认不动：
      它是自启入口，改它会改变开机行为，而大多数只想修图标；
      而它迟早要被 :func:`cleanup_duplicate_autostart` 删掉，
      为它特意修一次图标没有意义）

    **桌面不在修复范围内**（用户要求「桌面上不要留东西」，
    见 :func:`_plan`）。要顺手清掉桌面残留用
    :func:`remove_desktop_link`。

    ``skipped_icon`` 记的是「链接本身写成功了，但图标没设」的路径。
    它和 ``ok`` 不冲突：``ok`` 说快捷方式可用，``skipped_icon`` 说它的
    图标还停留在 target（Python）的图标上，调用方应该提示用户去跑
    ``make_icon.py` 生成 ``assets/launchpad.ico``。

    幂等，且不抛异常。
    """
    result = {"ok": 0, "fail": [], "skipped_icon": []}

    # 按**用途名**筛，不按下标切。原来是 plan[:3] / plan[3]，加一条删一条
    # 就会静默指错文件，而且不报错。
    todo = [(path, show) for role, path, show in _plan()
            if role in ("programs", "programs-subdir")] if shortcut else []
    if startup_link:
        todo += [(path, show) for role, path, show in _plan()
                 if role == "startup"]

    # 用同一个 _icon_location() 判断，跟 write_shortcut 实际写进去的
    # 依据保持一致 —— 两边各判一次的话，图标恰好在循环中途消失时
    # ok / skipped_icon 会和磁盘上的实际结果对不上。
    icon_loc = _icon_location()
    for path, show in todo:
        if not write_shortcut(path, show):
            result["fail"].append(str(path))
            continue
        result["ok"] += 1
        if not icon_loc:
            result["skipped_icon"].append(str(path))

    return result


# ── 开机自启（注册表 Run 键，唯一真相） ───────────────────

def _run_value() -> str:
    """读 Run 键里的自启命令；没有或读不到就返回空串（绝不抛）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, _RUN_VALUE)
        return value if isinstance(value, str) else str(value)
    except OSError:
        # 键或值不存在（FileNotFoundError）、无权限、值类型怪 —— 一律
        # 当作「没开」。自启状态永远不值得让调用方去处理异常。
        return ""


def autostart_enabled() -> bool:
    """当前是否开机自启。只看注册表 Run 键 —— 那是唯一真相。"""
    return bool(_run_value().strip())


def set_autostart(on: bool) -> bool:
    """
    开/关开机自启（写 HKCU Run 键）。成功返回 True。

    幂等：开是同一条 SetValueEx 覆盖，关是 DeleteValue；已经是对的状态
    再调一次不报错、结果不变。

    关闭时**不**顺手删 Startup 文件夹里的 .lnk —— 那件事由
    :func:`cleanup_duplicate_autostart` 单独负责，混在一起会让调用方
    说不清自己到底动了什么。
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            if on:
                # 引号是必须的：解释器路径只要沾上空格就不行。实际项目
                # 基本都放在带空格的路径下（…\.venv\Scripts\…），
                # 不加引号 Explorer 会在第一个空格处截断命令，得到一条
                # 永远启动失败的死条目，而且完全看不出哪里错了 ——
                # 注册表里读回来是「有一条 Launchpad 值」，看起来一切正常。
                #
                # 用 launch_command() 而不是 ``-m launchpad``：Run 项由
                # CreateProcess 解析、**没有 cwd**，而 ``-m`` 靠 cwd 找包。
                # 实测写 ``"<pythonw>" -m launchpad`` 之后开机自启**一个
                # 进程都没起来、日志零写入**，而注册表读回来完全正常 ——
                # pythonw 没有控制台，import 失败这件事没有任何出口。
                # 打包成 exe 后这条命令就是 exe 本身，三个依赖全都没有。
                command = launch_command()
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ,
                                  command)
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass    # 本来就没开，删不到不算失败（幂等）
        return True
    except OSError:
        return False


def autostart_status() -> dict:
    """
    自启现状：``{'enabled', 'run_value', 'startup_link_exists'}``。

    ``startup_link_exists`` 单独报出来，是为了把「重复自启」这个状态
    **显式摆到界面上**：两处都存在时，``enabled`` 只是 Run 键那一侧，
    单看它会漏掉开机时其实会被拉起两次这件事。
    """
    value = _run_value()
    return {
        "enabled": bool(value.strip()),
        "run_value": value,
        "startup_link_exists": _startup_link().exists(),
    }


def cleanup_duplicate_autostart() -> bool:
    """
    删掉 Startup 文件夹里的 Launchpad.lnk（自启已统一到注册表）。

    返回**是否真的删了**：文件本来就不在时返回 False（幂等，不算失败）。
    不删除注册表那一侧 —— 那是真相，删掉等于关掉自启。

    只按这一个精确路径删，绝不对 Startup 目录做 glob：那个文件夹里
    还躺着别的软件（Fluent Search、Ollama……），通配一次就可能连带
    干掉别人机器上不归我们管的东西。
    """
    path = _startup_link()
    try:
        if not path.exists():
            return False
        path.unlink()
        return True
    except OSError:
        # 被占用 / 权限不足 / 是个目录 —— 都留到下次再清，不抛。
        return False