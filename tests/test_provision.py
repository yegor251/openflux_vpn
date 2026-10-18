import json

import pytest

from oflux_admin.provision import build_channel_config, parse_json_line


def test_build_channel_config_shape():
    cfg = build_channel_config(
        channel="of-alice",
        key="ab" * 32,
        port=20123,
        yandex_url="https://docs.yandex.ru/edit/d/0123456789abcdef",
        mailru_url="https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2",
    )
    lines = cfg.strip().splitlines()
    assert lines[0] == "channel=of-alice"
    # our fork's keys, in plan-friendly order
    assert "yandex=https://docs.yandex.ru/edit/d/0123456789abcdef" in lines
    assert "mailru=https://cloud.mail.ru/public/AbCdEfGh1/IjKlMnOp2" in lines
    assert "key=abababababababababababababababababababababababababababababababab" in lines
    assert "port=20123" in lines
    assert "autoupdate=yes" in lines
    # the installer rejects unknown keys: no extras slipped in
    known = {l.split("=")[0] for l in lines}
    assert known == {"channel", "yandex", "mailru", "key", "port", "autoupdate"}


def test_build_channel_config_without_mailru():
    # the default since the mailru transport was dropped
    cfg = build_channel_config(
        channel="of-x",
        key="a" * 64,
        port=20123,
        yandex_url="https://docs.yandex.ru/edit/d/0123456789abcdef",
    )
    lines = cfg.strip().splitlines()
    assert "mailru=" not in cfg
    known = {l.split("=")[0] for l in lines}
    assert known == {"channel", "yandex", "key", "port", "autoupdate"}


def test_build_channel_config_mode():
    cfg = build_channel_config(channel="of-x", key="a" * 64, port=1, mode="l3")
    assert "mode=l3" in cfg.strip().splitlines()
    # default: no mode line, installer falls back to its own default (l4)
    cfg = build_channel_config(channel="of-x", key="a" * 64, port=1)
    assert "mode=" not in cfg


def test_parse_json_line_skips_noise():
    out = 'warning: something\n{"ok":true,"port":20123}\n'
    assert parse_json_line(out) == {"ok": True, "port": 20123}


def test_parse_json_line_last_wins():
    out = '{"progress":1}\n{"ok":true}\n'
    assert parse_json_line(out) == {"ok": True}


def test_parse_json_line_no_json():
    with pytest.raises(ValueError, match="no JSON"):
        parse_json_line("not json at all")


def test_config_is_valid_json_not_required():
    # the installer config is key=value, not json — guard against regressions
    cfg = build_channel_config(channel="of-x", key="a" * 64, port=1)
    with pytest.raises(json.JSONDecodeError):
        json.loads(cfg)
