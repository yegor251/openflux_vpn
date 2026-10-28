"""oflux_bot pure logic: invite tokens, alert dedup, link masking."""

import os
from datetime import datetime, timezone

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


def _carrier_watchdog(tmp_path, channels, admin, users):
    from oflux_bot.alerts import CarrierMonitor, Watchdog

    class FakeRegistry:
        def active(self):
            return channels

    class FakeCfg:
        config_dir = tmp_path

    async def send(text):
        admin.append(text)
        return True

    async def notify_user(tg, text, png=None):
        users.append((tg, text))
        return True

    monitor = CarrierMonitor(tmp_path / "carrier-state.json")
    return Watchdog(FakeCfg(), FakeRegistry, send, notify_user=notify_user, carrier_monitor=monitor)


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


def test_carrier_failure_warns_users_instead_of_minting(tmp_path, monkeypatch):
    """R3 (admin decision 2026-10-28): a dead carrier means ONE warning per
    episode to each affected user — the bot never mints links by itself."""
    import asyncio

    from oflux_bot import alerts

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(alerts, "probe_carrier_url", lambda url: "telemost" in url)

    admin, users = [], []
    wd = _carrier_watchdog(
        tmp_path, {"of-a": dict(CUPS_A), "of-b": dict(CUPS_B), "of-c": dict(TELE_C)},
        admin, users,
    )
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert sum("носитель cups признан нерабочим" in t for t in admin) == 1
    warnings = [t for tg, t in users if "/rotate" in t]
    assert len(warnings) == 2  # both cups users, not the telemost one
    assert wd.carrier.failed("cups")
    users.clear()
    for _ in range(alerts.K_FAILS):  # still dead: one warning per episode
        asyncio.run(wd.run_once())
    assert users == []


def test_carrier_recovery_offers_way_back(tmp_path, monkeypatch):
    """R4: cups recovers -> admin gets the all-clear and the moved user is
    offered the way back via /issue (never forced)."""
    import asyncio

    from oflux_bot import alerts

    cups_alive = {"ok": False}

    def probe(url):
        return True if "telemost" in url else cups_alive["ok"]

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(alerts, "probe_carrier_url", probe)

    moved_user = {
        "telemost_url": "https://telemost.yandex.ru/j/new", "carrier": "telemost",
        "tg_id": 1, "created": "2026-10-28T00:00:00+00:00",
    }
    admin, users = [], []
    wd = _carrier_watchdog(
        tmp_path, {"of-b": dict(CUPS_B), "of-a-new": moved_user}, admin, users,
    )
    wd.carrier.record_moved("cups", "of-a-new")  # as the /rotate move records

    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())
    assert wd.carrier.failed("cups")
    assert any(tg == 2 and "/rotate" in t for tg, t in users)  # of-b still on cups

    cups_alive["ok"] = True  # cups back up
    for _ in range(alerts.M_OKS):
        asyncio.run(wd.run_once())
    assert sum("носитель cups восстановился" in t for t in admin) == 1
    assert (1, alerts._BACK_TEXT) in users
    users.clear()
    for _ in range(3):  # no repeats after the offer
        asyncio.run(wd.run_once())
    assert users == []


def test_single_dead_channel_warns_once_per_episode(tmp_path, monkeypatch):
    """One expired conference on a healthy carrier: only that user is told
    to /rotate, once per episode; the carrier verdict stays clean."""
    import asyncio

    from oflux_bot import alerts

    monkeypatch.setattr(alerts, "probe_unit", lambda ch: True)
    monkeypatch.setattr(alerts, "probe_carrier_url", lambda url: "dead" not in url)

    admin, users = [], []
    dead = {"telemost_url": "https://telemost.yandex.ru/j/dead", "carrier": "telemost",
            "tg_id": 2, "created": "2026-10-27T10:01:00+00:00"}
    wd = _carrier_watchdog(
        tmp_path, {"of-t1": dict(TELE_C), "of-t2": dead}, admin, users,
    )
    for _ in range(alerts.K_FAILS):
        asyncio.run(wd.run_once())

    assert users == [(2, alerts._channel_warning_text())]
    assert not wd.carrier.failed("telemost")  # the carrier itself is fine
    for _ in range(alerts.K_FAILS):  # still dead in the fixture: quiet
        asyncio.run(wd.run_once())
    assert users == [(2, alerts._channel_warning_text())]


