"""Telegram handlers: user self-service for channels + admin alerts.

One bot for everyone; a TG user only ever touches their own channel.
Links/QR leave the bot as chat messages (that is their purpose) but
never land in logs — aiogram's logger is pinned to WARNING in main.
"""

from __future__ import annotations

import io

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

import qrcode

from .config import BotConfig, BotUsers
from .ops import doctor_channel, issue_channel, reissue_link, revoke_channel, rotate_channel

HELP_TEXT = """Команды:
/issue — выпустить свой VPN-канал (ссылка + QR)
/my — показать мою ссылку и QR
/rotate — новый ключ для канала (старая ссылка умрёт)
/revoke — удалить мой канал
/status — состояние моего канала
/download — как поставить приложение

Первый вход: /start <код-приглашение> (код даёт админ).
"""

DOWNLOAD_TEXT = """Как подключиться (пока заглушки — уточняй у админа):

📱 iOS: TestFlight — https://testflight.apple.com/join/XXXXXXXX
   (открыть ссылку, установить, отсканировать QR из /my)
💻 macOS: поставить CLI и конфиг из репо, затем
   sudo ./bin/openflux --config=<конфиг-канала>
🖥 Desktop/Android: релизы апстрима — https://github.com/p1neappleXpress/OpenFlux/releases
"""


def qr_photo(link: str) -> BufferedInputFile:
    buf = io.BytesIO()
    qrcode.make(link).save(buf, format="PNG")
    buf.seek(0)
    return BufferedInputFile(buf.getvalue(), filename="channel-qr.png")


def _link_message(channel: str, link: str, note: str) -> str:
    return f"Канал {channel} — {note}\n\n{link}\n\n(ссылка = ключ: не публикуй, храни как пароль)"


def register_handlers(dp: Dispatcher, bot: Bot, cfg: BotConfig, bot_users: BotUsers) -> None:
    def registered(message: Message) -> bool:
        return bot_users.is_registered(message.from_user.id)

    @dp.message(CommandStart(deep_link=True))
    async def start_with_token(message: Message, command: CommandObject) -> None:
        token = (command.args or "").strip()
        if not token:
            await message.answer(HELP_TEXT)
            return
        if bot_users.claim_invite(token, message.from_user.id):
            await message.answer(
                "Привет! Ты зарегистрирован.\nТеперь создай свой канал: /issue"
            )
        else:
            await message.answer(
                "Код не найден или уже использован. Попроси у админа новый."
            )

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        if registered(message):
            await message.answer(HELP_TEXT)
        else:
            await message.answer(
                "Это приватный бот. Вход — по коду-приглашению: /start <код>\n" + DOWNLOAD_TEXT
            )

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await message.answer(HELP_TEXT if registered(message) else DOWNLOAD_TEXT)

    @dp.message(Command("download"))
    async def download(message: Message) -> None:
        await message.answer(DOWNLOAD_TEXT)

    @dp.message(Command("issue"))
    async def issue(message: Message) -> None:
        if not registered(message):
            await message.answer("Сначала регистрация: /start <код-приглашение>")
            return
        tg_id = message.from_user.id
        if bot_users.channel_of(tg_id):
            await message.answer("У тебя уже есть канал: /my. Один канал на пользователя.")
            return
        msg = await message.answer("Создаю: документ-носитель...")
        try:
            result = issue_channel(cfg, bot_users, tg_id)
            await msg.edit_text("Создаю: канал на сервере...")
        except Exception as exc:  # noqa: BLE001 - user-facing error text
            await msg.edit_text(f"Не получилось: {str(exc)[:200]}")
            return
        await msg.edit_text(_link_message(result["channel"], result["link"], "готов"))
        await message.answer_photo(qr_photo(result["link"]), caption="QR для приложения")

    @dp.message(Command("my"))
    async def my(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await message.answer("У тебя пока нет канала. Создай: /issue")
            return
        try:
            link = reissue_link(cfg, channel)
        except Exception as exc:  # noqa: BLE001
            await message.answer(f"Канал недоступен: {str(exc)[:150]}")
            return
        await message.answer(_link_message(channel, link, "твоя ссылка"))
        await message.answer_photo(qr_photo(link), caption="QR для приложения")

    @dp.message(Command("rotate"))
    async def rotate(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await message.answer("У тебя нет канала. Создай: /issue")
            return
        await message.answer(
            f"Сменить ключ канала {channel}? Старая ссылка и QR перестанут работать.",
            reply_markup=_confirm_kb(f"rotate:{channel}"),
        )

    @dp.message(Command("revoke"))
    async def revoke(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await message.answer("У тебя нет канала.")
            return
        await message.answer(
            f"Удалить канал {channel} безвозвратно?",
            reply_markup=_confirm_kb(f"revoke:{channel}"),
        )

    @dp.message(Command("status"))
    async def status(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await message.answer("У тебя нет канала. Создай: /issue")
            return
        lines = [f"Канал {channel}:"]
        ok = True
        for name, healthy, detail in doctor_channel(cfg, channel):
            ok = ok and healthy
            lines.append(f"  [{'ok' if healthy else 'FAIL'}] {name}: {detail}")
        await message.answer("\n".join(lines))

    # -- confirmation buttons -------------------------------------------------

    @dp.callback_query(F.data.startswith("rotate:"))
    async def rotate_confirm(query) -> None:
        channel = query.data.split(":", 1)[1]
        if bot_users.channel_of(query.from_user.id) != channel:
            await query.answer("Это не твой канал", show_alert=True)
            return
        await query.answer("Меняю ключ...")
        try:
            result = rotate_channel(cfg, channel)
        except Exception as exc:  # noqa: BLE001
            await query.message.edit_text(f"Не получилось: {str(exc)[:200]}")
            return
        await query.message.edit_text(_link_message(channel, result["link"], "новый ключ"))
        await query.message.answer_photo(qr_photo(result["link"]), caption="новый QR")

    @dp.callback_query(F.data.startswith("revoke:"))
    async def revoke_confirm(query) -> None:
        channel = query.data.split(":", 1)[1]
        tg_id = query.from_user.id
        if bot_users.channel_of(tg_id) != channel:
            await query.answer("Это не твой канал", show_alert=True)
            return
        await query.answer("Удаляю...")
        try:
            errors = revoke_channel(cfg, bot_users, channel, tg_id)
        except Exception as exc:  # noqa: BLE001
            await query.message.edit_text(f"Не получилось: {str(exc)[:200]}")
            return
        text = f"Канал {channel} удалён."
        for err in errors:
            text += f"\nпредупреждение уборки: {err}"
        await query.message.edit_text(text)


def _confirm_kb(data: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Подтверждаю", callback_data=data)]]
    )
