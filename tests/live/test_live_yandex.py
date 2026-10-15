"""Real Yandex Disk: create, publish, verify via exported cookies."""

import time

import pytest

from docprovider.models import YandexAccount
from docprovider.validate import check_yandex_doc
from docprovider.yandex import YandexDocCreator

from conftest import _yandex_cookies

pytestmark = pytest.mark.live


def test_create_publish_verify():
    cookies = _yandex_cookies()
    name = f"of-live-{int(time.time())}"
    creator = YandexDocCreator(YandexAccount(id="live", cookies_path=cookies))

    doc = creator.create(name)
    # authenticated check first: the doc must open for its own account
    check_yandex_doc(doc.url, session=creator.session)
    # anonymous GET normally lands on the PoW captcha (showcaptchafast),
    # which the transport solves automatically — validator accepts it
    check_yandex_doc(doc.url)

    # best-effort cleanup: close public editing again
    try:
        creator.unpublish(doc.path)
    except Exception:
        pass  # report, but don't fail the test on cleanup trouble
