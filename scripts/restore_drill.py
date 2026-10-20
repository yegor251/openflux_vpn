"""SRV-04 restore drill: prove a restored registry reissues real links.

Runs ON the VPS with OFLUX_BOT_CONFIG_DIR pointing at the restored
copy (see backup_registry.sh drill). Strictly read-only: never touches
the live bot directory, nodes or documents.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from docprovider.links import parse_link

from oflux_admin.config import UserRegistry

from oflux_bot.config import load_bot_config
from oflux_bot.ops import reissue_link


def main() -> int:
    cfg = load_bot_config()
    channels = UserRegistry(cfg.registry_path).active()
    if not channels:
        print("drill FAIL: восстановленный реестр пуст")
        return 1
    first_link = None
    for name in channels:
        link = reissue_link(cfg, name)
        if not link.startswith("openflux://"):
            print(f"drill FAIL: {name} — ссылка не читается")
            return 1
        first_link = first_link or link
    parse_link(first_link, binary=cfg.admin.settings.openflux_bin)
    print(f"drill ok: {len(channels)} канал(ов), ключи и ссылки читаются")
    return 0


if __name__ == "__main__":
    sys.exit(main())
