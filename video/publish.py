#!/usr/bin/env python3
"""Publish built videos from video/outbox/ to YouTube and TikTok.

YouTube: official Data API v3 (OAuth once, refresh-token cached;
uploads may go through --proxy — from RF YouTube needs one).
TikTok:  browser emulation via tiktok-uploader (Selenium + exported
cookies in secrets/cookies.txt); needs a foreign-region account and,
from RF, a foreign IP (our own VPN is fine).

Usage:
  python3 publish.py --all                      # everything in outbox
  python3 publish.py --slug my-video            # one slug
  python3 publish.py --all --platforms youtube  # one platform only
  python3 publish.py --all --privacy unlisted --proxy socks5://127.0.0.1:1080

Secrets (gitignored, 0600): video/secrets/{client_secret.json,token.json,
cookies.txt}.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    OUTBOX_DIR,
    SECRETS_DIR,
    all_done,
    build_description,
    ensure_dirs,
    is_done,
    mark_done,
    mark_failed,
    move_to_published,
    parse_proxy,
)


def die(msg: str) -> None:
    print(f"publish: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_meta(slug: str) -> dict:
    meta_path = OUTBOX_DIR / slug / "meta.json"
    if not meta_path.exists():
        die(f"в outbox/{slug} нет meta.json — сначала build.py")
    return json.loads(meta_path.read_text(encoding="utf-8"))


# -- YouTube ------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def yt_service(proxy_url: str | None):
    try:
        import httplib2
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        die("нет зависимостей YouTube: pip install google-api-python-client google-auth-oauthlib")

    token_path = SECRETS_DIR / "token.json"
    secret = SECRETS_DIR / "client_secret.json"
    if not secret.exists():
        die("нет video/secrets/client_secret.json — см. README (Google Cloud → OAuth)")
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        token_path.chmod(0o600)
    http = httplib2.Http(proxy_info=parse_proxy(proxy_url))
    return build("youtube", "v3", http=creds.authorize(http))


def publish_youtube(slug: str, meta: dict, privacy: str, proxy_url: str | None, dry: bool) -> dict:
    from googleapiclient.http import MediaFileUpload

    service = yt_service(proxy_url)
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": build_description(meta.get("description", ""), meta.get("tags", []), limit=5000),
            "tags": meta.get("tags", []),
            "categoryId": "28",  # science & technology
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(
        str(OUTBOX_DIR / slug / "master_16x9.mp4"), chunksize=8 * 1024 * 1024,
        resumable=True, mimetype="video/mp4",
    )
    if dry:
        return {"id": "(dry-run)", "url": "https://youtu.be/(dry-run)"}
    request = service.videos().insert(body=body, media_body=media, fields="id")
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"  youtube: {int(status.progress() * 100)}%")
    video_id = response["id"]
    cover = OUTBOX_DIR / slug / "cover.png"
    if cover.exists():
        service.thumbnails().set(videoId=video_id, media_body=str(cover)).execute()
    return {"id": video_id, "url": f"https://youtu.be/{video_id}"}


# -- TikTok (browser emulation) ------------------------------------------------

def publish_tiktok(slug: str, meta: dict, dry: bool) -> dict:
    cookies = SECRETS_DIR / "cookies.txt"
    if not cookies.exists():
        die("нет video/secrets/cookies.txt — экспортируй куки tiktok.com (Netscape формат), см. README")
    try:
        from tiktok_uploader.upload import upload_tiktok
    except ImportError:
        die("нет зависимости TikTok: pip install tiktok-uploader")
    desc = build_description(meta.get("description", "") or meta["title"], meta.get("tags", []))
    if dry:
        return {"id": "(dry-run)"}
    failed = upload_tiktok(
        str(OUTBOX_DIR / slug / "vertical_9x16.mp4"),
        description=desc,
        cookies=str(cookies),
    )
    if failed:
        raise RuntimeError(f"tiktok-uploader: не опубликовано ({len(failed)})")
    return {"id": slug}


PLATFORMS = {
    "youtube": lambda slug, meta, args: publish_youtube(slug, meta, args.privacy, args.proxy, args.dry),
    "tiktok": lambda slug, meta, args: publish_tiktok(slug, meta, args.dry),
}


def publish_slug(slug: str, platforms: list[str], args) -> None:
    slug_dir = OUTBOX_DIR / slug
    if not slug_dir.is_dir():
        die(f"нет outbox/{slug}")
    meta = load_meta(slug)
    for platform in platforms:
        if args.dry:
            print(f"[{slug}] {platform}: dry-run")
            continue
        if is_done(slug, platform):
            print(f"[{slug}] {platform}: уже опубликовано, пропускаю")
            continue
        try:
            info = PLATFORMS[platform](slug, meta, args)
            mark_done(slug, platform, **info)
            print(f"[{slug}] {platform}: OK {info.get('url', '')}")
        except Exception as exc:  # noqa: BLE001 - one platform must not kill the other
            mark_failed(slug, platform, str(exc))
            print(f"[{slug}] {platform}: FAIL — {str(exc)[:200]}", file=sys.stderr)
    wanted = args.platforms.split(",")
    if all_done(slug, [p for p in wanted if p in PLATFORMS]):
        move_to_published(slug)
        print(f"[{slug}] всё опубликовано → published/{slug}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="опубликовать всё из outbox")
    ap.add_argument("--slug", default=None, help="один каталог из outbox")
    ap.add_argument("--platforms", default="youtube,tiktok", help="через запятую")
    ap.add_argument("--privacy", default="public", choices=["public", "unlisted", "private"])
    ap.add_argument("--proxy", default=None, help="прокси для YouTube: socks5://127.0.0.1:1080")
    ap.add_argument("--dry", action="store_true", help="ничего не публиковать, показать план")
    args = ap.parse_args()

    ensure_dirs()
    if not args.all and not args.slug:
        die("укажи --all или --slug")
    slugs = sorted(p.name for p in OUTBOX_DIR.iterdir() if p.is_dir()) if args.all else [args.slug]
    if not slugs:
        die("outbox пуст — сначала build.py")
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip() in PLATFORMS]
    if not platforms:
        die(f"--platforms: неизвестные платформы (доступны: {', '.join(PLATFORMS)})")

    for slug in slugs:
        publish_slug(slug, platforms, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
