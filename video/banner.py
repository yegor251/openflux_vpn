#!/usr/bin/env python3
"""Animated watermark banner for the clips, generated frame by frame.

Design (mine, admin approved by asking for it): a dark rounded pill with
a hue-cycling border, a pulsing glow, five bouncing equalizer bars and
the brand wordmark + optional part label ("ЧАСТЬ 2/12"). Renders to a
looping WebM with alpha, overlaid on clips by clipper/build.

    python3 banner.py preview            # banner.webm in video/out
    python3 banner.py frames --dir /tmp  # raw PNG frames
"""

from __future__ import annotations

import argparse
import math
import subprocess
from pathlib import Path

W, H = 380, 96
FRAMES = 24  # 1 s loop at 24 fps
ACCENTS = [(124, 92, 255), (60, 190, 255), (41, 211, 255), (124, 92, 255), (180, 92, 255)]

# bottom-band mode: a large lower-third strip (video content stays visible above)
BAND_W, BAND_H = 1080, 360



def _rounded(draw, box, radius, fill=None, outline=None, width=1):
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _hue_frame_color(i: int, frame: int, total: int):
    """Border color cycling through the accent palette."""
    a, b = ACCENTS[i % len(ACCENTS)], ACCENTS[(i + 1) % len(ACCENTS)]
    t = (math.sin(2 * math.pi * frame / total) + 1) / 2
    return tuple(int(a[k] + (b[k] - a[k]) * t) for k in range(3))


