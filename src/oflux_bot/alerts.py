"""Watchdog: periodic health probes with deduplicated TG alerts.

Probes (all local to the bot's VPS):
  - cookie session: live probe against the first carrier doc;
  - per channel: systemctl is-active, then the channel's own path —
    carrier channels get a read-only GET of the page they sit in (cups
    room shell / telemost join page), legacy direct channels a
    127.0.0.1:<port> connect;
  - registry backup stamp touched by the admin machine's daily pull.

Carrier verdicts (R1/R2): a carrier is "failed" after K bad cycles and
"recovered" after M good ones. Failed → the bot migrates every affected
user to the spare carrier (fresh link in the same message, old channel
retired) and offers the way back once it recovers. A channel that dies
alone while its carrier serves others (e.g. an expired telemost
conference) is reissued in place, at most once per hour.

Anti-flap: alerts after K consecutive failures, clears after M
consecutive successes. A mass failure (>= 50% down) collapses into ONE
aggregated alert. Repeat alerts per type cool down for cooldown seconds.
Death of the VPS itself alerts nobody (bot is on it).

send/notify_user(text) -> bool: an undelivered message must not consume
its state transition — the watchdog rolls back and retries the next
cycle, so a Telegram outage delays actions instead of swallowing them.
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from docprovider.cupsrooms import ROOM_URL, first_room

COOLDOWN_S = 3600
K_FAILS = 3
M_OKS = 2
CHECK_INTERVAL_S = 300
BACKUP_MAX_AGE_S = 36 * 3600  # daily cron on the admin machine + slack
REISSUE_CAP_S = 3600  # at most one auto-reissue per channel per hour

CARRIER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
)
SPARE = {"cups": "telemost", "telemost": "cups"}

_BACK_TEXT = (
    "🟢 Основной носитель снова работает. Хочешь вернуться на него — "
    "напиши /issue, выдам новую ссылку."
)


def probe_unit(channel: str) -> bool:
    rc, out = subprocess.getstatusoutput(f"systemctl is-active openflux-node@{channel}")
    return rc == 0 and out.strip() == "active"


def probe_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
            return True
    except OSError:
        return False


def probe_carrier_url(url: str, timeout: int = 15) -> bool:
    """The carrier page a channel sits in (cups room shell / telemost join
    page) still answers from the VPS. Both are read-only public pages:
    the cups shell mints nothing, join pages are what every visitor loads."""
    try:
        import requests

        with requests.get(url, headers={"User-Agent": CARRIER_UA}, timeout=timeout, stream=True) as resp:
            return resp.status_code < 400
    except Exception:  # noqa: BLE001 - any probe error counts as down
        return False


def probe_cookies(cookies_path, doc_url: str) -> bool:
    """Live validation of the bot's yandex session; any failure = stale."""
    try:
        import requests

        from docprovider.netscape import load_into_session
        from docprovider.validate import check_yandex_doc

        session = requests.Session()
        if not load_into_session(session, cookies_path, "yandex.ru"):
            return False
        check_yandex_doc(doc_url, session=session)
        return True
    except Exception:  # noqa: BLE001 - any probe error counts as stale
        return False


def probe_backup(config_dir, now: float | None = None) -> bool:
    """True while the .last-backup stamp (touched by a successful pull
    on the admin machine) is fresh enough."""
    try:
        mtime = (Path(config_dir) / ".last-backup").stat().st_mtime
    except OSError:
        return False
    return (time.time() if now is None else now) - mtime <= BACKUP_MAX_AGE_S


def _qr_png(link: str) -> bytes:
    import io

    import qrcode

    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    return buf.getvalue()


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")


def _link_footer(link: str) -> str:
    return f"{link}\n\n(ссылка = ключ: не публикуй, храни как пароль)"


def _move_text(spare: str, link: str) -> str:
    return (
        "⚠️ Носитель, на котором работал твой канал, недоступен у всех. "
        f"Держи новую ссылку — она уже на запасном носителе ({spare}). "
        "Импортируй её в приложении вместо старой:\n\n" + _link_footer(link)
    )


def _reissue_text(link: str) -> str:
    return (
        "⚠️ Твой канал перестал работать (сам носитель жив — ты просто попал "
        "на дохлую комнату/конференцию). Держи свежую ссылку:\n\n" + _link_footer(link)
    )


