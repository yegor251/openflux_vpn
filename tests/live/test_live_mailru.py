"""Real Mail.ru Cloud: login, create, publish, verify, unpublish."""

import time

import pytest

from docprovider.mailru import MailruClient
from docprovider.models import MailruAccount
from docprovider.validate import check_mailru_public, validate_mailru_url

from conftest import _mailru_creds

pytestmark = pytest.mark.live


def test_create_publish_verify_unpublish():
    login, password, cookies = _mailru_creds()
    name = f"of-live-{int(time.time())}"
    account = (
        MailruAccount(id="live", cookies_path=cookies)
        if cookies
        else MailruAccount(id="live", login=login, password=password)
    )
    client = MailruClient(account)

    doc = client.create_doc(name)
    assert validate_mailru_url(doc.url) == doc.url
    # anonymous check: exactly how the OpenFlux mailru transport sees it
    check_mailru_public(doc.url)

    client.unpublish(doc.url)
    # after unpublish the anonymous API must stop resolving the link
    from docprovider.errors import DocProviderError

    with pytest.raises(DocProviderError):
        check_mailru_public(doc.url)
