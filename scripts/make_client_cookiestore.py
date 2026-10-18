#!/usr/bin/env python3
"""Convert a Netscape cookies.txt into an OpenFlux client cookie store.

The transport reads its jar from the cookie store (cookies-yandex.json,
JSON: document URL -> name -> value) when opening the carrier document.
Seeding it with a logged-in Yandex session skips the captcha wall the
transport cannot pass headlessly (SmartCaptcha needs a browser; PoW alone
gets 403 from flagged IPs).

Usage:
  make_client_cookiestore.py ya-cookies.txt -o out/cookies-yandex.json [URL ...]

Without URLs the Yandex document URLs come from oflux-admin's users.json.
The output contains session material: it is written 0600, values are never
printed. Refresh it together with the cookies.txt (same expiry).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docprovider.netscape import parse_netscape

DOMAIN_SUFFIX = ".yandex.ru"


def registry_yandex_urls() -> list[str]:
    from oflux_admin.config import UserRegistry

    path = Path("~/.config/oflux-admin/users.json").expanduser()
    registry = UserRegistry(path)
    return sorted(
        u["yandex_url"]
        for u in registry.active().values()
        if u.get("yandex_url")
    )


def build_jar(cookies_path: Path, urls: list[str]) -> dict[str, dict[str, str]]:
    now = int(time.time())
    jar: dict[str, str] = {}
    for c in parse_netscape(cookies_path.read_text(encoding="utf-8")):
        if not c.domain.endswith(DOMAIN_SUFFIX):
            continue
        if c.expires and c.expires < now:
            continue
        jar[c.name] = c.value
    if not jar:
        raise SystemExit(f"no live {DOMAIN_SUFFIX} cookies in {cookies_path}")
    return {url: dict(jar) for url in urls}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cookies", type=Path, help="Netscape cookies.txt (Yandex login)")
    parser.add_argument("-o", "--out", type=Path, default=Path("out/cookies-yandex.json"))
    parser.add_argument("urls", nargs="*", help="carrier document URLs (default: users.json)")
    args = parser.parse_args()

    urls = args.urls or registry_yandex_urls()
    if not urls:
        raise SystemExit("no URLs given and users.json has none")

    jar = build_jar(args.cookies, urls)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(jar, indent=2), encoding="utf-8")
    args.out.chmod(0o600)
    print(f"{args.out}: {len(jar)} doc URL(s), {len(next(iter(jar.values())))} cookies each")
    return 0


if __name__ == "__main__":
    sys.exit(main())
