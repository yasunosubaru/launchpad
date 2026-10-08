# -*- coding: utf-8 -*-
"""
把 stdout / stderr 同时写进一个日志文件。

## 为什么需要它

这个程序**正常情况下是以 pythonw.exe 启动的**（桌面/开始菜单快捷方式、
注册表 Run 自启条目，全是 pythonw）。pythonw 是 GUI 子系统，
**没有控制台**。从资源管理器或注册表拉起时进程连一个父控制台都没有，
于是所有 `print()` 的去向是：

* 从我的终端里 `python -m launchpad` 启动 -> 看得见（stdout 继承自父进程）
* 双击快捷方式 / 开机自启 -> **一个字符都看不到**

这不是小事。热键注册失败、图标提取失败、库文件损坏、启动器没建出窗口
—— 这些的诊断信息**全部**只走 `print()`。用户报的现象永远是
「唤不出来」「图标不对」，而程序侧什么都看不到。

实测踩到的例子：`hotkeys.py` 会打印 `[Hotkey] 可用: Ctrl+Alt+K, F9, ...`。
这行是用户唯一能知道「按哪几个键」的途径，而在真实使用方式下**它必定
被丢弃**。于是「热键到底注册成功没有」「为什么唤不出来」变成了只能靠猜
的问题 —— 这正是「怎么唤出窗口？」这个问题的根源：不是热键坏了
（实测四组全部注册成功、注入按键能正常唤出并置前），而是**没人能看见
那行提示**。

## 设计取舍

* **在 `main()` 最开头安装**，而不是等到某处需要调试时再说。等到需要时
  已经晚了 —— 前面崩了就什么都没记下。
* **追加写，不截断**：一次崩溃现场要和之前的记录并存，排查时需要对比
  「上一次是怎么坏的」。
* **自己管大小上限**：这是个常驻程序，日志会一直长。不封顶的话迟早
  撑爆用户磁盘 —— 而一个启动器把用户磁盘写满是比不记日志严重得多的事。
  超过上限就整个重写（留最后 N 字节），不做轮转 —— 只有一个文件，
  轮转会在用户目录里留一串文件，那比「看不全历史」更烦。
* **绝不抛异常**：日志失败绝不能阻止程序启动。它只是个诊断辅助，
  写不进去就静默退化成「只有 stdout」。

## 用法

    import sys
    from . import log
    log.install()          # 必须在任何 print() 之前
    print("[X] 之后的内容自动进文件")
"""

import os
import sys
import time

#: 单个日志文件的上限（字节）。到顶后重写，只保留最后这么多内容。
MAX_BYTES = 512 * 1024

#: 安装标记，防止重复安装把 stdout 套两层
_installed = False


