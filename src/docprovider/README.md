# docprovider — поведение, точки входа, грабли

Пакет создаёт **документы-носители** для каналов OpenFlux: опубликованный
документ Яндекс.Доков + публичный файл в Облаке Mail.ru, по паре на каждый
пользовательский VPN-канал. Чистый provisioning: здесь нет туннелей,
пользователей и оркестрации — только «создать/проверить/отозвать документ».

Статус: **live-верифицирован на обоих сервисах** (2026-10-15,
`pytest -m live`). Python ≥ 3.13, зависимость — `requests`.

---

## Быстрый старт: создать пару документов для нового канала

```python
from docprovider import (
    ChannelDocs, MailruAccount, MailruClient,
    YandexAccount, YandexDocCreator,
    check_mailru_public, check_yandex_doc,
)

# 1. Яндекс: аккаунт = путь к cookies.txt (Netscape, из браузера)
yandex = YandexDocCreator(YandexAccount(id="ya1", cookies_path="ya-cookies.txt"))
ya = yandex.create("of-alice")            # -> YandexDoc(url, path, account_id)

# 2. Mail.ru: аккаунт = cookies_path (основной режим) или login+password
#    (запасной, см. «Грабли» №4)
mailru = MailruClient(MailruAccount(id="mr1", login="user@mail.ru",
                                    cookies_path="mr-cookies.txt"))
mr = mailru.create_doc("of-alice")        # -> MailruDoc(url, home, account_id)

# 3. Проверка «как это увидит транспорт» (необязательно, но дёшево)
check_yandex_doc(ya.url)
check_mailru_public(mr.url)               # анонимный запрос, как в транспорте
```

Логин у Mail.ru-клиента ленивый: сессия поднимается при первом
`create_doc`/`unpublish`. Токен и upload-shard кешируются в объекте,
при 403 клиент сам перелогинивается один раз и повторяет вызов.

## Компоненты

### `yandex.YandexDocCreator(account, session=None)`

| Вызов | Результат |
|---|---|
| `create(name)` → `YandexDoc` | создаёт `/disk/openflux/<name>.docx`, публикует с правом редактирования по ссылке, возвращает `office_online_sharing_url` |
| `unpublish(path)` | best-effort закрытие публичного доступа (`mpfs/unpublish-all`) |

Внутренний флоу (порт `OpenFluxClientShared/model/YandexDisk.kt`):
cookies → GET `disk.yandex.ru/client/disk` → CSRF `sk`/`skExternal` из
`#preloaded-data` → `mpfs/mkdir` → `GET /editnew/docx/disk/openflux`
→ опрос `mpfs/bulk-resource-info` (до 30×0.5с) → `mpfs/set-public` +
`mpfs/office-set-access-state {accessState: 'all'}` → чтение
`office_online_sharing_url`.

Ошибки: `CookiesExpiredError` (редирект на паспорт / нет `sk`),
`CaptchaRequiredError` (SmartCaptcha). `name` — `[A-Za-z0-9_-]{1,64}`.

### `mailru.MailruClient(account, session=None)`

| Вызов | Результат |
|---|---|
| `create_doc(name, file_ext="txt")` → `MailruDoc` | кладёт `/openflux/<name>.txt`, публикует, возвращает `https://cloud.mail.ru/public/<A>/<B>` |
| `unpublish(weblink_or_url)` | отзыв публичной ссылки (файл остаётся в облаке) |

**Живой контракт API (проверено 2026-10-15, отличается от старых рецептов
типа PyMailCloud):**

1. csrf-токен: `POST cloud.mail.ru/api/v2/tokens/csrf` по кукам сессии;
2. токен передаётся **в query-string** каждого POST
   (`?api=2&token=...&x-email=<login>`), а НЕ в form-body — иначе 403
   `{"body":"token"}`;
3. загрузка: `PUT` **сырого тела** файла на шард из dispatcher
   (`uploader.cloud.mail.ru/upload-web/`), params
   `x-email=<login>&cloud_domain=2`, ответ **201** с голым SHA1-хешем;
4. `file/add` принимает `home + hash + size + conflict=rename`;
   `file/publish` отвечает body = weblink;
5. публичная ссылка read-only достаточна — транспорт ходит через
   анонимный `api/v4/r7/edit` с `x-email:"anonym"`.

