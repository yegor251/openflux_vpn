"""Bot-side channel operations: thin wrappers over oflux-admin core.

The bot runs on the exit VPS itself, so provisioning goes through
LocalOps (node-install.sh + systemctl locally, no self-SSH). Cookie
files and the channel registry live in the bot's config dir.
"""

from __future__ import annotations

import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from docprovider import (
    ChannelDocs,
    YandexAccount,
    YandexDocCreator,
    make_link,
    parse_link,
)
from docprovider.cupsrooms import create_rooms, pack_rooms  # noqa: F401 - admin-side utility
from oflux_admin.channels import channel_for
from oflux_admin.config import UserRegistry
from oflux_admin.localops import LocalOps
from oflux_admin.provision import Provisioner, build_channel_config

from .config import BotConfig, BotUsers, mask_link

# Placeholder room list for the first node start: passes the installer's
# valid_rooms (9 chars, base64url alphabet) but is not a decodable list, so
# the exit creates fresh rooms at start and prints them to the journal.
CUPS_BOOTSTRAP_URL = "bootstrap"


def _cups_rooms_from_journal(ops: LocalOps, channel: str, timeout_s: int = 90) -> str:
    """Poll the unit journal for the packed list the exit prints on its
    first start without one ('=== COPY THIS TO CLIENT ===')."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, out, _ = ops.run(
            f"journalctl -u openflux-node@{channel}.service -o cat --since '-10 min'"
            " | grep -A1 'COPY THIS' | tail -1 || true"
        )
        packed = out.strip().splitlines()[-1].strip() if out.strip() else ""
        if packed.startswith("eyJ"):
            return packed
        time.sleep(3)
    raise RuntimeError("нода не напечатала список комнат cups.online вовремя")


def _pin_cups_rooms(ops: LocalOps, channel: str, rooms: str, timeout_s: int = 60) -> None:
    """Finalize a cups node conf: pin the room list into it, drop the
    installer's always-on direct listener (a dual-transport exit throttles
    a cups-only client to a trickle — TR-10 failover PoC), restart once and
    wait for the re-join line."""
    ops.run(
        f"sed -i 's|^URL = {CUPS_BOOTSTRAP_URL}|URL = {rooms}|'"
        f" /etc/openflux-node/{channel}/node.conf"
    )
    ops.run(f"sed -i '/\\[Transport direct\\]/,/Listen = /d' /etc/openflux-node/{channel}/node.conf")
    ops.run(f"systemctl restart openflux-node@{channel}.service")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, out, _ = ops.run(
            f"journalctl -u openflux-node@{channel}.service -o cat --since '-40 s'"
            " | grep -F 'зашли в' | tail -1 || true"
        )
        if out.strip():
            return
        time.sleep(3)
    raise RuntimeError("нода не перезашла в комнаты после пина списка")


def _registry(cfg: BotConfig) -> UserRegistry:
    return UserRegistry(cfg.registry_path)


def issue_channel(cfg: BotConfig, bot_users: BotUsers, tg_id: int, name_base: str | None = None) -> dict:
    """Full self-service issuance for one TG user. Raises on any step.

    Carrier-product channels (admin decision 2026-10-25, memory
    carrier-only-product): the link and the node carry the cupsonline
    carrier only — rooms are created anonymously here, no Yandex doc,
    no cookies, and the client never dials the VDS (the node keeps its
    direct listener for admin drills alone)."""
    if bot_users.channel_of(tg_id):
        raise RuntimeError("у пользователя уже есть канал")
    registry = _registry(cfg)
    server = cfg.admin.server(None)
    base = name_base or f"user{tg_id}"
    channel = channel_for(base)
    while registry.get(channel):
        channel = channel_for(f"{base}-{secrets.token_hex(2)}")
    if registry.active_count(server.name) >= server.max_channels:
        raise RuntimeError("сервер переполнен, попробуйте позже")

    key = secrets.token_hex(32)
    port = registry.alloc_port(server)
    ops = LocalOps()
    prov = Provisioner(ops, cfg.admin.settings.installer)
    prov.sync_installer()
    prov.probe()
    conf = build_channel_config(
        channel=channel, key=key, port=port, cupsonline=CUPS_BOOTSTRAP_URL, mode="l3"
    )
    result = prov.apply(channel, conf)

    rooms = _cups_rooms_from_journal(ops, channel)
    _pin_cups_rooms(ops, channel, rooms)

    link = make_link(
        name=channel,
        secret=key,
        cups_url=rooms,
        binary=cfg.admin.settings.openflux_bin,
    )
    parse_link(link, binary=cfg.admin.settings.openflux_bin)

    port_final = int(result.get("port", port))
    registry.add(
        channel,
        channel=channel,
        key=key,
        port=port_final,
        vds=server.name,
        host=server.host,
        cups_url=rooms,
        link=link,
        tg_id=tg_id,
        created=_now(),
    )
    bot_users.bind_channel(tg_id, channel)
    return {"channel": channel, "port": port_final, "link": link}


def reissue_link(cfg: BotConfig, channel: str) -> str:
    """Fresh openflux:// link + QR for an existing channel (same key)."""
    registry = _registry(cfg)
    user = registry.get(channel)
    if not user or user.get("revoked"):
        raise RuntimeError("канал не найден или отозван")
    return user["link"]


