import time

from docprovider.netscape import jar_summary, load_into_session, parse_netscape


SAMPLE = """\
# Netscape HTTP Cookie File
# comment lines are ignored

.yandex.ru\tTRUE\t/\tTRUE\t0\tSessionCookie\tabc
.yandex.ru\tTRUE\t/\tTRUE\t9999999999\tGood\tdef
#HttpOnly_.yandex.ru\tTRUE\t/\tTRUE\t9999999999\tHidden\txyz
.mail.ru\tTRUE\t/\tFALSE\t9999999999\tOtherSvc\tzzz
.yandex.ru\tTRUE\t/\tTRUE\t1000\tExpired\told
broken line without tabs
"""


def test_parse_skips_comments_and_malformed_rows():
    cookies = parse_netscape(SAMPLE)
    names = [c.name for c in cookies]
    assert names == ["SessionCookie", "Good", "Hidden", "OtherSvc", "Expired"]


def test_load_scopes_to_yandex_and_drops_expired():
    import requests

    session = requests.Session()
    applied = load_into_session(session, _write(SAMPLE), "yandex.ru")
    assert applied == 3  # SessionCookie, Good, Hidden
    names = {c.name for c in session.cookies}
    assert names == {"SessionCookie", "Good", "Hidden"}


def test_expired_cookies_are_dropped_when_time_passed():
    cookies = parse_netscape(SAMPLE)
    now = int(time.time())
    expired = [c for c in cookies if c.expires and c.expires <= now]
    assert [c.name for c in expired] == ["Expired"]


def test_summary_has_no_values():
    summary = jar_summary(_write(SAMPLE))
    assert "yandex.ru" in summary and "mail.ru" in summary
    assert "abc" not in summary and "def" not in summary


def _write(content: str) -> str:
    import tempfile
    import pathlib

    path = pathlib.Path(tempfile.mkdtemp()) / "cookies.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)
