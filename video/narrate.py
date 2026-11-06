#!/usr/bin/env python3
"""Vertical clip in Russian: composed narration + exact subtitles + big banner.

One command turns a source recording/footage into a ready vertical:
  1. narration text (written here or by the agent) is split into sentences,
     each is synthesized by the macOS TTS voice (Milena) — durations are
     known exactly, so the SRT is precise, not estimated;
  2. the video segment is cut to the narration length from --offset;
  3. render: blur-fill 9:16, subtitles above the big bottom banner,
     the animated lower-third band (banner.py) with the part label,
     original audio ducked to ~12% under the voice.

  python3 narrate.py src.mov --title "Чёрная дыра по-русски" \
      --text "Первое предложение. Второе." --tags космос,nasa
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from banner import render_webm_band  # noqa: E402
from build import make_cover  # noqa: E402
from common import OUTBOX_DIR, ensure_dirs, ffmpeg_bin, ffprobe_bin, file_hash, slugify  # noqa: E402

BAND_H = 360  # must match banner.BAND_H (kept literal: run via ffmpeg map only)
VOICE = "Milena"
ORIG_UNDER = 0.12  # original audio level under the narration


def die(msg: str) -> None:
    print(f"narrate: {msg}", file=sys.stderr)
    raise SystemExit(1)


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"{' '.join(cmd[:3])}... failed:\n{proc.stderr[-600:]}")
    return proc


def probe(path: Path) -> float:
    proc = run([ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(proc.stdout.strip())


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


def tts_sentence(text: str, out: Path) -> None:
    run(["say", "-v", VOICE, "-o", str(out), text])


def tts_to_wav(aiff: Path, wav: Path) -> float:
    run([ffmpeg_bin(), "-y", "-i", str(aiff), "-ar", "44100", "-ac", "2", str(wav)])
    return probe(wav)


def build_srt(sentences: list[dict], path: Path) -> None:
    def fmt(t: float) -> str:
        ms = int(round(t * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, s in enumerate(sentences, start=1):
        lines += [str(i), f"{fmt(s['start'])} --> {fmt(s['end'])}", s["text"], ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def synthesize(text: str, tmp: Path) -> tuple[Path, list[dict]]:
    """Per-sentence TTS -> one narration wav + exact-timing SRT data."""
    sentences = split_sentences(text)
    if not sentences:
        die("текст озвучки пуст")
    concat_list = tmp / "narr_list.txt"
    entries, cursor, wavs = [], 0.0, []
    for i, sent in enumerate(sentences):
        aiff = tmp / f"s{i:02d}.aiff"
        wav = tmp / f"s{i:02d}.wav"
        tts_sentence(sent, aiff)
        dur = tts_to_wav(aiff, wav)
        entries.append({"text": sent, "start": round(cursor, 2), "end": round(cursor + dur, 2)})
        wavs.append(wav)
        cursor += dur
    concat_list.write_text("\n".join(f"file '{w}'" for w in wavs), encoding="utf-8")
    narr = tmp / "narr.wav"
    run([ffmpeg_bin(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_list),
         "-c", "copy", str(narr)])
    return narr, entries


def render(src: Path, offset: float, seg_len: float, narr: Path, srt: Path,
           banner: Path, label: str, out: Path) -> None:
    txt = out.parent / f"{out.stem}.txt"
    txt.write_text(label, encoding="utf-8")
    fc = (
        f"[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        f"boxblur=24[bg];[0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[v0];"
        f"[v0]subtitles={srt}:force_style='FontSize=17,Outline=2,Shadow=1,"
        f"MarginV={BAND_H + 50}'[vs];"
        f"[2:v]scale=1080:-1[b];"
        f"[vs][b]overlay=x=0:y=H-h:shortest=1[vb];"
        f"[vb]drawtext=fontfile=/System/Library/Fonts/Helvetica.ttc:textfile={txt}:"
        f"fontsize=52:fontcolor=white@0.92:box=1:boxcolor=black@0.45:boxborderw=18:"
        f"x=36:y=44:enable='lte(t,2.5)'[vout];"
        f"[1:a]volume=1.0[nar];[0:a]volume={ORIG_UNDER}[under];"
        f"[nar][under]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )
    cmd = [
        ffmpeg_bin(), "-y",
        "-ss", f"{offset:.2f}", "-t", f"{seg_len:.2f}", "-i", str(src),
        "-i", str(narr), "-stream_loop", "-1", "-i", str(banner),
        "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
        "-shortest", str(out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    txt.unlink(missing_ok=True)
    if proc.returncode != 0:
        die(f"рендер не удался:\n{proc.stderr[-600:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("--title", required=True)
    ap.add_argument("--text", required=True, help="текст озвучки по-русски (предложения через точку)")
    ap.add_argument("--desc", default="")
    ap.add_argument("--tags", default="")
    ap.add_argument("--offset", type=float, default=None, help="откуда брать визуал (сек; по умолчанию 8%% длины)")
    ap.add_argument("--slug", default=None)
    ap.add_argument("--brand", default="OPENFLUX")
    args = ap.parse_args()

    src = Path(args.source)
    if not src.exists():
        die(f"не найден исходник: {src}")

    ensure_dirs()
    slug = args.slug or slugify(args.title)
    out_dir = OUTBOX_DIR / slug
    if out_dir.exists():
        die(f"outbox/{slug} уже есть — задай --slug")
    out_dir.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="narr-") as tmp_name:
        tmp = Path(tmp_name)
        narr, entries = synthesize(args.text, tmp)
        total = probe(narr)
        dur = probe(src)
        offset = args.offset if args.offset is not None else min(dur * 0.08, 20.0)
        seg_len = min(total + 0.7, max(dur - offset, 1.0))
        srt = out_dir / "subs.srt"
        build_srt(entries, srt)
        banner = out_dir / "banner.webm"
        render_webm_band(banner, brand=args.brand, part_label="ЧАСТЬ 1/1")
        render(src, offset, seg_len, narr, srt, banner, "Часть 1 из 1",
               out_dir / "vertical_9x16.mp4")
    make_cover(args.title, out_dir / "cover.png")
    meta = {
        "slug": slug, "title": args.title,
        "description": args.desc or args.text.strip()[:300],
        "tags": [t.strip() for t in args.tags.split(",") if t.strip()],
        "master_hash": file_hash(src),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"готово: outbox/{slug}/ ({total:.0f} с озвучки)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
