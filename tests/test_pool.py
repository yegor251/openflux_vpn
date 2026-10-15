import json

import pytest

from docprovider.errors import AllAccountsExhaustedError
from docprovider.pool import AccountPool, PoolLimits

CONFIG = {
    "mailru": [
        {"id": "mr1", "login": "a", "password": "x"},
        {"id": "mr2", "login": "b", "password": "y"},
    ],
    "yandex": [{"id": "ya1", "cookies_path": "/tmp/ya1.txt"}],
}


def _make(tmp_path, limits=None):
    config = tmp_path / "accounts.json"
    config.write_text(json.dumps(CONFIG))
    state = tmp_path / "accounts.state.json"
    return AccountPool(config, state_path=state, limits=limits)


def test_pick_round_robin_by_least_used(tmp_path):
    pool = _make(tmp_path)
    assert pool.pick("mailru").id == "mr1"
    pool.record("mailru", "mr1")
    assert pool.pick("mailru").id == "mr2"
    pool.record("mailru", "mr2")
    # equal usage: sorted order puts mr1 first again
    assert pool.pick("mailru").id == "mr1"


def test_daily_limit_exhaustion(tmp_path):
    pool = _make(tmp_path, limits=PoolLimits(mailru_per_day=2, yandex_per_day=1))
    for _ in range(2):
        pool.record("mailru", "mr1")
    for _ in range(2):
        pool.record("mailru", "mr2")
    with pytest.raises(AllAccountsExhaustedError):
        pool.pick("mailru")
    # other kind unaffected
    assert pool.pick("yandex").id == "ya1"


def test_state_survives_restart_same_day(tmp_path):
    pool = _make(tmp_path, limits=PoolLimits(mailru_per_day=1, yandex_per_day=1))
    pool.record("mailru", "mr1")
    pool.record("mailru", "mr2")
    # a fresh instance reads the same state file: everyone is used up
    pool2 = AccountPool(
        tmp_path / "accounts.json",
        state_path=tmp_path / "accounts.state.json",
        limits=PoolLimits(mailru_per_day=1, yandex_per_day=1),
    )
    with pytest.raises(AllAccountsExhaustedError):
        pool2.pick("mailru")


def test_restart_skips_exhausted_account(tmp_path):
    pool = _make(tmp_path, limits=PoolLimits(mailru_per_day=1, yandex_per_day=1))
    pool.record("mailru", "mr1")
    pool2 = AccountPool(
        tmp_path / "accounts.json",
        state_path=tmp_path / "accounts.state.json",
        limits=PoolLimits(mailru_per_day=1, yandex_per_day=1),
    )
    # mr1 is spent for today; the fresh pool must move on to mr2
    assert pool2.pick("mailru").id == "mr2"


def test_state_resets_next_day(tmp_path, monkeypatch):
    import time

    pool = _make(tmp_path)
    pool.record("mailru", "mr1")
    monkeypatch.setattr(
        time,
        "localtime",
        lambda t=None: time.struct_time((2026, 1, 2, 0, 0, 0, 0, 2, 0)),
    )
    pool2 = AccountPool(tmp_path / "accounts.json", state_path=tmp_path / "accounts.state.json")
    assert pool2._used_today("mailru", "mr1") == 0
