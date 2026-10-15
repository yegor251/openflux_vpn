"""Offline flow test: full document creation against a scripted session."""

import pathlib
import tempfile

import pytest

from docprovider.errors import CookiesExpiredError
from docprovider.yandex import YandexDocCreator, _extract_preloaded
from fakes import FakeResponse, FakeSession

PAGE = (
    '<html><script id="preloaded-data">{"config": '
    '{"sk": "sk1", "skExternal": "sk2", "idClient": "cid"}}'
    "</script></html>"
)
DOC_META = {
    "meta": {
        "resource_id": "res-1",
        "office_online_sharing_url": "https://docs.yandex.ru/edit/d/last",
        "office_access_state": "all",
    }
}


def _cookies_file() -> str:
    p = pathlib.Path(tempfile.mkdtemp()) / "cookies.txt"
    p.write_text(".yandex.ru\tTRUE\t/\tTRUE\t9999999999\tSessionCookie\tabc\n")
    return str(p)


def _account():
    return type("A", (), {"id": "ya", "cookies_path": _cookies_file()})()


def _scripted_session() -> FakeSession:
    session = FakeSession()
    session.on(
        "GET",
        "disk.yandex.ru/client/disk",
        lambda req: FakeResponse(
            200, url="https://disk.yandex.ru/client/disk", text=PAGE
        ),
    )
    session.on("POST", "models-v2", _dispatch)
    session.on(
        "GET",
        "editnew/docx",
        lambda req: FakeResponse(
            200, url="https://disk.yandex.ru/editnew/docx/disk/openflux"
        ),
    )
    return session


def _dispatch(req):
    method = req.json_body["apiMethod"]
    if method == "mpfs/bulk-resource-info":
        path = req.json_body["requestParams"]["ids"][0]
        if path == "/disk/openflux/of-alice.docx":
            # first probe: the doc does not exist yet; later probes: it does
            seen = _dispatch.__dict__.setdefault("probes", 0)
            _dispatch.__dict__["probes"] = seen + 1
            return FakeResponse(200, payload=[DOC_META] if seen else [])
        return FakeResponse(200, payload=[])
    if method in ("mpfs/mkdir", "mpfs/set-public", "mpfs/office-set-access-state"):
        return FakeResponse(200, payload={})
    raise AssertionError(f"unexpected method {method}")


def test_extract_preloaded_parses_config():
    config = _extract_preloaded(PAGE)
    assert config["sk"] == "sk1" and config["skExternal"] == "sk2"


def test_extract_preloaded_rejects_anonymous_page():
    with pytest.raises(CookiesExpiredError):
        _extract_preloaded(
            "<html><script id='preloaded-data'>{\"config\": {}}</script></html>"
        )


def test_full_create_flow():
    session = _scripted_session()
    creator = YandexDocCreator(_account(), session=session, sleep=lambda s: None)
    doc = creator.create("of-alice")

    assert doc.url == "https://docs.yandex.ru/edit/d/last"
    assert doc.path == "/disk/openflux/of-alice.docx"

    methods = [c[2].json_body.get("apiMethod") for c in session.calls if c[0] == "POST"]
    assert "mpfs/mkdir" in methods
    assert "mpfs/set-public" in methods
    assert "mpfs/office-set-access-state" in methods
    # editnew must carry the external sk and the file name
    editnew = [c for c in session.calls if "editnew" in c[1]][0]
    assert editnew[2].params["sk"] == "sk2"
    assert editnew[2].params["filename"] == "of-alice"


def test_passport_redirect_raises():
    session = FakeSession()
    session.on(
        "GET",
        "disk.yandex.ru/client/disk",
        lambda req: FakeResponse(
            200, url="https://passport.yandex.ru/auth?retpath=x", text="login"
        ),
    )
    creator = YandexDocCreator(_account(), session=session)
    with pytest.raises(CookiesExpiredError):
        creator.create("of-alice")


def test_no_cookie_rollover_to_captcha_page():
    session = FakeSession()
    session.on(
        "GET",
        "disk.yandex.ru/client/disk",
        lambda req: FakeResponse(
            200, url="https://disk.yandex.ru/showcaptcha?cc=1", text="captcha"
        ),
    )
    from docprovider.errors import CaptchaRequiredError

    creator = YandexDocCreator(_account(), session=session)
    with pytest.raises(CaptchaRequiredError):
        creator.create("of-alice")
