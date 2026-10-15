"""Netscape cookies.txt parsing, scoped to a single service domain."""

from __future__ import annotations

import http.cookiejar
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RawCookie:
    domain: str
    include_subdomains: bool
    path: str
    secure: bool
    expires: int  # 0 means session cookie
    name: str
    value: str


def parse_netscape(text: str) -> list[RawCookie]:
    """Parse cookies.txt content; malformed rows are skipped silently."""
    cookies: list[RawCookie] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("# "):
            continue
        # curl-style exports prefix HttpOnly cookies with '#HttpOnly_'
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, flag, path, secure, expires, name, value = parts
        try:
            cookies.append(
                RawCookie(
                    domain=domain,
                    include_subdomains=flag.upper() == "TRUE",
                    path=path or "/",
                    secure=secure.upper() == "TRUE",
                    expires=int(expires),
                    name=name,
                    value=value,
                )
            )
        except ValueError:
            continue
    return cookies


def load_into_session(session, cookies_path: str | Path, allowed_suffix: str) -> int:
    """Load cookies for ``allowed_suffix`` domains into a requests session.

    Returns the number of cookies applied. Expired cookies are dropped.
    """
    text = Path(cookies_path).read_text(encoding="utf-8")
    now = int(time.time())
    applied = 0
    for raw in parse_netscape(text):
        domain = raw.domain.lstrip(".")
        if not domain.endswith(allowed_suffix):
            continue
        if raw.expires and raw.expires <= now:
            continue
        session.cookies.set(
            raw.name,
            raw.value,
            domain=raw.domain if raw.include_subdomains else domain,
            path=raw.path,
            secure=raw.secure,
        )
        applied += 1
    return applied


def jar_summary(cookies_path: str | Path) -> str:
    """Human-readable one-liner about a cookies file (no values)."""
    cookies = parse_netscape(Path(cookies_path).read_text(encoding="utf-8"))
    domains = sorted({c.domain.lstrip(".").removeprefix(".") for c in cookies})
    return f"{len(cookies)} cookies for: {', '.join(domains) if domains else 'nothing'}"


# kept for potential future use with stdlib cookiejar-based clients
def to_cookiejar(cookies: list[RawCookie]) -> http.cookiejar.CookieJar:
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    now = int(time.time())
    for raw in cookies:
        if raw.expires and raw.expires <= now:
            continue
        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=raw.name,
                value=raw.value,
                port=None,
                port_specified=False,
                domain=raw.domain,
                domain_specified=raw.include_subdomains,
                domain_initial_dot=raw.domain.startswith("."),
                path=raw.path,
                path_specified=True,
                secure=raw.secure,
                expires=raw.expires or None,
                discard=raw.expires == 0,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar
