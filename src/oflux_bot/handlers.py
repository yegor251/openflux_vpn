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
    ("start", "Меню с кнопками"),
    ("issue", "Выпустить мой VPN-канал (ссылка + QR)"),
    ("my", "Показать мою ссылку и QR заново"),
    ("status", "Состояние моего канала"),
    ("rotate", "Новый ключ канала (старая ссылка умрёт)"),
    ("revoke", "Удалить мой канал"),
    ("download", "Как поставить приложение"),
    ("help", "Список команд"),
]

HELP_TEXT = """Команды:
/start — меню кнопками
/issue — выпустить свой VPN-канал (ссылка + QR)
/my — показать мою ссылку и QR
/rotate — новый ключ для канала (старая ссылка умрёт)
/revoke — удалить мой канал
/status — состояние моего канала
/download — как поставить приложение (кнопки по платформам)

Канал выдаётся каждому: просто напиши /issue.
"""

DL_INTRO = """Как поставить приложение — выбери платформу.
После установки нужен твой канал: в боте /issue или /my.
"""

ANDROID_TEXT = """🤖 Android

1. Открой официальные релизы (браузером на телефоне):
   https://github.com/OpenFluxTeam/OpenFluxAndroid/releases/latest
2. Скачай файл, оканчивающийся на arm64-v8a-release.apk — он для всех
   современных телефонов (universal — если не уверен в процессоре).
3. Открой скачанный файл → «Разрешить установку из этого источника»
   → Установить (предупреждение о не-Play-источнике — норма).
4. В этом чате напиши /my → бот пришлёт QR-код.
5. В приложении: «+» / «Добавить» → «Сканировать QR» → наведи на QR.
6. Нажми «Подключить». Статус «Подключено» = VPN работает.

Проверка: открой 2ip.ru — должен показывать IP сервера, не твой.
"""

IOS_TEXT = """📱 iPhone / iPad

1. Установи TestFlight из App Store (официальное приложение Apple).
2. Открой ссылку приглашения в билд:
   https://testflight.apple.com/join/BwnAcdus
   (TestFlight откроется → «Accept» → «Install»).
3. В этом чате напиши /my → бот пришлёт QR-код.
4. В приложении: «Добавить» → «Сканировать QR» → наведи на QR.
5. Нажми «Подключить». Статус «Подключено» = VPN работает.

Проверка: открой 2ip.ru — должен показывать IP сервера, не твой.
"""

MAC_TEXT = """💻 macOS (проверено на Apple Silicon)

1. Открой официальные релизы:
   https://github.com/OpenFluxTeam/OpenFluxDesktop/releases/latest
2. Скачай файл ...macos-arm64.dmg (Mac на M1/M2/M3/M4;
   для старых Intel-маков — ...macos-x64.dmg).
3. Открой dmg → перетащи OpenFlux в папку Applications.
4. В этом чате напиши /my и скопируй ТЕКСТ ссылки openflux://v1/...
5. В приложении: «Добавить» канал → «Вставить ссылку» → вставь.
6. Нажми «Подключить».

Проверка: открой 2ip.ru — должен показывать IP сервера, не твой.
"""

WIN_TEXT = """🪟 Windows

1. Открой официальные релизы:
   https://github.com/OpenFluxTeam/OpenFluxDesktop/releases/latest
2. Скачай ...windows-x64-setup.exe (или ...windows-x64.msi).
3. Установи и запусти OpenFlux.
4. В этом чате напиши /my и скопируй ТЕКСТ ссылки openflux://v1/...
5. В приложении: «Добавить» канал → «Вставить ссылку» → вставь.
6. Нажми «Подключить».

Проверка: открой 2ip.ru — должен показывать IP сервера, не твой.
"""

LINUX_TEXT = """🐧 Linux (x64)

1. Открой официальные релизы:
   https://github.com/OpenFluxTeam/OpenFluxDesktop/releases/latest
2. Debian/Ubuntu: скачай ...linux-x64.deb → sudo dpkg -i <файл>.
   Другие дистрибутивы: ...linux-x64.tar.gz → распакуй и запусти.
3. В этом чате напиши /my и скопируй ТЕКСТ ссылки openflux://v1/...
4. В приложении: «Добавить» канал → «Вставить ссылку» → вставь.
5. Нажми «Подключить».

Проверка: открой 2ip.ru — должен показывать IP сервера, не твой.
"""

# (callback id, button label, detailed text)
DL_PLATFORMS = [
    ("android", "🤖 Android", ANDROID_TEXT),
    ("ios", "📱 iPhone / iPad", IOS_TEXT),
    ("mac", "💻 macOS", MAC_TEXT),
    ("win", "🪟 Windows", WIN_TEXT),
    ("linux", "🐧 Linux", LINUX_TEXT),
]


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


START_TEXT = "Привет! Выбирай действие — или набирай команду текстом:"