def render_frames(out_dir: Path, brand: str = "OPENFLUX", part_label: str = "") -> list[Path]:
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    font_path = "/System/Library/Fonts/Helvetica.ttc"
    font = ImageFont.truetype(font_path, 34) if Path(font_path).exists() else ImageFont.load_default()
    small = ImageFont.truetype(font_path, 24) if Path(font_path).exists() else ImageFont.load_default()

    paths = []
    for frame in range(FRAMES):
        phase = 2 * math.pi * frame / FRAMES
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # pulsing glow: a soft halo that breathes around the pill
        glow = int(10 + 6 * math.sin(phase))
        _rounded(draw, (-glow // 2, -glow // 2, W + glow // 2, H + glow // 2), 48 + glow // 2,
                 fill=(41, 120, 255, 26 + int(18 * math.sin(phase))))

        # pill body + cycling border
        _rounded(draw, (0, 0, W - 1, H - 1), 44, fill=(16, 18, 26, 215))
        steps = 26
        for s in range(steps):
            x0 = s * (W - 1) // steps
            x1 = (s + 1) * (W - 1) // steps
            draw.line([(x0, 0), (x1, 0)], fill=_hue_frame_color(s, frame, FRAMES) + (255,))
            draw.line([(x0, H - 1), (x1, H - 1)], fill=_hue_frame_color(s + steps // 2, frame, FRAMES) + (255,))

        # equalizer bars (the "alive" part)
        bar_w, base_x, base_y = 7, 26, H - 26
        for i in range(5):
            hgt = int(18 + 16 * (0.5 + 0.5 * math.sin(phase * 2 + i * 1.1)))
            draw.rounded_rectangle(
                (base_x + i * (bar_w + 5), base_y - hgt, base_x + i * (bar_w + 5) + bar_w, base_y),
                radius=3, fill=ACCENTS[i] + (255,),
            )

        # wordmark + optional part label
        draw.text((base_x + 5 * (bar_w + 5) + 12, H // 2 - 22), brand, font=font, fill=(244, 244, 248, 255))
        if part_label:
            tw = draw.textlength(part_label, font=small)
            draw.text((W - tw - 18, H // 2 - 36), part_label, font=small, fill=(41, 211, 255, 235))

        path = out_dir / f"banner_{frame:02d}.png"
        img.save(path)
        paths.append(path)
    return paths


def render_frames_band(out_dir: Path, brand: str = "OPENFLUX", part_label: str = "") -> list[Path]:
    """Large lower-third band: dark strip, sweeping gradient on the top edge,
    big equalizer, wordmark + part label. Scaled 1:1 onto 1080-wide verticals."""
    from PIL import Image, ImageDraw, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    font_path = "/System/Library/Fonts/Helvetica.ttc"
    brand_font = ImageFont.truetype(font_path, 96)
    part_font = ImageFont.truetype(font_path, 56)
    paths = []
    for frame in range(FRAMES):
        phase = 2 * math.pi * frame / FRAMES
        img = Image.new("RGBA", (BAND_W, BAND_H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # body: dark strip with soft breathing glow along the top
        draw.rectangle((0, 6, BAND_W, BAND_H), fill=(13, 15, 23, 228))
        glow = int(8 + 5 * math.sin(phase))
        draw.rectangle((0, 6 - glow // 2, BAND_W, 10), fill=(41, 120, 255, 40))

        # sweeping gradient on the top edge (the "gif" element)
        seg = BAND_W // 5
        sweep_start = int((math.sin(phase * 0.9) * 0.5 + 0.5) * (BAND_W - seg))
        for s in range(6):
            x0 = sweep_start + s * seg // 6
            x1 = sweep_start + (s + 1) * seg // 6
            color = _hue_frame_color(s, frame, FRAMES)
            draw.rectangle((max(0, x0), 0, min(BAND_W, x1), 6), fill=color + (255,))

        # equalizer: 9 bars bouncing on the left block
        bar_w, gap, base_x, base_y = 22, 14, 56, BAND_H - 60
        for i in range(9):
            hgt = int(70 + 90 * (0.5 + 0.5 * math.sin(phase * 2 + i * 0.9)))
            draw.rounded_rectangle(
                (base_x + i * (bar_w + gap), base_y - hgt, base_x + i * (bar_w + gap) + bar_w, base_y),
                radius=8, fill=ACCENTS[i % len(ACCENTS)] + (255,),
            )

        # wordmark + part label
        draw.text((base_x + 9 * (bar_w + gap) + 30, BAND_H // 2 - 78), brand,
                  font=brand_font, fill=(244, 244, 248, 255))
        if part_label:
            tw = draw.textlength(part_label, font=part_font)
            draw.text((BAND_W - tw - 40, BAND_H // 2 - 40), part_label,
                      font=part_font, fill=(41, 211, 255, 240))

        path = out_dir / f"banner_{frame:02d}.png"
        img.save(path)
        paths.append(path)
    return paths


def render_webm_band(out_path: Path, brand: str = "OPENFLUX", part_label: str = "") -> Path:
    tmp = out_path.parent / f"banner-band-frames-{out_path.stem}"
    render_frames_band(tmp, brand, part_label)
    proc = subprocess.run([
        "ffmpeg", "-y", "-framerate", "24",
        "-i", str(tmp / "banner_%02d.png"),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "30",
        "-an", str(out_path),
    ], capture_output=True, text=True)
    for p in tmp.glob("*.png"):
        p.unlink()
    tmp.rmdir()
    if proc.returncode != 0:
        raise RuntimeError(f"banner band failed: {proc.stderr[-400:]}")
    return out_path


def render_webm(out_path: Path, brand: str = "OPENFLUX", part_label: str = "") -> Path:
    """Frames -> looping WebM with alpha (overlaid by ffmpeg later)."""
    tmp = out_path.parent / f"banner-frames-{out_path.stem}"
    render_frames(tmp, brand, part_label)
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import ffmpeg_bin

    cmd = [
        ffmpeg_bin(), "-y", "-framerate", "24",
        "-i", str(tmp / "banner_%02d.png"),
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p", "-b:v", "0", "-crf", "34",
        "-an", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    for p in tmp.glob("*.png"):
        p.unlink()
    tmp.rmdir()
    if proc.returncode != 0:
        raise RuntimeError(f"banner webm failed: {proc.stderr[-400:]}")
    return out_path


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("what", choices=["preview", "frames"], nargs="?", default="preview")
    ap.add_argument("--brand", default="OPENFLUX")
    ap.add_argument("--part", default="", help='метка вида "ЧАСТЬ 2/12"')
    args = ap.parse_args()
    out = Path(__file__).resolve().parent / "out"
    out.mkdir(exist_ok=True)
    if args.what == "preview":
        path = render_webm(out / "banner.webm", args.brand, args.part)
        print(f"banner: {path} — проверить: ffplay {path}")
    else:
        for p in render_frames(out / "banner-frames", args.brand, args.part):
            pass
        print(f"frames: {out / 'banner-frames'}/banner_00..{FRAMES - 1}.png")
