#!/usr/bin/env python3
"""Export service cookies (Yandex / Mail.ru) from Chrome-family browsers.

Decryption is delegated to browser_cookie3 (Chrome >= 155 changed the
macOS scheme faster than hand-rolled recipes age). Cookie values are
never printed; the output file gets 0600 and stays out of git.

Usage:
    python scripts/export_yandex_cookies.py [--service yandex|mailru]
                                            [--browser chrome|brave|edge|yandex]
                                            [--profile Default]
                                            [--out ...]
                                            [--all-hosts] [--verify]
"""

from __future__ import annotations

import argparse
import http.cookiejar
import stat
import sys
from pathlib import Path

BROWSERS = {
    "chrome": "chrome",
    "brave": "brave",
    "edge": "edge_chromium",
    "yandex": "yandex",
}

SERVICES = {
    "yandex": {"domain": ".yandex.ru", "keep_suffix": "yandex.ru", "out": "ya-cookies.txt"},
    "mailru": {"domain": ".mail.ru", "keep_suffix": "mail.ru", "out": "mr-cookies.txt"},
}


def export_cookies(browser: str, profile: str, domain: str) -> http.cookiejar.CookieJar:
    import browser_cookie3

    fn_name = BROWSERS[browser]
    fn = getattr(browser_cookie3, fn_name, None)
    if fn is None:
        raise SystemExit(f"browser_cookie3 has no loader {fn_name!r}")
    kwargs = {"domain_name": domain}
    if profile and profile != "Default":
        base = {
            "chrome": "Google/Chrome",
            "brave": "BraveSoftware/Brave-Browser",
            "edge": "Microsoft Edge",
            "yandex": "Yandex/YandexBrowser",
        }[browser]
        cookie_file = Path.home() / "Library" / "Application Support" / base / profile / "Network" / "Cookies"
        if not cookie_file.exists():
            cookie_file = cookie_file.parent.parent / "Cookies"
        kwargs["cookie_file"] = str(cookie_file)
    return fn(**kwargs)


def write_netscape(
    jar: http.cookiejar.CookieJar, out: Path, keep_suffix: str, all_hosts: bool
) -> int:
    lines = ["# Netscape HTTP Cookie File", "# exported by export_yandex_cookies.py"]
    count = 0
    for c in sorted(jar, key=lambda x: (x.domain, x.name)):
        host = c.domain
        if not all_hosts and not host.removeprefix(".").endswith(keep_suffix):
            continue
        value = c.value or ""
        try:
            value.encode("latin-1")  # the Cookie header must survive transport
        except UnicodeEncodeError:
            continue
        expires = str(int(c.expires)) if c.expires else "0"
        lines.append(
            "\t".join(
                [
                    host,
                    "TRUE" if host.startswith(".") else "FALSE",
                    c.path or "/",
                    "TRUE" if c.secure else "FALSE",
                    expires,
                    c.name,
                    value,
                ]
            )
        )
        count += 1
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return count


def verify(out: Path, service: str) -> None:
    """Load the file the way docprovider does and probe the service."""
    import requests

    from docprovider.netscape import jar_summary, load_into_session

    session = requests.Session()
    applied = load_into_session(session, out, SERVICES[service]["keep_suffix"])
    print(f"docprovider loaded {applied} cookies: {jar_summary(out)}")
    if service == "yandex":
        response = session.get("https://disk.yandex.ru/client/disk", timeout=30)
        if "passport.yandex." in response.url or "showcaptcha" in response.url:
            print("VERIFY: FAIL — redirected to a checkpoint (cookies not accepted)")
            sys.exit(2)
        if 'id="preloaded-data"' in response.text and '"sk"' in response.text:
            print("VERIFY: OK — Disk page returned signed-in editor config")
        else:
            print(f"VERIFY: WEAK — page loaded ({response.url[:60]}) but no editor config")
    else:
        response = session.post("https://cloud.mail.ru/api/v2/tokens/csrf", timeout=30)
        if response.status_code == 200 and "token" in response.text:
            print("VERIFY: OK — cloud.mail.ru issued a CSRF token (session alive)")
        else:
            print(f"VERIFY: FAIL — csrf answered {response.status_code} (session dead)")
            sys.exit(2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default="yandex", choices=sorted(SERVICES))
    parser.add_argument("--browser", default="chrome", choices=sorted(BROWSERS))
    parser.add_argument("--profile", default="Default")
    parser.add_argument("--out", default=None)
    parser.add_argument(
        "--all-hosts",
        action="store_true",
        help="keep every *.tld variant (default: only the service's own "
        "domain — the rest is tracker junk and bloats the Cookie header)",
    )
    parser.add_argument("--verify", action="store_true", help="probe the service after export")
    args = parser.parse_args()

    conf = SERVICES[args.service]
    jar = export_cookies(args.browser, args.profile, conf["domain"])
    out = Path(args.out or conf["out"])
    count = write_netscape(jar, out, conf["keep_suffix"], args.all_hosts)
    print(f"exported {count} {args.service} cookies -> {out}")
    if args.verify:
        verify(out, args.service)


if __name__ == "__main__":
    main()
