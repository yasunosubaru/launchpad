# -*- coding: utf-8 -*-
"""
图标提取：把 exe / 快捷方式 / .ico 的图标取出来，存成 PNG 缓存。

── 这一版为什么重写了提取层 ────────────────────────────────

上一版用 `QFileIconProvider.icon(QFileInfo(path))` 提取。查下来的根因
不是「图标不存在」，而是四处叠加：

1) **Qt 的 QFileInfo 会跟着 .lnk 跳到 target 再 stat。**
   `QFileInfo("Codex.lnk").exists()` 返回 False，但 `Path(...).exists()`
   和 `os.stat` 都是 True、`QFile.open(ReadOnly)` 也能打开。
   上一版在 `_extract_one` 开头写 `if not info.exists(): return QIcon()`，
   于是这几个 .lnk（四个应用商店链接）
   **在还没问 Shell 要图标之前就被自己否决了**。
   旁证：`QFileInfo("Google Chrome.lnk").size()` 报 4512408 —— 那是
   chrome.exe 的大小，不是 .lnk 自己的 2144 字节。反直觉但可复现：
   Qt 的 QFileInfo 对 .lnk 走的是「解析后」语义。

2) **QFileIconProvider 在 .lnk 上最多只给 40x40。**
   它内部走 SHGetFileInfo + 系统大图标（SHIL_LARGEICON），而本机是
   120 DPI，SM_CXICON = 40。实测 `icon.pixmap(128,128)` 对任何 .lnk 都
   返回 40x40 的位图 —— 也就是把 40px 拉伸到 128px 当图标显示，
   这正是用户看到的「有的缩略图糊」。对 .exe 目标 Qt 能给到 256，
   所以只有走 .lnk 的那批受影响。

3) **Entry.uid 会撞车，于是不同的应用共用同一张图标。**
   uid = sha1(target|args)。5 个应用商店链接的 TargetPath **全是空**
   字符串（只有 LinkTargetIDList），args 也空，sha1 之后完全相同；
   实测 122 个条目里有 3 组共 8 个条目撞 uid（5+2+2）。上一版拿 uid
   当内存缓存键，于是「谁先取到谁赢」，其余的应用显示的是**别的应用**
   的图标。library.py 的 uid 定义不在本文件的改动范围内，所以在图标层
   把签名并进缓存键解决。

4) **占位图不能用 isNull() 判定。**
   占位图是有效 QIcon，`isNull()` 返回 False。同一个陷阱还有第二个
   变体：Shell 对「什么都指不到」的 .lnk 会返回一张**合法的 256x256
   通用空白文档图**，程序同样无从察觉。所以判定要靠提取阶段的返回值，
   并且对 .lnk 多问一句「它还指得到真图标吗」，再把**原因**记录下来。

现在的做法：用 `IShellItemImageFactory::GetImage`（SHCreateItemFromParsingName
→ QueryInterface → GetImage）直接向 Shell 要指定尺寸的位图。

选它的原因（都实测过）：
* 对 .lnk 有效。Codex.lnk 是应用商店链接（IDList 指向
  `OpenAI.Codex_2p2nqsd0c76g0!App`，没有 TargetPath），SIIF 照样返回
  真实的 256x256 OpenAI 图标 —— 这正是 Qt 拿不到的那一张。
* 尺寸是自己给的，不受 SM_CXICON 限制：122 个 .lnk 请求 256 全部返回
  256x256，请求 128 全部返回 128x128。
* 同一路径同时尊重 IconLocation 与 target（Intel 那个 IconLocation 指向
  new_app_icon.exe，SIIF(lnk) 与 SIIF(IconLocation) 结果一致）。
* 宽高比与留白由 Shell 自己排版，不需要我们拉伸。

降级链：SIIF → QFileIconProvider → 占位图。SIIF 唯一的硬失败是
COM 未初始化；后台线程必须自己 CoInitialize（实测：不初始化时
122 个全部返回 0，初始化后 122 个全部成功且均值从 24ms 降到 10ms）。

实测结果（`python -m launchpad.tests_icons`，122 个 .lnk）：
真实图标 114 → **121**，占位图 6 → **1**（唯一剩下的那条快捷方式的
target 与 IconLocation 都被删了，Shell 也只剩通用空白文档图）。
"""

import ctypes
import hashlib
import os
import re
import uuid
from ctypes import wintypes
from pathlib import Path

from PyQt5.QtCore import (QFileInfo, QObject, QRect, QSize, Qt, pyqtSignal)
from PyQt5.QtGui import (QColor, QFont, QIcon, QImage, QLinearGradient,
                         QPainter, QPixmap)
from PyQt5.QtWidgets import QFileIconProvider

from .library import Entry, icon_dir


# ── 磁盘只缓存这一档 ─────────────────────────────────────
#
# icon_size 可调 48~256，若每档都落盘，116 个应用 × 5 档 = 580 个 PNG。
# 只缓存 256 之后所有尺寸都从它缩放：内存里按 self.size 生成 QIcon，
# 磁盘永远只有 116 个文件，且换尺寸不会留垃圾。
CACHE_SIZE = 256

# 提取策略变了就换版本号：旧缓存文件名带旧版本，直接被忽略（不会误命中，
# 也就不用删用户的目录）。
#
# 改任何影响**像素输出**的东西都要动它，否则已经缓存过的用户会继续看
# 旧图。本版加 _decorner（修 Qt 把小图标贴到画布左上角）时忘了加，
# 结果本机就出现同一条目两个不同签名的 256px 文件各存一份。
# siif4: _content_box 的 alpha 阈值从 10 提到 128（渐进回退），
#        修掉「一层 alpha 10~30 的薄雾铺满画布、真实图形只有中间一小块」
#        导致图标只占格子 20% 的问题。改提取策略必须升版本号，
#        否则磁盘上那 121 张未修正的 PNG 会被继续命中。
# siif5: 新增 _is_low_color —— IconLocation 指向
#        `Microsoft\Installer\{GUID}\ProductIcon`（安装程序产品图标，
#        不是应用图标）时，提取出的是纯灰白渐变。实测「某 Squirrel 安装的单 exe 应用」
#        烘出来只有 1224 字节、量化后仅 3 种灰阶。现在这种结果会被
#        跳过，继续试下一个候选（target 通常能给正常彩色图标）。
#        影响像素输出，必须升版本号让旧缓存失效。
# siif6: 主判据改成**按路径**识别 —— `_is_installer_product_icon()`
#        认 `...\Microsoft\Installer\{GUID}\<无扩展名>`（MSIX/Squirrel
#        的产品图标，在定义上就不是应用图标）。siif5 靠像素猜，
#        加了「实心占比」条件后仍然会在 CCS Theia 上撞车（它的真图标
#        实心占比也是 54%，与占位图完全一样）。
#        路径识别没有这种「统计特征撞车」的问题。像素判据保留作兜底。
#        影响像素输出，必须升版本号。
EXTRACTOR_VERSION = "siif6"

