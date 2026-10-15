# openflux_vpn

Готовое решение «личный multi-user VPN» на базе
[OpenFlux](https://github.com/p1neappleXpress/OpenFlux): у каждого
пользователя свой канал (ключ + документы-посредники на Яндекс.Доках и
Mail.ru), exit-нода на своём VDS. План и обоснования —
[docs/PLAN.md](docs/PLAN.md).

## docprovider — модуль создания каналов (этап M1)

Создаёт документы, через которые транспорты OpenFlux гоняют туннель:

```python
from docprovider import (
    AccountPool, MailruClient, YandexDocCreator,
    MailruAccount, YandexAccount, check_mailru_public, check_yandex_doc,
    make_link, parse_link,
)

# Mail.ru: логин+пароль, куки не нужны
mailru = MailruClient(MailruAccount(id="mr1", login="...", password="..."))
mr_doc = mailru.create_doc("of-alice")          # -> url, home, account_id

# Яндекс: cookies.txt из браузера (Netscape, разовый экспорт)
yandex = YandexDocCreator(YandexAccount(id="ya1", cookies_path="ya1-cookies.txt"))
ya_doc = yandex.create("of-alice")              # -> url, path, account_id

# живая проверка «как её увидит транспорт»
check_mailru_public(mr_doc.url)
check_yandex_doc(ya_doc.url)

# openflux://-ссылка для клиента (через настоящий бинарник)
link = make_link(
    name="of-alice",
    secret=channel_key,       # 32 байта hex
    docs=ChannelDocs(name="of-alice", yandex=ya_doc, mailru=mr_doc),
    direct_dial="203.0.113.10:8445",  # опционально: прямой порт ноды
)
```

Пул аккаунтов с дневными лимитами — `AccountPool("accounts.json")`,
формат в докстринге `docprovider/pool.py`. Файлы с паролями и куками
держать с правами 0600, в git не коммитить (уже в .gitignore).

## Разработка

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cd OpenFlux && go build -o ../bin/openflux . && cd ..   # для теста ссылок
.venv/bin/pytest          # юнит-тесты, сеть не трогают
.venv/bin/pytest -m live  # живые тесты, см. tests/live/README.md
```

Обновление яндекс-кук (Chrome на macOS, спросит разрешение Keychain):

```bash
.venv/bin/python scripts/export_yandex_cookies.py --verify
```

После финальной приёмки live-тесты удаляются одной командой:
`rm -rf tests/live`.

## Статус

| Этап | Статус |
|---|---|
| Исследование библиотеки, план | готово (`docs/PLAN.md`) |
| M1 `docprovider`: код + офлайн-тесты | готово |
| M1 live-верификация (реальные аккаунты) | не запускалась |
| M2 `oflux-admin add-user` end-to-end | не начат |
