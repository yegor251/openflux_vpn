import pytest

from docprovider.errors import CaptchaRequiredError, ValidationError
from docprovider.validate import (
    check_mailru_public,
    check_yandex_doc,
    normalize_mailru_weblink,
    validate_mailru_url,
    validate_yandex_url,
)
from fakes import FakeResponse, FakeSession

FULL = "https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2"


def test_normalize_accepts_bare_and_full_forms():
    assert normalize_mailru_weblink("AbCdEfGh1/IjKlMnOp2") == FULL
    assert normalize_mailru_weblink(FULL) == FULL
    assert normalize_mailru_weblink(FULL + "/") == FULL


def test_normalize_rejects_garbage():
    with pytest.raises(ValidationError):
        normalize_mailru_weblink("https://example.com/public/a/b")
    with pytest.raises(ValidationError):
        normalize_mailru_weblink("https://cloud.mail.ru/public/x")


def test_strict_validators():
    assert validate_mailru_url(FULL) == FULL
    doc = "https://docs.yandex.ru/edit/d/abcdef0123456789"
    assert validate_yandex_url(doc) == doc
    with pytest.raises(ValidationError):
        validate_yandex_url("https://disk.yandex.ru/client/disk")
    with pytest.raises(ValidationError):
        validate_mailru_url("https://cloud.mail.ru/home/file.txt")


def test_mailru_live_check_parses_editor_answer():
    session = FakeSession()
    session.on(
        "POST",
        "r7/edit",
        lambda req: FakeResponse(
            200, payload={"document": {"key": "k"}, "editorConfig": {}}
        ),
    )
    check_mailru_public(FULL, session=session)
    sent = session.calls[0][2].json_body
    assert sent == {
        "x-email": "anonym",
        "public": "/AbCdEfGh1/IjKlMnOp2",
        "platform": "desktop_web",
    }


def test_mailru_live_check_rejects_dead_link():
    session = FakeSession()
    session.on("POST", "r7/edit", lambda req: FakeResponse(404, text="nope"))
    with pytest.raises(ValidationError):
        check_mailru_public(FULL, session=session)


def test_yandex_live_check_detects_captcha_redirect():
    session = FakeSession()
    session.on(
        "GET",
        "docs.yandex.ru",
        lambda req: FakeResponse(200, url="https://docs.yandex.ru/showcaptcha?cc=1"),
    )
    with pytest.raises(CaptchaRequiredError):
        check_yandex_doc("https://docs.yandex.ru/edit/d/abcdef0123456789", session=session)


def test_yandex_live_check_accepts_editor_page():
    session = FakeSession()
    session.on(
        "GET",
        "docs.yandex.ru",
        lambda req: FakeResponse(
            200,
            url="https://docs.yandex.ru/edit/d/abcdef0123456789",
            text='<script id="client-config">{}</script>',
        ),
    )
    check_yandex_doc("https://docs.yandex.ru/edit/d/abcdef0123456789", session=session)


def test_yandex_live_check_accepts_pow_wall():
    session = FakeSession()
    session.on(
        "GET",
        "docs.yandex.ru",
        lambda req: FakeResponse(
            200,
            url="https://docs.yandex.ru/showcaptchafast?d=DEADBEEF",
            text="<html>captcha</html>",
        ),
    )
    check_yandex_doc("https://docs.yandex.ru/edit/d/abcdef0123456789", session=session)