# ── IShellItemImageFactory 的 ctypes 封装 ────────────────
#
# PyQt5 **没有** QPixmap.fromWinHICON（调它直接 AttributeError）。
# Qt 也不暴露任何 HICON/HBITMAP → QPixmap 的入口。所以在 ctypes 里
# 完整走一遍 COM：SHCreateItemFromParsingName 拿 IShellItem，
# QueryInterface 成 IShellItemImageFactory，vtable[3] 是 GetImage，
# 拿到 HBITMAP 后用 GetDIBits 抠成 32bpp BGRA 再喂给 QImage。

_SH = ctypes.WinDLL("shell32")
_GDI = ctypes.WinDLL("gdi32")


class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_uint32), ("d2", ctypes.c_uint16),
                ("d3", ctypes.c_uint16), ("d4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    # bytes_le 才是 Windows 内存里 GUID 的字节序；用 str 拼会得到
    # 一个恰好错位的 IID，表现为 hr=0x800401f0（E_NOINTERFACE），
    # 而且对**所有** IID 都失败 —— 这种「整齐地全失败」第一反应
    # 应该是 GUID 布局错了，不是 COM 坏了。
    return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


_IID_SHELL_ITEM_IMAGE_FACTORY = "{BCC18B79-BA16-442F-80C4-8A59C30C463B}"


class _SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class _BITMAP(ctypes.Structure):
    _fields_ = [("bmType", wintypes.DWORD), ("bmWidth", ctypes.c_long),
                ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                ("bmBits", ctypes.c_void_p)]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


_SH.SHCreateItemFromParsingName.argtypes = [
    wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_void_p)]
_SH.SHCreateItemFromParsingName.restype = ctypes.c_long
_GDI.GetObjectW.argtypes = [wintypes.HGDIOBJ, ctypes.c_int, ctypes.c_void_p]
_GDI.GetObjectW.restype = wintypes.HGDIOBJ
_GDI.CreateCompatibleDC.argtypes = [wintypes.HDC]
_GDI.CreateCompatibleDC.restype = wintypes.HDC
_GDI.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_GDI.SelectObject.restype = wintypes.HGDIOBJ
_GDI.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                           wintypes.UINT, ctypes.c_void_p,
                           ctypes.POINTER(_BITMAPINFO), wintypes.UINT]
_GDI.GetDIBits.restype = ctypes.c_int
_GDI.DeleteDC.argtypes = [wintypes.HDC]
_GDI.DeleteObject.argtypes = [wintypes.HGDIOBJ]


def _vmethod(ppv, index, restype, *argtypes):
    """取 COM 对象 vtable 的第 index 个方法并绑好签名。"""
    vtbl = ctypes.cast(ppv, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vtbl[index])


def co_initialize() -> None:
    """
    初始化当前线程的 COM 单元。**必须**在每个要用 SIIF 的线程里调用。

    主线程通常被 Qt 初始化过（QApplication 会做），后台线程不会。
    实测：不调用时 122 个图标全部返回 0（E_FAIL）；调用后全部成功。
    """
    try:
        import pythoncom
    except ImportError:
        return
    try:
        pythoncom.CoInitialize()
    except Exception:
        # RPC_E_CHANGED_MODE：线程已被初始化成另一种单元模式。
        # 这不算失败，COM 在该线程里已经可用了。
        pass


def _hbitmap_to_image(hbm, to_image) -> object:
    """
    HBITMAP → 32bpp ARGB 图像。

    必须走 CreateCompatibleDC + SelectObject 再 GetDIBits：直接拿
    GetDC(NULL) 调 GetDIBits 会返回 0（DIB 还没选中），
    这是个很容易踩的静默失败点。
    biHeight 传负值拿到 top-down 行序，省掉一次翻转。
    """
    bm = _BITMAP()
    if not _GDI.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm)):
        return None
    w, h = bm.bmWidth, bm.bmHeight
    # 老图标（1/4/8/24bpp）没有 alpha 通道，走这条路只会得到一张
    # 抠不出透明背景的图。直接放弃，交给降级链。
    if bm.bmBitsPixel != 32 or w <= 0 or h <= 0:
        return None

    bmi = _BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h          # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0      # BI_RGB
    stride = ((w * 32 + 31) // 32) * 4

    buf = ctypes.create_string_buffer(stride * h)
    hdc = _GDI.CreateCompatibleDC(None)
    old = _GDI.SelectObject(hdc, hbm)
    try:
        n = _GDI.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bmi), 0)
    finally:
        _GDI.SelectObject(hdc, old)
        _GDI.DeleteDC(hdc)
    if n == 0:
        return None
    return to_image(bytes(buf.raw), w, h, stride)


def shell_image(path: str, size: int, to_image):
    """
    通过 IShellItemImageFactory 取 Shell 认为 path 该有的图标。

    返回 (hr, image)。hr != 0 或 image 为 None 表示这条路走不通，
    调用方负责降级。**HBITMAP 在这里就被 DeleteObject 掉了** ——
    漏掉的话每个图标泄漏一个 GDI 对象，一百多个图标下来
    进程的 GDI handle 会明显上涨。
    """
    ppv = ctypes.c_void_p()
    hr = _SH.SHCreateItemFromParsingName(
        path, None, ctypes.byref(_guid(_IID_SHELL_ITEM_IMAGE_FACTORY)),
        ctypes.byref(ppv))
    if hr != 0 or not ppv:
        return hr, None

    get_image = _vmethod(ppv, 3, ctypes.c_long, _SIZE, wintypes.UINT,
                         ctypes.POINTER(ctypes.c_void_p))
    hbm = ctypes.c_void_p()
    # flags 传 0。SIIGBF_RESIZETOFIT 本身就是 0，所以这等于「让 Shell
    # 按我给的尺寸自己排」；实测请求多少就返回多少，256 时是真 256 而不是
    # 放大（把 128 的结果和 256 的结果对比，像素不同）。
    # 反直觉的是传 SIIGBF_BIGGERSIZEOK|SIIGBF_ICONONLY（0x3）反而
    # **一律** E_FAIL（0x80004005）—— 组合这两个标志在本机是坏的。
    hr2 = get_image(ppv, _SIZE(size, size), wintypes.UINT(0),
                    ctypes.byref(hbm))
    try:
        if hr2 != 0 or not hbm:
            return hr2, None
        return hr2, _hbitmap_to_image(hbm.value, to_image)
    finally:
        if hbm:
            _GDI.DeleteObject(hbm)
        # 释放 COM 对象（vtable[2] = Release）。漏掉的话每提取一个图标
        # 泄漏一个 IShellItem，122 个下来就是 122 个 COM 引用。
        _vmethod(ppv, 2, ctypes.c_ulong)(ppv)


