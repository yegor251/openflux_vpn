"""Configuration for oflux-admin: servers.yaml + users.json registry."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

CHANNEL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def sanitize_channel(name: str) -> str:
    """Channel names must satisfy node-install.sh's own regex."""
    channel = name.strip().lower().replace("_", "-").replace(" ", "-")
    if not CHANNEL_RE.match(channel):
        raise ValueError(
            f"invalid channel {name!r}: expected [a-z0-9][a-z0-9-]{{0,30}}"
        )
    return channel


@dataclass(frozen=True)
class Server:
    name: str
    host: str
    user: str = "root"
    key_path: str = ""
    port_range: tuple[int, int] = (20000, 20099)
    max_channels: int = 20


@dataclass(frozen=True)
class AdminSettings:
    mailru_login: str = ""
    installer: str = "deploy/node-install.sh"
    openflux_bin: str = "bin/openflux"
    out_dir: str = "out"


@dataclass(frozen=True)
class AdminConfig:
    settings: AdminSettings
    servers: list[Server]

    def server(self, name: str | None) -> Server:
        if name is None:
            if not self.servers:
                raise ValueError("no servers configured")
            return self.servers[0]
        for s in self.servers:
            if s.name == name:
                return s
        raise ValueError(f"unknown server {name!r}")


def load_config(path: str | Path) -> AdminConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    settings = AdminSettings(**(raw.get("settings") or {}))
    servers = [
        Server(
            name=s["name"],
            host=s["host"],
            user=s.get("user", "root"),
            key_path=s.get("key_path", ""),
            port_range=tuple(s.get("port_range", [20000, 20099])),
            max_channels=s.get("max_channels", 20),
        )
        for s in raw.get("servers", [])
    ]
    return AdminConfig(settings=settings, servers=servers)


class UserRegistry:
    """users.json: one entry per VPN channel handed to a person."""

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
        data.setdefault("users", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")

    # -- queries -------------------------------------------------------------

    def get(self, name: str) -> dict | None:
        return self._data["users"].get(name)

    def active(self) -> dict[str, dict]:
        return {n: u for n, u in self._data["users"].items() if not u.get("revoked")}

    def used_ports(self, vds: str) -> set[int]:
        return {
            u["port"]
            for u in self._data["users"].values()
            if u.get("vds") == vds and not u.get("revoked") and u.get("port")
        }

    def alloc_port(self, server: Server) -> int:
        used = self.used_ports(server.name)
        low, high = server.port_range
        for port in range(low, high + 1):
            if port not in used:
                return port
        raise ValueError(f"server {server.name}: port range {low}-{high} exhausted")

    def active_count(self, vds: str) -> int:
        return len([u for u in self.active().values() if u.get("vds") == vds])

    # -- mutations -----------------------------------------------------------

    def add(self, name: str, **fields) -> dict:
        if self.get(name) is not None:
            raise ValueError(f"user {name!r} already exists")
        user = {"created": fields.pop("created"), "revoked": None, **fields}
        self._data["users"][name] = user
        self.save()
        return user

    def update(self, name: str, **fields) -> dict:
        user = self._data["users"][name]
        user.update(fields)
        self.save()
        return user

    def revoke(self, name: str) -> None:
        user = self.get(name)
        if user is None:
            raise ValueError(f"no user {name!r}")
        if user.get("revoked"):
            raise ValueError(f"user {name!r} is already revoked")
        self.update(name, revoked=True)
