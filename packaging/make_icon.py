#!/usr/bin/env python3
"""Generate the app icon (a floppy-disk glyph) as PNG and SVG using only the stdlib.

Usage: python3 packaging/make_icon.py OUT_DIR [--size 256]
Writes OUT_DIR/simple-rom-organiser.png and OUT_DIR/simple-rom-organiser.svg.
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

ICON_NAME = "simple-rom-organiser"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Geometry on a 256-unit canvas: (x0, y0, x1, y1, corner radius, RGBA). Later shapes paint over earlier ones.
Rect = tuple[float, float, float, float, float, tuple[int, int, int, int]]
SHAPES: list[Rect] = [
    (16, 16, 240, 240, 28, (38, 70, 140, 255)),     # disk body (blue)
    (72, 16, 192, 100, 6, (196, 202, 212, 255)),    # metal shutter
    (146, 30, 174, 88, 4, (38, 70, 140, 255)),      # shutter window
    (48, 132, 208, 240, 10, (245, 245, 240, 255)),  # paper label
    (68, 156, 188, 166, 3, (120, 160, 220, 255)),   # label lines
    (68, 182, 188, 192, 3, (120, 160, 220, 255)),
    (68, 208, 150, 218, 3, (120, 160, 220, 255)),
    (24, 210, 38, 228, 2, (20, 40, 90, 255)),       # write-protect notch
]


def _inside(px: float, py: float, rect: Rect) -> bool:
    """True if point lies within the rounded rectangle."""
    x0, y0, x1, y1, r, _ = rect
    if px < x0 or px >= x1 or py < y0 or py >= y1:
        return False
    cx = min(max(px, x0 + r), x1 - r)
    cy = min(max(py, y0 + r), y1 - r)
    return (px - cx) ** 2 + (py - cy) ** 2 <= r * r


def render_rgba(size: int = 256, supersample: int = 3) -> bytes:
    """Rasterise SHAPES to straight-alpha RGBA rows (with PNG filter bytes)."""
    scale = 256.0 / size
    n = supersample
    out = bytearray()
    for y in range(size):
        out.append(0)  # PNG filter type 0 (None) for this scanline
        for x in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(n):
                for sx in range(n):
                    px = (x + (sx + 0.5) / n) * scale
                    py = (y + (sy + 0.5) / n) * scale
                    color = (0, 0, 0, 0)
                    for rect in SHAPES:
                        if _inside(px, py, rect):
                            color = rect[5]
                    a = color[3]
                    acc[0] += color[0] * a
                    acc[1] += color[1] * a
                    acc[2] += color[2] * a
                    acc[3] += a
            total_a = acc[3]
            if total_a:
                out += bytes((acc[0] // total_a, acc[1] // total_a, acc[2] // total_a, total_a // (n * n)))
            else:
                out += b"\x00\x00\x00\x00"
    return bytes(out)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def make_png(size: int = 256) -> bytes:
    """Return a complete PNG file (8-bit RGBA) of the icon."""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(render_rgba(size), 9)
    return PNG_SIGNATURE + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


def make_svg() -> str:
    """Return the same glyph as a scalable SVG document."""
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256">']
    for x0, y0, x1, y1, r, (cr, cg, cb, _a) in SHAPES:
        parts.append(
            f'  <rect x="{x0:g}" y="{y0:g}" width="{x1 - x0:g}" height="{y1 - y0:g}" '
            f'rx="{r:g}" fill="#{cr:02x}{cg:02x}{cb:02x}"/>'
        )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_icons(out_dir: Path, size: int = 256) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / f"{ICON_NAME}.png"
    svg = out_dir / f"{ICON_NAME}.svg"
    png.write_bytes(make_png(size))
    svg.write_text(make_svg(), encoding="utf-8")
    return png, svg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out_dir", type=Path)
    parser.add_argument("--size", type=int, default=256)
    args = parser.parse_args(argv)
    for path in write_icons(args.out_dir, args.size):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
