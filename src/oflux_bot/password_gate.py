"""Attempt limiter for password self-issue (brute force would hit Telegram)."""

from __future__ import annotations

import secrets
import time


MAX_ATTEMPTS = 5
LOCKOUT_S = 600  # after the limit, retry waits this long

OK = "ok"
WRONG = "wrong"
LOCKED = "locked"


class PasswordGate:
    """Per-tg-id wrong-password counter. In-memory on purpose: a bot restart
    forgetting a lockout is fine, the password check itself is per attempt."""

    def __init__(self, max_attempts: int = MAX_ATTEMPTS, lockout_s: int = LOCKOUT_S) -> None:
        self.max_attempts = max_attempts
        self.lockout_s = lockout_s
        self._fails: dict[int, list[float]] = {}

    def check(self, tg_id: int, candidate: str, expected: str) -> str:
        """OK / WRONG:<attempts left> / LOCKED for this candidate."""
        fails = self._fresh_fails(tg_id)
        if len(fails) >= self.max_attempts:
            return LOCKED
        if secrets.compare_digest(candidate, expected):
            self._fails.pop(tg_id, None)
            return OK
        fails.append(time.monotonic())
        left = max(0, self.max_attempts - len(fails))
        return f"{WRONG}:{left}"

    def _fresh_fails(self, tg_id: int) -> list[float]:
        now = time.monotonic()
        fails = [t for t in self._fails.get(tg_id, []) if now - t < self.lockout_s]
        self._fails[tg_id] = fails
        return fails
