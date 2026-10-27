"""Room creation/packing for the cupsonline carrier (no network)."""

import base64
import json

from docprovider.cupsrooms import create_rooms, pack_rooms
from oflux_admin.provision import build_channel_config


def test_pack_rooms_is_v2_envelope_without_padding():
    packed = pack_rooms(["11111111-2222-3333-4444-555555555555"])
    assert "=" not in packed
    assert all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in packed)
    raw = packed + "=" * (-len(packed) % 4)
    assert json.loads(base64.urlsafe_b64decode(raw)) == {
        "v": 2,
        "rooms": ["11111111-2222-3333-4444-555555555555"],
    }


def test_create_rooms_parses_room_uuid(monkeypatch):
    import urllib.error

    from docprovider import cupsrooms

    html = (
        '<div data-room="{&quot;uuid&quot;: &quot;aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee&quot;,'
        ' &quot;title&quot;: null}" data-user="{&quot;uuid&quot;: &quot;ffff0000-1111-2222-3333-444444444444&quot;}">'
    )

    class FakeResponse:
        status = 200

        def read(self):
            return html.encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    seen = []

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        return FakeResponse()

    monkeypatch.setattr(cupsrooms.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cupsrooms.time, "sleep", lambda s: None)
    ids = cupsrooms.create_rooms(3)
    assert ids == ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"] * 3
    assert seen == [cupsrooms.ROOM_URL] * 3


def test_create_room_raises_on_http_error(monkeypatch):
    import urllib.error

    import docprovider.cupsrooms as cupsrooms
    from docprovider.cupsrooms import CupsRoomError, create_room

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(cupsrooms.ROOM_URL, 403, "Forbidden", None, None)

    monkeypatch.setattr(cupsrooms.urllib.request, "urlopen", fake_urlopen)
    try:
        create_room()
        raise AssertionError("CupsRoomError expected")
    except CupsRoomError as exc:
        assert "403" in str(exc)


def test_channel_config_carries_cupsonline():
    conf = build_channel_config(
        channel="of-x",
        key="k" * 64,
        port=20010,
        cupsonline="eyJ2Ijoy",
        mode="l3",
    )
    assert "cupsonline=eyJ2Ijoy" in conf
    assert "yandex=" not in conf


def test_first_room_roundtrip():
    from docprovider.cupsrooms import first_room

    packed = pack_rooms(["u-1", "u-2"])
    assert first_room(packed) == "u-1"
