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


def test_bot_users_sees_externally_added_invite(tmp_path):
    # the invite CLI writes the file while the long-running bot holds
    # its own BotUsers instance - the bot must see (and keep) it
    path = tmp_path / "bot_users.json"
    bot_view = BotUsers(path)
    cli_view = BotUsers(path)
    token = cli_view.new_invite()
    assert bot_view.claim_invite(token, 555)


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


# -- watchdog delivery rollback (telegram outage must not eat alerts) --------

def test_mass_alert_retries_until_delivered(monkeypatch):
    import asyncio

    from oflux_bot import alerts

    class FakeRegistry:
        def active(self):
            return {"a": {"port": 1}, "b": {"port": 2}}

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: False)
    monkeypatch.setattr(alerts, "probe_port", lambda p: False)

    sent: list[str] = []
    attempts = {"n": 0}

    async def flaky_send(text):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return False  # first mass alert is "lost" in an outage
        sent.append(text)
        return True

    wd = alerts.Watchdog(None, FakeRegistry, flaky_send)

    asyncio.run(wd.run_once())  # delivery failed: must NOT latch active
    assert wd.state.active.get("mass") is not True
    assert sent == []

    asyncio.run(wd.run_once())  # connectivity back: exactly one 🚨 goes out
    assert wd.state.active["mass"] is True
    assert len([s for s in sent if "МАССОВЫЙ" in s]) == 1


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


def test_issue_progress_message_sent_plain_and_editable(tmp_path, monkeypatch):
    """A message sent with ReplyKeyboardRemove can never be edited (Telegram
    rejects editMessageText) - so the issuance progress message goes plain."""
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.config import BotUsers

    monkeypatch.setattr(
        handlers, "issue_channel",
        lambda cfg, bu, tg: {"channel": "chan-x", "link": "openflux://v1/x"},
    )
    bot_users = BotUsers(tmp_path / "bot_users.json")

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users)
    issue = next(
        h.callback for h in dp.message.handlers
        if getattr(h.callback, "__name__", "") == "issue"
    )

    class FakeSent:
        async def edit_text(self, text, **kw):
            self.edited = text

    class FakeMessage:
        def __init__(self):
            self.from_user = type("U", (), {"id": 555})()
            self.answers = []

        async def answer(self, text=None, **kw):
            sent = FakeSent()
            sent.text = text
            self.answers.append((sent, kw))
            return sent

        async def answer_photo(self, *a, **kw):
            self.answers.append((FakeSent(), {"photo": True}))

    msg = FakeMessage()
    asyncio.run(issue(msg))

    progress_sent, progress_kw = msg.answers[0]
    assert "reply_markup" not in progress_kw  # plain, so edit_text works
    assert progress_sent.edited.startswith("Канал chan-x")


def test_download_menu_platform_roundtrip():
    """dl:<platform> shows its guide, back button returns to the menu."""
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=None)

    def find(name):
        return next(
            h.callback for h in dp.callback_query.handlers
            if getattr(h.callback, "__name__", "") == name
        )

    class FakeMessage:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kw):
            self.edits.append((text, kw.get("reply_markup")))

    class FakeQuery:
        def __init__(self, data):
            self.data = data
            self.message = FakeMessage()
            self.answered = 0

        async def answer(self):
            self.answered += 1

    dl_platform, dl_menu = find("dl_platform"), find("dl_menu")

    async def scenario():
        q = FakeQuery("dl:mac")
        await dl_platform(q)
        back = q.message.edits[-1]
        q2 = FakeQuery("dl:menu")
        await dl_menu(q2)
        menu = q2.message.edits[-1]
        return q, back, menu

    q, (mac_text, back_kb), (menu_text, menu_kb) = asyncio.run(scenario())

    assert q.answered == 1
    assert "macos-arm64.dmg" in mac_text
    assert back_kb is not None  # "← К платформам"
    assert "TestFlight" not in mac_text
    assert menu_kb is not None and "платформу" in menu_text


def test_rotate_double_tap_rotates_once(tmp_path, monkeypatch):
    """Two taps on "Подтверждаю" = one rotation, not one per tap (bug 2026-10-25)."""
    import asyncio
    import time

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.config import BotUsers

    calls = []

    def slow_rotate(cfg, channel):  # sync, like the real ops (runs via to_thread)
        calls.append(channel)
        time.sleep(0.05)
        return {"link": "openflux://v1/x"}

    monkeypatch.setattr(handlers, "rotate_channel", slow_rotate)

    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(1134181734, "chan-1")

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users)
    confirm = next(
        h.callback for h in dp.callback_query.handlers
        if getattr(h.callback, "__name__", "") == "rotate_confirm"
    )

    class FakeMessage:
        def __init__(self):
            self.texts = []

        async def edit_reply_markup(self, reply_markup=None):
            self.markup_dropped = True

        async def edit_text(self, text, **kw):
            self.texts.append(("edit", text))

        async def answer_photo(self, *a, **kw):
            self.texts.append(("photo", kw.get("caption")))

    class FakeQuery:
        def __init__(self):
            self.data = "rotate:chan-1"
            self.from_user = type("U", (), {"id": 1134181734})()
            self.message = FakeMessage()
            self.answers = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    async def scenario():
        q1, q2 = FakeQuery(), FakeQuery()
        await asyncio.gather(confirm(q1), confirm(q2))
        return q1, q2

    q1, q2 = asyncio.run(scenario())

    assert calls == ["chan-1"]  # exactly one rotation
    assert q1.message.markup_dropped  # button removed on the first tap
    assert any(t == ("edit",) or "новый ключ" in t[1] for t in q1.message.texts if t[0] == "edit")
    assert any("Уже выполняется" in (a or "") for a in q2.answers)  # tap 2 rejected
