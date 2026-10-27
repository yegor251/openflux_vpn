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


# -- carrier verdicts (R1/R2) -------------------------------------------------

def _pack(rooms):
    from docprovider.cupsrooms import pack_rooms

    return pack_rooms(rooms)


def test_carrier_monitor_verdicts_and_persistence(tmp_path):
    from oflux_bot.alerts import CarrierMonitor

    m = CarrierMonitor(tmp_path / "carrier-state.json")
    assert [m.feed("cups", False, now=t) for t in (1, 2)] == [None, None]
    assert m.feed("cups", False, now=3) == "failed"
    assert m.feed("cups", False, now=4) is None  # already marked
    # the mark survives a restart (fresh instance, same file)
    m2 = CarrierMonitor(tmp_path / "carrier-state.json")
    assert m2.failed("cups") and m2.failed_carriers() == ["cups"]
    # recovery needs M consecutive oks
    assert m2.feed("cups", True, now=5) is None
    assert m2.feed("cups", True, now=6) == "recovered"
    assert m2.recovered_carriers() == ["cups"] and not m2.failed("cups")


def test_carrier_monitor_moved_bookkeeping(tmp_path):
    from oflux_bot.alerts import CarrierMonitor

    m = CarrierMonitor(tmp_path / "carrier-state.json")
    m.record_moved("cups", "of-a")
    m.record_moved("cups", "of-a")  # no duplicates
    m.record_moved("cups", "of-b")
    assert m.moved_channels("cups") == ["of-a", "of-b"]
    m2 = CarrierMonitor(tmp_path / "carrier-state.json")
    assert m2.moved_channels("cups") == ["of-a", "of-b"]
    m2.drop_moved("cups", "of-a")
    assert m2.moved_channels("cups") == ["of-b"]
    m2.drop_moved("cups", "of-b")
    assert m2.moved_channels("cups") == []  # everyone settled: entry gone


def _carrier_watchdog(tmp_path, channels, admin, users, migrated, retired):
    from oflux_bot.alerts import CarrierMonitor, Watchdog

    class FakeRegistry:
        def active(self):
            return channels

    class FakeCfg:
        config_dir = tmp_path

    def migrate(old, target):
        migrated.append((old, target))
        return {"channel": f"{old}-new", "link": f"openflux://v1/{old}-{target}"}

    async def send(text):
        admin.append(text)
        return True

    async def notify_user(tg, text, png=None):
        users.append((tg, text))
        return True

    monitor = CarrierMonitor(tmp_path / "carrier-state.json")
    return Watchdog(
        FakeCfg(), FakeRegistry, send,
        notify_user=notify_user,
        carrier_monitor=monitor,
        migrate=migrate,
        retire=lambda ch: retired.append(ch),
        undo=lambda new, old: migrated.append(("undo", new, old)),
    )


CUPS_A = {
    "cups_url": _pack(["uuid-a"]), "carrier": "cups", "tg_id": 1,
    "created": "2026-10-27T10:00:00+00:00",
}
CUPS_B = {
    "cups_url": _pack(["uuid-b"]), "carrier": "cups", "tg_id": 2,
    "created": "2026-10-27T10:01:00+00:00",
}
TELE_C = {
    "telemost_url": "https://telemost.yandex.ru/j/1", "carrier": "telemost",
    "tg_id": 3, "created": "2026-10-27T10:02:00+00:00",
}


def test_carrier_failure_migrates_users_to_spare(tmp_path, monkeypatch):
    """R3: cups dead for everyone -> after K cycles the bot moves every
    cups user to telemost with a link in the same message; telemost users
    are not touched."""
    import asyncio

    from oflux_bot import alerts

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(alerts, "probe_carrier_url", lambda url: "telemost" in url)

    admin, users, migrated, retired = [], [], [], []
    wd = _carrier_watchdog(
        tmp_path, {"of-a": dict(CUPS_A), "of-b": dict(CUPS_B), "of-c": dict(TELE_C)},
        admin, users, migrated, retired,
    )
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert sum("носитель cups признан нерабочим" in t for t in admin) == 1
    assert ("of-a", "telemost") in migrated and ("of-b", "telemost") in migrated
    assert not any(old == "of-c" for old, _ in migrated)  # spare users untouched
    # both moved users got a link + QR, nothing was rolled back
    assert len(users) == 2 and all("openflux://" in t for _, t in users)
    assert retired == []
    state = wd.carrier._state
    assert state["cups"]["failed"] is True
    assert sorted(state["cups"]["moved"]) == ["of-a", "of-b"]


