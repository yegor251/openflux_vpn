"""video/common.py pure helpers (loaded from the standalone video/ folder)."""

import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "video_common", Path(__file__).resolve().parent.parent / "video" / "common.py"
)
video_common = importlib.util.module_from_spec(_SPEC)
sys.modules["video_common"] = video_common
_SPEC.loader.exec_module(video_common)


def test_slugify_keeps_cyrillic_and_strips_garbage():
    assert video_common.slugify("Своя VPN-ферма за вечер!") == "своя-vpn-ферма-за-вечер"
    assert video_common.slugify("   ...  ") == "video"
    assert len(video_common.slugify("x" * 200)) <= 48


def test_state_mark_and_query(tmp_path, monkeypatch):
    monkeypatch.setattr(video_common, "STATE_PATH", tmp_path / "state.json")
    assert not video_common.is_done("s1", "youtube")
    video_common.mark_done("s1", "youtube", id="abc", url="https://youtu.be/abc")
    video_common.mark_done("s1", "tiktok", id="s1")
    assert video_common.is_done("s1", "youtube") and video_common.is_done("s1", "tiktok")
    assert video_common.all_done("s1", ["youtube", "tiktok"])
    assert not video_common.all_done("s1", ["youtube", "tiktok", "vimeo"])
    video_common.mark_failed("s2", "tiktok", "boomed")
    assert not video_common.is_done("s2", "tiktok")
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_parse_proxy():
    info = video_common.parse_proxy("socks5://127.0.0.1:1080")
    import httplib2

    assert info.proxy_type == httplib2.socks.PROXY_TYPE_SOCKS5
    assert info.proxy_host == "127.0.0.1" and info.proxy_port == 1080
    http_info = video_common.parse_proxy("http://proxy.local:8080")
    assert http_info.proxy_type == httplib2.socks.PROXY_TYPE_HTTP
    assert video_common.parse_proxy(None) is None


def test_build_description_trims_with_hashtags():
    tags = ["vpn", "open flux!", "selfhosted"]
    out = video_common.build_description("Короткое описание.", tags, limit=100)
    assert out == "Короткое описание.\n#vpn #openflux #selfhosted"
    long = "х" * 300
    out = video_common.build_description(long, tags, limit=100)
    assert len(out) <= 100 and out.endswith("#vpn #openflux #selfhosted")
    assert video_common.build_description("", []) == ""


def test_file_hash_is_stable_and_short(tmp_path):
    p = tmp_path / "f.mp4"
    p.write_bytes(b"0" * 5000)
    h1 = video_common.file_hash(p)
    assert h1 == video_common.file_hash(p) and len(h1) == 16
    p.write_bytes(b"1" * 5000)
    assert video_common.file_hash(p) != h1
