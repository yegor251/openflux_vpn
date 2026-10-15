import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _mailru_creds():
    cookies = os.environ.get("DOCPROVIDER_MAILRU_COOKIES")
    if cookies:
        return "", "", cookies
    login = os.environ.get("DOCPROVIDER_MAILRU_LOGIN")
    password = os.environ.get("DOCPROVIDER_MAILRU_PASSWORD")
    if not login or not password:
        pytest.skip("DOCPROVIDER_MAILRU_COOKIES or LOGIN/PASSWORD not set")
    return login, password, ""


def _yandex_cookies():
    path = os.environ.get("DOCPROVIDER_YANDEX_COOKIES")
    if not path or not Path(path).exists():
        pytest.skip("DOCPROVIDER_YANDEX_COOKIES not set or file missing")
    return path
