#!/usr/bin/env python3
"""Cut a long recording into ~1-minute publishable parts.

The pipeline (everything automatic after one command):
  1. audio -> speech regions. Engine `silero` (pip install silero-vad)
     tells speech from music/silence; fallback engine `silence` only
     drops quiet passages (music passes through — see README).
  2. speech regions are packed into parts of ~--part-length seconds,
     cut boundaries snap to speech edges, dead air never spans parts.
  3. each part renders both formats (16:9 + blur-fill 9:16) with:
     burned subtitles (Whisper/--srt), the animated banner (banner.py,
     carrying "ЧАСТЬ N/M"), and a 2.5 s flash "Часть N из M".
  4. each part lands in outbox/<slug>-pNN/ with meta.json — ready for
     the regular `publish.py --all`.

  python3 clipper.py long.mov --title "Разбор закона" \
      [--part-length 60] [--format both] [--engine auto] [--srt-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banner import render_webm  # noqa: E402
from common import OUTBOX_DIR, ensure_dirs, ffmpeg_bin, ffprobe_bin, file_hash, slugify  # noqa: E402

TARGET = 60.0
MAX_PART = 75.0
MIN_PART = 25.0
GAP_MERGE = 0.6  # speech gaps shorter than this merge into one region
MIN_REGION = 1.2  # a speech blip shorter than this is noise


def die(msg: str) -> None:
    print(f"clipper: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"{' '.join(cmd[:3])}... failed:\n{proc.stderr[-600:]}")
    return proc


def duration_of(path: Path) -> float:
    proc = run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(proc.stdout.strip())


def extract_audio(src: Path, wav: Path) -> None:
    run([ffmpeg_bin(), "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(wav)])


# -- speech regions -----------------------------------------------------------

def parse_silences(stderr: str) -> list[tuple[float, float]]:
    """ffmpeg silencedetect log -> [(silence_start, silence_end), ...]."""
    starts = [float(m) for m in re.findall(r"silence_start: ([0-9.]+)", stderr)]
    ends = [float(m) for m in re.findall(r"silence_end: ([0-9.]+)", stderr)]
    return list(zip(starts, ends[: len(starts)]))


def speech_by_silence(duration: float, wav: Path, tmp: Path) -> list[tuple[float, float]]:
    """Fallback engine: everything that is not quiet is 'speech' (music too)."""
    proc = subprocess.run(
        [ffmpeg_bin(), "-i", str(wav), "-af", "silencedetect=noise=-32dB:d=1.2", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    silences = parse_silences(proc.stderr)
    regions, cursor = [], 0.0
    for s, e in silences + [(duration, duration)]:
        if s - cursor >= MIN_REGION:
            regions.append((cursor, min(s, duration)))
        cursor = max(cursor, e)
    return regions


def speech_by_silero(wav: Path) -> list[tuple[float, float]]:
    """Proper engine: silero-VAD keeps only voice (music/noise dropped)."""
    try:
        import torch  # noqa: F401
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError:
        return []
    import wave

    with wave.open(str(wav), "rb") as w:
        frames = w.readframes(w.getnframes())
    audio = torch.frombuffer(bytearray(frames), dtype=torch.int16).float() / 32768.0
    model = load_silero_vad()
    ts = get_speech_timestamps(audio, model, sampling_rate=16000,
                               min_speech_duration_ms=int(MIN_REGION * 1000),
                               min_silence_duration_ms=int(GAP_MERGE * 1000))
    return [(t["start"] / 16000, t["end"] / 16000) for t in ts]


def speech_regions(src: Path, duration: float, wav: Path, tmp: Path, engine: str) -> list[tuple[float, float]]:
    regions: list[tuple[float, float]] = []
    if engine in ("auto", "silero"):
        regions = speech_by_silero(wav)
        if regions:
            engine = "silero"
        elif engine == "silero":
            die("silero-vad не установлен: pip install silero-vad (или --engine silence)")
    if not regions:
        regions = speech_by_silence(duration, wav, tmp)
        engine = "silence"
    # merge tiny gaps, drop blips
    merged: list[tuple[float, float]] = []
    for s, e in regions:
        if e - s < MIN_REGION:
            continue
        if merged and s - merged[-1][1] <= GAP_MERGE:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    print(f"clipper: движок {engine}, озвученных участков: {len(merged)}, "
          f"всего речи {sum(e - s for s, e in merged):.0f} с из {duration:.0f} с")
    return merged


# -- part planning ------------------------------------------------------------

def plan_parts(regions: list[tuple[float, float]], target: float = TARGET,
               max_len: float = MAX_PART, min_part: float = MIN_PART) -> list[tuple[float, float]]:
    """Pack speech regions into parts of ~target s, cutting on speech edges.

    Returns cut ranges [(start, end)] — the source time spans to render.
    The last part may be short (>= min_part), otherwise it merges back.
    """
    parts: list[tuple[float, float]] = []
    cur_start: float | None = None
    cur_end = 0.0
    for s, e in regions:
        if cur_start is None:
            cur_start, cur_end = s, e
            continue
        if (e - cur_start) <= max_len:
            cur_end = e
        else:
            parts.append((cur_start, cur_end))
            cur_start, cur_end = s, e
    if cur_start is not None:
        if parts and cur_end - cur_start < min_part:
            # too short tail: pull the previous part's edge back to speech start
            parts[-1] = (parts[-1][0], cur_end)
        else:
            parts.append((cur_start, cur_end))
    return parts


# -- render -------------------------------------------------------------------

def build_part(src: Path, start: float, length: float, srt: Path | None,
               banner: Path, label: str, fmt: str, out: Path, cover_title: str) -> None:
    from PIL import Image

    # the on-screen "Часть N из M" flash lives in a textfile (no escaping pain)
    txt = out.parent / f"{out.stem}.txt"
    txt.write_text(label, encoding="utf-8")

    if fmt == "wide":
        fc = (
            "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[v0]"
        )
        banner_scale, flash_size, xy = "380", "44", "x=28:y=28"
    else:
        fc = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "boxblur=24[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0]"
        )
        banner_scale, flash_size, xy = "320", "52", "x=36:y=44"
    if srt:
        style = "FontSize=18,Outline=2" if fmt == "wide" else "FontSize=16,Outline=3,MarginV=170"
        fc += f";[v0]subtitles={srt}:force_style='{style}'[vs]"
    else:
        fc += ";[v0]null[vs]"
    fc += (
        f";[1:v]scale={banner_scale}:-1[b];"
        f"[vs][b]overlay=x=W-w-24:y=H-h-24:shortest=1[vb];"
        f"[vb]drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:textfile={txt}:"
        f"fontsize={flash_size}:fontcolor=white@0.92:box=1:boxcolor=black@0.45:"
        f"boxborderw=18:{xy}:enable='lte(t,2.5)'[vout]"
    )
    cmd = [
        ffmpeg_bin(), "-y", "-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src),
        "-stream_loop", "-1", "-i", str(banner),
        "-filter_complex", fc, "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
        "-shortest", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    txt.unlink(missing_ok=True)
    if proc.returncode != 0:
        die(f"рендер части не удался ({fmt}):\n{proc.stderr[-600:]}")


def make_cover_part(title: str, out_path: Path) -> None:
    from build import make_cover

    make_cover(title, out_path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="длинное видео (своё! чужое кино = Content ID страйк)")
    ap.add_argument("--title", required=True)
    ap.add_argument("--desc", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--part-length", type=float, default=TARGET)
    ap.add_argument("--format", default="both", choices=["both", "vertical", "wide"])
    ap.add_argument("--engine", default="auto", choices=["auto", "silero", "silence"])
    ap.add_argument("--srt-dir", default=None, help="готовые SRT per part (clipper skips Whisper)")
    ap.add_argument("--brand", default="OPENFLUX")
    ap.add_argument("--limit", type=int, default=0, help="первая N частей (0 = все)")
    args = ap.parse_args()

    if not shutil.which(ffmpeg_bin().split("/")[-1]) and not Path(ffmpeg_bin()).exists():
        die("ffmpeg не найден")
    src = Path(args.source)
    if not src.exists():
        die(f"не найден исходник: {src}")

    ensure_dirs()
    duration = duration_of(src)
    with tempfile.TemporaryDirectory(prefix="clip-") as tmp_name:
        tmp = Path(tmp_name)
        wav = tmp / "audio.wav"
        extract_audio(src, wav)
        regions = speech_regions(src, duration, wav, tmp, args.engine)
        if not regions:
            die("речь не найдена — резать нечего")
        parts = plan_parts(regions, target=args.part_length)
        if args.limit:
            parts = parts[: args.limit]
        total = len(parts)
        print(f"clipper: частей будет {total}")

        formats = ["vertical", "wide"] if args.format == "both" else [args.format]
        base_slug = slugify(args.title)
        for i, (start, end) in enumerate(parts, start=1):
            label = f"Часть {i} из {total}"
            slug = f"{base_slug}-p{i:02d}"
            out_dir = OUTBOX_DIR / slug
            if out_dir.exists():
                print(f"[{slug}] уже в outbox — пропускаю")
                continue
            out_dir.mkdir(parents=True)
            banner = out_dir / "banner.webm"
            render_webm(banner, brand=args.brand, part_label=f"ЧАСТЬ {i}/{total}".upper())

            # subtitles: per-part SRT (--srt-dir/pNN.srt) or Whisper on the part
            srt = None
            if args.srt_dir:
                cand = Path(args.srt_dir) / f"p{i:02d}.srt"
                srt = cand if cand.exists() else None
            elif shutil.which("whisper"):
                part_wav = tmp / f"p{i:02d}.wav"
                run([ffmpeg_bin(), "-y", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
                     "-i", str(wav), str(part_wav)])
                run(["whisper", str(part_wav), "--model", "small", "--language", "ru",
                     "--output_format", "srt", "--output_dir", str(tmp)])
                cand = tmp / f"p{i:02d}.srt"
                srt = cand if cand.exists() else None
            if srt:
                dst = out_dir / "subs.srt"
                shutil.copy(srt, dst)
                srt = dst

            length = end - start
            for fmt in formats:
                name = "master_16x9.mp4" if fmt == "wide" else "vertical_9x16.mp4"
                build_part(src, start, length, srt, banner, label, fmt, out_dir / name, args.title)
            make_cover_part(f"{args.title} — часть {i}", out_dir / "cover.png")
            meta = {
                "slug": slug,
                "title": f"{args.title} · часть {i}/{total}",
                "description": args.desc,
                "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
                "master_hash": file_hash(src),
                "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"[{slug}] {label}: {length:.0f} с, форматов {len(formats)} — в outbox")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
