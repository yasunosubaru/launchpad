# -*- coding: utf-8 -*-
"""
生成 Launchpad 的应用图标（.ico + 各尺寸 .png）。

跑法::

    python make_icon.py

产物在 ``assets/`` 下：``launchpad.ico``（7 档）+ ``icon-{size}.png``
+ ``_contact_sheet.png``（各档对照图）。

## 为什么用 QPainter 参数化绘制，不用 AI 生成位图

``.ico`` 至少要 16/24/32/48/64/128/256 七档都清晰。位图只有一份原始分辨率，
降采样之后 16px 那一档全是糊的。所以每档都**按目标尺寸单独矢量渲染**，
细节还能随尺寸增减：≤24px 自动简化成 2x2（3x3 在 16px 上必然糊成一团灰）。

## 布局必须从容器反推，不能填魔数

第一版在 16px 那档把格子的 x0 写成 74、格宽算出 107，于是
74+107+26+107 = 314 —— 冲出 256 的画布，右侧的强调色被裁掉半边。
魔数在某一档越界，肉眼在小尺寸上只表现为「右边那块蓝的不完整」，
很容易被当成抗锯齿问题放过去。所以统一走 ``_grid_cells()``。

配色取自 ``launchpad/theme.py``：近黑底 + ``#2b6cb0`` 强调蓝，
保证图标和界面是同一套视觉语言。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PyQt5.QtCore import QBuffer, QByteArray, QPointF, QRectF, Qt
from PyQt5.QtGui import (QBrush, QColor, QImage, QLinearGradient, QPainter,
                         QPainterPath, QPen, QRadialGradient)
from PyQt5.QtWidgets import QApplication

OUT = Path(__file__).resolve().parent / "assets"

#: .ico 里放的尺寸档位。Windows 会挑最接近的显示，但把这几档都放进去，
#: 资源管理器 / 任务栏 / Alt-Tab 就都不需要临时缩放。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

# ── 配色（与 launchpad/theme.py 一致）────────────────────
BG_TOP = QColor(0x1C, 0x20, 0x28)
BG_BOTTOM = QColor(0x05, 0x06, 0x08)
EDGE = QColor(0x2E, 0x34, 0x40)
ACCENT = QColor(0x2B, 0x6C, 0xB0)
ACCENT_WARM = QColor(0x4A, 0x9E, 0xD8)
TILE = QColor(0xE8, 0xEC, 0xF2)
TILE_DIM = QColor(0x5A, 0x62, 0x70)
DOT = QColor(0x8A, 0x93, 0xA3)


def _grid_cells(x: float, y: float, span: float, gap: float, n: int):
    """
    在长度 ``span`` 的区间里居中排 n 列/行。

    返回 ``(起点, 格边长)``。**必须从 span 反推，不能手填起点。**
    第一版在 16px 那档把 x0 写成 74 而格宽是 (240-26)/2 = 107，
    74+107+26+107 = 314 直接冲出画布，右侧的强调色被裁掉半边。
    """
    cell = (span - gap * (n - 1)) / n
    start = x + (span - (cell * n + gap * (n - 1))) / 2.0
    return start, cell


def _center_in(container_start: float, container_span: float,
               inner_span: float) -> float:
    """把长度 ``inner_span`` 的内容在容器里居中，返回它的起点。"""
    return container_start + (container_span - inner_span) / 2.0


def _png_bytes(img: QImage) -> bytes:
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QBuffer.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)


def draw(size: int) -> QImage:
    """画一档图标（256 的坐标系，按 size 缩放）。"""
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)

    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.scale(size / 256.0, size / 256.0)       # 之后一律用 256 坐标系

    radius = 56.0                            # 圆角半径（≈22%）
    plate_rect = QRectF(8, 8, 240, 240)

    # ── 底板：圆角方形 + 垂直渐变 ─────────────────────
    plate = QPainterPath()
    plate.addRoundedRect(plate_rect, radius, radius)
    grad = QLinearGradient(0, 8, 0, 248)
    grad.setColorAt(0.0, BG_TOP)
    grad.setColorAt(1.0, BG_BOTTOM)
    p.fillPath(plate, QBrush(grad))

    # 顶部一道内高光：让底板在浅色桌面上有体积感，而不是一块死黑。
    # 用「一个大椭圆与底板求交」模拟柔和顶光。
    hi = QPainterPath()
    hi.addEllipse(QRectF(-60, -150, 376, 220))
    light = QLinearGradient(0, 8, 0, 150)
    light.setColorAt(0.0, QColor(255, 255, 255, 26))
    light.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(hi.intersected(plate), QBrush(light))

    # 描边。**宽度必须随尺寸放大**：3 单位在 256px 上是 3px，在 16px 上
    # 只有 0.19px —— 抗锯齿会把它摊成一层几乎看不见的灰，底板就和
    # 深色桌面糊成一片。至少保证 1 个物理像素。
    p.setPen(QPen(EDGE, max(3.0, 256.0 / size)))
    p.setBrush(Qt.NoBrush)
    p.drawPath(plate)

    if size <= 24:
        # ── 小尺寸：2x2 ─────────────────────────────
        # 16px 上画 3x3 必然糊成一团灰，2x2 + 一个强调色是还能读出的
        # 最少信息。
        #
        # **必须留边距。** 第一版直接把 plate_rect.width() 当 span 传给
        # _grid_cells，算出格宽 107、起点 8 —— 瓷砖正好贴死底板左右边缘，
        # 一点呼吸空间都没有，16px 上看起来就是「四块颜色糊满整个方块」。
        # 改成和 3x3 共用 grid_span（150）再居中，才有和上面几档一样的
        # 45 单位边距。
        gap = 26.0
        grid_top = 74.0
        grid_span = 150.0
        gx = _center_in(plate_rect.left(), plate_rect.width(), grid_span)
        gx, cell = _grid_cells(gx, grid_top, grid_span, gap, 2)
        gy, cell_y = _grid_cells(grid_top, grid_top, grid_span, gap, 2)
        assert abs(cell - cell_y) < 1e-6, (cell, cell_y)
        for cx, cy, col in ((0, 0, TILE), (1, 0, ACCENT),
                            (0, 1, TILE_DIM), (1, 1, TILE_DIM)):
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(col))
            p.drawRoundedRect(
                QRectF(gx + cx * (cell + gap), gy + cy * (cell + gap),
                       cell, cell),
                cell * 0.30, cell * 0.30)
        p.end()
        return img

    # ── 常规尺寸：3x3 图标网格 ─────────────────────────
    # 底部留出页码点 —— 页码指示器是这个产品的核心交互之一，
    # 放进图标里一眼就能认出是 Launchpad 而不是普通启动器。
    gap = 18.0
    grid_span = 150.0                         # 网格是正方形，两个轴共用这一个数
    grid_top = 58.0
    # 网格在底板里**居中**，宽高都是 grid_span。
    #
    # 第一版横轴传了 plate_rect.width()（240）、纵轴传 grid_span（150），
    # 于是格边长一个 68 一个 38 —— 3x3 变成「横向铺满整块底板、纵向只占
    # 一半」的怪形状。格子尺寸必须两轴一致，所以这里**只**用 grid_span，
    # 居中交给 _center_in。
    gx = _center_in(plate_rect.left(), plate_rect.width(), grid_span)
    gx, cell = _grid_cells(gx, grid_top, grid_span, gap, 3)
    gy, cell_y = _grid_cells(grid_top, grid_top, grid_span, gap, 3)
    assert abs(cell - cell_y) < 1e-6, (cell, cell_y)

    # 中间那格用强调蓝（选中/启动），其余明暗交替
    palette = (
        (TILE_DIM, TILE, TILE_DIM),
        (TILE, ACCENT, TILE),
        (TILE_DIM, TILE, TILE_DIM),
    )
    for row in range(3):
        for col in range(3):
            x = gx + col * (cell + gap)
            y = gy + row * (cell + gap)
            base = palette[row][col]
            p.setPen(Qt.NoPen)
            if base == ACCENT:
                # 强调格给个径向渐变，避免整块死板的纯色
                rg = QRadialGradient(QPointF(x + cell * 0.34,
                                              y + cell * 0.28),
                                     cell * 0.95)
                rg.setColorAt(0.0, ACCENT_WARM)
                rg.setColorAt(1.0, ACCENT.darker(140))
                p.setBrush(QBrush(rg))
            else:
                fg = QLinearGradient(0, y, 0, y + cell)
                fg.setColorAt(0.0, base.lighter(112))
                fg.setColorAt(1.0, base.darker(118))
                p.setBrush(QBrush(fg))
            p.drawRoundedRect(QRectF(x, y, cell, cell),
                              cell * 0.28, cell * 0.28)

    # ── 底部页码点：3 个点，中间是当前页 ────────────────
    # 半径有下限：再小在 32px 上三个点会连成一条灰线（实测过）。
    dot_r = max(11.0, cell * 0.13)
    dot_gap = dot_r * 3.2
    dot_y = (grid_top + grid_span + plate_rect.bottom()) / 2.0
    cx0 = plate_rect.center().x() - dot_gap
    for i in range(3):
        c = QColor(ACCENT if i == 1 else DOT)
        c.setAlpha(255 if i == 1 else 170)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(c))
        p.drawEllipse(QPointF(cx0 + i * dot_gap, dot_y), dot_r, dot_r)

    p.end()
    return img


def build() -> list[Path]:
    from PIL import Image

    OUT.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    frames = []

    for s in ICO_SIZES:
        raw = _png_bytes(draw(s))
        png = OUT / f"icon-{s}.png"
        png.write_bytes(raw)
        written.append(png)
        # 再过一次 Pillow：ICO 容器里放的必须是标准 PNG 编码，
        # 直接塞 Qt 的输出有些解码器会认不出（tEXt/gamma 块的差异）。
        frames.append(Image.open(io.BytesIO(raw)))

    ico = OUT / "launchpad.ico"
    frames[-1].save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES])
    written.append(ico)
    return written


def contact_sheet(path: Path, cols: int = 4) -> Path:
    """
    把各尺寸拼成一张对照图 —— **必须真的看一眼**。
    16px 上糊没糊、强调色还认不认得出来，只能看，不能靠推理。

    **每档画两遍：浅底一遍、深底一遍。** 这个图标是近黑色的，只在深底上
    看会以为没问题，而真实的 Windows 桌面默认是浅色的 —— 深色图标在浅底
    上能不能立住，是个必须用眼睛确认的问题，不能推断。

    放大用 NEAREST：这样看到的方块就是真实像素，能判断糊不糊。
    """
    from PIL import Image
    sizes = sorted(ICO_SIZES)
    imgs = {s: Image.open(OUT / f"icon-{s}.png").convert("RGBA")
            for s in sizes}
    pad, cell = 20, 200
    rowh = cell * 2 + 34                       # 上浅底、下深底
    rows = (len(sizes) + cols - 1) // cols
    sheet = Image.new("RGBA",
                      (cols * (cell + pad) + pad,
                       rows * (rowh + pad) + pad),
                      (0x0E, 0x0F, 0x12, 255))
    for i, s in enumerate(sizes):
        im = imgs[s]
        x = pad + (i % cols) * (cell + pad)
        y = pad + (i // cols) * (rowh + pad)
        big = im.resize((cell, cell), Image.NEAREST)
        # 上：浅底（真实 Windows 桌面最常见的情形）
        light = Image.new("RGBA", (cell, cell), (0xF2, 0xF3, 0xF5, 255))
        light.alpha_composite(big)
        sheet.alpha_composite(light, (x, y))
        # 下：深底（第二显示器 / 深色主题）
        dark = Image.new("RGBA", (cell, cell), (0x1B, 0x1D, 0x22, 255))
        dark.alpha_composite(big)
        sheet.alpha_composite(dark, (x, y + cell + 6))
    sheet.save(path)
    return path


if __name__ == "__main__":
    QApplication(sys.argv)
    files = build()
    print(f"生成 {len(files)} 个文件：")
    for f in files:
        print(f"  {f.name:<20} {f.stat().st_size:>8,} bytes")
    print(f"对照图: {contact_sheet(OUT / '_contact_sheet.png').name}")