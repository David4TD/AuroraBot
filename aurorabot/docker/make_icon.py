#!/usr/bin/env python3
"""Generate ``docker/icon.png`` — the AuroraBot container icon.

Kept in-repo (rather than committing an opaque binary blob) so the icon can be
regenerated or tweaked without a design tool. Uses only the standard library:
the image is rendered procedurally at 4x and box-downsampled for antialiasing,
then written as an RGBA PNG by hand via ``zlib`` + ``struct``.

    python docker/make_icon.py            # writes docker/icon.png at 256x256
    python docker/make_icon.py --size 512
"""
from __future__ import annotations

import argparse
import math
import struct
import zlib
from pathlib import Path

SUPERSAMPLE = 4

# Aurora ribbons: (base_y, amplitude, frequency, phase, thickness, rgb, gain)
RIBBONS = [
    (0.74, 0.055, 1.05, 0.4, 0.050, (86, 240, 176), 0.95),   # green
    (0.65, 0.070, 1.30, 2.1, 0.040, (56, 214, 232), 1.00),   # teal
    (0.56, 0.055, 0.90, 4.0, 0.034, (138, 99, 255), 1.00),   # AuroraBot violet
    (0.83, 0.045, 1.55, 5.3, 0.030, (232, 92, 196), 0.70),   # magenta
]

# Monogram strokes: (x0, y0, x1, y1) — the two legs and crossbar of an "A".
MONOGRAM = [
    (0.50, 0.20, 0.335, 0.62),
    (0.50, 0.20, 0.665, 0.62),
    (0.398, 0.485, 0.602, 0.485),
]
MONOGRAM_WIDTH = 0.036

CORNER_RADIUS = 0.22   # fraction of the icon width
BG_TOP = (20, 17, 38)
BG_BOTTOM = (9, 9, 20)
GLOW = (108, 74, 210)


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    if edge1 == edge0:
        return 0.0 if x < edge0 else 1.0
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _rounded_box_alpha(x: float, y: float, feather: float) -> float:
    """Signed-distance coverage for a rounded square spanning [0,1]^2."""
    r = CORNER_RADIUS
    dx = abs(x - 0.5) - (0.5 - r)
    dy = abs(y - 0.5) - (0.5 - r)
    outside = math.hypot(max(dx, 0.0), max(dy, 0.0))
    dist = outside + min(max(dx, dy), 0.0) - r
    return 1.0 - _smoothstep(-feather, feather, dist)


