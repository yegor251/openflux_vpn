"""Round-robin pool over carrier accounts with per-day creation limits.

Account config (JSON, keep at 0600 — it holds passwords and cookie paths):

    {
      "mailru": [{"id": "mr1", "login": "...", "password": "..."}],
      "yandex": [{"id": "ya1", "cookies_path": "ya1-cookies.txt"}]
    }

Usage counters persist next to the config so restarts keep the daily
limits honest.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .errors import AllAccountsExhaustedError
from .models import MailruAccount, PoolLimits, YandexAccount

_KINDS = ("mailru", "yandex")


class AccountPool:
    def __init__(
        self,
        config_path: str | Path,
        state_path: str | Path | None = None,
        limits: PoolLimits | None = None,
    ) -> None:
        self.limits = limits or PoolLimits()
        self._today = lambda: time.strftime("%Y-%m-%d", time.localtime())
        config_path = Path(config_path)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        self._mailru = [
            MailruAccount(
                id=a["id"],
                login=a.get("login", ""),
                password=a.get("password", ""),
                cookies_path=a.get("cookies_path", ""),
            )
            for a in raw.get("mailru", [])
        ]
        self._yandex = [
            YandexAccount(id=a["id"], cookies_path=a["cookies_path"])
            for a in raw.get("yandex", [])
        ]
        self.state_path = (
            Path(state_path) if state_path else config_path.with_suffix(".state.json")
        )
        self._state = self._load_state()

    # -- state ---------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("day") != self._today():
            state = {"day": self._today(), "used": {}}
        state.setdefault("used", {})
        return state

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state), encoding="utf-8")

    def _used_today(self, kind: str, account_id: str) -> int:
        return self._state["used"].get(kind, {}).get(account_id, 0)

    # -- public API ----------------------------------------------------------

    def accounts(self, kind: str) -> list[MailruAccount] | list[YandexAccount]:
        if kind not in _KINDS:
            raise ValueError(f"unknown account kind: {kind}")
        return self._mailru if kind == "mailru" else self._yandex

    def pick(self, kind: str) -> MailruAccount | YandexAccount:
        """Least-used account of the kind that still has daily headroom."""
        if kind not in _KINDS:
            raise ValueError(f"unknown account kind: {kind}")
        limit = self.limits.mailru_per_day if kind == "mailru" else self.limits.yandex_per_day
        candidates = sorted(
            self.accounts(kind),
            key=lambda a: self._used_today(kind, a.id),
        )
        for account in candidates:
            if self._used_today(kind, account.id) < limit:
                return account
        raise AllAccountsExhaustedError(
            f"all {kind} accounts hit the {limit}/day limit"
        )

    def record(self, kind: str, account_id: str) -> None:
        used = self._state["used"].setdefault(kind, {})
        used[account_id] = used.get(account_id, 0) + 1
        self._save_state()
