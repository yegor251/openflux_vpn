"""Watchdog: periodic health probes with deduplicated TG alerts.

Probes (all local to the bot's VPS):
  - cookie session: live probe against the first carrier doc;
  - per channel: systemctl is-active + 127.0.0.1:<port> connect.

Anti-flap: a channel alerts after K consecutive failures and clears
after M consecutive successes. A mass failure (>= 50% down) collapses
into ONE aggregated alert. Repeat alerts per type cool down for
cooldown seconds. Death of the VPS itself alerts nobody (bot is on it).
"""

from __future__ import annotations

import socket
import subprocess
import time

COOLDOWN_S = 3600
K_FAILS = 3
M_OKS = 2
CHECK_INTERVAL_S = 300


def probe_unit(channel: str) -> bool:
    rc, out = subprocess.getstatusoutput(f"systemctl is-active openflux-node@{channel}")
    return rc == 0 and out.strip() == "active"


def probe_port(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
            return True
    except OSError:
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


class Watchdog:
    def __init__(self, cfg, registry, send) -> None:
        self.cfg = cfg
        self.registry = registry
        self.send = send  # coroutine: send(text) -> None
        self.state = AlertState()

    async def run_once(self) -> None:
        import asyncio

        alerts: list[str] = []
        channels = self.registry.active()
        down = 0

        for channel, user in sorted(channels.items()):
            healthy = probe_unit(channel) and probe_port(user["port"])
            down += 0 if healthy else 1
            action = self.state.feed(f"channel:{channel}", healthy)
            if action == "alert":
                alerts.append(f"🔴 канал {channel} недоступен (порт {user['port']})")
            elif action == "clear":
                alerts.append(f"🟢 канал {channel} восстановился")

        # mass failure: one aggregated alert instead of per-channel spam
        mass_key = "mass"
        if channels and down * 2 >= len(channels) and down >= 2:
            if not self.state.active.get(mass_key):
                self.state.active[mass_key] = True
                self.state.last_sent[mass_key] = time.time()
                await self.send(
                    f"🚨 МАССОВЫЙ СБОЙ: недоступны {down} из {len(channels)} каналов — "
                    "похоже на проблему VPS или вышестоящей сети"
                )
                return  # individual alerts folded into the aggregate
        elif self.state.active.get(mass_key) and down == 0:
            self.state.active[mass_key] = False
            await self.send("🟢 массовый сбой прошёл: все каналы доступны")

        for text in alerts:
            await self.send(text)

        # cookies: one shared session for doc creation
        url = next((u["yandex_url"] for u in channels.values() if u.get("yandex_url")), None)
        if url:
            healthy = probe_cookies(self.cfg.cookies_path, url)
            action = self.state.feed("cookies", healthy)
            if action == "alert":
                await self.send(
                    "🟠 Яндекс-куки протухли: /issue не работает. Нужен свежий "
                    "экспорт (скилл refresh-cookies на машине админа)."
                )
            elif self.state.active.get("cookies") and self.state.repeat_due("cookies"):
                await self.send("🟠 напоминание: куки всё ещё протухли")
            elif action == "clear":
                await self.send("🟢 яндекс-куки снова живы")