def test_carrier_recovery_offers_way_back(tmp_path, monkeypatch):
    """R4: cups dies (carrier verdict — not a reissue, even for a lone
    channel), then recovers -> the moved user is offered the return,
    nobody is forced to move."""
    import asyncio

    from oflux_bot import alerts

    cups_alive = {"ok": False}
    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(
        alerts, "probe_carrier_url", lambda url: "cups" in url and cups_alive["ok"]
    )

    admin, users, migrated, retired = [], [], [], []
    wd = _carrier_watchdog(tmp_path, {"of-a": dict(CUPS_A)}, admin, users, migrated, retired)
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())
    assert migrated == [("of-a", "telemost")]  # carrier verdict, no reissue
    assert wd.carrier.failed("cups")

    cups_alive["ok"] = True  # cups back up
    for _ in range(alerts.M_OKS):
        asyncio.run(wd.run_once())
    assert sum("носитель cups восстановился" in t for t in admin) == 1
    assert users[-1] == (1, alerts._BACK_TEXT)  # the way back is offered
    migrated.clear()
    for _ in range(3):  # no forced moves, no repeats
        asyncio.run(wd.run_once())
    assert migrated == []


def test_single_dead_channel_reissued_in_place(tmp_path, monkeypatch):
    """One expired conference on a healthy carrier: only that user gets a
    fresh link on the same carrier, at most once per hour."""
    import asyncio

    from oflux_bot import alerts

    state = {"t2_dead": True}
    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(
        alerts, "probe_carrier_url",
        lambda url: (False if "dead" in url and state["t2_dead"] else True),
    )

    admin, users, migrated, retired = [], [], [], []
    dead = {"telemost_url": "https://telemost.yandex.ru/j/dead", "carrier": "telemost",
            "tg_id": 2, "created": "2026-10-27T10:01:00+00:00"}
    wd = _carrier_watchdog(
        tmp_path, {"of-t1": dict(TELE_C), "of-t2": dead}, admin, users, migrated, retired,
    )
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert migrated == [("of-t2", "telemost")]  # same carrier, fresh conference
    assert retired == ["of-t2"]  # the dead channel is retired after delivery
    assert not wd.carrier.failed("telemost")  # the carrier itself is fine

    state["t2_dead"] = False  # episode clears...
    for _ in range(alerts.M_OKS):
        asyncio.run(wd.run_once())
    state["t2_dead"] = True  # ...and re-fires within the hour
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert migrated == [("of-t2", "telemost")]  # hourly cap: no second reissue
    assert any("авто-перевыписка за час уже была" in t for t in admin)


def test_migration_rolls_back_when_link_undelivered(tmp_path, monkeypatch):
    """Telegram down at delivery time: the fresh channel is undone, the old
    one stays, nobody is marked moved (the retry happens next cycle)."""
    import asyncio

    from oflux_bot import alerts

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(alerts, "probe_carrier_url", lambda url: "telemost" in url)

    admin, users, migrated, retired = [], [], [], []
    wd = _carrier_watchdog(tmp_path, {"of-a": dict(CUPS_A)}, admin, users, migrated, retired)

    async def undelivered(tg, text, png=None):
        users.append((tg, text))
        return False

    wd.notify_user = undelivered

    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert migrated[-1] == ("undo", "of-a-new", "of-a")
    assert retired == []  # the old channel was never touched
    assert wd.carrier.moved_channels("cups") == []


# -- /issue as the move (R3/R4 user-facing path) ------------------------------

