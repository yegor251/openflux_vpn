import json

import pytest

from docprovider.errors import DocProviderError  # noqa: F401 - package import smoke
from oflux_admin.config import Server, UserRegistry, load_config, sanitize_channel


def test_sanitize_channel():
    assert sanitize_channel("Alice") == "alice"
    assert sanitize_channel("Bob_Marley 1") == "bob-marley-1"
    with pytest.raises(ValueError):
        sanitize_channel("-nope")
    with pytest.raises(ValueError):
        sanitize_channel("x" * 40)


def test_alloc_port_skips_used(tmp_path):
    reg = UserRegistry(tmp_path / "users.json")
    server = Server(name="test1", host="h", port_range=(20000, 20005))
    assert reg.alloc_port(server) == 20000
    reg.add("of-a", created="now", port=20000, vds="test1")
    assert reg.alloc_port(server) == 20001
    reg.add("of-b", created="now", port=20001, vds="test1")
    assert reg.alloc_port(server) == 20002
    # revoked users free their ports
    reg.revoke("of-a")
    assert reg.alloc_port(server) == 20000


def test_alloc_port_exhausted(tmp_path):
    reg = UserRegistry(tmp_path / "users.json")
    server = Server(name="test1", host="h", port_range=(20000, 20000))
    reg.add("of-a", created="now", port=20000, vds="test1")
    with pytest.raises(ValueError, match="exhausted"):
        reg.alloc_port(server)


def test_add_duplicate_raises_and_persists(tmp_path):
    reg = UserRegistry(tmp_path / "users.json")
    reg.add("of-a", created="now", port=20000, vds="test1")
    with pytest.raises(ValueError, match="already exists"):
        reg.add("of-a", created="now", port=20001, vds="test1")
    # a fresh instance reads the same file
    reg2 = UserRegistry(tmp_path / "users.json")
    assert reg2.get("of-a")["port"] == 20000
    data = json.loads((tmp_path / "users.json").read_text())
    assert data["version"] == 1


def test_revoke_idempotence_checks(tmp_path):
    reg = UserRegistry(tmp_path / "users.json")
    reg.add("of-a", created="now", port=20000, vds="test1")
    reg.revoke("of-a")
    assert reg.get("of-a")["revoked"] is True
    with pytest.raises(ValueError, match="already revoked"):
        reg.revoke("of-a")


def test_load_config_yaml(tmp_path):
    cfg_file = tmp_path / "servers.yaml"
    cfg_file.write_text(
        """
settings:
  mailru_login: u@mail.ru
  openflux_bin: /x/openflux
servers:
  - name: test1
    host: 1.2.3.4
    user: root
    key_path: ~/.ssh/id
    port_range: [20000, 20099]
    max_channels: 7
"""
    )
    cfg = load_config(cfg_file)
    assert cfg.settings.mailru_login == "u@mail.ru"
    s = cfg.server("test1")
    assert (s.host, s.port_range, s.max_channels) == ("1.2.3.4", (20000, 20099), 7)
    assert cfg.server(None) is s
    with pytest.raises(ValueError, match="unknown server"):
        cfg.server("nope")
