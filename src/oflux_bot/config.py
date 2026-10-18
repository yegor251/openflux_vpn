"""Bot configuration and per-user state (invite tokens, bindings)."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from oflux_admin.config import AdminConfig, load_config


@dataclass(frozen=True)
class BotConfig:
    token: str
    alert_tg_id: int
    max_channels_per_user: int
    admin: AdminConfig  # servers.yaml of the bot host (single server)

    @property
    def config_dir(self) -> Path:
        return Path("~/.config/oflux-bot").expanduser()

    @property
    def registry_path(self) -> Path:
        return self.config_dir / "users.json"

    @property
    def cookies_path(self) -> Path:
        return self.config_dir / "ya-cookies.txt"

    @property
    def state_path(self) -> Path:
        return self.config_dir / "bot_users.json"


def load_bot_config(path: str | Path = "~/.config/oflux-bot/bot.yaml") -> BotConfig:
    raw = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    admin = load_config(Path(raw.get("servers_yaml", "~/.config/oflux-bot/servers.yaml")).expanduser())
    token = raw.get("bot_token", "")
    alert_tg_id = int(raw.get("alert_tg_id", 0))
    if not token:
        raise SystemExit("bot.yaml: bot_token is empty — fill it from BotFather")
    if not alert_tg_id:
        raise SystemExit("bot.yaml: alert_tg_id is empty — alerts have no recipient")
    return BotConfig(
        token=token,
        alert_tg_id=alert_tg_id,
        max_channels_per_user=int(raw.get("max_channels_per_user", 1)),
        admin=admin,
    )


def mask_link(link: str) -> str:
    """Safe-for-logs form of an openflux:// link (it carries the key)."""
    if link.startswith("openflux://"):
        return "openflux://v1/<redacted>"
    return link[:24] + "…"


class BotUsers:
    """bot_users.json: invite tokens and tg_id -> channel bindings.

    Kept separate from the channel registry (users.json): a registered
    TG user may exist before their channel is issued.
    """

    VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data.setdefault("version", self.VERSION)
        data.setdefault("users", {})  # tg_id -> {"channel": name|None, "invite": token}
        data.setdefault("invites", {})  # token -> {"created": iso, "tg_id": int|None}
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        self.path.chmod(0o600)

    # -- invites -------------------------------------------------------------

    def new_invite(self) -> str:
        token = secrets.token_urlsafe(12)
        self._data["invites"][token] = {
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tg_id": None,
        }
        self.save()
        return token

    def claim_invite(self, token: str, tg_id: int) -> bool:
        """Bind a fresh invite to tg_id. A used invite never re-binds."""
        invite = self._data["invites"].get(token)
        if invite is None or invite["tg_id"] is not None:
            return False
        invite["tg_id"] = tg_id
        self._data["users"].setdefault(str(tg_id), {"channel": None, "invite": token})
        self.save()
        return True

    def is_registered(self, tg_id: int) -> bool:
        return str(tg_id) in self._data["users"]

    # -- bindings ------------------------------------------------------------

    def channel_of(self, tg_id: int) -> str | None:
        user = self._data["users"].get(str(tg_id))
        return user.get("channel") if user else None

    def bind_channel(self, tg_id: int, channel: str) -> None:
        self._data["users"][str(tg_id)]["channel"] = channel
        self.save()

    def unbind(self, tg_id: int) -> None:
        user = self._data["users"].get(str(tg_id))
        if user:
            user["channel"] = None
            self.save()

    def channels_per_user(self, tg_id: int) -> int:
        return sum(
            1
            for u in self._data["users"].values()
            if u.get("channel") and self.channel_owner(u["channel"]) == tg_id
        )

    def channel_owner(self, channel: str) -> int | None:
        for tg_id, user in self._data["users"].items():
            if user.get("channel") == channel:
                return int(tg_id)
        return None
