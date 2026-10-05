#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
McLink 图标生成器（纯标准库，不依赖 Pillow）
============================================
McLink 的窗口图标、任务栏图标、快捷方式图标都由这里现算出来，
避免往仓库里塞二进制资源，也避免引入 Pillow 这种大依赖。

- `png_bytes(size)`  -> PNG 字节，喂给 tkinter.PhotoImage
- `ico_bytes(size)`  -> Vista+ 的 PNG-in-ICO 容器，给快捷方式 / 启动器用

图形：圆角方块 + 薄荷绿→天蓝渐变 + 两个白色箭头（表示"端口转发"）。
"""

from __future__ import annotations

import struct
import zlib

# 与 Web 控制台保持一致的配色
C_FROM = (0x3d, 0xdc, 0x97)      # 薄荷绿
C_TO = (0x35, 0xb6, 0xff)        # 天蓝
C_GLYPH = (0xff, 0xff, 0xff)


# ---------------------------------------------------------------- PNG 容器

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    raw = b"".join(b"\x00" + r for r in rows)          # 每行前置 filter=0
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, 9))
            + _chunk(b"IEND", b""))


def _bmp_image(size: int, rows: list[bytes]) -> bytes:
    """把 RGBA 行转成经典 ICO 里的 32 位 DIB（BITMAPINFOHEADER + XOR + AND）。

    注意：csc.exe（编译启动器用的）只认这种经典格式，
    不认 Vista 之后流行的 "PNG 塞进 ICO" 写法，所以这里不能用 PNG。
    """
    # BITMAPINFOHEADER：高度写 2*h，因为后面跟着 XOR 位图和 AND 掩码
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         size * size * 4, 0, 0, 0, 0)
    xor = bytearray()
    for y in range(size - 1, -1, -1):          # DIB 是自下而上存的
        row = rows[y]
        for x in range(size):
            r, g, b, a = row[x * 4], row[x * 4 + 1], row[x * 4 + 2], row[x * 4 + 3]
            xor += bytes((b, g, r, a))         # BGRA
    stride = ((size + 31) // 32) * 4           # AND 掩码每行按 4 字节对齐
    and_mask = b"\x00" * (stride * size)       # 透明度交给 alpha 通道
    return header + bytes(xor) + and_mask


def ico_bytes(sizes=(16, 32, 48, 256)) -> bytes:
    """生成多尺寸经典 ICO，供启动器 / 快捷方式 / 托盘使用。

    注意：ICONDIR 和 ICONDIRENTRY 是 **小端序**（Windows 结构体一律小端），
    而里面的 DIB 也是小端——整个文件从头到尾都是小端。
    """
    images = [(s, _bmp_image(s, _render(s, ss=3 if s <= 64 else 2))) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return out + entries + blobs


# ---------------------------------------------------------------- 几何

def _rounded_rect_inside(x: float, y: float, r: float) -> bool:
    """点是否落在 [0,1]² 的圆角正方形内，圆角半径 r（归一化）。"""
    if x < 0 or x > 1 or y < 0 or y > 1:
        return False
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    if x == cx or y == cy:
        return True
    dx, dy = x - cx, y - cy
    return dx * dx + dy * dy <= r * r


def _tri_inside(px: float, py: float, a, b, c) -> bool:
    def sign(p1, p2, p3):
        return (p1[0] - p3[0]) * (p2[1] - p3[1]) - (p2[0] - p3[0]) * (p1[1] - p3[1])
    d1, d2, d3 = sign((px, py), a, b), sign((px, py), b, c), sign((px, py), c, a)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def _rect_inside(px: float, py: float, x1: float, y1: float, x2: float, y2: float) -> bool:
    return x1 <= px <= x2 and y1 <= py <= y2


# 两个箭头：上面指向右，下面指向左（归一化坐标）
_ARROWS = [
    # 右箭头：箭杆 + 三角形箭头
    ("rect", 0.20, 0.335, 0.615, 0.395),
    ("tri", (0.575, 0.275), (0.815, 0.365), (0.575, 0.455)),
    # 左箭头
    ("rect", 0.385, 0.605, 0.80, 0.665),
    ("tri", (0.425, 0.545), (0.185, 0.635), (0.425, 0.725)),
]


def _glyph_inside(x: float, y: float) -> bool:
    for shape in _ARROWS:
        if shape[0] == "rect":
            if _rect_inside(x, y, shape[1], shape[2], shape[3], shape[4]):
                return True
        else:
            if _tri_inside(x, y, shape[1], shape[2], shape[3]):
                return True
    return False


# ---------------------------------------------------------------- 渲染

def _render(size: int, ss: int = 3) -> list[bytes]:
    """用 ss×ss 超采样做抗锯齿，返回 RGBA 行列表。"""
    r = 0.235
    rows: list[bytes] = []
    inv = 1.0 / (size * ss)
    n = ss * ss
    for py in range(size):
        row = bytearray()
        for px in range(size):
            bg_a = 0.0
            glyph_a = 0.0
            for sy in range(ss):
                for sx in range(ss):
                    fx = (px * ss + sx + 0.5) * inv
                    fy = (py * ss + sy + 0.5) * inv
                    if _rounded_rect_inside(fx, fy, r):
                        bg_a += 1.0
                        if _glyph_inside(fx, fy):
                            glyph_a += 1.0
            bg_a /= n
            glyph_a /= n
            if bg_a <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            # 渐变按 y 走
            t = (py + 0.5) / size
            br = C_FROM[0] + (C_TO[0] - C_FROM[0]) * t
            bg = C_FROM[1] + (C_TO[1] - C_FROM[1]) * t
            bb = C_FROM[2] + (C_TO[2] - C_FROM[2]) * t
            # 白色箭头按比例混进去
            g = glyph_a / bg_a if bg_a > 0 else 0.0
            rr = br * (1 - g) + C_GLYPH[0] * g
            gg = bg * (1 - g) + C_GLYPH[1] * g
            bl = bb * (1 - g) + C_GLYPH[2] * g
            row += bytes((int(rr + 0.5), int(gg + 0.5), int(bl + 0.5),
                          int(bg_a * 255 + 0.5)))
        rows.append(bytes(row))
    return rows


_cache: dict[int, bytes] = {}


def png_bytes(size: int = 64) -> bytes:
    """生成 size×size 的 PNG（带缓存）。"""
    if size not in _cache:
        _cache[size] = _png(size, size, _render(size))
    return _cache[size]


# ---------------------------------------------------------------- CLI

def main() -> None:
    import argparse
    import os

    ap = argparse.ArgumentParser(description="生成 McLink 图标")
    ap.add_argument("-o", "--out-dir", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "assets"))
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    targets = {
        "mclink.ico": ico_bytes(),
        "mclink-256.png": png_bytes(256),
        "mclink-64.png": png_bytes(64),
        "mclink-32.png": png_bytes(32),
    }
    for name, data in targets.items():
        path = os.path.join(args.out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"  {name:<18} {len(data):>7} 字节")


if __name__ == "__main__":
    main()