Ошибки: `CookiesExpiredError` (в файле нет кук `.mail.ru`),
`LoginError` (парольный вход отклонён), `ApiError` (403/507/…;
507 = лимит публичных ссылок аккаунта).

### `pool.AccountPool(config_path, state_path=None, limits=None)`

Ротация аккаунтов «наименее использованный сегодня» с дневными лимитами.
Конфиг (JSON, права 0600, в git не коммитить):

```json
{
  "mailru": [{"id": "mr1", "login": "u@mail.ru", "cookies_path": "mr1-cookies.txt"}],
  "yandex": [{"id": "ya1", "cookies_path": "ya1-cookies.txt"}]
}
```

`pick(kind)` поднимает `AllAccountsExhaustedError`, когда лимиты исчерпаны.
Счётчики живут в `<config>.state.json` и обнуляются по дате.

### `validate`

- `validate_mailru_url` / `normalize_mailru_weblink` — форма ссылки
  (регексы совпадают с `provision/transports.go` ядра);
- `check_mailru_public(url)` — анонимный `api/v4/r7/edit`: если отдаёт
  `editorConfig`, документ виден транспорту;
- `check_yandex_doc(url, session=None)` — страница дока отвечает 200 и
  содержит `id="client-config"` (маркер, который скрейпит сам транспорт).
  **`showcaptchafast` (PoW) — это успех**: транспорт решает его сам.
  Провал — SmartCaptcha (`showcaptcha`) и паспорт-редирект.

### `links.make_link / parse_link`

Ссылка `openflux://v1/...` строится **настоящим бинарником** (subprocess
`openflux --make-link -`), формат кодека не дублируем. Бинарник ищется в
`OPENFLUX_BIN`, `PATH`, `bin/openflux`. Приоритеты транспортов в ссылке:
`direct:200, yandex:100, mailru:50`. `mode` (l3/l4) в ссылку **не входит**
— бэкенд выбирает exit-нода.

## Куки: как обновлять

```bash
# Chrome на macOS; спросит разрешение Keychain, сам проверит живость
.venv/bin/python scripts/export_yandex_cookies.py --service yandex --verify
.venv/bin/python scripts/export_yandex_cookies.py --service mailru --verify
```

Альтернатива — расширение «Get cookies.txt LOCALLY» на странице сервиса.
Файлы: `ya-cookies.txt`, `mr-cookies.txt`, права 0600, в `.gitignore`.
`Mpop` длиной 83 символа — **норма** (не «битая кука»).

Признаки протухания: Яндекс — `CookiesExpiredError`; Mail.ru — csrf
отвечает 403. Протухшие куки ломают только создание **новых** каналов,
существующие каналы продолжают работать.

## Грабли (проверено болью)

1. **Не вызывать `auth.mail.ru/sdc` в куковой сессии** — ротация
   сервисных кук убивает сессию (проверено: csrf начинает отвечать 403).
2. **`showcaptchafast` содержит подстроку `showcaptcha`** — порядок
   проверок в валидаторе важен: сначала PoW-выход, потом SmartCaptcha.
3. Первое открытие свежесозданного яндекс-дока может попасть на PoW
   даже под своей сессией — это не ошибка публикации.
4. Парольный вход Mail.ru из скриптов закрыт reCAPTCHA: сервер
   отличает верный пароль от неверного (разные коды), но программный
   вход не пускает. Рабочий путь — куки из браузера.
5. Токен Mail.ru в form-body даёт 403 `{"body":"token"}` — только
   query-string (грабль №2 из «контракта» выше).
6. Секреты (значения кук, пароли) никогда не печатаются: в скрипте
   экспорта, в логах, в ошибках — только имена и длины.

## Тесты

- Юнит (офлайн): `pytest` — фейки в `tests/fakes.py`, полный флоу обоих
  сервисов на скриптованной сессии + round-trip ссылки с бинарником.
- Live: `pytest -m live`, переменные `DOCPROVIDER_YANDEX_COOKIES`,
  `DOCPROVIDER_MAILRU_COOKIES` (или `..._LOGIN`/`..._PASSWORD`).
  Артефакты live-прогонов: папка `/disk/openflux/` на Яндекс.Диске и
  файлы в `/openflux` Облака — тесты сами убирают за собой (best-effort).
- После финальной приёмки live-тесты удаляются: `rm -rf tests/live`.
