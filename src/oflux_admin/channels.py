"""High-level channel operations: add-user, rotate, doctor, revoke, link.

One call = one person's VPN channel: carrier documents (docprovider),
a channel on the VDS (node-install.sh fork over SSH), and an
openflux:// link with QR for the user's app.
"""

from __future__ import annotations

import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path

import qrcode

from docprovider import (
    ChannelDocs,
    MailruAccount,
    MailruClient,
    YandexAccount,
    YandexDocCreator,
    check_yandex_doc,
    make_link,
    parse_link,
)

from .config import AdminConfig, Server, UserRegistry, sanitize_channel
from .provision import Provisioner, build_channel_config
from .sshops import SSHOps

DIRECT_PRIORITY = 200
YANDEX_PRIORITY = 100
MAILRU_PRIORITY = 90


def channel_for(name: str) -> str:
    """User-facing name -> installer-safe channel id (of-<name>)."""
    base = name.strip().lower()
    if base.startswith("of-"):
        base = base[3:]
    return sanitize_channel(f"of-{sanitize_channel(base)}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def pick_server(config: AdminConfig, registry: UserRegistry) -> Server:
    """The server with the most free channel slots; raises if all full."""
    candidates = [
        s for s in config.servers if registry.active_count(s.name) < s.max_channels
    ]
    if not candidates:
        raise ValueError(
            "all servers are at max_channels: "
            + ", ".join(f"{s.name} ({s.max_channels})" for s in config.servers)
        )
    return max(candidates, key=lambda s: s.max_channels - registry.active_count(s.name))


def _docs_from_registry(user: dict) -> ChannelDocs:
    mailru = type("D", (), {"url": user["mailru_url"]})() if user.get("mailru_url") else None
    return ChannelDocs(
        name=user["channel"],
        yandex=type("D", (), {"url": user["yandex_url"]})(),
        mailru=mailru,
    )


def add_user(
    *,
    name: str,
    config: AdminConfig,
    registry: UserRegistry,
    server: Server | None,
    yandex_cookies: str,
    out_dir: Path,
    mode: str = "l3",
) -> dict:
    server = server or pick_server(config, registry)
    channel = channel_for(name)
    if registry.get(channel):
        raise ValueError(f"user {name!r} already exists (channel {channel})")
    if registry.active_count(server.name) >= server.max_channels:
        raise ValueError(f"server {server.name!r} is at max_channels={server.max_channels}")

    # 1. carrier documents (admin machine; needs live cookies)
    yandex = YandexDocCreator(YandexAccount(id="admin", cookies_path=yandex_cookies))
    ya = yandex.create(channel)

    # 2. credentials and port
    key = secrets.token_hex(32)
    port = registry.alloc_port(server)

    # 3. channel on the VDS
    with SSHOps(server.host, server.user, server.key_path) as ops:
        prov = Provisioner(ops, config.settings.installer)
        prov.sync_installer()
        prov.probe()
        cfg = build_channel_config(
            channel=channel,
            key=key,
            port=port,
            yandex_url=ya.url,
            mode=mode,
        )
        result = prov.apply(channel, cfg)

    # 4. user-facing link + QR
    docs = ChannelDocs(name=channel, yandex=ya)
    link = make_link(
        name=channel,
        secret=key,
        docs=docs,
        direct_dial=f"{server.host}:{result.get('port', port)}",
        binary=config.settings.openflux_bin,
    )
    parse_link(link, binary=config.settings.openflux_bin)  # self-check

    out_dir.mkdir(parents=True, exist_ok=True)
    qr_path = out_dir / f"{channel}.png"
    qrcode.make(link).save(qr_path)

    return registry.add(
        channel,
        channel=channel,
        key=key,
        port=int(result.get("port", port)),
        vds=server.name,
        host=server.host,
        yandex_url=ya.url,
        yandex_path=ya.path,
        link=link,
        qr=str(qr_path),
        created=_now(),
    )


def link_user(
    *,
    name: str,
    config: AdminConfig,
    registry: UserRegistry,
    out_dir: Path,
) -> str:
    """Re-print the openflux:// link + QR for an existing channel."""
    channel = channel_for(name)
    user = registry.get(channel)
    if not user or user.get("revoked"):
        raise ValueError(f"no active user {name!r}")
    link = make_link(
        name=channel,
        secret=user["key"],
        docs=_docs_from_registry(user),
        direct_dial=f"{user['host']}:{user['port']}",
        binary=config.settings.openflux_bin,
    )
    qr_path = out_dir / f"{channel}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    qrcode.make(link).save(qr_path)
    registry.update(channel, link=link, qr=str(qr_path))
    return link


def rotate_user(
    *,
    name: str,
    config: AdminConfig,
    registry: UserRegistry,
    out_dir: Path,
) -> dict:
    """New channel key on the VDS + new user link. Documents untouched.

    The old link stops working the moment the channel is re-applied; the
    user must import the new one.
    """
    channel = channel_for(name)
    user = registry.get(channel)
    if not user or user.get("revoked"):
        raise ValueError(f"no active user {name!r}")
    server = config.server(user["vds"])
    new_key = secrets.token_hex(32)

    with SSHOps(server.host, server.user, server.key_path) as ops:
        prov = Provisioner(ops, config.settings.installer)
        prov.sync_installer()
        mode = prov.channel_mode(channel)
        prov.remove(channel)
        conf = build_channel_config(
            channel=channel,
            key=new_key,
            port=user["port"],
            yandex_url=user["yandex_url"],
            mailru_url=user.get("mailru_url", ""),
            mode=mode,
        )
        result = prov.apply(channel, conf)

    link = make_link(
        name=channel,
        secret=new_key,
        docs=_docs_from_registry(user),
        direct_dial=f"{user['host']}:{result.get('port', user['port'])}",
        binary=config.settings.openflux_bin,
    )
    parse_link(link, binary=config.settings.openflux_bin)  # self-check
    qr_path = out_dir / f"{channel}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    qrcode.make(link).save(qr_path)
    registry.update(
        channel,
        key=new_key,
        link=link,
        qr=str(qr_path),
        rotated=_now(),
    )
    return {"channel": channel, "port": int(result.get("port", user["port"])), "link": link, "qr": str(qr_path)}


def doctor_user(*, name: str, config: AdminConfig, registry: UserRegistry) -> dict:
    """Health checks for one channel: unit, port, documents."""
    channel = channel_for(name)
    user = registry.get(channel)
    if not user:
        raise ValueError(f"no user {name!r}")
    checks: dict[str, dict] = {}
    report = {"channel": channel, "revoked": bool(user.get("revoked")), "checks": checks}
    if user.get("revoked"):
        checks["registry"] = {"ok": False, "detail": "channel is revoked"}
        return report

    server = config.server(user["vds"])
    with SSHOps(server.host, server.user, server.key_path) as ops:
        prov = Provisioner(ops, config.settings.installer)
        _, out, _ = ops.run(f"systemctl is-active openflux-node@{channel}")
        state = out.strip() or "unknown"
        checks["unit"] = {"ok": state == "active", "detail": state}
        _, out, _ = ops.run(
            f"systemctl show openflux-node@{channel} -p NRestarts --value"
        )
        restarts = out.strip() or "0"
        checks["restarts"] = {"ok": restarts == "0", "detail": restarts}
        mode = prov.channel_mode(channel)
        checks["mode"] = {"ok": True, "detail": mode or "unknown"}

    try:
        with socket.create_connection((user["host"], int(user["port"])), timeout=4):
            checks["port"] = {"ok": True, "detail": f"{user['host']}:{user['port']} reachable"}
    except OSError as exc:
        checks["port"] = {"ok": False, "detail": str(exc)}

    try:
        check_yandex_doc(user["yandex_url"])
        checks["yandex_doc"] = {"ok": True, "detail": "reachable (PoW wall = normal)"}
    except Exception as exc:  # noqa: BLE001 - report, don't crash doctor
        checks["yandex_doc"] = {"ok": False, "detail": str(exc)[:120]}

    if user.get("mailru_url"):
        checks["mailru_doc"] = {
            "ok": False,
            "detail": "legacy channel: mailru anon access closed (upstream #193)",
        }
    return report


def revoke_user(
    *,
    name: str,
    config: AdminConfig,
    registry: UserRegistry,
    yandex_cookies: str,
    mailru_cookies: str,
) -> dict:
    """Kill the channel on the VDS and revoke carrier links. Best-effort
    cleanup of documents; the registry entry stays (marked revoked)."""
    channel = channel_for(name)
    user = registry.get(channel)
    if not user:
        raise ValueError(f"no user {name!r}")
    if user.get("revoked"):
        raise ValueError(f"user {name!r} is already revoked")

    server = config.server(user["vds"])
    errors: list[str] = []

    with SSHOps(server.host, server.user, server.key_path) as ops:
        prov = Provisioner(ops, config.settings.installer)
        prov.sync_installer()
        try:
            prov.remove(channel)
        except RuntimeError as exc:
            errors.append(f"vds: {exc}")

    try:
        if user.get("mailru_url"):  # legacy channels provisioned before 2026-10-18
            mailru = MailruClient(
                MailruAccount(
                    id="admin",
                    login=config.settings.mailru_login,
                    cookies_path=mailru_cookies,
                )
            )
            mailru.unpublish(user["mailru_url"])
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup
        errors.append(f"mailru unpublish: {exc}")

    try:
        yandex = YandexDocCreator(YandexAccount(id="admin", cookies_path=yandex_cookies))
        yandex.unpublish(user["yandex_path"])
    except Exception as exc:  # noqa: BLE001
        errors.append(f"yandex unpublish: {exc}")

    registry.revoke(channel)
    return {"channel": channel, "cleanup_errors": errors}