def test_issue_moves_user_when_carrier_failed(tmp_path, monkeypatch):
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.alerts import CarrierMonitor
    from oflux_bot.config import BotUsers

    class FakeRegistry:
        def get(self, ch):
            return {"carrier": "cups", "cups_url": "x"} if ch == "chan-1" else None

    calls = []
    monkeypatch.setattr(handlers, "load_registry", lambda cfg: FakeRegistry())
    monkeypatch.setattr(
        handlers, "migrate_user_channel",
        lambda cfg, bu, old, target:
            calls.append((old, target)) or {"channel": "chan-2", "link": "openflux://v1/z"},
    )
    monkeypatch.setattr(handlers, "retire_channel", lambda cfg, ch: calls.append(("retire", ch)))

    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(7, "chan-1")
    monitor = CarrierMonitor(tmp_path / "carrier-state.json")
    for t in (1, 2, 3):
        monitor.feed("cups", False, now=t)

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users, carrier_monitor=monitor)
    issue = next(
        h.callback for h in dp.message.handlers
        if getattr(h.callback, "__name__", "") == "issue"
    )

    class FakeSent:
        async def edit_text(self, text, **kw):
            self.edited = text

    class FakeMessage:
        def __init__(self):
            self.from_user = type("U", (), {"id": 7})()
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

    assert ("chan-1", "telemost") in calls and ("retire", "chan-1") in calls
    assert "chan-2" in msg.answers[0][0].edited  # the new link was delivered


def test_issue_still_refuses_without_carrier_problem(tmp_path, monkeypatch):
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.alerts import CarrierMonitor
    from oflux_bot.config import BotUsers

    class FakeRegistry:
        def get(self, ch):
            return {"carrier": "telemost", "telemost_url": "https://x"} if ch == "chan-1" else None

    monkeypatch.setattr(handlers, "load_registry", lambda cfg: FakeRegistry())
    monkeypatch.setattr(
        handlers, "migrate_user_channel",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not migrate")),
    )

    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(7, "chan-1")
    monitor = CarrierMonitor(tmp_path / "carrier-state.json")

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users, carrier_monitor=monitor)
    issue = next(
        h.callback for h in dp.message.handlers
        if getattr(h.callback, "__name__", "") == "issue"
    )

    class FakeSent:
        async def edit_text(self, text, **kw):
            self.edited = text

    class FakeMessage:
        def __init__(self):
            self.from_user = type("U", (), {"id": 7})()
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

    assert msg.answers[0][0].text.startswith("У тебя уже есть канал")


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


def test_menu_keyboard_is_contextual():
    """Buttons that cannot work in the current state are not shown; the
    full non-contextual list stays available as the fallback variant."""
    from oflux_bot.handlers import _menu_kb

    def callbacks(kb):
        return [btn.callback_data for row in kb.inline_keyboard for btn in row]

    fresh, owned, full = callbacks(_menu_kb(False)), callbacks(_menu_kb(True)), callbacks(_menu_kb(None))

    assert "cmd:issue" in fresh and "cmd:my" not in fresh
    assert "cmd:revoke" not in fresh
    assert "cmd:my" in owned and "cmd:issue" not in owned
    assert "cmd:revoke" in owned
    for cmd in ("cmd:issue", "cmd:my", "cmd:status", "cmd:rotate", "cmd:revoke", "cmd:download", "cmd:help"):
        assert cmd in full


def test_start_menu_buttons_route_to_commands(tmp_path, monkeypatch):
    """/start menu buttons run the same code as typed commands."""
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.config import BotUsers

    monkeypatch.setattr(handlers, "reissue_link", lambda cfg, ch: "openflux://v1/x")
    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(42, "chan-9")

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users)
    router = next(
        h.callback for h in dp.callback_query.handlers
        if getattr(h.callback, "__name__", "") == "cmd_via_button"
    )

    class FakeMessage:
        def __init__(self):
            self.answers = []

        async def answer(self, text=None, **kw):
            self.answers.append(text)
            return None

        async def answer_photo(self, *a, **kw):
            return None

    class FakeQuery:
        def __init__(self, data, user_id):
            self.data = data
            self.from_user = type("U", (), {"id": user_id})()
            self.message = FakeMessage()
            self.answered = 0

        async def answer(self):
            self.answered += 1

    async def scenario():
        q_my = FakeQuery("cmd:my", 42)
        await router(q_my)
        q_status = FakeQuery("cmd:status", 43)  # user without a channel
        await router(q_status)
        q_bogus = FakeQuery("cmd:nope", 42)
        await router(q_bogus)
        return q_my, q_status, q_bogus

    q_my, q_status, q_bogus = asyncio.run(scenario())

    assert q_my.answered == 1
    assert any("Канал chan-9" in t for t in q_my.message.answers if t)
    assert any("нет канала" in t for t in q_status.message.answers if t)
    assert q_bogus.message.answers == []  # unknown route: silent


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
