"""Round-trip against the real openflux binary (offline, no network).

Skipped unless the binary is found via OPENFLUX_BIN, PATH, or ./openflux.
Build it once: cd OpenFlux && go build -o ../openflux .
"""

import os
import shutil
from pathlib import Path

import pytest

from docprovider.links import DIRECT_PRIORITY, MAILRU_PRIORITY, YANDEX_PRIORITY, make_link, parse_link
from docprovider.models import ChannelDocs, MailruDoc, YandexDoc


def _binary() -> str | None:
    candidate = os.environ.get("OPENFLUX_BIN") or shutil.which("openflux")
    if candidate:
        return candidate
    local = Path(__file__).resolve().parent.parent / "bin" / "openflux"
    return str(local) if local.exists() else None


@pytest.fixture()
def binary():
    path = _binary()
    if not path:
        pytest.skip("openflux binary not built/found")
    return path


DOCS = ChannelDocs(
    name="of-test",
    yandex=YandexDoc(
        url="https://docs.yandex.ru/edit/d/0123456789abcdef",
        path="/disk/openflux/of-test.docx",
        account_id="ya",
    ),
    mailru=MailruDoc(
        url="https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2",
        home="/openflux/of-test.txt",
        account_id="mr",
    ),
)


def test_make_and_parse_roundtrip(binary):
    link = make_link(
        name="of-test",
        secret="unit-test-secret-0123456789abcdef",
        docs=DOCS,
        direct_dial="203.0.113.10:8445",
        binary=binary,
    )
    assert link.startswith("openflux://v1/")

    config = parse_link(link, binary=binary)
    assert config["name"] == "of-test"
    assert config["negotiate"] is True
    assert config["secret"] == "unit-test-secret-0123456789abcdef"

    transports = {t["type"]: t for t in config["transports"]}
    assert transports["direct"]["dial"] == "203.0.113.10:8445"
    assert transports["direct"]["priority"] == DIRECT_PRIORITY
    assert transports["yandex"]["url"] == DOCS.yandex.url
    assert transports["yandex"]["priority"] == YANDEX_PRIORITY
    assert transports["mailru"]["url"] == DOCS.mailru.url
    assert transports["mailru"]["priority"] == MAILRU_PRIORITY


def test_make_link_without_direct(binary):
    link = make_link(
        name="of-test",
        secret="unit-test-secret-0123456789abcdef",
        docs=DOCS,
        binary=binary,
    )
    config = parse_link(link, binary=binary)
    assert {t["type"] for t in config["transports"]} == {"yandex", "mailru"}


def test_secret_too_short_rejected(binary):
    from docprovider.errors import OpenFluxBinaryError

    with pytest.raises(OpenFluxBinaryError):
        make_link(
            name="of-test",
            secret="short",
            docs=DOCS,
            binary=binary,
        )
