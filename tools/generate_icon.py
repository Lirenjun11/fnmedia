#!/usr/bin/env python3
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FPK = ROOT / "packaging" / "fpk"
IMAGES = FPK / "app" / "ui" / "images"
WEB_ICON = ROOT / "web" / "public" / "app-icon.png"

BG_A = (14, 34, 48)
BG_B = (26, 62, 92)
FG_A = (94, 234, 212)
FG_B = (171, 245, 214)


def clamp(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def cover(signed_distance: float, aa: float) -> float:
    return clamp(0.5 - signed_distance / aa)


def sd_round_rect(x: float, y: float, cx: float, cy: float,
                  hw: float, hh: float, r: float) -> float:
    qx = abs(x - cx) - (hw - r)
    qy = abs(y - cy) - (hh - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


def rect_cov(x: float, y: float, box: tuple, radius: float, aa: float) -> float:
    x0, y0, x1, y1 = box
    return cover(sd_round_rect(x, y, (x0 + x1) / 2, (y0 + y1) / 2,
                               (x1 - x0) / 2, (y1 - y0) / 2, radius), aa)


def lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def render(size: int, supersample: int) -> bytes:
    w = size * supersample
    aa = 1.0 / w
    buf = bytearray(w * w * 4)

    stem = (0.335, 0.225, 0.455, 0.775)
    top = (0.335, 0.225, 0.700, 0.345)
    mid = (0.335, 0.455, 0.620, 0.565)
    ring_c = (0.735, 0.735)
    ring_out, ring_in = 0.088, 0.052

    for py in range(w):
        ny = (py + 0.5) / w
        row = py * w * 4
        for px in range(w):
            nx = (px + 0.5) / w
            t = clamp((nx * 0.55 + ny * 0.45))
            bg = lerp(BG_A, BG_B, t)
            fg = lerp(FG_A, FG_B, t)
            sheen = 0.055 * math.exp(-((nx + ny - 1.0) ** 2) / 0.018)
            bg = tuple(min(255.0, c + sheen * 255.0) for c in bg)

            bg_cov = cover(sd_round_rect(nx, ny, 0.5, 0.5, 0.475, 0.475, 0.115), aa)
            f_cov = max(rect_cov(nx, ny, stem, 0.018, aa),
                        rect_cov(nx, ny, top, 0.018, aa),
                        rect_cov(nx, ny, mid, 0.018, aa))
            d = math.hypot(nx - ring_c[0], ny - ring_c[1])
            r_cov = max(0.0, cover(d - ring_out, aa) - cover(d - ring_in, aa))
            mark = max(f_cov, r_cov)

            color = tuple(bg[i] * (1.0 - mark) + fg[i] * mark for i in range(3))
            offset = row + px * 4
            buf[offset] = int(color[0])
            buf[offset + 1] = int(color[1])
            buf[offset + 2] = int(color[2])
            buf[offset + 3] = int(bg_cov * 255)

    if supersample == 1:
        return bytes(buf)

    out = bytearray(size * size * 4)
    block = supersample * supersample
    for y in range(size):
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(supersample):
                base = ((y * supersample + dy) * w + x * supersample) * 4
                for dx in range(supersample):
                    o = base + dx * 4
                    for k in range(4):
                        acc[k] += buf[o + k]
            o = (y * size + x) * 4
            out[o:o + 4] = bytes(int(v / block) for v in acc)
    return bytes(out)


def chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int, rgba: bytes) -> None:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]
    blob = (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)


def main() -> None:
    targets = [
        (512, 1, [FPK / "ICON.PNG", FPK / "ICON_256.PNG"]),
        (256, 2, [IMAGES / "icon_256.png", IMAGES / "icon_1.png", WEB_ICON]),
        (128, 2, [IMAGES / "icon_128.png"]),
        (64, 2, [IMAGES / "icon_64.png", IMAGES / "icon_0.png"]),
        (48, 2, [IMAGES / "icon_48.png"]),
        (32, 2, [IMAGES / "icon_32.png"]),
        (16, 2, [IMAGES / "icon_16.png"]),
    ]
    for size, ss, paths in targets:
        data = render(size, ss)
        for path in paths:
            write_png(path, size, data)
            print(f"generated {path.relative_to(ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
