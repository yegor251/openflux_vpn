"""Multi-VDS placement: pick_server chooses the emptiest server."""

import pytest

from oflux_admin.channels import pick_server
from oflux_admin.config import AdminConfig, Server, UserRegistry


def _registry(tmp_path, **users):
    reg = UserRegistry(tmp_path / "users.json")
    for name, vds in users.items():
        reg.add(name, created="2026-10-18T00:00:00+00:00", vds=vds, port=20000)
    return reg


SERVERS = [
    Server(name="a", host="203.0.113.1", max_channels=2),
    Server(name="b", host="203.0.113.2", max_channels=20),
]


def _config():
    return AdminConfig(settings=None, servers=SERVERS)


def test_picks_server_with_most_free_slots(tmp_path):
    reg = _registry(tmp_path, **{"of-a1": "a", "of-a2": "a", "of-b1": "b"})
    assert pick_server(_config(), reg).name == "b"


def test_skips_full_server(tmp_path):
    reg = _registry(tmp_path, **{"of-a1": "a", "of-a2": "a"})
    assert pick_server(_config(), reg).name == "b"


def test_empty_registry_picks_the_biggest(tmp_path):
    reg = _registry(tmp_path)
    assert pick_server(_config(), reg).name == "b"


def test_all_full_raises(tmp_path):
    reg = _registry(tmp_path, **{"of-a1": "a", "of-a2": "a", **{f"of-b{i}": "b" for i in range(20)}})
    with pytest.raises(ValueError, match="all servers are at max_channels"):
        pick_server(_config(), reg)


def test_revoked_users_free_their_slots(tmp_path):
    servers = [Server(name="a", host="203.0.113.1", max_channels=2),
               Server(name="b", host="203.0.113.2", max_channels=2)]
    cfg = AdminConfig(settings=None, servers=servers)
    reg = _registry(tmp_path, **{"of-a1": "a", "of-a2": "a", "of-b1": "b", "of-b2": "b"})
    with pytest.raises(ValueError, match="all servers"):
        pick_server(cfg, reg)
    reg.revoke("of-a1")
    assert pick_server(cfg, reg).name == "a"
