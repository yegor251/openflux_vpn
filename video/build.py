#!/usr/bin/env python3
"""Build the publishable set from one master recording.

Input : one 16:9 recording (OBS export, any resolution with audio).
Output: outbox/<slug>/
          master_16x9.mp4   normalized master (+ intro/outro cards, subs)
          vertical_9x16.mp4 blur-fill vertical with big burned subs
          cover.png         YouTube thumbnail (1280x720)
          meta.json         title/description/tags for publish.py

Subtitles: --srt file wins; else Whisper (if installed: `pip install
openai-whisper` + ffmpeg) transcribes the voice track; else none.

Usage:
  python3 build.py master.mov --title "Своя VPN-ферма за вечер" \
      --tags vpn,openflux,selfhosted [--srt subs.srt] [--no-cards]
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUTBOX_DIR, ensure_dirs, ffmpeg_bin, file_hash, slugify  # noqa: E402


def die(msg: str) -> None:
    print(f"build: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"{' '.join(cmd[:3])}... failed:\n{proc.stderr[-800:]}")


def need_ffmpeg() -> None:
    if shutil.which(ffmpeg_bin()) is None:
        die("ffmpeg не найден — brew install ffmpeg или статик в video/bin/")


def find_font(size: int) -> str:
    for candidate in (
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNS.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        if Path(candidate).exists():
            return candidate
    from PIL import ImageFont

    return ImageFont.load_default().path or ""


def make_cards(title: str, out_dir: Path, vertical: bool) -> list[Path]:
    """Intro/outro cards: title on a dark gradient; 2 s each, made on demand."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = (1080, 1920) if vertical else (1920, 1080)
    font_path = find_font(72)
    paths = []
    for label in ("→", "Подпишись · ссылка в описании"):
        img = Image.new("RGB", (w, h), (16, 18, 26))
        draw = ImageDraw.Draw(img)
        for y in range(h):  # simple vertical gradient
            shade = 16 + int(30 * y / h)
            draw.line([(0, y), (w, y)], fill=(shade, shade + 2, shade + 8))
        size = int(w * (0.06 if vertical else 0.055))
        try:
            font = ImageFont.truetype(font_path, size * 2) if font_path else None
        except OSError:
            font = None
        text = title if label == "→" else label
        if font:
            bbox = draw.textbbox((0, 0), text, font=font)
            draw.text(((w - bbox[2] + bbox[0]) / 2, h / 2 - size), text, font=font, fill=(240, 240, 245))
        path = out_dir / f"card_{len(paths)}.png"
        img.save(path)
        paths.append(path)
    return paths


def make_cover(title: str, out_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1280, 720), (16, 18, 26))
    draw = ImageDraw.Draw(img)
    for y in range(720):
        shade = 16 + int(60 * y / 720)
        draw.line([(0, y), (1280, y)], fill=(shade, shade + 4, shade + 14))
    font_path = find_font(64)
    try:
        font = ImageFont.truetype(font_path, 64) if font_path else None
    except OSError:
        font = None
    if font:
        words = title.split()
        line, lines = "", []
        for word in words:
            if len(line) + len(word) > 24 and line:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines.append(line)
        y = 360 - 40 * len(lines)
        for text in lines[:4]:
            bbox = draw.textbbox((0, 0), text, font=font)
            draw.text(((1280 - bbox[2] + bbox[0]) / 2, y), text, font=font, fill=(255, 255, 255))
            y += 80
    img.save(out_path)


def make_srt(master: Path, out_dir: Path, srt_file: str | None) -> Path | None:
    if srt_file:
        src = Path(srt_file)
        if not src.exists():
            die(f"--srt файл не найден: {srt_file}")
        dst = out_dir / "subs.srt"
        shutil.copy(src, dst)
        return dst
    if shutil.which("whisper"):
        run([
            "whisper", str(master), "--model", "small", "--language", "ru",
            "--output_format", "srt", "--output_dir", str(out_dir),
        ])
        found = list(out_dir.glob("*.srt"))
        return found[0] if found else None
    print("build: whisper не установлен и --srt не задан — ролики будут без субтитров")
    return None


