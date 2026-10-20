"""oflux_bot pure logic: invite tokens, alert dedup, link masking."""

import os

from oflux_bot.alerts import BACKUP_MAX_AGE_S, AlertState, probe_backup
from oflux_bot.config import BotUsers, bot_config_dir, mask_link


# -- invite tokens / bindings ------------------------------------------------

def test_invite_claim_is_single_use(tmp_path):
    users = BotUsers(tmp_path / "bot_users.json")
    token = users.new_invite()
    assert users.claim_invite(token, 111)
    assert not users.claim_invite(token, 222)  # already used
    assert users.is_registered(111)
    assert not users.is_registered(222)


def test_unknown_invite_rejected(tmp_path):
    users = BotUsers(tmp_path / "bot_users.json")
    assert not users.claim_invite("nope", 111)


def test_bind_and_channel_ownership(tmp_path):
    users = BotUsers(tmp_path / "bot_users.json")
    token = users.new_invite()
    users.claim_invite(token, 111)
    assert users.channel_of(111) is None
    users.bind_channel(111, "of-user111")
    assert users.channel_of(111) == "of-user111"
    assert users.channel_owner("of-user111") == 111
    users.unbind(111)
    assert users.channel_of(111) is None


# -- watchdog dedup ----------------------------------------------------------

def test_alert_after_k_fails_clear_after_m_oks():
    st = AlertState()
    assert [st.feed("ch", False, now=t) for t in range(1, 3)] == [None, None]
    assert st.feed("ch", False, now=3) == "alert"
    # still down: no repeat before cooldown
    assert st.feed("ch", False, now=4) is None
    # recovery needs M consecutive oks
    assert st.feed("ch", True, now=5) is None
    assert st.feed("ch", True, now=6) == "clear"


def test_flapping_does_not_realert_without_recovery():
    st = AlertState()
    for t in range(1, 4):
        st.feed("ch", False, now=t)
    assert st.active["ch"]
    st.feed("ch", True, now=4)  # only one ok - not recovered
    st.feed("ch", False, now=5)  # fails again while still active
    assert st.feed("ch", False, now=6) is None  # no second 'alert'


def test_repeat_reminder_only_after_cooldown():
    st = AlertState()
    for t in range(1, 4):
        st.feed("cookies", False, now=t)
    assert st.active["cookies"]
    assert not st.repeat_due("cookies", now=100)  # too soon
    assert st.repeat_due("cookies", now=3601 + 3)  # cooldown passed


# -- link masking ------------------------------------------------------------

def test_mask_link_hides_key():
    assert mask_link("openflux://v1/AbCdEf") == "openflux://v1/<redacted>"
    assert "AbCdEf" not in mask_link("openflux://v1/AbCdEf")
    assert mask_link("not-a-link").startswith("not-a-link")


# -- backup freshness (SRV-04) -------------------------------------------------

def test_backup_probe_staleness(tmp_path):
    now = 1_000_000.0
    assert probe_backup(tmp_path, now=now) is False  # no stamp at all
    stamp = tmp_path / ".last-backup"
    stamp.write_text("")
    os.utime(stamp, (now, now))
    assert probe_backup(tmp_path, now=now) is True  # fresh stamp
    os.utime(stamp, (now - BACKUP_MAX_AGE_S - 1, now - BACKUP_MAX_AGE_S - 1))
    assert probe_backup(tmp_path, now=now) is False  # too old


def test_config_dir_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OFLUX_BOT_CONFIG_DIR", str(tmp_path))
    assert bot_config_dir() == tmp_path