def _segment_distance(px: float, py: float, seg: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = seg
    dx, dy = x1 - x0, y1 - y0
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else ((px - x0) * dx + (py - y0) * dy) / length_sq
    t = min(1.0, max(0.0, t))
    return math.hypot(px - (x0 + t * dx), py - (y0 + t * dy))


def _monogram(x: float, y: float, feather: float) -> tuple[float, float]:
    """Return (core coverage, halo intensity) for the "A" glyph."""
    d = min(_segment_distance(x, y, seg) for seg in MONOGRAM)
    core = 1.0 - _smoothstep(MONOGRAM_WIDTH - feather, MONOGRAM_WIDTH + feather, d)
    halo = math.exp(-((d - MONOGRAM_WIDTH) / 0.055) ** 2) if d > MONOGRAM_WIDTH else 1.0
    return core, halo


def _star_field(x: float, y: float) -> float:
    """Deterministic sparse stars — hashed grid cells, one star per cell."""
    cells = 22
    gx, gy = int(x * cells), int(y * cells)
    total = 0.0
    for ox in (-1, 0, 1):
        for oy in (-1, 0, 1):
            cx, cy = gx + ox, gy + oy
            h = (cx * 374761393 + cy * 668265263) & 0xFFFFFFFF
            h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
            if (h >> 28) > 2:          # ~19% of cells hold a star
                continue
            sx = (cx + ((h >> 8) & 0xFF) / 255.0) / cells
            sy = (cy + ((h >> 16) & 0xFF) / 255.0) / cells
            if sy > 0.52:              # keep the sky above the ribbons
                continue
            radius = 0.0016 + ((h >> 4) & 0x7) / 7.0 * 0.0026
            d = math.hypot(x - sx, y - sy)
            total += math.exp(-(d / radius) ** 2) * (0.45 + ((h >> 24) & 0xF) / 15.0 * 0.55)
    return min(total, 1.0)


def _sample(x: float, y: float, feather: float) -> tuple[float, float, float, float]:
    """Return premultiplied (r, g, b, a) in 0..1 for one sample point."""
    alpha = _rounded_box_alpha(x, y, feather)
    if alpha <= 0.0:
        return 0.0, 0.0, 0.0, 0.0

    # Night-sky gradient with a soft violet glow behind the ribbons.
    r = BG_TOP[0] + (BG_BOTTOM[0] - BG_TOP[0]) * y
    g = BG_TOP[1] + (BG_BOTTOM[1] - BG_TOP[1]) * y
    b = BG_TOP[2] + (BG_BOTTOM[2] - BG_TOP[2]) * y
    glow = math.exp(-(((x - 0.5) / 0.55) ** 2 + ((y - 0.70) / 0.30) ** 2)) * 0.50
    r += GLOW[0] * glow
    g += GLOW[1] * glow
    b += GLOW[2] * glow

    star = _star_field(x, y)
    if star:
        r += 235 * star
        g += 240 * star
        b += 255 * star

    # Aurora curtains: a sine ridge with a gaussian falloff, faded at the edges
    # and broken up by a higher-frequency vertical streak pattern.
    edge = math.sin(math.pi * min(max(x, 0.0), 1.0)) ** 0.6
    for base, amp, freq, phase, thick, colour, gain in RIBBONS:
        ridge = base + amp * math.sin(2.0 * math.pi * freq * x + phase)
        ridge += amp * 0.35 * math.sin(2.0 * math.pi * freq * 2.3 * x + phase * 1.7)
        falloff = math.exp(-((y - ridge) / thick) ** 2)
        if falloff < 0.004:
            continue
        streak = 0.72 + 0.28 * math.sin(2.0 * math.pi * 7.0 * x + phase * 3.1)
        tail = 1.0 - _smoothstep(0.0, 0.30, y - ridge)  # softer below the ridge
        intensity = falloff * edge * streak * gain * (0.55 + 0.45 * tail)
        r += colour[0] * intensity
        g += colour[1] * intensity
        b += colour[2] * intensity

    # "A" monogram: a violet halo lifts it off the sky, a near-white core keeps
    # it legible once Unraid scales the icon down to ~48px.
    core, halo = _monogram(x, y, feather)
    if halo > 0.01:
        r += 96 * halo * 0.9
        g += 64 * halo * 0.9
        b += 190 * halo * 0.9
    if core > 0.0:
        r += (247 - r) * core
        g += (245 - g) * core
        b += (255 - b) * core

    # Vignette so the ribbons don't run flat into the icon border.
    vig = 1.0 - 0.35 * ((x - 0.5) ** 2 + (y - 0.5) ** 2) * 2.2
    r, g, b = r * vig, g * vig, b * vig

    r = min(r, 255.0) / 255.0
    g = min(g, 255.0) / 255.0
    b = min(b, 255.0) / 255.0
    return r * alpha, g * alpha, b * alpha, alpha


def render(size: int) -> bytearray:
    """Render to raw RGBA bytes (``size`` x ``size``), supersampled."""
    s = SUPERSAMPLE
    hi = size * s
    inv = 1.0 / hi
    feather = 1.5 * inv
    weight = 1.0 / (s * s)
    out = bytearray(size * size * 4)

    for oy in range(size):
        acc = [0.0] * (size * 4)
        for sy in range(s):
            y = ((oy * s) + sy + 0.5) * inv
            for ox in range(size):
                base = ox * 4
                for sx in range(s):
                    x = ((ox * s) + sx + 0.5) * inv
                    pr, pg, pb, pa = _sample(x, y, feather)
                    acc[base] += pr
                    acc[base + 1] += pg
                    acc[base + 2] += pb
                    acc[base + 3] += pa
        row = oy * size * 4
        for ox in range(size):
            base = ox * 4
            a = acc[base + 3] * weight
            if a <= 0.0:
                continue
            # Un-premultiply so partially covered edge pixels keep their colour.
            for c in range(3):
                v = (acc[base + c] * weight) / a
                out[row + base + c] = min(255, max(0, round(v * 255)))
            out[row + base + 3] = min(255, max(0, round(a * 255)))
    return out


def write_png(path: Path, pixels: bytearray, size: int) -> None:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render the AuroraBot icon.")
    parser.add_argument("--size", type=int, default=256, help="output edge length in px")
    parser.add_argument(
        "--out", type=Path, default=Path(__file__).with_name("icon.png"), help="output path"
    )
    args = parser.parse_args()

    pixels = render(args.size)
    write_png(args.out, pixels, args.size)
    print(f"Wrote {args.out} ({args.size}x{args.size}, {args.out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