def _menu_kb(has_channel: bool | None = None):
    """Action buttons for /start and error replies.

    has_channel=True/False shows only the actions that can work right now;
    None is the full non-contextual list (both variants kept: UI states
    may drift from the registry, the full menu is the safe fallback).
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def b(label, cmd):
        return InlineKeyboardButton(text=label, callback_data=f"cmd:{cmd}")

    if has_channel is None:
        rows = [
            [b("🚀 Выпустить канал", "issue"), b("🔗 Моя ссылка", "my")],
            [b("📊 Статус", "status"), b("🔄 Новый ключ", "rotate")],
            [b("🗑 Удалить канал", "revoke"), b("⬇️ Как поставить", "download")],
            [b("❓ Помощь", "help")],
        ]
    elif has_channel:
        rows = [
            [b("🔗 Моя ссылка", "my"), b("📊 Статус", "status")],
            [b("🔄 Новый ключ", "rotate"), b("🗑 Удалить канал", "revoke")],
            [b("⬇️ Как поставить", "download"), b("❓ Помощь", "help")],
        ]
    else:
        rows = [
            [b("🚀 Выпустить канал", "issue"), b("⬇️ Как поставить", "download")],
            [b("❓ Помощь", "help")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


class _MsgAsUser:
    """The bot's menu message re-addressed to the user who pressed it:
    handlers read from_user.id and answer via .answer into the same chat."""

    def __init__(self, message: Message, user_id: int) -> None:
        self.from_user = type("U", (), {"id": user_id})()
        self._message = message

    def __getattr__(self, name):
        return getattr(self._message, name)


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
                await clean(
                    message,
                    "У тебя уже есть канал. Один канал на пользователя.",
                    reply_markup=_menu_kb(True),
                )
                return
            # plain message on purpose: a message sent with ReplyKeyboardRemove
            # can never be edited by the bot (Telegram rejects editMessageText)
            msg = await message.answer("Создаю: канал на сервере...")
            try:
                # doc creation + provisioning block for tens of seconds
                result = await asyncio.to_thread(issue_channel, cfg, bot_users, tg_id)
            except Exception as exc:  # noqa: BLE001 - user-facing error text
                await msg.edit_text(f"Не получилось: {str(exc)[:200]}")
                await clean(message, "Что дальше?", reply_markup=_menu_kb(False))
                return
        await msg.edit_text(_link_message(result["channel"], result["link"], "готов"))
        await message.answer_photo(
            qr_photo(result["link"]), caption="QR для приложения",
            reply_markup=ReplyKeyboardRemove(),
        )

    @dp.message(CommandStart())
    async def start(message: Message) -> None:
        await clean(
            message, START_TEXT,
            reply_markup=_menu_kb(bot_users.channel_of(message.from_user.id)),
        )

    @dp.message(Command("help"))
    async def help_cmd(message: Message) -> None:
        await clean(message, HELP_TEXT)

    @dp.message(Command("download"))
    async def download(message: Message) -> None:
        await message.answer(DL_INTRO, reply_markup=_dl_menu_kb())

    @dp.callback_query(F.data == "dl:menu")
    async def dl_menu(query) -> None:
        await query.answer()
        await query.message.edit_text(DL_INTRO, reply_markup=_dl_menu_kb())

    @dp.callback_query(F.data.startswith("dl:"))
    async def dl_platform(query) -> None:
        pid = query.data.split(":", 1)[1]
        entry = next((p for p in DL_PLATFORMS if p[0] == pid), None)
        await query.answer()
        if entry is None:
            return
        await query.message.edit_text(entry[2], reply_markup=_dl_back_kb())

    @dp.message(Command("issue"))
    async def issue(message: Message) -> None:
        await _do_issue(message)

    @dp.message(Command("my"))
    async def my(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя пока нет канала. Создай: /issue", reply_markup=_menu_kb(False))
            return
        try:
            link = reissue_link(cfg, channel)
        except Exception as exc:  # noqa: BLE001
            await clean(message, f"Канал недоступен: {str(exc)[:150]}", reply_markup=_menu_kb(True))
            return
        await clean(message, _link_message(channel, link, "твоя ссылка"))
        await message.answer_photo(
            qr_photo(link), caption="QR для приложения", reply_markup=ReplyKeyboardRemove()
        )

    @dp.message(Command("rotate"))
    async def rotate(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя нет канала. Создай: /issue", reply_markup=_menu_kb(False))
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
            await clean(message, "У тебя нет канала.", reply_markup=_menu_kb(False))
            return
        await message.answer(
            f"Удалить канал {channel} безвозвратно?",
            reply_markup=_confirm_kb(f"revoke:{channel}"),
        )

    @dp.message(Command("status"))
    async def status(message: Message) -> None:
        channel = bot_users.channel_of(message.from_user.id)
        if not channel:
            await clean(message, "У тебя нет канала. Создай: /issue", reply_markup=_menu_kb(False))
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

    # -- /start menu: buttons run the same code as the typed commands --------

    @dp.callback_query(F.data.startswith("cmd:"))
    async def cmd_via_button(query) -> None:
        name = query.data.split(":", 1)[1]
        routes = {
            "issue": lambda m: _do_issue(m),
            "my": lambda m: my(m),
            "status": lambda m: status(m),
            "rotate": lambda m: rotate(m),
            "revoke": lambda m: revoke(m),
            "download": lambda m: download(m),
            "help": lambda m: help_cmd(m),
        }
        route = routes.get(name)
        await query.answer()
        if route is None:
            return
        await route(_MsgAsUser(query.message, query.from_user.id))


def _confirm_kb(data: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Подтверждаю", callback_data=data)]]
    )


def _dl_menu_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    labels = {pid: label for pid, label, _ in DL_PLATFORMS}
    rows = [
        [InlineKeyboardButton(text=labels["android"], callback_data="dl:android"),
         InlineKeyboardButton(text=labels["ios"], callback_data="dl:ios")],
        [InlineKeyboardButton(text=labels["mac"], callback_data="dl:mac")],
        [InlineKeyboardButton(text=labels["win"], callback_data="dl:win"),
         InlineKeyboardButton(text=labels["linux"], callback_data="dl:linux")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _dl_back_kb():
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← К платформам", callback_data="dl:menu")]]
    )
