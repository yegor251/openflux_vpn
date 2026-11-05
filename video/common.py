"""Shared helpers for the video pipeline: dirs, state, slug, proxy parse.

Layout (everything except code is gitignored):
  video/
    secrets/         client_secret.json, token.json, cookies.txt (0600)
    outbox/<slug>/   build outputs + meta.json, waiting for publish
    published/<slug>/  moved here once every requested platform is done
    state.json       what was published where
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse

VIDEO_DIR = Path(__file__).resolve().parent
SECRETS_DIR = VIDEO_DIR / "secrets"
OUTBOX_DIR = VIDEO_DIR / "outbox"
PUBLISHED_DIR = VIDEO_DIR / "published"
STATE_PATH = VIDEO_DIR / "state.json"

TIKTOK_DESC_LIMIT = 2200


def ensure_dirs() -> None:
    for d in (SECRETS_DIR, OUTBOX_DIR, PUBLISHED_DIR):
        d.mkdir(parents=True, exist_ok=True)


def slugify(text: str, maxlen: int = 48) -> str:
    """Filesystem-safe slug that keeps cyrillic (mac/ext4 are fine with it)."""
    s = re.sub(r"\s+", "-", text.strip().lower())
    s = re.sub(r"[^a-z0-9а-яё-]", "", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:maxlen].rstrip("-") or "video"


def file_hash(path: str | Path, nbytes: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(nbytes))
    return h.hexdigest()[:16]


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    STATE_PATH.chmod(0o600)


def is_done(slug: str, platform: str) -> bool:
    entry = load_state().get(slug, {}).get(platform)
    return bool(entry and entry.get("ok"))


def mark_done(slug: str, platform: str, **data) -> None:
    state = load_state()
    entry = state.setdefault(slug, {})
    entry[platform] = {"ok": True, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **data}
    save_state(state)


def mark_failed(slug: str, platform: str, error: str) -> None:
    state = load_state()
    state.setdefault(slug, {})[platform] = {"ok": False, "error": error[:200]}
    save_state(state)


def all_done(slug: str, platforms: list[str]) -> bool:
    entry = load_state().get(slug, {})
    return all(entry.get(p, {}).get("ok") for p in platforms)


def move_to_published(slug: str) -> None:
    src = OUTBOX_DIR / slug
    if not src.is_dir():
        return
    dst = PUBLISHED_DIR / slug
    if dst.exists():
        return
    src.replace(dst)


def parse_proxy(url: str | None):
    """'socks5://127.0.0.1:1080' / 'http://host:port' -> httplib2.ProxyInfo.
    Returns None for None; socks needs PySocks installed."""
    if not url:
        return None
    parsed = urlparse(url)
    host, port = parsed.hostname, parsed.port or (1080 if parsed.scheme.startswith("socks") else 8080)
    import httplib2

    ptype = httplib2.socks.PROXY_TYPE_SOCKS5 if parsed.scheme.startswith("socks") else httplib2.socks.PROXY_TYPE_HTTP
    return httplib2.ProxyInfo(proxy_type=ptype, proxy_host=host, proxy_port=port)


def build_description(description: str, tags: list[str], limit: int = TIKTOK_DESC_LIMIT) -> str:
    """Description + hashtags, trimmed to the platform limit."""
    hashtags = " ".join(f"#{re.sub(r'[^a-zа-яё0-9]', '', t.lower())}" for t in tags if t)
    base = description.strip()
    if not hashtags:
        return base[:limit]
    room = limit - len(hashtags) - 1
    if room <= 0:
        return hashtags[:limit]
    return (base[: room - 1] + "\n" + hashtags) if len(base) > room else (base + "\n" + hashtags)
