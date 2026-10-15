"""Create published Yandex documents as a signed-in user.

Ports the flow of OpenFluxClientShared ``model/YandexDisk.kt``: the same
Disk web-client calls (models-v2 + /editnew), driven from Python with a
cookie jar instead of an in-browser script. Requires fresh cookies from a
real browser session (Netscape cookies.txt).
"""

from __future__ import annotations

import re
import time

import requests

from .errors import ApiError, CaptchaRequiredError, CookiesExpiredError
from .models import YandexAccount, YandexDoc, sanitize_name
from .netscape import load_into_session

DISK_CLIENT_URL = "https://disk.yandex.ru/client/disk"
MODELS_V2_URL = "https://disk.yandex.ru/models-v2"
EDITNEW_URL = "https://disk.yandex.ru/editnew/docx/disk/openflux"

CHANNEL_DIR = "/disk/openflux"
INFO_PROBE_ATTEMPTS = 30
INFO_PROBE_DELAY_S = 0.5
DEFAULT_TIMEOUT = 30.0

_CHECKPOINT_RE = re.compile(r"passport\.yandex\.|showcaptcha")


def _extract_preloaded(html: str) -> dict:
    """Pull the config object out of the page's #preloaded-data script."""
    match = re.search(
        r'<script[^>]*id="preloaded-data"[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if not match:
        raise CookiesExpiredError("disk page has no #preloaded-data (not signed in?)")
    import json

    data = json.loads(match.group(1))
    config = data.get("config") or {}
    if not config.get("sk") or not config.get("skExternal"):
        raise CookiesExpiredError("disk page config lacks sk/skExternal (not signed in?)")
    return config


class YandexDocCreator:
    """Creates one published document per call, as the cookies' user."""

    def __init__(
        self,
        account: YandexAccount,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sleep=time.sleep,
    ) -> None:
        self.account = account
        self.timeout = timeout
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": DISK_CLIENT_URL,
            }
        )
        applied = load_into_session(self.session, account.cookies_path, "yandex.ru")
        if applied == 0:
            raise CookiesExpiredError(
                f"{account.cookies_path}: no .yandex.ru cookies found"
            )

    # -- low-level Disk API -------------------------------------------------

    def _check_page(self, response: requests.Response) -> None:
        if _CHECKPOINT_RE.search(response.url):
            if "showcaptcha" in response.url:
                raise CaptchaRequiredError("yandex disk: captcha checkpoint")
            raise CookiesExpiredError(
                "yandex disk redirected to passport (cookies expired?)"
            )

    def _disk_page_config(self) -> dict:
        response = self.session.get(DISK_CLIENT_URL, timeout=self.timeout)
        self._check_page(response)
        if response.status_code != 200:
            raise ApiError("yandex", response.status_code, "disk client page")
        return _extract_preloaded(response.text)

    def _models_v2(self, config: dict, method: str, params: dict):
        response = self.session.post(
            MODELS_V2_URL,
            params={"m": method},
            json={
                "sk": config["sk"],
                "connection_id": config.get("idClient", ""),
                "apiMethod": method,
                "requestParams": params,
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise ApiError("yandex", response.status_code, f"models-v2 {method}")
        payload = response.json()
        if isinstance(payload, dict) and payload.get("error"):
            err = payload["error"]
            raise ApiError(
                "yandex", response.status_code, f"{method}: {err.get('title') or err.get('code')}"
            )
        return payload

    def _resource_info(self, config: dict, path: str) -> dict | None:
        result = self._models_v2(
            config, "mpfs/bulk-resource-info", {"ids": [path]}
        )
        if isinstance(result, list) and result:
            return result[0]
        return None

    # -- public API ----------------------------------------------------------

    def create(self, name: str) -> YandexDoc:
        """Create /disk/openflux/<name>.docx and publish it for editing."""
        name = sanitize_name(name)
        config = self._disk_page_config()

        if self._resource_info(config, CHANNEL_DIR) is None:
            self._models_v2(config, "mpfs/mkdir", {"path": CHANNEL_DIR})

        path = f"{CHANNEL_DIR}/{name}.docx"
        file = self._resource_info(config, path)
        if file is None:
            response = self.session.get(
                EDITNEW_URL,
                params={"sk": config["skExternal"], "filename": name},
                timeout=self.timeout,
            )
            if response.status_code != 200:
                raise ApiError("yandex", response.status_code, "editnew docx")
            for _ in range(INFO_PROBE_ATTEMPTS):
                file = self._resource_info(config, path)
                if file is not None:
                    break
                self._sleep(INFO_PROBE_DELAY_S)
        if not file or not file.get("meta"):
            raise ApiError("yandex", 200, "document did not appear on disk")

        self._models_v2(
            config,
            "mpfs/set-public",
            {
                "path": path,
                "type": "file",
                "allowDefaultSettingsAvailable": True,
            },
        )
        self._models_v2(
            config,
            "mpfs/office-set-access-state",
            {
                "resourceId": file["meta"]["resource_id"],
                "accessState": "all",
            },
        )
        file = self._resource_info(config, path) or file
        meta = file.get("meta", {})
        url = meta.get("office_online_sharing_url")
        if not url or meta.get("office_access_state") != "all":
            raise ApiError("yandex", 200, "failed to enable link editing")
        return YandexDoc(url=url, path=path, account_id=self.account.id)

    def unpublish(self, path: str) -> None:
        """Best-effort revoke: close public access to the document."""
        config = self._disk_page_config()
        self._models_v2(config, "mpfs/unpublish-all", {"path": path})