class AlertState:
    """Dedup bookkeeping. Pure logic, no I/O — easy to test."""

    def __init__(self, k_fails: int = K_FAILS, m_oks: int = M_OKS, cooldown: int = COOLDOWN_S) -> None:
        self.k = k_fails
        self.m = m_oks
        self.cooldown = cooldown
        self.fails: dict[str, int] = {}
        self.oks: dict[str, int] = {}
        self.active: dict[str, bool] = {}  # currently-alerting?
        self.last_sent: dict[str, float] = {}

    def feed(self, key: str, healthy: bool, now: float | None = None) -> str | None:
        """Feed one probe result; returns an action: 'alert' | 'clear' | None."""
        now = time.time() if now is None else now
        if healthy:
            self.fails[key] = 0
            self.oks[key] = self.oks.get(key, 0) + 1
            if self.active.get(key) and self.oks[key] >= self.m:
                self.active[key] = False
                return "clear"
            return None
        self.oks[key] = 0
        self.fails[key] = self.fails.get(key, 0) + 1
        if self.fails[key] >= self.k and not self.active.get(key):
            self.active[key] = True
            return "alert"
        return None

    def repeat_due(self, key: str, now: float | None = None) -> bool:
        """While a problem stays active, remind at most once per cooldown."""
        if not self.active.get(key):
            return False
        now = time.time() if now is None else now
        last = self.last_sent.get(key, 0.0)
        if now - last >= self.cooldown:
            self.last_sent[key] = now
            return True
        return False


class CarrierMonitor:
    """Carrier verdicts with the channel-grade anti-flap: 'failed' after K
    bad cycles, 'recovered' after M good ones (feed()).

    The failed/moved bookkeeping persists (carrier-state.json) so migration
    decisions and the way-back offer survive restarts; the K/M counters are
    in-memory and simply recount after one."""

    def __init__(self, state_path, k_fails: int = K_FAILS, m_oks: int = M_OKS) -> None:
        self.path = Path(state_path)
        self.k = k_fails
        self.m = m_oks
        self._fails: dict[str, int] = {}
        self._oks: dict[str, int] = {}
        self._state: dict = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        self.path.chmod(0o600)

    def _entry(self, carrier: str) -> dict:
        return self._state.setdefault(carrier, {"failed": False, "recovered": False, "moved": []})

    def feed(self, carrier: str, ok: bool, now: float | None = None) -> str | None:
        """Feed one carrier health result; returns 'failed' | 'recovered' | None."""
        now = time.time() if now is None else now
        entry = self._entry(carrier)
        if ok:
            self._fails[carrier] = 0
            if not entry["failed"]:
                return None
            self._oks[carrier] = self._oks.get(carrier, 0) + 1
            if self._oks[carrier] >= self.m:
                entry["failed"] = False
                entry["recovered"] = True
                entry["recovered_at"] = _iso(now)
                self._save()
                return "recovered"
            return None
        self._oks[carrier] = 0
        self._fails[carrier] = self._fails.get(carrier, 0) + 1
        if self._fails[carrier] >= self.k and not entry["failed"]:
            entry["failed"] = True
            entry["recovered"] = False
            entry["since"] = _iso(now)
            self._save()
            return "failed"
        return None

    def failed(self, carrier: str) -> bool:
        return bool(self._state.get(carrier, {}).get("failed"))

    def failed_carriers(self) -> list[str]:
        return sorted(c for c, e in self._state.items() if e.get("failed"))

    def recovered_carriers(self) -> list[str]:
        return sorted(
            c for c, e in self._state.items() if e.get("recovered") and not e.get("failed")
        )

    def record_moved(self, carrier: str, channel: str) -> None:
        entry = self._entry(carrier)
        if channel not in entry["moved"]:
            entry["moved"].append(channel)
        self._save()

    def moved_channels(self, carrier: str) -> list[str]:
        return list(self._state.get(carrier, {}).get("moved", []))

    def drop_moved(self, carrier: str, channel: str) -> None:
        entry = self._state.get(carrier)
        if not entry:
            return
        entry["moved"] = [c for c in entry["moved"] if c != channel]
        if not entry["moved"] and not entry.get("failed"):
            del self._state[carrier]  # everyone settled: nothing left to remember
        self._save()


def _newest_per_tg(channels: dict) -> dict[int, str]:
    """Newest active channel per owner: a user mid-handover may still show
    the dead predecessor; only the newest one may migrate/reissue."""
    newest: dict[int, str] = {}
    for channel, user in channels.items():
        tg = user.get("tg_id")
        if tg is None:
            continue
        cur = newest.get(tg)
        if cur is None or str(user.get("created", "")) > str(channels[cur].get("created", "")):
            newest[tg] = channel
    return newest


