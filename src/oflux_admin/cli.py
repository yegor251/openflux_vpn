"""oflux-admin CLI: manage per-user VPN channels on OpenFlux exit nodes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .channels import add_user, channel_for, link_user, revoke_user
from .config import AdminConfig, UserRegistry, load_config

DEFAULT_CONFIG_DIR = Path("~/.config/oflux-admin").expanduser()


def _default(name: str) -> str:
    path = Path(name)
    return str(path) if path.exists() else name


def _open(config_path: str) -> tuple[AdminConfig, UserRegistry]:
    cfg = load_config(Path(config_path).expanduser())
    registry = UserRegistry(Path(config_path).expanduser().parent / "users.json")
    return cfg, registry


def cmd_add_user(args, cfg: AdminConfig, registry: UserRegistry) -> int:
    server = cfg.server(args.server)
    user = add_user(
        name=args.name,
        config=cfg,
        registry=registry,
        server=server,
        yandex_cookies=args.yandex_cookies,
        mailru_cookies=args.mailru_cookies,
        out_dir=Path(cfg.settings.out_dir),
    )
    print(f"user {args.name} -> channel {user['channel']} on {server.name} ({server.host}:{user['port']})")
    print(f"link: {user['link']}")
    print(f"qr:   {user['qr']}")
    print("hand the link (or scan the QR) to the user over a secure channel")
    return 0


def cmd_list(args, cfg: AdminConfig, registry: UserRegistry) -> int:
    users = registry.active()
    if not users:
        print("no active users")
        return 0
    print(f"{'user':<20} {'vds':<10} {'port':<7} created")
    for channel, u in sorted(users.items()):
        print(f"{channel:<20} {u['vds']:<10} {u['port']:<7} {u['created']}")
    return 0


def cmd_link(args, cfg: AdminConfig, registry: UserRegistry) -> int:
    link = link_user(
        name=args.name,
        config=cfg,
        registry=registry,
        out_dir=Path(cfg.settings.out_dir),
    )
    print(link)
    return 0


def cmd_revoke(args, cfg: AdminConfig, registry: UserRegistry) -> int:
    result = revoke_user(
        name=args.name,
        config=cfg,
        registry=registry,
        yandex_cookies=args.yandex_cookies,
        mailru_cookies=args.mailru_cookies,
    )
    print(f"revoked {result['channel']}")
    for err in result["cleanup_errors"]:
        print(f"cleanup warning: {err}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oflux-admin", description=__doc__)
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_DIR / "servers.yaml"),
        help="servers.yaml path (default: ~/.config/oflux-admin/servers.yaml)",
    )
    parser.add_argument(
        "--yandex-cookies",
        default=_default("ya-cookies.txt"),
        help="Netscape cookies.txt of the admin's Yandex account",
    )
    parser.add_argument(
        "--mailru-cookies",
        default=_default("mr-cookies.txt"),
        help="Netscape cookies.txt of the admin's Mail.ru account",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add-user", help="create a new user's VPN channel")
    p_add.add_argument("name", help="user name (a-z, 0-9, dash)")
    p_add.add_argument("--server", default=None, help="server name from servers.yaml")

    sub.add_parser("list", help="list active users")

    p_link = sub.add_parser("link", help="re-print the openflux:// link and QR")
    p_link.add_argument("name")

    p_revoke = sub.add_parser("revoke", help="kill a user's channel and revoke documents")
    p_revoke.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg, registry = _open(args.config)
    handlers = {
        "add-user": cmd_add_user,
        "list": cmd_list,
        "link": cmd_link,
        "revoke": cmd_revoke,
    }
    return handlers[args.cmd](args, cfg, registry)


if __name__ == "__main__":
    sys.exit(main())
