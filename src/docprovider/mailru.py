"""Create published files in Mail.ru Cloud for OpenFlux carrier channels.

Verified against the live API (2026-10-15). The web client's current
contract differs from older published recipes:

- the csrf token goes in the **query string** of every POST
  (``?api=2&token=...&x-email=...``), not in the form body;
- upload is **PUT** with the raw file body (no multipart) to the shard
  from the dispatcher, answering ``201`` with the bare SHA-1 hash;
- a cookie session exported from a browser works as-is; password login
  is gated behind reCAPTCHA for new programmatic clients.

A read-only public link is enough for the OpenFlux mailru transport, so
the carrier is a tiny text file.
"""

from __future__ import annotations

import requests

from .errors import ApiError, CookiesExpiredError, LoginError
from .models import MailruAccount, MailruDoc, sanitize_name
from .netscape import load_into_session

AUTH_URL = "https://auth.mail.ru/cgi-bin/auth"
SDC_URL = "https://auth.mail.ru/sdc?from=https://cloud.mail.ru/home"
CSRF_URL = "https://cloud.mail.ru/api/v2/tokens/csrf"
DISPATCHER_URL = "https://cloud.mail.ru/api/v2/dispatcher"
FILE_ADD_URL = "https://cloud.mail.ru/api/v2/file/add"
FILE_PUBLISH_URL = "https://cloud.mail.ru/api/v2/file/publish"
FILE_UNPUBLISH_URL = "https://cloud.mail.ru/api/v2/file/unpublish"
FOLDER_ADD_URL = "https://cloud.mail.ru/api/v2/folder/add"
UPLOAD_SHARD_URL = "https://uploader.cloud.mail.ru/upload-web/"

CHANNEL_DIR = "/openflux"
PUBLIC_URL_PREFIX = "https://cloud.mail.ru/public/"
DEFAULT_TIMEOUT = 30.0


class MailruClient:
    """One authenticated session per account; tokens refresh on 403."""

    def __init__(
        self,
        account: MailruAccount,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.account = account
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://cloud.mail.ru/home",
                "Origin": "https://cloud.mail.ru",
            }
        )
        self._token: str | None = None
        self._upload_url: str | None = None

    # -- auth ----------------------------------------------------------------

    def login(self) -> None:
        if self.account.cookies_path:
            self._login_with_cookies()
        else:
            self._login_with_password()

    def _login_with_cookies(self) -> None:
        """Session from a browser export; no auth POST (it hits reCAPTCHA).

        No SDC bootstrap here either: the exported cookies already carry a
        working cloud.mail.ru session, and an SDC round-trip rotates them
        into a state the API rejects.
        """
        applied = load_into_session(self.session, self.account.cookies_path, "mail.ru")
        if applied == 0:
            raise CookiesExpiredError(
                f"{self.account.cookies_path}: no .mail.ru cookies found"
            )
        self._session_keys()

    def _login_with_password(self) -> None:
        response = self.session.post(
            AUTH_URL,
            data={
                "page": "https://cloud.mail.ru/",
                "Login": self.account.login,
                "Password": self.account.password,
            },
            timeout=self.timeout,
        )
        # the web flow answers with a redirect chain on success
        if response.status_code != 200 or not response.history:
            raise LoginError(f"mail.ru login failed for {self.account.id}")
        self.session.get(SDC_URL, timeout=self.timeout)
        self._session_keys()

    def _session_keys(self) -> None:
        token_response = self.session.post(CSRF_URL, timeout=self.timeout)
        if token_response.status_code != 200:
            raise ApiError("mailru", token_response.status_code, "csrf token")
        self._token = token_response.json()["body"]["token"]
        dispatcher = self.session.get(
            DISPATCHER_URL, params={"token": self._token}, timeout=self.timeout
        )
        if dispatcher.status_code != 200:
            raise ApiError("mailru", dispatcher.status_code, "dispatcher")
        self._upload_url = dispatcher.json()["body"]["upload"][0]["url"]

    def _require_token(self) -> str:
        if self._token is None:
            self.login()
        assert self._token is not None
        return self._token

    def _params(self, token: str) -> dict:
        # api v2 POSTs carry auth in the query string, not the form body
        params = {"api": "2", "token": token}
        if self.account.login:
            params["x-email"] = self.account.login
        return params

    def _post(self, url: str, params: dict, data: dict):
        response = self.session.post(url, params=params, data=data, timeout=self.timeout)
        if response.status_code == 403 and self._token is not None:
            # token expired: re-login once and retry with the fresh one
            self.login()
            response = self.session.post(
                url, params=self._params(self._token), data=data, timeout=self.timeout
            )
        return response

    # -- public API ----------------------------------------------------------

    def create_doc(self, name: str, *, file_ext: str = "txt") -> MailruDoc:
        """Upload a small carrier file and publish it; returns the public URL."""
        name = sanitize_name(name)
        token = self._require_token()
        home = f"{CHANNEL_DIR}/{name}.{file_ext}"

        # best-effort folder creation; an existing folder is fine
        self._post(FOLDER_ADD_URL, self._params(token), {"home": CHANNEL_DIR})

        body = f"OpenFlux channel carrier {name}\n".encode()
        upload = self.session.put(
            self._upload_url,
            params={"x-email": self.account.login} if self.account.login else {},
            headers={"Content-Type": f"text/{file_ext}"},
            data=body,
            timeout=self.timeout,
        )
        if upload.status_code not in (200, 201):
            raise ApiError("mailru", upload.status_code, "upload")
        file_hash = upload.text.strip()
        if not file_hash:
            raise ApiError("mailru", upload.status_code, "empty upload reply")

        added = self._post(
            FILE_ADD_URL,
            self._params(token),
            {
                "home": home,
                "hash": file_hash,
                "size": str(len(body)),
                "conflict": "rename",
            },
        )
        if added.status_code != 200:
            raise ApiError("mailru", added.status_code, f"file/add: {added.text[:120]}")

        published = self._post(FILE_PUBLISH_URL, self._params(token), {"home": home})
        if published.status_code == 507:
            raise ApiError("mailru", 507, "public links limit exceeded")
        if published.status_code != 200:
            raise ApiError("mailru", published.status_code, f"publish: {published.text[:120]}")
        return MailruDoc(
            url=PUBLIC_URL_PREFIX + published.json()["body"],
            home=home,
            account_id=self.account.id,
        )

    def unpublish(self, weblink_or_url: str) -> None:
        """Revoke a public link (the file itself stays in the cloud)."""
        weblink = weblink_or_url.removeprefix(PUBLIC_URL_PREFIX)
        weblink = weblink.removeprefix("https://cloud.mail.ru/public/")
        token = self._require_token()
        response = self._post(
            FILE_UNPUBLISH_URL, self._params(token), {"weblink": weblink}
        )
        if response.status_code != 200:
            raise ApiError("mailru", response.status_code, "unpublish")