def _to_qimage(data: bytes, w: int, h: int, stride: int) -> QImage:
    # .copy() 是必须的：QImage(data, ...) 只是借用那块 ctypes 缓冲区，
    # 函数返回后缓冲区就没了，不 copy 会得到随机内容或直接段错误。
    return QImage(data, w, h, stride, QImage.Format_ARGB32).copy()


# 内容占画布不到这个比例，就认为四周是「多余的留白」，需要裁剪放大。
#
# 0.82 而不是 0.75/0.72：实测有 4 个图标（Bh560v2Config Utility /
# CCS Theia / SysConfig / UniFlash）的实心内容最长边只占画布 73%，
# 卡在旧阈值 0.72 与测试要求的 0.75 之间，界面上就是「略小一圈」。
# 既然归一化的目的是「铺满格子」，73% 显然没铺满，该一起处理。
_CROP_FILL = 0.82
# 内容中心偏离画布中心超过这个比例（相对边长）就重居中。
_RECENTER_OFF = 0.06


def _content_box(img: QImage, alpha_thr: int = 128):
    """
    按 alpha 找出**实心内容**的区域，返回 (x, y, w, h)；全透明返回 None。

    ## 阈值为什么是 128 而不是 10

    实测 Cloudflare WARP：提取器返回 256x256，其中
        alpha>8   的像素 bbox = 255x255（占画布 99.2%）
        alpha>32  的像素 bbox = 255x255
        alpha>128 的像素 bbox =  59x27 （占画布  2.4%）
    也就是说图上有一层 alpha 10~30 的**极淡薄雾铺满整张画布**，
    真正的图形本体只有中间 59x27 那一小块。
    用 alpha>10 找内容框会把那层薄雾当成内容，于是 bbox 撑满画布，
    _decorner 判定「已经是满幅图」直接放行 —— 结果界面上就是一个
    只占格子 20% 的小图标，其余全是空的黑色方框。
    这正是用户报的「有的应用缩略图存在问题」。

    ## 为什么要渐进阈值

    有些图标本体就是很淡的（浅灰描边、白色细线）。一刀切 128 会把
    它们整张判成全透明。所以从高往低试，第一个有结果的阈值胜出：
    优先按实心内容裁剪，实在找不到才退到「几乎任何非透明像素」。
    """
    for thr in (alpha_thr, 64, 16, 1):
        box = _box_at(img, thr)
        if box is not None:
            return box
    return None


def _box_at(img: QImage, alpha_thr: int):
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return None
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > alpha_thr:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if maxx < 0:
        return None
    return (minx, miny, maxx - minx + 1, maxy - miny + 1)


#: 判「低质量图标」时，采样步长（每隔几个像素取一点）。
_IS_LOW_COLOR_STEP = 3
#: 量化到每档多少级（0/32/64/.../224）。
_IS_LOW_COLOR_BUCKET = 32
#: 采样时判定「实心」的 alpha 阈值。
_SOLID_ALPHA = 128

#: 量化色彩数的上限。占位图实测 3 个灰阶（96/160/224）；
#: 真实图标里桶数最低的是 4（Vercel），中间没有 1~3 的样本。
_LOW_COLOR_MAX_BUCKETS = 3

#: 认得出来的「安装程序占位图」路径特征。
#:
#: 只匹配 `...\Microsoft\Installer\{GUID}\<任意文件名>`（不带扩展名，
#: 因为 Installer 布局里的图标文件常常就没有扩展名）。
#:
#: **必须同时要求文件没有扩展名**：Office 的
#: `C:\Program Files\Microsoft Office\Root\VFS\Windows\Installer\{GUID}\...`
#: 也在这个目录下（实测库里有 4 条），但那些是**真图标**，
#: 共享 Installer 这个词纯属巧合。按「有没有扩展名」分开，
#: 两类不会互相误伤。
#: 匹配 `[\\/]Microsoft[\\/]Installer[\\/]{GUID}[\\/]文件名`。
#:
#: 拆成两段拼，而不是一整条正则 —— 一整条里 `$` 的位置很容易写错，
#: 而且出错时不报异常、只是**静默地一条都不匹配**（实测踩过：
#: 4 条正例全部返回 False，看起来像「判据不适用」，其实是正则写坏了）。
#: 分段之后每段职责单一，也能单独测。
_DIR_GUID = (r"[\\/]Microsoft[\\/]Installer[\\/]"
             r"\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
             r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}[\\/]")
#: 文件名部分：**不含点**（即无扩展名）且不含路径分隔符。
#: 无扩展名是 Installer 布局的标志。对照组：Office 的
#: `...\Microsoft Office\Root\VFS\Windows\Installer\{GUID}\*.ico`
#: 和 `C:\Windows\Installer\{GUID}\*.dll` 也共享 Installer 这个词，
#: 但那些是**真图标**（本机库里有 5 条），靠「有没有点」分开。
_NAME_NO_EXT = r"[^\\/.]+$"

_INSTALLER_ICON = re.compile(_DIR_GUID + _NAME_NO_EXT, re.IGNORECASE)


def _is_installer_product_icon(path: str) -> bool:
    """
    这个 IconLocation 是不是 MSIX/Installer 布局的「产品图标」？

    命中就说明它**不是应用图标**：对单 exe 应用它只是一块纯灰白渐变。
    本机库里的真实案例是「某 Squirrel 安装的单 exe 应用」——实测它的图标字段正好是这个形状。

    比「看像素猜」可靠得多，所以这是**第一道**筛；像素判据只作为
    兜底（见 `_is_low_color` 的说明，那里记录了为什么它不能单独用）。
    """
    if not path:
        return False
    return bool(_INSTALLER_ICON.search(path))


