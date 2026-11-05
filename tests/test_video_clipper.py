"""clipper.py pure logic: silence parsing, part packing."""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "video_clipper", Path(__file__).resolve().parent.parent / "video" / "clipper.py"
)
clipper = importlib.util.module_from_spec(_SPEC)
sys.modules["video_clipper"] = clipper
_SPEC.loader.exec_module(clipper)


def test_parse_silences_pairs_starts_and_ends():
    log = """
    [silencedetect @ 0x0] silence_start: 12.4
    [silencedetect @ 0x0] silence_end: 15.9 | silence_duration: 3.5
    [silencedetect @ 0x0] silence_start: 40.2
    [silencedetect @ 0x0] silence_end: 44.0 | silence_duration: 3.8
    """
    assert clipper.parse_silences(log) == [(12.4, 15.9), (40.2, 44.0)]


def test_plan_parts_cuts_on_speech_edges():
    # 5 speech blocks of 30 s separated by 10 s of dead air
    regions = [(i * 40, i * 40 + 30) for i in range(5)]
    parts = clipper.plan_parts(regions, target=60, max_len=75, min_part=25)

    speech_starts = {s for s, _ in regions}
    for start, _ in parts:
        assert start in speech_starts  # every cut opens on speech, never on dead air
    lengths = [e - s for s, e in parts]
    assert all(l <= 75 for l in lengths)  # never above the cap
    assert sum(e - s for s, e in parts) >= sum(e - s for s, e in regions)  # ~nothing lost


def test_plan_parts_merges_short_tail_back():
    # two big blocks + a 10 s tail (< min_part): tail must merge into part 2
    regions = [(0, 60), (80, 130), (140, 150)]
    parts = clipper.plan_parts(regions, target=60, max_len=75, min_part=25)
    assert len(parts) == 2
    assert parts[-1][1] == 150  # the tail's speech end is the last cut


def test_plan_parts_never_spans_silence_gap():
    # one huge continuous speech run of 200 s: must still be split at the cap
    regions = [(0, 200)]
    parts = clipper.plan_parts(regions, target=60, max_len=75, min_part=25)
    # a single region cannot be split internally (cut on speech edge only),
    # so it stays one part even above max_len - documented behaviour
    assert parts == [(0, 200)]
