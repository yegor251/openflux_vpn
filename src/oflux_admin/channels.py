"""High-level channel operations: add-user, revoke, link.

One call = one person's VPN channel: carrier documents (docprovider),
a channel on the VDS (node-install.sh fork over SSH), and an
openflux:// link with QR for the user's app.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

import qrcode

from docprovider import (
    ChannelDocs,
    MailruAccount,
    MailruClient,
    YandexAccount,
    YandexDocCreator,
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


def add_user(
    *,
    name: str,
    config: AdminConfig,
    registry: UserRegistry,
    server: Server,
    yandex_cookies: str,
    mailru_cookies: str,
    out_dir: Path,
) -> dict:
    channel = channel_for(name)
    if registry.get(channel):
        raise ValueError(f"user {name!r} already exists (channel {channel})")
    if registry.active_count(server.name) >= server.max_channels:
        raise ValueError(f"server {server.name!r} is at max_channels={server.max_channels}")

    # 1. carrier documents (admin machine; needs live cookies)
    yandex = YandexDocCreator(YandexAccount(id="admin", cookies_path=yandex_cookies))
    ya = yandex.create(channel)
    mailru = MailruClient(
        MailruAccount(
            id="admin",
            login=config.settings.mailru_login,
            cookies_path=mailru_cookies,
        )
    )
    mr = mailru.create_doc(channel)

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
            mailru_url=mr.url,
        )
        result = prov.apply(channel, cfg)

    # 4. user-facing link + QR
    docs = ChannelDocs(name=channel, yandex=ya, mailru=mr)
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
        mailru_url=mr.url,
        mailru_home=mr.home,
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
    docs = ChannelDocs(
        name=channel,
        yandex=type("D", (), {"url": user["yandex_url"]})(),
        mailru=type("D", (), {"url": user["mailru_url"]})(),
    )
    link = make_link(
        name=channel,
        secret=user["key"],
        docs=docs,
        direct_dial=f"{user['host']}:{user['port']}",
        binary=config.settings.openflux_bin,
    )
    qr_path = out_dir / f"{channel}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    qrcode.make(link).save(qr_path)
    registry.update(channel, link=link, qr=str(qr_path))
    return link


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
