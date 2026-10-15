"""URL shape checks and live checks for carrier documents."""

from __future__ import annotations

import re

import requests

from .errors import CaptchaRequiredError, ValidationError

# same shape the OpenFlux provisioner enforces (provision/transports.go)
MAILRU_PUBLIC_RE = re.compile(
    r"^https://cloud\.mail\.ru/public/[A-Za-z0-9_-]{2,64}/[A-Za-z0-9_-]{2,128}$"
)
YANDEX_DOC_RE = re.compile(
    r"^https://docs\.yandex\.ru/[A-Za-z0-9/_.?=&%-]+$"
)

MAILRU_EDIT_API = "https://cloud.mail.ru/api/v4/r7/edit"
# showcaptchafast is the proof-of-work wall the OpenFlux transport solves
# automatically, so it is NOT a failure; SmartCaptcha and the passport
# login wall are.
_CHECKPOINT_MARKERS = ("showcaptcha", "passport.yandex.")


def normalize_mailru_weblink(url: str) -> str:
    """Accept bare weblink or full public URL, return the full URL form."""
    url = url.strip()
    match = re.match(r"^(?:https?://cloud\.mail\.ru/public/)?([A-Za-z0-9_-]{2,64}/[A-Za-z0-9_-]{2,128})/?$", url)
    if not match:
        raise ValidationError(f"not a mail.ru public link: {url[:80]!r}")
    return "https://cloud.mail.ru/public/" + match.group(1)


def validate_mailru_url(url: str) -> str:
    url = normalize_mailru_weblink(url)
    if not MAILRU_PUBLIC_RE.match(url):
        raise ValidationError(f"mail.ru url failed strict check: {url[:80]!r}")
    return url


def validate_yandex_url(url: str) -> str:
    if not YANDEX_DOC_RE.match(url.strip()):
        raise ValidationError(f"not a docs.yandex.ru document url: {url[:80]!r}")
    return url.strip()


def check_mailru_public(
    url: str, session: requests.Session | None = None, timeout: float = 15.0
) -> None:
    """Live check: the anonymous edit API must resolve the public link."""
    url = validate_mailru_url(url)
    weblink = url.removeprefix("https://cloud.mail.ru/public/")
    own = session is None
    session = session or requests.Session()
    try:
        response = session.post(
            MAILRU_EDIT_API,
            json={"x-email": "anonym", "public": f"/{weblink}", "platform": "desktop_web"},
            timeout=timeout,
        )
    finally:
        if own:
            session.close()
    if response.status_code != 200:
        raise ValidationError(f"mail.ru edit API answered {response.status_code}")
    body = response.json()
    if "document" not in body and "editorConfig" not in body:
        raise ValidationError(f"mail.ru edit API gave unexpected body: {str(body)[:120]}")


def check_yandex_doc(
    url: str, session: requests.Session | None = None, timeout: float = 15.0
) -> None:
    """Live check: the document opens without a login/SmartCaptcha wall.

    An anonymous GET normally lands on the proof-of-work captcha
    (``showcaptchafast``) — that is expected: the OpenFlux transport
    solves PoW itself, so it only means the document exists and is
    served. Real failures are SmartCaptcha and the passport login wall.
    """
    url = validate_yandex_url(url)
    own = session is None
    session = session or requests.Session()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
    finally:
        if own:
            session.close()
    # PoW check must run first: 'showcaptchafast' contains 'showcaptcha'
    if "showcaptchafast" in response.url:
        return  # PoW wall: the doc is up, the transport will pass it
    if any(marker in response.url for marker in _CHECKPOINT_MARKERS):
        raise CaptchaRequiredError(f"yandex doc redirects to a checkpoint: {response.url[:80]}")
    if response.status_code != 200:
        raise ValidationError(f"yandex doc page answered {response.status_code}")
    # client-config is the editor shell the OpenFlux yandex transport
    # itself scrapes (balancer url, editor token, doc key)
    if 'id="client-config"' not in response.text:
        raise ValidationError("yandex doc page lacks the editor client-config")