def ffmpeg_master(master: Path, out: Path, srt: Path | None, cards: bool, title: str, tmp: Path) -> None:
    """Normalize to 1080p h264/aac; prepend/append 2 s cards when asked."""
    norm = tmp / "norm.mp4"
    vf = f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
    if srt:
        vf += f",subtitles={srt}:force_style='FontSize=18,Outline=2'"
    run([ffmpeg_bin(), "-y", "-i", str(master), "-vf", vf, "-c:v", "libx264", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", str(norm)])

    if not cards:
        norm.replace(out)
        return
    intro, outro = make_cards(title, tmp, vertical=False)
    for card in (intro, outro):
        run([ffmpeg_bin(), "-y", "-loop", "1", "-t", "2", "-i", str(card),
             "-f", "lavfi", "-t", "2", "-i", "anullsrc=r=44100:cl=stereo",
             "-vf", "scale=1920:1080", "-c:v", "libx264", "-crf", "20",
             "-c:a", "aac", "-b:a", "160k", "-shortest", str(tmp / (card.stem + ".mp4"))])
    listing = tmp / "list.txt"
    listing.write_text(
        "\n".join(f"file '{p}'" for p in
                  (tmp / "card_0.mp4", norm, tmp / "card_1.mp4")), encoding="utf-8")
    run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(out)])


def ffmpeg_vertical(master: Path, out: Path, srt: Path | None) -> None:
    fc = (
        "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=24[bg];"
        "[0:v]scale=1080:-2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"
    )
    if srt:
        fc += f";[v0]subtitles={srt}:force_style='FontSize=16,Outline=3,MarginV=160'[v1]"
    cmd = ["ffmpeg", "-y", "-i", str(master), "-filter_complex", fc]
    if srt:
        cmd += ["-map", "[v1]", "-map", "0:a"]
    cmd += ["-c:v", "libx264", "-crf", "21", "-c:a", "aac", "-b:a", "128k", str(out)]
    run(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("master", help="исходная запись (16:9, со звуком)")
    ap.add_argument("--title", required=True, help="заголовок (в карточки, обложку и meta)")
    ap.add_argument("--desc", default="", help="описание для meta.json")
    ap.add_argument("--tags", default="", help="через запятую: vpn,openflux,selfhosted")
    ap.add_argument("--srt", default=None, help="готовые субтитры (иначе Whisper, если стоит)")
    ap.add_argument("--no-cards", action="store_true", help="без интро/аутро-карточек")
    ap.add_argument("--slug", default=None, help="имя каталога в outbox (по умолчанию из --title)")
    args = ap.parse_args()

    need_ffmpeg()
    master = Path(args.master)
    if not master.exists():
        die(f"не найден исходник: {master}")

    ensure_dirs()
    slug = args.slug or slugify(args.title)
    out_dir = OUTBOX_DIR / slug
    if out_dir.exists():
        die(f"outbox/{slug} уже есть — задай --slug")
    out_dir.mkdir(parents=True)
    srt = make_srt(master, out_dir, args.srt)

    with tempfile.TemporaryDirectory(prefix="vidbuild-") as tmp_name:
        tmp = Path(tmp_name)
        ffmpeg_master(master, out_dir / "master_16x9.mp4", srt, not args.no_cards, args.title, tmp)
        ffmpeg_vertical(master, out_dir / "vertical_9x16.mp4", srt)
    make_cover(args.title, out_dir / "cover.png")

    import json

    meta = {
        "slug": slug,
        "title": args.title,
        "description": args.desc,
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "master_hash": file_hash(master),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: outbox/{slug}/ (master_16x9, vertical_9x16, cover.png, meta.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
