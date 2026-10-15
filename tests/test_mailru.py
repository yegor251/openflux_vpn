"""Offline flow test: cookie session -> upload -> publish against a fake."""

import pytest

from docprovider.errors import ApiError, LoginError
from docprovider.mailru import MailruClient
from docprovider.models import MailruAccount
from fakes import FakeResponse, FakeSession

HASH = "19D0AFE530FC6BD58F4BFF1880D23E2D79839524"


def _scripted_session() -> FakeSession:
    session = FakeSession()
    session.on("GET", "auth.mail.ru/sdc", lambda req: FakeResponse(200))
    session.on("POST", "api/v2/tokens/csrf", lambda req: FakeResponse(200, payload={"body": {"token": "TOKEN"}}))
    session.on(
        "GET",
        "api/v2/dispatcher",
        lambda req: FakeResponse(200, payload={"body": {"upload": [{"url": "https://uploader.cloud.mail.ru/upload-web/"}]}}),
    )
    return session


def _cookie_account():
    import tempfile, pathlib

    p = pathlib.Path(tempfile.mkdtemp()) / "cookies.txt"
    p.write_text(".mail.ru\tTRUE\t/\tTRUE\t9999999999\tMpop\tabc\n")
    return MailruAccount(id="mr", login="u@mail.ru", cookies_path=str(p))


def test_full_create_flow_from_cookies():
    session = _scripted_session()
    session.on(
        "PUT",
        "uploader.cloud.mail.ru/upload-web/",
        lambda req: FakeResponse(201, text=HASH),
    )
    session.on("POST", "api/v2/folder/add", lambda req: FakeResponse(200, payload={}))
    session.on("POST", "api/v2/file/add", lambda req: FakeResponse(200, payload={"body": "/openflux/of-alice.txt"}))
    session.on("POST", "api/v2/file/publish", lambda req: FakeResponse(200, payload={"body": "AbCdEfGh1/IjKlMnOp2"}))

    client = MailruClient(_cookie_account(), session=session)
    doc = client.create_doc("of-alice")

    assert doc.url == "https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2"
    assert doc.home == "/openflux/of-alice.txt"

    # upload: PUT raw body, no multipart, x-email in query
    upload = [c for c in session.calls if c[0] == "PUT"][0]
    assert upload[2].params == {"x-email": "u@mail.ru"}
    assert b"of-alice" in upload[2].data and len(upload[2].data) < 200

    # v2 POSTs carry auth in the query string, not the form body
    added = [c for c in session.calls if "file/add" in c[1]][0][2]
    assert added.params == {"api": "2", "token": "TOKEN", "x-email": "u@mail.ru"}
    assert added.data["hash"] == HASH and added.data["conflict"] == "rename"
    published = [c for c in session.calls if "publish" in c[1]][0][2]
    assert published.data == {"home": "/openflux/of-alice.txt"}


def test_unpublish_strips_prefix():
    session = _scripted_session()
    session.on("POST", "api/v2/file/unpublish", lambda req: FakeResponse(200, payload={}))

    client = MailruClient(_cookie_account(), session=session)
    client.unpublish("https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2")
    sent = [c for c in session.calls if "unpublish" in c[1]][0][2]
    assert sent.data == {"weblink": "AbCdEfGh1/IjKlMnOp2"}
    assert sent.params["token"] == "TOKEN"


def test_cookie_login_failure_raises():
    from docprovider.errors import CookiesExpiredError

    import tempfile, pathlib

    p = pathlib.Path(tempfile.mkdtemp()) / "cookies.txt"
    p.write_text("no cookies here\n")
    session = FakeSession()
    client = MailruClient(MailruAccount(id="mr", cookies_path=str(p)), session=session)
    with pytest.raises(CookiesExpiredError):
        client.create_doc("of-alice")


def test_password_login_failure_raises():
    session = FakeSession()
    session.on(
        "POST",
        "auth.mail.ru/cgi-bin/auth",
        lambda req: FakeResponse(200, url="https://auth.mail.ru/cgi-bin/auth", history=[]),
    )
    client = MailruClient(MailruAccount(id="mr", login="u", password="bad"), session=session)
    with pytest.raises(LoginError):
        client.create_doc("of-alice")


def test_expired_token_relogins_and_retries():
    session = FakeSession()
    logins = {"count": 0}

    def auth(req):
        logins["count"] += 1
        return FakeResponse(200, url="https://cloud.mail.ru/", history=[object()])

    session.on("POST", "auth.mail.ru/cgi-bin/auth", auth)
    session.on("GET", "auth.mail.ru/sdc", lambda req: FakeResponse(200))

    def csrf(req):
        return FakeResponse(200, payload={"body": {"token": f"T{logins['count']}"}})

    session.on("POST", "api/v2/tokens/csrf", csrf)
    session.on(
        "GET",
        "api/v2/dispatcher",
        lambda req: FakeResponse(200, payload={"body": {"upload": [{"url": "https://uploader.cloud.mail.ru/upload-web/"}]}}),
    )
    session.on("PUT", "uploader.cloud.mail.ru", lambda req: FakeResponse(201, text=HASH))
    session.on("POST", "api/v2/folder/add", lambda req: FakeResponse(200, payload={}))

    def publish(req):
        # the first attempt rides the stale token, the retry must use T2
        return (
            FakeResponse(403, payload={"body": "token"})
            if req.params.get("token") == "T1"
            else FakeResponse(200, payload={"body": "Ab1/Cd2"})
        )

    session.on("POST", "api/v2/file/publish", publish)
    session.on("POST", "api/v2/file/add", lambda req: FakeResponse(200, payload={}))

    client = MailruClient(MailruAccount(id="mr", login="u@mail.ru", password="p"), session=session)
    doc = client.create_doc("of-alice")
    assert doc.url == "https://cloud.mail.ru/public/Ab1/Cd2"
    assert logins["count"] == 2


def test_public_links_limit_is_reported():
    session = _scripted_session()
    session.on("PUT", "uploader.cloud.mail.ru", lambda req: FakeResponse(201, text=HASH))
    session.on("POST", "api/v2/folder/add", lambda req: FakeResponse(200, payload={}))
    session.on("POST", "api/v2/file/add", lambda req: FakeResponse(200, payload={}))
    session.on("POST", "api/v2/file/publish", lambda req: FakeResponse(507, payload={}))

    client = MailruClient(_cookie_account(), session=session)
    with pytest.raises(ApiError, match="limit"):
        client.create_doc("of-alice")
