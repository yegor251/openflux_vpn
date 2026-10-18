#!/usr/bin/env python3
"""Cookie health check and semi-automatic refresh for the Yandex session.

The login cookies (Session_id / sessionid2) carry a far-future calendar
expiry but can be revoked server-side at any moment (a login elsewhere,
anti-fraud), so validity is decided by a live probe, not by dates.

  refresh_cookies.py check            is the current ya-cookies.txt alive?
  refresh_cookies.py install FILE     validate + install a fresh export

`install` validates the new export against a live document first, backs
up the old file, then feeds every consumer:
  - ya-cookies.txt                     docprovider / oflux-admin (admin machine)
  - out/cookies-yandex.json            client cookie store (regenerated)
  - /var/lib/openflux-node/<ch>/...    node cookie store, ONLY channels
                                       whose node.conf carries a yandex
                                       lane; restarting those units kicks
                                       their clients (reported).

Cookie values are never printed; files land 0600; everything is
gitignored. See .claude/skills/refresh-cookies for the agent workflow.
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from docprovider.netscape import load_into_session, parse_netscape
from docprovider.validate import check_yandex_doc
from oflux_admin.config import UserRegistry
from oflux_admin.sshops import SSHOps

COOKIES_PATH = ROOT / "ya-cookies.txt"
CLIENT_STORE = ROOT / "out" / "cookies-yandex.json"
REGISTRY = Path("~/.config/oflux-admin/users.json").expanduser()
NODE_STORE = "/var/lib/openflux-node/{channel}/cookies.json"
KEY_COOKIES = ("Session_id", "sessionid2", "yandex_session", "i")


def _probe(cookies_file: Path) -> str:
    """Validate a cookies.txt against a live carrier doc; return doc URL."""
    registry = UserRegistry(REGISTRY)
    urls = sorted({u["yandex_url"] for u in registry.active().values() if u.get("yandex_url")})
    if not urls:
        raise SystemExit("registry has no active yandex docs to probe against")
    session = requests.Session()
    loaded = load_into_session(session, cookies_file, "yandex.ru")
    if not loaded:
        raise SystemExit(f"{cookies_file}: no yandex.ru cookies parsed")
    check_yandex_doc(urls[0], session=session)
    return urls[0]


def _summary(cookies_file: Path) -> list[str]:
    lines = []
    now = time.time()
    cookies = {c.name: c for c in parse_netscape(cookies_file.read_text(encoding="utf-8"))}
    for name in KEY_COOKIES:
        c = cookies.get(name)
        if c is None:
            lines.append(f"  {name}: НЕТ (сессия, вероятно, мертва)")
        elif c.expires == 0:
            lines.append(f"  {name}: до конца сессии браузера")
        else:
            mark = "" if c.expires > now else " (календарно протухла)"
            lines.append(
                f"  {name}: до {datetime.fromtimestamp(c.expires):%Y-%m-%d}{mark}"
            )
    return lines


def cmd_check() -> int:
    if not COOKIES_PATH.exists():
        print(f"FAIL: {COOKIES_PATH} не существует")
        return 1
    print(f"файл: {COOKIES_PATH}")
    for line in _summary(COOKIES_PATH):
        print(line)
    try:
        url = _probe(COOKIES_PATH)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: живая проверка не прошла: {str(exc)[:160]}")
        print("нужен свежий экспорт кук (скилл refresh-cookies)")
        return 1
    print(f"OK: сессия жива, док {url[:60]}... открывается с этими куками")
    return 0


def cmd_install(new_file: Path) -> int:
    if not new_file.exists():
        print(f"FAIL: {new_file} не существует")
        return 1
    print("валидация свежего экспорта против живого дока...")
    try:
        _probe(new_file)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: экспорт не работает ({str(exc)[:160]}) — ничего не меняем")
        return 1
    print("OK: экспорт жив")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if COOKIES_PATH.exists() and not COOKIES_PATH.samefile(new_file):
        backup = COOKIES_PATH.with_name(f"ya-cookies.txt.bak-{stamp}")
        shutil.copy2(COOKIES_PATH, backup)
        backup.chmod(0o600)
        print(f"бэкап старых: {backup.name}")
    if not COOKIES_PATH.samefile(new_file):
        shutil.copy2(new_file, COOKIES_PATH)
        COOKIES_PATH.chmod(0o600)
        print(f"установлены: {COOKIES_PATH}")
    else:
        print("источник и приёмник совпадают — копирование не нужно")

    sys.path.insert(0, str(ROOT / "scripts"))
    from make_client_cookiestore import build_jar, registry_yandex_urls

    urls = registry_yandex_urls()
    jar = build_jar(COOKIES_PATH, urls)
    CLIENT_STORE.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_STORE.write_text(__import__("json").dumps(jar, indent=2), encoding="utf-8")
    CLIENT_STORE.chmod(0o600)
    print(f"пересобран клиентский стор: {CLIENT_STORE} ({len(jar)} док(ов))")

    _push_to_bot_host()
    _seed_nodes(jar)
    print("готово. напоминание: значения кук нигде не печатаем, файлы 0600")
    return 0


BOT_HOST = "root@159.194.227.248"
BOT_COOKIES_REMOTE = "/root/.config/oflux-bot/ya-cookies.txt"


def _push_to_bot_host() -> None:
    """The telegram bot issues channels from the VPS: it needs the cookies."""
    import subprocess

    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", BOT_HOST,
             f"mkdir -p /root/.config/oflux-bot && cat > {BOT_COOKIES_REMOTE} && chmod 600 {BOT_COOKIES_REMOTE}"],
            stdin=open(COOKIES_PATH, "rb"),
            capture_output=True,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - best-effort sync
        print(f"  пуш кук на {BOT_HOST}: FAIL ({exc}) — бот обновит при следующем install")
        return
    if proc.returncode == 0:
        print(f"  скопированы боту: {BOT_HOST}:{BOT_COOKIES_REMOTE}")
    else:
        print(f"  пуш кук на {BOT_HOST}: FAIL (rc={proc.returncode})")


def _seed_nodes(jar: dict) -> None:
    """Feed the node cookie store for channels that actually run a yandex lane."""
    registry = UserRegistry(REGISTRY)
    import json

    kicked = []
    for channel, user in sorted(registry.active().items()):
        url = user.get("yandex_url")
        if not url or url not in jar:
            continue
        with SSHOps(_server_host(user)) as ops:
            rc, out, _ = ops.run(
                f"test -f /etc/openflux-node/{channel}/node.conf && "
                f"grep -q '\\[Transport yandex\\]' /etc/openflux-node/{channel}/node.conf"
            )
            if rc != 0:
                print(f"  {channel}: без яндекс-лейна — пропуск")
                continue
            ops.put_text(json.dumps({url: jar[url]}, indent=2), NODE_STORE.format(channel=channel), mode=0o600)
            ops.run(f"chown openflux-node:openflux-node {NODE_STORE.format(channel=channel)}")
            ops.run(f"systemctl restart openflux-node@{channel}")
            kicked.append(channel)
    for channel in kicked:
        print(f"  {channel}: стор засеян, юнит перезапущен — КЛИЕНТУ НУЖЕН РЕКОННЕКТ")
    if not kicked:
        print("  каналов с яндекс-лейном нет — ноды не трогали")


def _server_host(user: dict) -> str:
    from oflux_admin.config import load_config

    cfg = load_config(Path("~/.config/oflux-admin/servers.yaml").expanduser())
    return cfg.server(user["vds"]).host


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "install"):
        print(__doc__)
        return 2
    if sys.argv[1] == "check":
        return cmd_check()
    if len(sys.argv) < 3:
        print("usage: refresh_cookies.py install <cookies.txt>")
        return 2
    return cmd_install(Path(sys.argv[2]).expanduser())


if __name__ == "__main__":
    sys.exit(main())
