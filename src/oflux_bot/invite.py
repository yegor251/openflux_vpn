"""Issue one-time invite deep-links. Admin runs this on the bot VPS:

    .venv/bin/python -m oflux_bot.invite [count]
"""

from __future__ import annotations

import sys
from pathlib import Path

from .config import BotUsers, load_bot_config


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    cfg = load_bot_config()
    users = BotUsers(cfg.state_path)
    me = "?"
    try:
        import requests

        resp = requests.get(f"https://api.telegram.org/bot{cfg.token}/getMe", timeout=10)
        me = resp.json()["result"]["username"]
    except Exception:  # noqa: BLE001 - offline still prints tokens
        pass
    for _ in range(count):
        token = users.new_invite()
        print(f"t.me/{me}?start={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
