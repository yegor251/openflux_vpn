"""Bot-side channel operations: thin wrappers over oflux-admin core.

The bot runs on the exit VPS itself, so provisioning goes through
LocalOps (node-install.sh + systemctl locally, no self-SSH). Cookie
files and the channel registry live in the bot's config dir.
"""

from __future__ import annotations

import json
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
from docprovider.cupsrooms import CupsRoomError, create_rooms, pack_rooms
from docprovider.telemostconf import TelemostAuthError, create_conference
from oflux_admin.channels import channel_for
from oflux_admin.config import UserRegistry
from oflux_admin.localops import LocalOps
from oflux_admin.provision import Provisioner, build_channel_config

from .config import BotConfig, BotUsers, mask_link

# cups.online walls ROOM CREATION from datacenter IPs after a volume
# (2026-10-25: 403 after ~40 rooms/day from the VDS; joining existing
# rooms keeps working). Rooms are therefore created on the admin machine
# (scripts/refill_cups_pool.py) into a pool; /issue pops one set.
POOL_PATH = "cups-pool.json"
ROOMS_PER_SET = 8
TELEMOST_SESSION_FILE = "telemost-session-cookies.txt"


def _pop_room_set(cfg: BotConfig) -> str:
    """Take one packed room set from the pool (oldest first)."""
    path = cfg.config_dir / POOL_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"sets": []}
    sets = [s for s in data.get("sets", []) if s]
    if not sets:
        raise RuntimeError(
            "пул комнат cups.online пуст — запусти scripts/refill_cups_pool.py на машине админа"
        )
    rooms, sets = sets[0], sets[1:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sets": sets}, indent=1), encoding="utf-8")
    path.chmod(0o600)
    return rooms


def _push_room_set_back(cfg: BotConfig, rooms: str) -> None:
    # never used by a live channel: put it back at the front of the pool
    LocalOps().run(
        "python3 -c "
        f"'import json,pathlib; p=pathlib.Path(\"{cfg.config_dir}/{POOL_PATH}\"); "
        f"d=json.loads(p.read_text()); d[\"sets\"].insert(0, \"{rooms}\"); "
        "p.write_text(json.dumps(d, indent=1))'"
    )


def _telemost_url(cfg: BotConfig) -> str:
    """A fresh conference from the admin session file (lives ~24 h without
    activity, extends while the channel is in use)."""
    session_file = str(cfg.config_dir / TELEMOST_SESSION_FILE)
    if not Path(session_file).exists():
        raise RuntimeError(
            "нет файла сессии telemost — залогинься в telemost.yandex.ru и захвати "
            "запрос создания конференции (см. telemostconf.py)"
        )
    try:
        return create_conference(session_file)
    except TelemostAuthError as exc:
        raise RuntimeError(f"сессия telemost истекла: {exc}") from exc


def _finalize_carrier_conf(ops: LocalOps, channel: str, marker: str, timeout_s: int = 60) -> None:
    """A carrier node is ready when its conf carries only the carrier: drop
    the installer's always-on direct listener (a dual-transport exit
    throttles a single-carrier client to a trickle — TR-10 failover PoC),
    restart once and wait for the carrier's live line in the journal."""
    ops.run(f"sed -i '/\\[Transport direct\\]/,/Listen = /d' /etc/openflux-node/{channel}/node.conf")
    ops.run(f"systemctl restart openflux-node@{channel}.service")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _, out, _ = ops.run(
            f"journalctl -u openflux-node@{channel}.service -o cat --since '-40 s'"
            f" | grep -F '{marker}' | tail -1 || true"
        )
        if out.strip():
            return
        time.sleep(3)
    raise RuntimeError(f"нода не подняла носитель (нет строки «{marker}» в журнале)")


CARRIER_MARKERS = {"cups": "зашли в", "telemost": "publisher path"}


def _registry(cfg: BotConfig) -> UserRegistry:
    return UserRegistry(cfg.registry_path)


def load_registry(cfg: BotConfig) -> UserRegistry:
    """Fresh registry view (the file changes between operations)."""
    return _registry(cfg)


def carrier_of(user: dict) -> str | None:
    """Carrier a registry entry sits on (explicit field for new entries,
    derived for older ones that predate it)."""
    if user.get("carrier"):
        return user["carrier"]
    if user.get("telemost_url"):
        return "telemost"
    if user.get("cups_url"):
        return "cups"
    return None


def _fresh_channel(
    cfg: BotConfig,
    registry: UserRegistry,
    base: str,
    tg_id: int | None,
    *,
    carrier_kind: str,
    rooms: str | None,
    telemost_url: str | None,
) -> dict:
    """Allocate a name and port, apply the node, finalize the carrier,
    make + re-parse the link, register the channel. Raises on any step."""
    server = cfg.admin.server(None)
    channel = channel_for(base)
    while registry.get(channel):
        channel = channel_for(f"{base}-{secrets.token_hex(2)}")
    key = secrets.token_hex(32)
    port = registry.alloc_port(server)
    ops = LocalOps()
    prov = Provisioner(ops, cfg.admin.settings.installer)
    prov.sync_installer()
    prov.probe()
    conf = build_channel_config(
        channel=channel, key=key, port=port,
        cupsonline=rooms or "", telemost_url=telemost_url or "", mode="l3",
    )
    try:
        result = prov.apply(channel, conf)
        _finalize_carrier_conf(ops, channel, CARRIER_MARKERS[carrier_kind])
    except Exception:
        if rooms:
            _push_room_set_back(cfg, rooms)
        raise

    link = make_link(
        name=channel,
        secret=key,
        cups_url=rooms,
        telemost_url=telemost_url,
        binary=cfg.admin.settings.openflux_bin,
    )
    parse_link(link, binary=cfg.admin.settings.openflux_bin)

    registry.add(
        channel,
        channel=channel,
        key=key,
        port=int(result.get("port", port)),
        vds=server.name,
        host=server.host,
        cups_url=rooms,
        telemost_url=telemost_url,
        carrier=carrier_kind,
        link=link,
        tg_id=tg_id,
        created=_now(),
    )
    return {"channel": channel, "port": int(result.get("port", port)), "link": link}


def issue_channel(
    cfg: BotConfig,
    bot_users: BotUsers,
    tg_id: int,
    name_base: str | None = None,
    *,
    telemost_url: str | None = None,
) -> dict:
    """Full self-service issuance for one TG user. Raises on any step.

    Carrier-product channels (admin decision 2026-10-25, memory
    carrier-only-product): the link and the node carry the carrier only —
    no Yandex doc, no cookies, and the client never dials the VDS. The
    default carrier is cupsonline (a room set from the pool);
    ``telemost_url`` switches the channel to a Telemost conference
    created by the admin."""
    if bot_users.channel_of(tg_id):
        raise RuntimeError("у пользователя уже есть канал")
    registry = _registry(cfg)
    server = cfg.admin.server(None)
    base = name_base or f"user{tg_id}"
    if registry.active_count(server.name) >= server.max_channels:
        raise RuntimeError("сервер переполнен, попробуйте позже")

    carrier_kind = "cups" if not telemost_url else "telemost"
    rooms = None
    if not telemost_url:
        # carrier 1: cups — a set from the pool, or minted right here (cups
        # rate-limits creation after volume; when it refuses, fall through)
        try:
            rooms = _pop_room_set(cfg)
        except RuntimeError:
            rooms = None
        if rooms is None:
            try:
                rooms = pack_rooms(create_rooms())
                carrier_kind = "cups"
            except CupsRoomError:
                rooms = None
        if rooms is None:
            # carrier 2: a fresh Telemost conference, created from the admin
            # session file; lives ~24 h without activity and extends while
            # the channel is in use
            carrier_kind = "telemost"
            telemost_url = _telemost_url(cfg)

    result = _fresh_channel(
        cfg, registry, base, tg_id,
        carrier_kind=carrier_kind, rooms=rooms, telemost_url=telemost_url,
    )
    bot_users.bind_channel(tg_id, result["channel"])
    return result


def migrate_user_channel(
    cfg: BotConfig, bot_users: BotUsers, old_channel: str, target_carrier: str
) -> dict:
    """Hand a user over to target_carrier: a fresh channel (new rooms or
    conference, new key) replaces theirs, the TG binding switches at once.

    Replacement semantics: per-user/server caps are not consulted (the net
    channel count does not grow). The old channel is retired by the caller
    AFTER the new link is delivered — until then a failed hand-over can be
    undone with undo_migration. Raises on any failure; the old channel
    stays untouched."""
    registry = _registry(cfg)
    user = registry.get(old_channel)
    if not user or user.get("revoked"):
        raise RuntimeError("канал не найден или отозван")
    if target_carrier not in ("cups", "telemost"):
        raise RuntimeError(f"неизвестный носитель: {target_carrier}")

    rooms = telemost_url = None
    if target_carrier == "cups":
        try:
            rooms = _pop_room_set(cfg)
        except RuntimeError:
            rooms = None
        if rooms is None:
            # CupsRoomError propagates: the spare refused, nobody moves
            rooms = pack_rooms(create_rooms())
    else:
        telemost_url = _telemost_url(cfg)

    tg_id = user.get("tg_id")
    base = f"user{tg_id}" if tg_id else old_channel
    result = _fresh_channel(
        cfg, registry, base, tg_id,
        carrier_kind=target_carrier, rooms=rooms, telemost_url=telemost_url,
    )
    if tg_id:
        bot_users.bind_channel(tg_id, result["channel"])
    return result


def retire_channel(cfg: BotConfig, channel: str) -> None:
    """Remove a replaced channel's node and mark it revoked (binding stays
    with the replacement)."""
    prov = Provisioner(LocalOps(), cfg.admin.settings.installer)
    prov.sync_installer()
    try:
        prov.remove(channel)
    except RuntimeError:
        pass  # already gone (a prior failed retire) - revocation is the point
    _registry(cfg).revoke(channel)


def undo_migration(cfg: BotConfig, bot_users: BotUsers, new_channel: str, old_channel: str) -> None:
    """Roll a hand-over back (the new link could not be delivered): drop the
    fresh channel, restore the old binding. The old node was never touched."""
    registry = _registry(cfg)
    user = registry.get(new_channel)
    tg_id = user.get("tg_id") if user else None
    retire_channel(cfg, new_channel)
    if tg_id:
        bot_users.bind_channel(tg_id, old_channel)


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
    try:
        prov.remove(channel)
    except RuntimeError:
        # absence is fine: a prior failed rotate may have removed the node
        # already; apply below recreates it from scratch
        pass
    if user.get("telemost_url"):
        conf = build_channel_config(
            channel=channel,
            key=new_key,
            port=user["port"],
            telemost_url=user["telemost_url"],
            mode=mode,
        )
    elif user.get("cups_url"):
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

    # the installer re-adds its direct listener on every apply
    if user.get("cups_url"):
        _finalize_carrier_conf(ops, channel, CARRIER_MARKERS["cups"])
        link = make_link(
            name=channel,
            secret=new_key,
            cups_url=user["cups_url"],
            binary=cfg.admin.settings.openflux_bin,
        )
    elif user.get("telemost_url"):
        _finalize_carrier_conf(ops, channel, CARRIER_MARKERS["telemost"])
        link = make_link(
            name=channel,
            secret=new_key,
            telemost_url=user["telemost_url"],
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
    if user.get("cups_url") or user.get("telemost_url"):
        kind = "telemost" if user.get("telemost_url") else "cups"
        # carrier channel: no client-facing listener; health = the node is
        # sitting in its carrier (journal line from the last (re)start)
        _, out, _ = LocalOps().run(
            f"journalctl -u openflux-node@{channel}.service -o cat"
            f" | grep -F '{CARRIER_MARKERS[kind]}' | tail -1 || true"
        )
        line = out.strip()
        checks.append((f"carrier ({kind})", bool(line), line or "нет записи о живом носителе"))
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
    "carrier_of",
    "issue_channel",
    "load_registry",
    "migrate_user_channel",
    "reissue_link",
    "retire_channel",
    "rotate_channel",
    "revoke_channel",
    "doctor_channel",
    "undo_migration",
    "mask_link",
]