def rotate_channel(cfg: BotConfig, channel: str) -> dict:
    """New key on the VDS + new link. Old link dies.

    Room lists survive rotation on purpose: cups rooms have no key
    inside, re-creating them would invalidate every imported profile
    for nothing."""
    registry = _registry(cfg)
    user = registry.get(channel)
    if not user or user.get("revoked"):
        raise RuntimeError("канал не найден или отозван")
    new_key = secrets.token_hex(32)

    ops = LocalOps()
    prov = Provisioner(ops, cfg.admin.settings.installer)
    prov.sync_installer()
    mode = prov.channel_mode(channel)
    prov.remove(channel)
    if user.get("cups_url"):
        conf = build_channel_config(
            channel=channel,
            key=new_key,
            port=user["port"],
            cupsonline=user["cups_url"],
            mode=mode,
        )
    else:
        conf = build_channel_config(
            channel=channel,
            key=new_key,
            port=user["port"],
            yandex_url=user["yandex_url"],
            mode=mode,
        )
    result = prov.apply(channel, conf)

    if user.get("cups_url"):
        # the installer re-adds its direct listener on every apply
        _pin_cups_rooms(ops, channel, user["cups_url"])
        link = make_link(
            name=channel,
            secret=new_key,
            cups_url=user["cups_url"],
            binary=cfg.admin.settings.openflux_bin,
        )
    else:
        docs = ChannelDocs(
            name=channel, yandex=type("D", (), {"url": user["yandex_url"]})()
        )
        link = make_link(
            name=channel,
            secret=new_key,
            docs=docs,
            direct_dial=f"{user['host']}:{result.get('port', user['port'])}",
            binary=cfg.admin.settings.openflux_bin,
        )
    parse_link(link, binary=cfg.admin.settings.openflux_bin)
    registry.update(channel, key=new_key, link=link, rotated=_now())
    return {"link": link, "port": int(result.get("port", user["port"]))}


def revoke_channel(cfg: BotConfig, bot_users: BotUsers, channel: str, tg_id: int) -> list[str]:
    """Remove the channel and unpublish documents. Best-effort cleanup."""
    registry = _registry(cfg)
    user = registry.get(channel)
    if not user:
        raise RuntimeError("канал не найден")
    errors: list[str] = []

    ops = LocalOps()
    prov = Provisioner(ops, cfg.admin.settings.installer)
    prov.sync_installer()
    try:
        prov.remove(channel)
    except RuntimeError as exc:
        errors.append(f"vds: {exc}")

    if user.get("yandex_path"):
        try:
            YandexDocCreator(YandexAccount(id="bot", cookies_path=str(cfg.cookies_path))).unpublish(
                user["yandex_path"]
            )
        except Exception as exc:  # noqa: BLE001 - known limitation (README)
            errors.append(f"yandex unpublish: {exc}")

    registry.revoke(channel)
    bot_users.unbind(tg_id)
    return errors


def doctor_channel(cfg: BotConfig, channel: str) -> list[tuple[str, bool, str]]:
    """Health checks runnable on the bot's own VPS (no port-from-outside)."""
    import subprocess

    registry = _registry(cfg)
    user = registry.get(channel)
    if not user:
        return [("registry", False, "канал не найден")]
    checks: list[tuple[str, bool, str]] = []
    rc, out = subprocess.getstatusoutput(f"systemctl is-active openflux-node@{channel}")
    checks.append(("unit", out.strip() == "active", out.strip() or "unknown"))
    if user.get("cups_url"):
        # carrier channel: no client-facing listener; health = the node is
        # sitting in its rooms (journal line from the last (re)start)
        _, out, _ = LocalOps().run(
            f"journalctl -u openflux-node@{channel}.service -o cat"
            " | grep -F 'зашли в' | tail -1 || true"
        )
        line = out.strip()
        checks.append(("carrier", bool(line), line or "нет записи о входе в комнаты"))
        return checks
    port = user["port"]
    import socket

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=3):
            checks.append(("port", True, f"127.0.0.1:{port} слушается"))
    except OSError as exc:
        checks.append(("port", False, str(exc)))
    try:
        from docprovider.validate import check_yandex_doc

        check_yandex_doc(user["yandex_url"])
        checks.append(("yandex_doc", True, "доступен (PoW-стена = норма)"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("yandex_doc", False, str(exc)[:100]))
    return checks


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "issue_channel",
    "reissue_link",
    "rotate_channel",
    "revoke_channel",
    "doctor_channel",
    "mask_link",
]