def test_carrier_flap_dampening(tmp_path):
    """Third failure episode within an hour -> unstable: verdicts go quiet
    and recovery needs M * UNSTABLE_M_FACTOR good cycles."""
    from oflux_bot.alerts import K_FAILS, M_OKS, UNSTABLE_M_FACTOR, CarrierMonitor

    m = CarrierMonitor(tmp_path / "carrier-state.json")
    t = 100.0

    def run(ok, n):
        nonlocal t
        out = []
        for _ in range(n):
            t += 1
            out.append(m.feed("cups", ok, now=t))
        return out

    assert run(False, K_FAILS)[-1] == "failed"  # episode 1
    assert run(True, M_OKS)[-1] == "recovered"
    assert run(False, K_FAILS)[-1] == "failed"  # episode 2 (same hour)
    assert run(True, M_OKS)[-1] == "recovered"
    assert run(False, K_FAILS)[-1] == "unstable"  # episode 3 -> dampened
    assert m.failed("cups")  # user-initiated moves stay available
    assert run(False, 2 * K_FAILS) == [None] * (2 * K_FAILS)  # quiet
    oks = run(True, M_OKS * UNSTABLE_M_FACTOR - 1)
    assert oks == [None] * (M_OKS * UNSTABLE_M_FACTOR - 1)
    assert run(True, 1) == ["recovered_stable"]
    assert not m.failed("cups")


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
    monkeypatch.setattr(handlers, "load_registry", lambda cfg: {"chan-1": {"rotated": None}})

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


# -- rotate: cooldown + move on a dead carrier (admin decision 2026-10-28) ----

def _confirm_handler(dp):
    return next(
        h.callback for h in dp.callback_query.handlers
        if getattr(h.callback, "__name__", "") == "rotate_confirm"
    )


class _FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit_text(self, text, **kw):
        self.edits.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        self.markup_dropped = True

    async def answer_photo(self, *a, **kw):
        self.edits.append("<photo>")


class _FakeQuery:
    def __init__(self, user_id=9):
        self.data = "rotate:chan-1"
        self.from_user = type("U", (), {"id": user_id})()
        self.message = _FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def test_rotate_has_cooldown(tmp_path, monkeypatch):
    """A channel may change at most once per ROTATE_COOLDOWN_S."""
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.config import BotUsers

    calls = []
    monkeypatch.setattr(handlers, "rotate_channel", lambda cfg, ch: calls.append(ch) or {"link": "openflux://v1/x"})
    registry = {
        "chan-1": {"rotated": "2026-10-01T00:00:00+00:00"},  # old -> cooldown over
    }
    monkeypatch.setattr(handlers, "load_registry", lambda cfg: registry)

    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(9, "chan-1")
    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users)
    confirm = _confirm_handler(dp)

    asyncio.run(confirm(_FakeQuery()))
    assert calls == ["chan-1"]  # old rotated ts: allowed

    calls.clear()
    registry["chan-1"]["rotated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q = _FakeQuery()
    asyncio.run(confirm(q))
    assert calls == []  # just changed: refused
    assert any("подожди" in (a[0] or "") for a in q.answers)


def test_rotate_moves_to_spare_when_carrier_failed(tmp_path, monkeypatch):
    """/rotate on a dead carrier is the move to the spare (as the warnings
    promise), with the way-back bookkeeping pointing at the new channel."""
    import asyncio

    from aiogram import Dispatcher

    from oflux_bot import handlers
    from oflux_bot.alerts import CarrierMonitor
    from oflux_bot.config import BotUsers

    moves, retired = [], []
    monkeypatch.setattr(
        handlers, "migrate_user_channel",
        lambda cfg, bu, old, target:
            moves.append((old, target)) or {"channel": "chan-2", "link": "openflux://v1/z"},
    )
    monkeypatch.setattr(handlers, "retire_channel", lambda cfg, ch: retired.append(ch))
    monkeypatch.setattr(
        handlers, "load_registry",
        lambda cfg: {"chan-1": {"carrier": "cups", "cups_url": "x", "rotated": None}},
    )

    bot_users = BotUsers(tmp_path / "bot_users.json")
    bot_users.bind_channel(9, "chan-1")
    monitor = CarrierMonitor(tmp_path / "carrier-state.json")
    for t in (1, 2, 3):
        monitor.feed("cups", False, now=t)

    dp = Dispatcher()
    handlers.register_handlers(dp, bot=None, cfg=None, bot_users=bot_users, carrier_monitor=monitor)
    confirm = _confirm_handler(dp)

    q = _FakeQuery()
    asyncio.run(confirm(q))

    assert moves == [("chan-1", "telemost")]  # the move, not a plain rotate
    assert retired == ["chan-1"]
    assert monitor.moved_channels("cups") == ["chan-2"]  # the NEW channel recorded