class Watchdog:
    def __init__(
        self,
        cfg,
        registry_source,
        send,
        notify_user=None,
        carrier_monitor: CarrierMonitor | None = None,
        migrate=None,
        retire=None,
        undo=None,
    ) -> None:
        # registry_source: callable returning a FRESH UserRegistry — the
        # registry file changes between cycles (bot-issued channels), a
        # startup snapshot would probe ghosts and miss new channels.
        # send(text) -> bool: False = not delivered, roll back and retry.
        # notify_user(tg_id, text, png|None) -> bool and the migrate/retire/
        # undo ops enable the carrier-failure hand-over (R3); without them
        # the watchdog still probes and alerts, it just never migrates.
        self.cfg = cfg
        self.registry_source = registry_source
        self.send = send
        self.notify_user = notify_user
        self.carrier = carrier_monitor
        self.migrate = migrate
        self.retire = retire
        self.undo = undo
        self.state = AlertState()
        self._reissued_at: dict[str, float] = {}

    async def run_once(self) -> None:
        channels = self.registry_source().active()
        results: list[tuple[str, dict, str | None, bool, bool]] = []
        carrier_healthy: dict[str, bool] = {}

        # one read-only probe decides cups liveness for all its channels:
        # they share the service, and GETting every room separately is what
        # the core warns to avoid (a room check can mint a fresh room)
        cups_ok = True
        for _, user in sorted(channels.items()):
            if _kind_of(user) == "cups":
                try:
                    cups_ok = probe_carrier_url(f"{ROOM_URL}?room={first_room(user['cups_url'])}")
                except Exception:  # noqa: BLE001 - malformed set = dead signal
                    cups_ok = False
                break

        for channel, user in sorted(channels.items()):
            unit_ok = probe_unit(channel)
            kind = _kind_of(user)
            if kind == "cups":
                healthy = unit_ok and cups_ok
            elif kind == "telemost":
                healthy = unit_ok and probe_carrier_url(user["telemost_url"])
            else:
                healthy = unit_ok and probe_port(user["port"])
            if kind:
                carrier_healthy[kind] = carrier_healthy.get(kind, False) or healthy
            results.append((channel, user, kind, unit_ok, healthy))

        # carrier verdicts (R1/R2) and the hand-over (R3)
        if self.carrier is not None:
            for kind in sorted(carrier_healthy):
                verdict = self.carrier.feed(kind, carrier_healthy[kind])
                if verdict == "failed":
                    await self.send(
                        f"🚨 носитель {kind} признан нерабочим ({self.carrier.k} проверок подряд) — "
                        f"перевожу пользователей на {SPARE[kind]}"
                    )
                elif verdict == "recovered":
                    await self.send(f"🟢 носитель {kind} восстановился")
                    await self._offer_return(kind)
            # (re)run the hand-over for every carrier still marked failed;
            # migrated users drop out because their channel now sits on the spare
            if self.migrate is not None:
                for kind in self.carrier.failed_carriers():
                    targets = {c: u for c, u in channels.items() if _kind_of(u) == kind}
                    if targets:
                        await self._migrate_carrier(kind, SPARE[kind], targets)

        # per-channel alerts + the single-channel reissue (expired room or
        # conference while the carrier itself serves everyone else fine)
        now = time.time()
        pending: list[tuple[str, str, str]] = []  # (kind, state_key, text)
        newest = _newest_per_tg(channels)
        down = 0
        for channel, user, kind, unit_ok, healthy in results:
            down += 0 if healthy else 1
            action = self.state.feed(f"channel:{channel}", healthy)
            if action == "alert":
                if not kind:
                    text = f"🔴 канал {channel} недоступен (порт {user['port']})"
                else:
                    text = f"🔴 канал {channel} недоступен (носитель {kind})"
                if (
                    self.carrier is not None
                    and self.migrate is not None
                    and kind
                    and unit_ok
                    and carrier_healthy.get(kind)
                    and user.get("tg_id")
                    and newest.get(user["tg_id"]) == channel
                ):
                    if now - self._reissued_at.get(channel, 0.0) >= REISSUE_CAP_S:
                        self._reissued_at[channel] = now
                        await self._reissue(channel, user["tg_id"], kind)
                        continue
                    text += " — авто-перевыписка за час уже была, разберись руками"
                pending.append(("alert", f"channel:{channel}", text))
            elif action == "clear":
                pending.append(("clear", f"channel:{channel}", f"🟢 канал {channel} восстановился"))

        # mass failure: one aggregated alert instead of per-channel spam
        mass_key = "mass"
        if channels and down * 2 >= len(channels) and down >= 2:
            if not self.state.active.get(mass_key):
                if await self.send(
                    f"🚨 МАССОВЫЙ СБОЙ: недоступны {down} из {len(channels)} каналов — "
                    "похоже на проблему VPS, вышестоящей сети или носителя"
                ):
                    self.state.active[mass_key] = True
                    self.state.last_sent[mass_key] = time.time()
                return  # individual alerts folded into the aggregate
        elif self.state.active.get(mass_key) and down == 0:
            if await self.send("🟢 массовый сбой прошёл: все каналы доступны"):
                self.state.active[mass_key] = False

        for kind_, key, text in pending:
            if await self.send(text):
                continue
            # not delivered: undo the transition so it retries next cycle
            self.state.active[key] = kind_ == "clear"

        # cookies: one shared session for doc creation
        url = next((u["yandex_url"] for u in channels.values() if u.get("yandex_url")), None)
        if url:
            healthy = probe_cookies(self.cfg.cookies_path, url)
            action = self.state.feed("cookies", healthy)
            if action == "alert":
                if not await self.send(
                    "🟠 Яндекс-куки протухли: /issue не работает. Нужен свежий "
                    "экспорт (скилл refresh-cookies на машине админа)."
                ):
                    self.state.active["cookies"] = False
            elif self.state.active.get("cookies") and self.state.repeat_due("cookies"):
                if not await self.send("🟠 напоминание: куки всё ещё протухли"):
                    self.state.last_sent["cookies"] = 0.0
            elif action == "clear":
                if not await self.send("🟢 яндекс-куки снова живы"):
                    self.state.active["cookies"] = True

        # registry backups: stamped by the admin machine's daily pull
        healthy = probe_backup(self.cfg.config_dir)
        action = self.state.feed("backup", healthy)
        if action == "alert":
            if not await self.send(
                "📭 Бэкап реестра не снимался больше 36 ч. На машине админа: "
                "scripts/backup_registry.sh pull"
            ):
                self.state.active["backup"] = False
        elif self.state.active.get("backup") and self.state.repeat_due("backup"):
            if not await self.send("📭 напоминание: бэкап реестра всё ещё не снимался"):
                self.state.last_sent["backup"] = 0.0
        elif action == "clear":
            if not await self.send("🟢 бэкап реестра снова свежий"):
                self.state.active["backup"] = True

    async def _migrate_carrier(self, kind: str, spare: str, targets: dict) -> None:
        """Move every owned channel of a dead carrier to the spare (R3);
        stale predecessors of already-moved users are just retired."""
        assert self.carrier is not None and self.migrate and self.retire and self.undo
        newest = _newest_per_tg(targets)
        moved, errors, notes = 0, 0, []
        for channel, user in sorted(targets.items()):
            tg = user.get("tg_id")
            if tg is None:
                continue  # ownerless (drill) channel: the 🔴 alert is its voice
            if newest.get(tg) != channel:
                try:
                    await asyncio.to_thread(self.retire, channel)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{channel}: уборка старого не вышла: {str(exc)[:60]}")
                continue
            try:
                result = await asyncio.to_thread(self.migrate, channel, spare)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                notes.append(f"{channel}: {str(exc)[:80]}")
                continue
            if not await self.notify_user(tg, _move_text(spare, result["link"]), _qr_png(result["link"])):
                errors += 1
                notes.append(f"{channel}: ссылка не доставлена, откатываю")
                try:
                    await asyncio.to_thread(self.undo, result["channel"], channel)
                except Exception as exc:  # noqa: BLE001
                    notes.append(f"{channel}: откат не вышел: {str(exc)[:60]}")
                continue
            self.carrier.record_moved(kind, channel)
            moved += 1
        if moved or notes:
            text = f"носитель {kind}: переездов на {spare}: {moved}"
            if errors:
                text += f", ошибок: {errors} (повтор в следующем цикле)"
            for note in notes[:5]:
                text += f"\n- {note}"
            await self.send(text)

    async def _reissue(self, channel: str, tg_id: int, kind: str) -> None:
        """Fresh rooms/conference on the SAME carrier for one dead channel."""
        assert self.migrate and self.retire and self.undo
        try:
            result = await asyncio.to_thread(self.migrate, channel, kind)
        except Exception as exc:  # noqa: BLE001
            await self.send(f"🔴 канал {channel}: авто-перевыписка не вышла: {str(exc)[:80]}")
            return
        if await self.notify_user(tg_id, _reissue_text(result["link"]), _qr_png(result["link"])):
            await asyncio.to_thread(self.retire, channel)
        else:
            await self.send(f"🔴 канал {channel}: свежая ссылка не доставлена, откатываю")
            try:
                await asyncio.to_thread(self.undo, result["channel"], channel)
            except Exception as exc:  # noqa: BLE001
                await self.send(f"🔴 канал {channel}: откат не вышел: {str(exc)[:60]}")

    async def _offer_return(self, kind: str) -> None:
        """R4: everyone moved off a recovered carrier may move back — by
        their own /issue, never imposed (a move is a new key + re-import)."""
        if self.carrier is None or self.notify_user is None:
            return
        active = self.registry_source().active()
        for channel in self.carrier.moved_channels(kind):
            user = active.get(channel)
            if user and user.get("tg_id"):
                await self.notify_user(user["tg_id"], _BACK_TEXT)


def _kind_of(user: dict) -> str | None:
    if user.get("telemost_url") or user.get("carrier") == "telemost":
        return "telemost"
    if user.get("cups_url") or user.get("carrier") == "cups":
        return "cups"
    return None