def log_file() -> str:
    """日志文件路径（``%APPDATA%\\Launchpad\\launchpad.log``）。

    不走 ``paths.py``：这个模块要能在任何东西出错时用，包括
    ``paths`` 自己出问题的时候。手写路径换来的是零依赖。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Launchpad", "launchpad.log")


class _Tee:
    """
    把写入同时送到原流和文件。

    用 ``write`` 而不是继承 ``TextIOBase``：只实现 ``write`` 就够了，
    而 ``print`` 只需要 ``write``。``flush`` / ``isatty`` 补上是为了让
    某些第三方代码（以及 ``-u`` 模式下的行为）不因为少了个方法就炸。
    """

    def __init__(self, original, path):
        self._original = original
        self._path = path
        self._fh = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 行缓冲：崩溃时最后几行也能落盘。全缓冲会丢掉尾巴，
            # 而「崩溃前的最后几行」往往正是关键。
            self._fh = open(path, "a", encoding="utf-8", errors="replace",
                            buffering=1)
        except OSError:
            # 文件打不开（权限、磁盘满、目录被占）—— 退化成只有 stdout。
            self._fh = None

    # ── 真正干活的地方 ──────────────────────────────────
    def _emit(self, text: str) -> None:
        fh = self._fh
        if fh is None:
            return
        try:
            stamp = time.strftime("%H:%M:%S")
            for line in text.splitlines(True):
                if not line.endswith("\n"):
                    line += "\n"
                fh.write(f"[{stamp}] {line}")
            fh.flush()
        except (OSError, ValueError, ValueError):
            # 日志失败一律吞掉：它只是诊断辅助，不能把程序带崩。
            pass
        self._maybe_trim()

    def _maybe_trim(self) -> None:
        fh = self._fh
        try:
            if fh is None or fh.tell() <= MAX_BYTES:
                return
            fh.close()
            # 只保留尾部。这里不 try 全量读入 —— 文件已封顶在
            # MAX_BYTES，一次读进来最多 512KB，可接受。
            with open(self._path, "rb") as f:
                f.seek(-MAX_BYTES // 2, os.SEEK_END)
                tail = f.read()
            # 从第一个完整行开始切，避免半行开头
            nl = tail.find(b"\n")
            tail = tail[nl + 1:] if nl >= 0 else tail
            with open(self._path, "wb") as f:
                f.write(b"--- (log trimmed) ---\n")
                f.write(tail)
            self._fh = open(self._path, "a", encoding="utf-8",
                            errors="replace", buffering=1)
        except OSError:
            # 截断失败就把流置空，别再试了（否则每次写都重试一遍）
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
            self._fh = None

    # ── 流接口 ──────────────────────────────────────────
    def write(self, text):
        self._emit(text)
        orig = self._original
        if orig is None:
            # pythonw 直接跑（没有 shim 转发）时 sys.stdout 就是 None。
            # 那时原样返回，不能让它把程序带崩。
            return len(text)
        try:
            return orig.write(text)
        except (OSError, ValueError):
            return len(text)

    def flush(self):
        if self._fh is not None:
            try:
                self._fh.flush()
            except (OSError, ValueError):
                pass
        if self._original is not None:
            try:
                self._original.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        return False            # 有文件了就不是终端

    def fileno(self):
        # 明确不提供：某些库会拿 fileno 去 fdopen，谎报一个会让它们
        # 往错误的句柄写。让它 AttributeError 比静默写错地方好。
        raise OSError("log tee 没有真实 fd")


def install() -> str:
    """
    安装日志。返回日志文件路径（安装失败时也返回路径，便于提示）。

    必须在 ``main()`` 的**第一行**附近调用，否则前面的 print 不会进文件。
    重复调用是安全的（第二次直接返回）。

    ## 原流是 ``None`` 时**照样装**（这一条是打包之后才暴露的）

    第一版写成::

        if sys.stdout is not None:
            sys.stdout = _Tee(sys.stdout, path)

    那个 ``is not None`` 判断看起来是「防御性编程」，实际上造成了一个
    **只在打包后才出现、而且完全静默**的故障：PyInstaller 的
    ``--windowed``（GUI 子系统）按其既定行为把 ``sys.stdout`` /
    ``sys.stderr`` 设成 ``None`` —— 官方文档原话是「standard streams
    are unavailable」。于是条件恒为假 -> 日志永远不装 -> 程序零输出。

    实测（不是推断）：双击 ``dist\\Launchpad\\Launchpad.exe`` 起第二个
    实例，它被单实例互斥体挡掉后立刻退出，而 ``main()`` 里
    ``install_log()`` 在 ``acquire_single_instance()`` **之前**，本该至少
    写一行 ``[Main] 启动`` —— 日志 mtime **零变化**。此后两整天，程序里
    发生的任何事都留不下痕迹，包括「手势被误触发」这种只有日志能回答的
    问题：症状是「滚轮一划就弹出来」，而判据（手势起点离边缘多少像素、
    攒了多少 delta）只存在于那行 ``print`` 里。

    所以这里**无条件**装，原流是 ``None`` 就把 ``None`` 交给
    :class:`_Tee` —— 它的 ``write`` 本来就处理了 ``orig is None``
    （:meth:`_Tee.write` 里那个分支）。判断放在这里等于把「有没有地方
    输出」和「要不要记日志」混成一件事，而这两件事在无控制台程序里
    恰好是**相反**的。
    """
    global _installed
    path = log_file()
    if _installed:
        return path
    _installed = True
    try:
        sys.stdout = _Tee(getattr(sys, "stdout", None), path)
        sys.stderr = _Tee(getattr(sys, "stderr", None), path)
    except Exception:
        # 套壳失败也绝不影响启动
        pass
    return path


def note(msg: str) -> None:
    """往日志里写一行（不依赖 stdout 是否已被替换）。"""
    try:
        path = log_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def read_lines(limit: int = 200) -> list[str]:
    """读回最近若干行（给「设置 → 关于」或排障用）。读不到返回空表。"""
    try:
        with open(log_file(), "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-limit:]
    except OSError:
        return []