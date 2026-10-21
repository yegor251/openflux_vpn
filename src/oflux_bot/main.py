"""Entry point: python -m oflux_bot

Long-polling bot + watchdog loop. aiogram's own logger stays on WARNING
so message texts (which may contain channel links) never hit the logs.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher

from oflux_admin.config import UserRegistry

from .alerts import CHECK_INTERVAL_S, Watchdog
from .config import BotConfig, BotUsers, load_bot_config
from .handlers import register_handlers


def _log() -> logging.Logger:
    return logging.getLogger("oflux_bot")


async def _watchdog_loop(cfg: BotConfig, bot: Bot) -> None:
    from oflux_admin.config import UserRegistry

    async def send(text: str) -> bool:
        try:
            await bot.send_message(cfg.alert_tg_id, text)
            return True
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the loop
            _log().warning("alert delivery failed: %s", exc)
            return False

    watchdog = Watchdog(cfg, lambda: UserRegistry(cfg.registry_path), send)
    while True:
        try:
            await watchdog.run_once()
        except Exception as exc:  # noqa: BLE001
            _log().warning("watchdog cycle failed: %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_S)


async def amain(cfg: BotConfig) -> None:
    # plain text mode: nothing in our messages uses HTML, and any stray
    # angle bracket would make Telegram reject the whole message
    bot = Bot(cfg.token)
    dp = Dispatcher()
    bot_users = BotUsers(cfg.state_path)
    register_handlers(dp, bot, cfg, bot_users)

    asyncio.create_task(_watchdog_loop(cfg, bot))
    _log().info("oflux-bot started (alert_tg_id=%s)", cfg.alert_tg_id)
    # Telegram outages must degrade to "retry", never kill the process
    while True:
        try:
            await dp.start_polling(bot)
            break  # clean shutdown (signal)
        except Exception as exc:  # noqa: BLE001
            _log().warning("polling failed (%s), retrying in 15 s", exc)
            await asyncio.sleep(15)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.ERROR)
    cfg = load_bot_config()
    try:
        asyncio.run(amain(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
