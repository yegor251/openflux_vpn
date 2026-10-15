"""Data types shared across docprovider."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def sanitize_name(name: str) -> str:
    """Channel/file names must be filesystem- and URL-safe."""
    if not _NAME_RE.match(name):
        raise ValueError(
            f"invalid channel name {name!r}: expected [A-Za-z0-9_-], 1..64 chars"
        )
    return name


@dataclass(frozen=True)
class MailruAccount:
    """Mail.ru account used to host carrier files.

    Password login is blocked by reCAPTCHA for new programmatic clients,
    so the working mode is a browser-exported cookie session
    (scripts/export_yandex_cookies.py --service mailru). Password
    credentials are still accepted for accounts where Mail.ru allows it.
    """

    id: str
    login: str = ""
    password: str = ""
    cookies_path: str = ""


@dataclass(frozen=True)
class YandexAccount:
    """Yandex account represented by an exported Netscape cookies.txt."""

    id: str
    cookies_path: str


@dataclass(frozen=True)
class MailruDoc:
    """A published Mail.ru cloud file and its public link."""

    url: str          # https://cloud.mail.ru/public/<A>/<B>
    home: str         # cloud path, e.g. /openflux/of-alice.txt
    account_id: str


@dataclass(frozen=True)
class YandexDoc:
    """A published, link-editable Yandex document."""

    url: str          # office_online_sharing_url (docs.yandex.ru/edit/...)
    path: str         # disk path, e.g. /disk/openflux/of-alice.docx
    account_id: str


@dataclass(frozen=True)
class ChannelDocs:
    """Carrier documents for one VPN channel (one per user)."""

    name: str
    yandex: YandexDoc
    mailru: MailruDoc


@dataclass
class PoolLimits:
    """Per-account daily creation limits."""

    mailru_per_day: int = 5
    yandex_per_day: int = 5