def _solid_sample(img: QImage) -> tuple[int, int]:
    """
    采样统计：(实心像素数, 总采样数)。

    只看实心像素（alpha >= _SOLID_ALPHA）—— 半透明的边缘抗锯齿、
    铺在画布上的那层薄雾都不算「内容」。这是「图标是不是一整块纯色」
    这个判断的基础。
    """
    if img is None or img.isNull():
        return 0, 0
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return 0, 0
    solid = total = 0
    for y in range(0, h, _IS_LOW_COLOR_STEP):
        for x in range(0, w, _IS_LOW_COLOR_STEP):
            total += 1
            if img.pixelColor(x, y).alpha() >= _SOLID_ALPHA:
                solid += 1
    return solid, total


def _color_buckets(img: QImage) -> int:
    """量化后不同色彩的数量（只看实心像素）。采样，不全扫。"""
    if img is None or img.isNull():
        return 0
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return 0
    b = _IS_LOW_COLOR_BUCKET
    buckets: set[tuple[int, int, int]] = set()
    for y in range(0, h, _IS_LOW_COLOR_STEP):
        for x in range(0, w, _IS_LOW_COLOR_STEP):
            c = img.pixelColor(x, y)
            if c.alpha() < _SOLID_ALPHA:
                continue
            buckets.add((c.red() // b * b, c.green() // b * b,
                         c.blue() // b * b))
    return len(buckets)


def _is_low_color(img: QImage) -> bool:
    """
    这张图是不是「安装程序占位图」那种毫无信息的渐变？

    ## 为什么需要

    MSIX / Store 包装类安装（Squirrel、Installer 布局）的 .lnk 会把
    IconLocation 写成

        %APPDATA%\\Microsoft\\Installer\\{GUID}\\ProductIcon,0

    那是**安装程序的产品图标**，不是应用图标。对单 exe 应用来说它就是
    一张纯灰白渐变 —— 真实案例「某 Squirrel 安装的单 exe 应用」实测：

        走 icon  这条路 -> 量化后仅 **3** 种色彩（96/160/224 三个灰阶）
        走 target 这条路 -> **34** 种色彩（正常彩色图标）

    界面上就是一个说不清是什么的灰白方块。

    ## 判据：量化色彩数 <= 3

    这是**兜底**判据，不是主判据 —— 主判据是
    `_is_installer_product_icon()` 按路径认（可靠、无误伤）。
    这里只负责「路径认不出来、但像素明显不对劲」的情况。

    **不用「是否灰阶」做判据。** 我最初想的是「真图标必然有彩色、
    占位图必然纯灰」，实测 114 个真实图标后发现完全不成立：
    Notion / OBS Studio / Epic Games / 剪映专业版 / DSH Desktop …
    **20 多个都是 100% 纯灰阶**（黑白 logo 是正规设计）。按灰阶判会
    把这批真图标全误杀。

    ## 阈值 3 的来历

    占位图量化后是 3 个灰阶（96/160/224）。120 个真实条目里桶数
    最低的是 **4**（Vercel），1~3 这一档里**一个真实图标都没有**。

    库里确实有 4 个 1 桶的（Bili23 / CanMV / Codex / CCS），
    但它们要么根本没有 icon 候选（icon 字段是 `,0`），要么走的是
    别的来源 —— `kind == "icon"` 这条路径上它们不会出现。

    ## 为什么这里**不能**再加「实心占比」当条件

    试过（siif6 的第一版）：`实心 >= 0.6`。数据上看着很干净 ——
    真实图标的实心占比落在 14%~43% 与 96%~100% 两档，中间空档里
    没有样本，而占位图实测 54%。**但 54% 恰好撞上了 CCS 的真图标**：
    CCS Theia 走 target 拿到的图标实心占比正好也是 54%，被这条规则
    判成占位图。它没有更差的候选可用所以行为没变，但这是靠运气，
    不是靠设计。

    这正是「拿像素猜语义」的极限：两张不同的图可以有一模一样的
    统计特征。所以分工改成：路径识别（有明确答案、无误伤）去管
    「它到底是不是产品图标」，像素判据只保留色彩数这一个真实的
    低信息量指纹。

    代价：一个**纯色方块**（正红/正蓝，1 个桶）仍会被判成占位图。
    这个代价可以接受 —— 合成图实测没有任何 Windows 图标资源长这样，
    而且即使误判，也只是「换用 target 那张图」，而那也是同一个
    程序的正规图标，视觉上并无损失。

    ## 全透明图必须在这里挡掉

    `solid == 0` -> False。全透明图算出来的桶数是 0，本来会被 <= 3
    判成占位图，但「图标根本没取到」和「取到一张占位图」是两回事：
    前者应该让调用方返回失败去降级，不该在这里伪装成「质量差、
    换个候选」，否则 _diagnose 会给出误导性的原因。
    """
    solid, total = _solid_sample(img)
    if solid == 0:
        return False                      # 没取到东西，不是「质量差」
    return _color_buckets(img) <= _LOW_COLOR_MAX_BUCKETS


def _decorner(img: QImage) -> QImage:
    """
    修掉 Qt 的 QFileIconProvider 在「图标资源里没有大尺寸」时的怪输出。

    实测 BH560V~1.ICO 内部最大只有 64px：
      request 48  -> 60x60，内容填满           （正常）
      request 64  -> 256x256，内容只有左上角 60x60   （歪）
    也就是说一旦请求尺寸超过资源里最大的那张，Qt 会**造一个请求大小的
    空白画布、把小图标贴在左上角**，而不是放大它。直接拿去显示，
    界面上就是一个缩在角落的小图标 —— 这正是「有的缩略图有问题」。

    这里把内容裁出来、等比放大回满画布并居中。注意只用**等比**缩放：
    Windows 图标资源里本来就有非正方形的内容（122 个 .lnk 里 39 个，
    最大偏差 22%），强行拉成正方形会变形。
    """
    box = _content_box(img)
    if box is None:
        return img
    x, y, w, h = box
    side = max(w, h)
    fill = side / float(max(img.width(), img.height()))
    dx = abs((x + w / 2.0) - img.width() / 2.0)
    dy = abs((y + h / 2.0) - img.height() / 2.0)
    off = max(dx, dy) / float(max(img.width(), img.height()))
    if fill >= _CROP_FILL and off <= _RECENTER_OFF:
        return img                     # 已经是正常的满幅居中图
    crop = img.copy(x, y, w, h)
    scale = img.width() / float(side)
    scaled = crop.scaled(int(round(side * scale)),
                         int(round(side * scale)),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
    out = QImage(img.size(), QImage.Format_ARGB32)
    out.fill(Qt.transparent)
    ox = (out.width() - scaled.width()) // 2
    oy = (out.height() - scaled.height()) // 2
    p = QPainter(out)
    p.drawImage(ox, oy, scaled)
    p.end()
    return out


_LNK_FACTS_CACHE: dict[str, dict] = {}


def lnk_facts(path: str) -> dict:
    """
    读 .lnk 的关键事实：target / IconLocation / AppUserModelID。

    为什么需要 IShellLink 而不是 WScript.Shell：
    WScript.Shell 对 5 个链接返回 TargetPath="" 与 IconLocation="" ——
    其中 Codex / OpenCode / Visual Studio 是**应用商店链接**（只有
    LinkTargetIDList，flags=0x80081，没有 LinkInfo 段），SIIF 能从
    这些链接拿到真实的应用图标；而「某停止脚本」是真的什么都
    指不到了（.cmd 和 .ico 都被删了），SIIF 只会回一张 Windows 的
    通用空白文档图。两者都返回「一张有效的 256x256 位图」，光看
    返回值分不出来 —— 这正是 isNull() 那个坑的同一个形状。

    判据是 `SIGDN_FILESYSPATH` 取不取得到：
    * 取不到（抛异常）→ 这是**虚拟 shell 项**（应用商店链接），
      Shell 手里有它的应用 logo，SIIF 的结果可信
    * 取得到一个路径 → 用该路径与 IconLocation 的实际存在性判断
      文件系统里的东西是不是真的还在

    注意判据不是「AUMID 里有没有 `!`」：实测 OpenCode.lnk 解析出的是
    `ai.opencode.desktop`、Visual Studio.lnk 是 `VisualStudio.14a9df33`，
    都不带 `!`，但它们同样是商店链接、同样有真图标。按 `!` 判断会把
    它们误判成坏链接（实测：这样会把 OpenCode 和 Visual Studio
    重新打成占位图）。

    结果按路径缓存在进程内：同一进程里同一个 .lnk 只解析一次
    （实测全量 122 个只要 113ms，均值 0.93ms，但没必要重复付）。
    """
    key = os.path.normcase(os.path.abspath(path))
    hit = _LNK_FACTS_CACHE.get(key)
    if hit is not None:
        return hit

    facts = {"target": "", "icon": "", "index": 0, "app_id": "",
             "virtual": False}
    try:
        import pythoncom
        from pywintypes import IID
        from win32com.shell import shell, shellcon
    except ImportError:
        _LNK_FACTS_CACHE[key] = facts
        return facts
    try:
        iid_pf = IID("{0000010b-0000-0000-c000-000000000046}")
        ipf = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER, iid_pf)
        ipf.Load(path)
        sl = ipf.QueryInterface(shell.IID_IShellLinkW)
        try:
            facts["target"] = sl.GetPath(shell.SLGP_RAWPATH)[0] or ""
        except Exception:
            pass
        try:
            ico, idx = sl.GetIconLocation()
            facts["icon"] = ico or ""
            facts["index"] = int(idx)
        except Exception:
            pass
        try:
            idl = sl.GetIDList()
            if idl:
                item = shell.SHCreateItemFromIDList(idl)
                try:
                    fs = item.GetDisplayName(shellcon.SIGDN_FILESYSPATH)
                    if isinstance(fs, str) and fs:
                        facts["target"] = facts["target"] or fs
                    else:
                        facts["virtual"] = True
                except Exception:
                    # 取不到文件系统路径 = 虚拟 shell 项 = 商店应用
                    facts["virtual"] = True
                name = item.GetDisplayName(
                    shellcon.SIGDN_DESKTOPABSOLUTEPARSING)
                facts["app_id"] = name if isinstance(name, str) else ""
        except Exception:
            pass
    except Exception:
        pass

    _LNK_FACTS_CACHE[key] = facts
    return facts


def lnk_has_real_icon(path: str) -> bool:
    """
    这个 .lnk 是否还指得到一个**真实**图标。

    返回 False 表示 SIIF 即使给了位图，也只会是 Windows 的通用空白
    文档图（对「某停止脚本」实测就是如此）—— 那种图看起来像
    成功，实际上是在骗用户，比诚实的字母块更糟。
    """
    f = lnk_facts(path)
    if f.get("virtual"):
        return True                     # 商店应用，Shell 手里有它的 logo
    for p in (f["icon"], f["target"]):
        if p and os.path.isfile(p):
            return True
    return False


class IconCache(QObject):
    """
    条目 -> QIcon 的惰性缓存。磁盘缓存避免每次启动重新提取 100+ 个图标。

    ── 为什么缓存键不是 uid ──
    `Entry.uid` 只由 `target + args` 构成。5 个链接
    （Codex / OpenCode / Visual Studio / Intel® Processor Identification
    Utility / 某 NAS 工具）的 TargetPath **全是空字符串**
    （它们只有 LinkTargetIDList，没有 LinkInfo 段），args 也都是空，
    于是 sha1 之后 uid 完全相同 —— 实测这 5 个条目共用同一个 uid，
    另有 2 组各 2 个条目也撞 uid。

    后果很直接：上一版用 uid 做内存缓存键，这 5 个应用会拿到**同一张**
    图标（谁先取到谁赢），界面上就是「有的应用图标是别的应用的」。
    这很可能就是用户报的「有的缩略图有问题」之一。

    所以这里用 `uid + 签名` 作键：uid 保留可读性和外部 API 兼容性，
    签名把每个候选文件的路径与 (mtime_ns, size) 纳入，于是这 5 个链接
    因为 source_lnk 不同而自然分开。library.py 的 uid 定义不能改
    （不在本任务的改动范围内），在图标层解决就够了。
    """

    ready = pyqtSignal(int)          # 已完成的图标数

    def __init__(self, size: int = 128, parent=None):
        super().__init__(parent)
        self.size = max(16, int(size))
        self._dir = icon_dir()
        # 缓存键 = f"{uid}_{签名}"
        self._mem: dict[str, QIcon] = {}
        # 占位图集合。上一版的教训：占位图是有效 QIcon，isNull() 为 False，
        # 所以「有没有提取成功」只能靠提取阶段的返回值，不能靠 QIcon。
        self._placeholders: set[str] = set()
        # 缓存键 -> 占位原因（中文，直接给界面/日志用）。
        self._reasons: dict[str, str] = {}
        # 缓存键 -> Shell 实际返回的原生边长。用于如实报告「请求 256 但
        # 只有 48」这类情况，而不是假装拿到了 256。
        self._native: dict[str, int] = {}
        # 缓存键 -> 提取来源描述：icon / target / lnk / disk / placeholder。
        self._origin: dict[str, str] = {}
        # uid -> 该 uid 下所有缓存键。查询接口按 uid 查，这里做反查。
        self._by_uid: dict[str, set[str]] = {}
        # QFileIconProvider 走 Qt 的 Shell API 封装，HICON 生命周期由 Qt 管。
        # 必须在 QApplication 之后构造，否则 provider 无效。
        self._provider = QFileIconProvider()
        self._com_ready = False

    # ── 磁盘路径 ──
    def _path(self, key: str) -> Path:
        # 键里已经带签名：换图标或程序更新都会让签名变，于是文件名变，
        # 旧文件不再被命中（也不主动删用户目录里的旧文件）。
        # EXTRACTOR_VERSION 在签名里，提取策略一改老缓存自动作废。
        return self._dir / f"{key}_{CACHE_SIZE}.png"

    def _cache_key(self, entry: Entry, cands: list[tuple[str, str]]) -> str:
        return f"{entry.uid}_{self._signature(entry, cands)}"

    # ── 主入口 ──
    def get(self, entry: Entry) -> QIcon:
        cands = self._candidates(entry)
        key = self._cache_key(entry, cands)
        if key in self._mem:
            return self._mem[key]

        icon = QIcon()
        if cands:
            p = self._path(key)
            if p.is_file():
                icon = QIcon(str(p))
                if not icon.isNull():
                    pm = icon.pixmap(self.size, self.size)
                    if not pm.isNull():
                        self._remember(entry, key, icon, origin="disk")
                        return icon

        if not self._com_ready:
            co_initialize()
            self._com_ready = True

        image, origin, native = self._extract(entry, cands)
        if image is None or image.isNull():
            self._remember(entry, key, self._placeholder(entry.name),
                           origin="placeholder",
                           reason=self._diagnose(entry, cands))
        else:
            self._save(key, image)
            self._remember(entry, key, QIcon(self._fit(image)),
                           origin=origin, native=native or image.width())
        return self._mem[key]

    def _remember(self, entry: Entry, key: str, icon: QIcon, *,
                  origin: str, native: int = 0, reason: str = "") -> None:
        self._mem[key] = icon
        self._origin[key] = origin
        self._by_uid.setdefault(entry.uid, set()).add(key)
        if origin == "placeholder":
            self._placeholders.add(key)
            self._reasons[key] = reason
        if native:
            self._native[key] = native

    # ── 状态查询（给调用方区分占位图/真实图标） ──
    #
    # 都按 uid 查以保持外部接口（grid.py 只用 get()，但自检和界面提示
    # 需要按条目问）。一个 uid 下的多个条目里只要有一个是真图标，
    # 就报 False —— 也就是说「这一组里有几个是字母块」由调用方用
    # placeholder_keys() 去看。
    def _keys_of(self, uid: str) -> list[str]:
        keys = sorted(self._by_uid.get(uid, ()))
        return keys or [uid]

    def is_placeholder(self, uid: str) -> bool:
        """该条目用的是占位图而非真实图标。用于自检和界面提示。"""
        keys = self._keys_of(uid)
        return all(k in self._placeholders for k in keys)

    def reason(self, uid: str) -> str:
        """占位原因；非占位条目返回空串。多个原因用 '；' 连接。"""
        seen = []
        for k in self._keys_of(uid):
            r = self._reasons.get(k, "")
            if r and r not in seen:
                seen.append(r)
        return "；".join(seen)

    def origin(self, uid: str) -> str:
        """图标来源：icon / target / lnk / disk / placeholder。"""
        for k in self._keys_of(uid):
            o = self._origin.get(k)
            if o and o != "placeholder":
                return o
        # 全是占位图（或还没取过）
        for k in self._keys_of(uid):
            if self._origin.get(k) == "placeholder":
                return "placeholder"
        return ""

    def native_size(self, uid: str) -> int:
        """Shell 实际给的原生边长；未知返回 0。"""
        for k in self._keys_of(uid):
            if k in self._native:
                return self._native[k]
        return 0

    def placeholder_keys(self) -> list[str]:
        """所有占位图的缓存键。给自检脚本用。"""
        return sorted(self._placeholders)

    def clear_cache(self) -> int:
        """
        清空磁盘缓存。返回删掉的文件数。

        换图标、批量更新程序之后想让用户立刻看到新图标时用；
        自动失效靠缓存键里的签名就足够了，这个是给「我刚知道之前提过
        bug」用的手动出口。内存缓存一并清掉。

        只删 png 和同名的 .tmp —— 目录里若有别的东西（用户手动放的、
        或未来加的索引文件）一律不动。
        """
        n = 0
        for f in list(self._dir.glob("*.png")) + list(self._dir.glob("*.png.tmp")):
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
        self._mem.clear()
        self._placeholders.clear()
        self._reasons.clear()
        self._native.clear()
        self._origin.clear()
        self._by_uid.clear()
        return n

    # ── 来源候选 ──
    def _candidates(self, entry: Entry) -> list[tuple[str, str]]:
        """
        按可靠性排序的 (种类, 路径) 候选，当场过滤掉不存在的文件。

        注意用 os.path.isfile 而不是 QFileInfo.exists()：后者对 .lnk
        会跳到 target 去 stat，target 没了就说 .lnk 不存在（见模块
        文档第 1 条）。os.stat 不解析快捷方式，才是「这个文件在不在」。

        种类和路径必须成对返回并一起去重：Google Chrome 的 icon 与
        target 是同一个 chrome.exe，若两个列表分别去重再 zip，
        会把「实际取自 target」错标成「取自 icon」。
        """
        # IconLocation 格式是 "path,index"
        raw = (entry.icon or "").split(",")[0].strip().strip('"')
        ordered = [("icon", raw if raw.lower() != "none" else ""),
                   ("target", entry.target or ""),
                   ("lnk", entry.source_lnk or "")]
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for kind, p in ordered:
            if not p or not os.path.isfile(p):
                continue
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((kind, p))
        return out

    def _signature(self, entry: Entry, cands: list[tuple[str, str]]) -> str:
        """
        缓存键的一部分：提取策略版本 + 候选列表 + 每个候选的
        (mtime_ns, size)。

        uid 只由 target+args 构成，所以「用户换了快捷方式图标」或
        「程序在 Program Files 里被更新」都不会改变 uid，缓存因此
        永远命中旧图。把候选文件的 mtime_ns + size 纳入签名即可：
        图标换了，.lnk 或 exe 的 mtime 就变，签名变，文件名变，
        旧文件自然不再被命中（也不会主动删用户目录里的旧文件）。

        只用 mtime 不够稳 —— 有些替换可能 mtime 相同而 size 变化；
        两者都带上就够了，又不需要读文件内容。
        """
        h = hashlib.sha1()
        h.update(EXTRACTOR_VERSION.encode())
        h.update((entry.target or "").lower().encode("utf-8"))
        h.update((entry.icon or "").lower().encode("utf-8"))
        for _, p in cands:
            h.update(p.lower().encode("utf-8"))
            try:
                st = os.stat(p)
                h.update(f"{st.st_mtime_ns}:{st.st_size}".encode())
            except OSError:
                h.update(b"?")
        return h.hexdigest()[:10]

    # ── 从系统中提取 ──
    def _extract(self, entry: Entry, cands: list[tuple[str, str]]) -> tuple:
        """
        返回 (QImage|None, 来源标签, 原生边长)。

        顺序：IconLocation / target / .lnk 本身。SIIF 优先，因为它对
        .lnk 也有效；Qt 的 QFileIconProvider 留作降级（它在 .lnk 上
        只有 40x40，但胜在不需要 COM —— 万一 SIIF 在某个系统上不可用，
        还能拿到一个虽然偏小但正确的图标，而不是直接占位图）。

        原生边长如实上报：有些资源里最大的图标就 48px（kiwix.ico），
        请求 256 也只给 48。报出来是为了让测试能说真话，而不是把
        放大后的 256 记成原生 256。

        ## 为什么要对 IconLocation 做质量筛查

        有一种 IconLocation **根本不是应用图标**：MSIX/Store 包装类安装
        （Squirrel / Installer 布局）会写

            %APPDATA%\\Microsoft\\Installer\\{GUID}\\ProductIcon,0

        那是**安装程序的产品图标**，对单 exe 应用就是一张纯灰白渐变。
        实测「某 Squirrel 安装的单 exe 应用」：从这条路拿到 256x256，但量化后只有 **3 种灰阶**
        （96/160/224），中心一大片 (250,250,250)，烘出来的 PNG 只有
        **1224 字节**（其它条目 14KB~47KB）—— 界面上就是一个说不清
        是什么的灰白方块，正是用户报的「ccswitch 这个失效图标」。

        而同一个 exe 自带的图标是好的（走 target 能拿到正常彩色图标）。
        所以：**IconLocation 提取结果质量不合格时继续试下一个候选**，
        而不是直接返回。

        判据见 `_is_low_color`：一张真正的应用图标几乎必然带彩色，
        纯灰阶 + 大面积近白就是「安装程序占位图」的指纹。
        """
        for kind, path in cands:
            # 只剩 .lnk 可试时，先确认它还指得到真图标。否则 SIIF 会
            # 返回 Windows 的通用空白文档图 —— 那是一张完全合法的
            # 256x256 位图，isNull() 为 False，程序无从察觉，用户
            # 只会看到一个说不清是什么的空白图标。这比字母块更糟：
            # 字母块至少明说「这个我拿不到」。
            if kind == "lnk" and not lnk_has_real_icon(path):
                continue
            hr, img = shell_image(path, CACHE_SIZE, _to_qimage)
            if hr == 0 and img is not None and not img.isNull():
                # SIIF 的输出**要**过 _decorner。
                #
                # 这里原先有一条例外，理由是「SIIF 总是把内容居中排版，
                # 四周留白是图标本身的设计」。那个判断是错的，实测反例：
                #   微信         实心内容占画布 96%   （留白确实是设计）
                #   Cloudflare   实心内容占画布  2.3%  ← 这不是设计，是坏图
                #   CanMV/RustDesk 等 13 个都在 3~6%
                # 它们的真身只有中间 59x27 那一小块，外面铺着一层
                # alpha 10~30 的极淡薄雾。直接拿去显示，界面上就是一个
                # 只占格子 20% 的小图标 + 一圈空黑方框 —— 用户报的
                # 「有的应用缩略图存在问题」就是这批。
                #
                # _decorner 只在实心内容占画布 <72% 时才动手，且按
                # 长边等比放大（保持长宽比），所以本来就满幅的图标
                # （微信、Chrome、VS Code…）一个像素都不会变。
                cand_img = _decorner(img)
                # IconLocation 指向「安装程序产品图标」时拿到的是
                # 纯灰白渐变（见上面 docstring）。这种情况**跳过这个候选
                # 继续试下一个** —— 通常下一个候选（target，也就是
                # 真正的 exe）能给出正常彩色图标。
                #
                # 两道筛，顺序有讲究：
                #   1. **路径**（`_is_installer_product_icon`）—— 主判据。
                #      MSIX/Installer 布局的产品图标**在定义上就不是应用
                #      图标**，形状是固定的，认它不靠猜，所以不会有误伤。
                #      不必先花时间提取像素。
                #   2. **像素**（`_is_low_color`）—— 兜底。路径认不出来
                #      但图明显然没有信息量时才用。
                #
                # 只对 kind == "icon" 筛查：target / lnk 拿到的就是
                # 系统认为该文件的图标，不该被这条规则否掉。
                if kind == "icon":
                    if _is_installer_product_icon(path):
                        continue
                    if _is_low_color(cand_img):
                        continue
                return cand_img, kind, img.width()
            img = self._qt_extract(path)
            if img is not None:
                # 同样两道筛。Qt 这条降级路径尤其要看路径 —— SIIF 对
                # ProductIcon 返回的那块渐变，QFileIconProvider 同样会
                # 原样吐出来。
                if kind == "icon":
                    if _is_installer_product_icon(path):
                        continue
                    if _is_low_color(img):
                        continue
                return img, kind + "+qt", img.width()
        return None, "", 0

    def _qt_extract(self, path: str) -> QImage | None:
        """
        降级路径：QFileIconProvider。

        这里的 QFileInfo 只用来构造查询对象，**不看它的 exists()** ——
        那正是上一版的 bug。文件在不在由调用方的候选列表保证。
        """
        try:
            icon = self._provider.icon(QFileInfo(path))
            if icon.isNull():
                return None
            pm = icon.pixmap(QSize(CACHE_SIZE, CACHE_SIZE))
            if pm.isNull():
                return None
            return _decorner(pm.toImage())
        except Exception:
            return None

    def _diagnose(self, entry: Entry, cands: list[tuple[str, str]]) -> str:
        """
        说清楚为什么只能占位图 —— 界面要能区分「占位图」和「真实图标」，
        光有这个布尔值不够，用户会问「为什么这个是字母块」。

        分类依据只用 os.path 的事实判断，不猜：
        * 商店链接（AUMID 里带 !）而 Shell 仍不给图 —— 系统问题
        * target 与 IconLocation 都指向已删除的文件 —— 这条快捷方式
          真的什么都指不到了（实测：「某停止脚本」的 .cmd 和
          .ico 都被删了）
        * 只有 IconLocation 死了、target 还活着 —— 用户重装即可
        """
        lnk = entry.source_lnk or ""
        facts = lnk_facts(lnk) if lnk and os.path.isfile(lnk) else {}
        app = facts.get("app_id") or ""
        ltarget = facts.get("target") or ""
        lico = facts.get("icon") or ""

        # _extract 会主动跳过「指不到真图标」的 .lnk（见 lnk_has_real_icon）。
        # 这时 cands 非空但结果是占位图，原因必须说清楚，
        # 不能笼统写成「Shell 返回了空图标」—— 那不是事实，
        # Shell 明明给了图，只是我们主动不要。
        if len(cands) == 1 and cands[0][0] == "lnk" and lnk:
            f = lnk_facts(lnk)
            return (f"快捷方式指向的图标与目标都不存在，Shell 只能给出"
                    f"通用空白文档图，故未采用："
                    f"{f.get('icon') or f.get('target') or lnk}")

        if cands:
            return "Shell 对这些来源都返回了空图标：" + "、".join(
                f"{k}:{p}" for k, p in cands)

        raw = (entry.icon or "").split(",")[0].strip().strip('"')
        target = (entry.target or "").strip()
        dead_icon = bool(raw) and not os.path.exists(raw)
        dead_target = bool(target) and not os.path.exists(target)

        if facts.get("virtual"):
            return f"应用商店链接，Shell 未提供图标（{app}）"
        if dead_icon and dead_target:
            return f"IconLocation 与目标都已删除（{raw}；{target}）"
        if dead_icon and lnk and not lnk_has_real_icon(lnk):
            return (f"快捷方式指向的图标与目标都不存在，Shell 只能给出"
                    f"通用空白文档图，故未采用：{lico or ltarget or lnk}")
        if dead_icon:
            return f"IconLocation 指向的文件已删除：{raw}"
        if dead_target:
            return f"目标已删除：{target}"
        if not raw and not target and lnk:
            return ("快捷方式没有 TargetPath，Shell 也未给出图标"
                    + (f"（{app}）" if app else ""))
        return "没有任何可用的图标来源"

    # ── 缩放 ──
    def _fit(self, image: QImage) -> QPixmap:
        """
        统一缩到 self.size。

        这里**不再**调 _decorner：归一化已经放在 _extract 里做了
        （SIIF 与 Qt 降级两条路都在那里归一化），落盘的也是归一化后的图。
        再在这里跑一遍纯属重复 —— _content_box 是逐像素扫，256x256
        约 25ms/张，121 个图标白跑一遍就是 3 秒。

        KeepAspectRatio：绝不做非等比拉伸。Windows 图标资源本身就有
        非正方形的（实测 122 个 .lnk 里 39 个的内容区不是正方形，最大
        偏差 22%），拉成正方形会明显变形。
        """
        if image.width() == self.size and image.height() == self.size:
            return QPixmap.fromImage(image)
        return QPixmap.fromImage(image.scaled(
            self.size, self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ── 占位图标 ──
    def _placeholder(self, name: str) -> QIcon:
        """取不到图标时画一个带首字符的圆角方块，总比空白好。"""
        pm = QPixmap(self.size, self.size)
        pm.fill(Qt.transparent)

        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)

        grad = QLinearGradient(0, 0, self.size, self.size)
        grad.setColorAt(0.0, QColor(88, 96, 112))
        grad.setColorAt(1.0, QColor(52, 58, 70))
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        r = self.size // 6
        p.drawRoundedRect(QRect(0, 0, self.size, self.size), r, r)

        ch = (name.strip() or "?")[0].upper()
        f = QFont("Segoe UI", int(self.size * 0.42))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(238, 242, 248))
        p.drawText(pm.rect(), Qt.AlignCenter, ch)
        p.end()

        return QIcon(pm)

    # ── 落盘 ──
    def _save(self, key: str, image: QImage) -> None:
        """
        把 CACHE_SIZE 的图写成 PNG。任何失败只打印，不抛。

        key 由调用方传入而不是在这里重算：重算要对每个候选做 os.stat，
        白花系统调用，还留了个 TOCTOU 窗口（算键的 stat 与写文件之间
        源文件被换掉，会写出一个键与内容不符的缓存）。
        """
        target = self._path(key)
        try:
            # 目录可能被用户或清理工具删掉。不补建的话每次都写失败，
            # 而写失败不抛异常 —— 结果就是**永远**退化成占位图，
            # 且没有任何界面提示，只能靠翻日志才发现。
            target.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：先写 .tmp 再 os.replace。直接写的话中途崩溃会留下
            # 半张 PNG，下次启动读出来就是花屏 —— 而且花屏是**有效**的
            # QIcon，不会触发任何回退。
            tmp = target.with_suffix(".png.tmp")
            if not image.save(str(tmp), "PNG"):
                print(f"[IconCache] 写入失败 {tmp}")
                return
            os.replace(tmp, target)
        except Exception as exc:
            print(f"[IconCache] 写入失败 {target}: {exc}")

    # ── 预热（后台线程用）──
    def warm(self, entries: list[Entry]) -> None:
        co_initialize()
        self._com_ready = True
        for i, e in enumerate(entries):
            self.get(e)
            if i % 8 == 0:
                self.ready.emit(i)


def warm_in_background(entries: list[Entry], size: int = 128,
                       on_done=None, on_progress=None):
    """
    后台线程提取图标，避免首次启动时界面卡住。

    worker 用自己的 IconCache，不与主线程共享内存缓存：跨线程共享
    QIcon/QPixmap 在 Qt 里不安全（QPixmap 绑定 GUI 线程），
    磁盘缓存已经让第二次启动无需提取，所以两份内存缓存只是几 MB。
    """
    from PyQt5.QtCore import QThread

    class _Worker(QThread):
        def run(self_inner):
            # 必须在 worker 里初始化 COM：QApplication 只初始化主线程，
            # 不初始化的话这里 122 个图标会全部提取失败（实测 hr=E_FAIL），
            # 然后界面上一片字母方块 —— 正是这一版要根治的现象。
            co_initialize()
            cache = IconCache(size)
            for i, e in enumerate(entries):
                cache.get(e)
                if on_progress and i % 8 == 0:
                    on_progress(i)
            if on_done:
                on_done()

    w = _Worker()
    w.start()
    return w