"""Telegram handlers: user self-service for channels + admin alerts.

One bot for everyone; a TG user only ever touches their own channel.
Open self-issue by design (admin's call 2026-10-25): anyone who finds
the bot writes /issue and gets a channel; the guard is one channel per
TG-ID and the server's max_channels. Links/QR leave the bot as chat
messages (that is their purpose) but never land in logs — aiogram's
logger is pinned to WARNING in main.
"""

from __future__ import annotations

import asyncio
import io

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import BufferedInputFile, Message, ReplyKeyboardRemove

import qrcode

from .config import BotConfig, BotUsers
from .ops import doctor_channel, issue_channel, reissue_link, revoke_channel, rotate_channel

BOT_COMMANDS = [
    ("issue", "Выпустить мой VPN-канал (ссылка + QR)"),
    ("my", "Показать мою ссылку и QR заново"),
    ("status", "Состояние моего канала"),
    ("rotate", "Новый ключ канала (старая ссылка умрёт)"),
    ("revoke", "Удалить мой канал"),
    ("download", "Как поставить приложение"),
    ("help", "Список команд"),
]

HELP_TEXT = """Команды:
/issue — выпустить свой VPN-канал (ссылка + QR)
/my — показать мою ссылку и QR
/rotate — новый ключ для канала (старая ссылка умрёт)
/revoke — удалить мой канал
/status — состояние моего канала
/download — как поставить приложение

Канал выдаётся каждому: просто напиши /issue.
"""

DOWNLOAD_TEXT = """Как подключиться:

🤖 Android: github.com/p1neappleXpress/OpenFluxAndroid/releases
   (свежий релиз → файл ...arm64-v8a-release.apk; сейчас это версия
   3.7.0. Разрешить установку из источника, затем /my и отсканировать QR)
📱 iOS: TestFlight — ссылка выдаётся админом
   (открыть, установить, затем /my и отсканировать QR)
💻 macOS: CLI из репо проекта, затем
   sudo ./bin/openflux --config=конфиг-канала
"""


def set_command_menu(bot: Bot) -> None:
    """Telegram command menu (the hints above the keyboard); best-effort."""
    import logging

    from aiogram.types import BotCommand

    async def run() -> None:
        try:
            await bot.set_my_commands(
                [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]
            )
        except Exception as exc:  # noqa: BLE001 - menu is cosmetic, never fatal
            logging.getLogger("oflux_bot").warning("set_my_commands failed: %s", exc)

    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:  # no loop (tests): skip silently
        pass


def qr_photo(link: str) -> BufferedInputFile:
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename="channel-qr.png")


def _link_message(channel: str, link: str, note: str) -> str:
    return f"Канал {channel} — {note}\n\n{link}\n\n(ссылка = ключ: не публикуй, храни как пароль)"


def register_handlers(dp: Dispatcher, bot: Bot, cfg: BotConfig, bot_users: BotUsers) -> None:
    # issuance is long; a double-tap must not create two channels
    issue_lock = asyncio.Lock()
    # channel mutations: a second tap on "Подтверждаю" must never re-enter
    # (each re-entry would rotate the key again and send another QR)
    ops_lock = asyncio.Lock()

    def clean(message: Message, text: str, **kwargs):
        # no interaction may leave buttons on the bottom panel
        kwargs.setdefault("reply_markup", ReplyKeyboardRemove())
        return message.answer(text, **kwargs)

    async def _do_issue(message: Message) -> None:
        tg_id = message.from_user.id
        async with issue_lock:
            if bot_users.channel_of(tg_id):  # re-check under the lock
                await clean(message, "У тебя уже есть канал: /my. Один канал на пользователя.")
                return
            msg = await clean(message, "Создаю: канал на сервере...")
            try:
                # doc creation + provisioning block for tens of seconds
                result = await asyncio.to_thread(issue_channel, cfg, bot_users, tg_id)
            except Exception as exc:  # noqa: BLE001 - user-facing error text
                await msg.edit_text(f"Не получилось: {str(exc)[:200]}")
                return
        await msg.edit_text(_link_message(result["channel"], result["link"], "готов"))
        await message.answer_photo(
            qr_photo(result["link"]), caption="QR для приложения",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        await clean(message, "Привет! " + HELP_TEXT)

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await clean(message, HELP_TEXT)

    @dp.message(Command("download"))
    async def download(message: Message) -> None:
        await clean(message, DOWNLOAD_TEXT)

    @dp.message(Command("issue"))
    async def issue(message: Message) -> None:
        await _do_issue(message)

    @dp.message(Command("my"))
    async def my(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя пока нет канала. Создай: /issue")
            return
        try:
            link = reissue_link(cfg, channel)
        except Exception as exc:  # noqa: BLE001
            await clean(message, f"Канал недоступен: {str(exc)[:150]}")
            return
        await clean(message, _link_message(channel, link, "твоя ссылка"))
        await message.answer_photo(
            qr_photo(link), caption="QR для приложения", reply_markup=ReplyKeyboardRemove()
        )

    @dp.message(Command("rotate"))
    async def rotate(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя нет канала. Создай: /issue")
            return
        # the inline confirm keyboard stays: it is the interaction itself
        await message.answer(
            f"Сменить ключ канала {channel}? Старая ссылка и QR перестанут работать.",
            reply_markup=_confirm_kb(f"rotate:{channel}"),
        )

    @dp.message(Command("revoke"))
    async def revoke(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя нет канала.")
            return
        await message.answer(
            f"Удалить канал {channel} безвозвратно?",
            reply_markup=_confirm_kb(f"revoke:{channel}"),
        )

    @dp.message(Command("status"))
    async def status(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя нет канала. Создай: /issue")
            return
        lines = [f"Канал {channel}:"]
        ok = True
        for name, healthy, detail in doctor_channel(cfg, channel):
            ok = ok and healthy
            lines.append(f"  [{'ok' if healthy else 'FAIL'}] {name}: {detail}")
        await clean(message, "\n".join(lines))

    # -- confirmation buttons -------------------------------------------------

    @dp.callback_query(F.data.startswith("rotate:"))
    async def rotate_confirm(query) -> None:
        channel = query.data.split(":", 1)[1]
        if bot_users.channel_of(query.from_user.id) != channel:
            await query.answer("Это не твой канал", show_alert=True)
            return
        if ops_lock.locked():  # no await before acquire: check+take is atomic
            await query.answer("Уже выполняется, подожди пару секунд", show_alert=True)
            return
        async with ops_lock:
            await query.answer("Меняю ключ...")
            await _drop_confirm_buttons(query)
            try:
                # ops are blocking subprocess/systemctl calls: keep the loop free
                result = await asyncio.to_thread(rotate_channel, cfg, channel)
            except Exception as exc:  # noqa: BLE001
                await query.message.edit_text(f"Не получилось: {str(exc)[:200]}")
                return
            await query.message.edit_text(_link_message(channel, result["link"], "новый ключ"))
            await query.message.answer_photo(
                qr_photo(result["link"]), caption="новый QR",
                reply_markup=ReplyKeyboardRemove(),
            )

    @dp.callback_query(F.data.startswith("revoke:"))
    async def revoke_confirm(query) -> None:
        channel = query.data.split(":", 1)[1]
        tg_id = query.from_user.id
        if bot_users.channel_of(tg_id) != channel:
            await query.answer("Это не твой канал", show_alert=True)
            return
        if ops_lock.locked():
            await query.answer("Уже выполняется, подожди пару секунд", show_alert=True)
            return
        async with ops_lock:
            await query.answer("Удаляю...")
            await _drop_confirm_buttons(query)
            try:
                errors = await asyncio.to_thread(revoke_channel, cfg, bot_users, channel, tg_id)
            except Exception as exc:  # noqa: BLE001
                await query.message.edit_text(f"Не получилось: {str(exc)[:200]}")
                return
            text = f"Канал {channel} удалён."
            for err in errors:
                text += f"\nпредупреждение уборки: {err}"
            await query.message.edit_text(text)

    async def _drop_confirm_buttons(query) -> None:
        """Remove inline buttons at once: repeated taps must not re-enter."""
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001 - message may already be edited
            pass


def _confirm_kb(data: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Подтверждаю", callback_data=data)]]
    )
