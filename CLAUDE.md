# openflux_vpn — памятка агенту

Личный multi-user VPN поверх библиотеки OpenFlux (транспорт-y посредники +
exit-нода на VPS). Бизнес-логики нет: только выпуск каналов и ссылки
`openflux://` для клиентов. Текущее состояние и планы — `docs/PLAN.md`.

## Рабочий цикл (стоячая инструкция пользователя)

Любой файл — писать в этой локальной репе, коммитить и переносить через git:

```
git add -A && git commit && git push -q origin main
ssh root@159.194.227.248 'cd /root/openflux_vpn && git pull -q origin main'
```

SSH работает по ключу (`BatchMode=yes`), `sshpass` на машине НЕ установлен —
не использовать. Сервер тестовый; root-пароль нигде не хранить и не спрашивать.

Отвечать пользователю по-русски; код/идентификаторы/коммиты — по-английски.

## Секреты (не печатать, не коммитить)

- `openflux://`-ссылка содержит ключ шифрования канала; `out/*.key`,
  `users.json` (в `~/.config/oflux-admin/`) — тоже. Показывать можно только
  с маскировкой (`openflux://v1/<redacted>`).
- Куки-файлы: `ya-cookies.txt`, `mr-cookies.txt`, `out/cookies-yandex.json`
  (0600). Логировать только имена кук и их количество, не значения.
- `out/` целиком в .gitignore. Пароли/куки/ключи в git не попадают —
  перед коммитом смотреть `git status`.

## Проверка перед «готово»

```
.venv/bin/python -m pytest tests/ -q     # должен быть зелёным всегда
```

Live-тесты (`tests/live/`) удалены после приёмки 2026-10-18 — при нужде
восстанавливать из git-истории, а не писать заново.

## Быстрый стенд для проверки туннеля (без root)

```
./bin/openflux --config=out/alice.conf --inbound=socks5 --socks5=127.0.0.1:18080 -d
curl -x socks5h://127.0.0.1:18080 -o /dev/null -w '%{http_code} %{speed_download} B/s\n' \
  -r 0-10485759 http://mirror.yandex.ru/ubuntu/ls-lR.gz
```

`-d` — per-packet логи. socks5h резолвит имя на клиенте, т.е. DNS-путь
l3 (UDP NAT) этот стенд НЕ проверяет — только живой tun-клиент.

Боевой клиент: `sudo ./bin/openflux --config=out/alice.conf` (Inbound=tun).

## Окружение и грабли (все подтверждены)

- macOS — файловая система БЕЗ учёта регистра: `go build -o ../openflux`
  из `OpenFlux/` затирает сам клон. Бинари класть в `bin/`
  (`scripts/update_openflux.sh` это делает).
- `pkill -f <pattern>` матчит и собственную ssh-командную строку (ssh падает
  с 255). Использовать скобочный трюк: `pkill -f "l3tes[t].conf"`.
- Compound-команды с `cd` сбивают cwd → git-операции в чужой репе. Всегда
  `git -C <path>` или абсолютные пути.
- Права на сервере: `root`, инсталлятор сам требует root (иначе fail с
  понятной ошибкой).
- GitHub API (`api.github.com`) доступен и безопасен; issues апстрима —
  основной источник контрактов.

## Интеграция с OpenFlux

- `OpenFlux/` — одноразовый референс-клон; в него не коммитить, он
  пересоздаётся `scripts/update_openflux.sh` (он же пересобирает
  `bin/openflux`, `bin/openflux-linux` и пере-форкает инсталлятор через
  `deploy/make_fork.py`).
- Кодек ссылок НЕ переизобретать: только subprocess бинаря
  (`--make-link` / `--parse-link`), см. `src/docprovider/links.py`.
- `deploy/node-install.sh` — генерируется `deploy/make_fork.py` из
  апстрима (заякоренные патчи, drift падает громко). Руками не править —
  править патчи в `make_fork.py`. Наши ключи: `yandex=`, `mode=`.
- Ядро ноды на сервере обновляется само (таймер 6ч, node-v* релизы,
  SHA-256 pin, rollback). `upgrade` НЕ трогает node.conf каналов.

## Оркестратор (oflux-admin)

Конфиг: `~/.config/oflux-admin/servers.yaml`, реестр: там же `users.json`
(содержит ключи каналов!). CLI: `.venv/bin/oflux-admin add-user|list|link|revoke`.
После apply `Provisioner` делает warm-up (см. ниже) — не убирать.

## Известные гонки апстрима (не наши баги, но помнить)

1. **Первое подключение после старта ноды** может попасть в «зомби»:
   handshake ок, дата-плейн молчит навсегда (`[CRYPTO] no answer...` в
   цикле). Второе подключение к той же ноде всегда живое. На l3 и l4
   одинаково. Митигация — warm-up в `Provisioner.apply`; рестарты от
   авто-апдейтера НЕ покрыты (лечится переподключением клиента).
2. **Рестарт ноды под живым клиентом** — клиент уходит в зомби сам и не
   восстанавливается. Правило: трогал ноду — перезапусти клиент.
3. **kernel-RST правило под ufw** обязано стоять ПЕРВОЙ строкой OUTPUT
   (`-I OUTPUT 1`): цепочки ufw ACCEPT-ят `ctstate NEW` раньше любого
   appended-правила. В форке уже так; не «упрощать» обратно на `-A`.
4. Диагностика ноды: дописать `Debug = 2` в
   `/etc/openflux-node/<канал>/node.conf` + рестарт юнита → в journal
   появляются `[L3]` и `[L3-STATS]` (счётчики fromTr/toNet/fromNet/toCli,
   drops: noct = нет conntrack-записи).

## Транспорты-посредники (сейчас оба отключены)

- `mailru` — Mail.ru закрыл анонимный доступ к публичным ссылкам
  (апстрим issue #193). Модуль `docprovider/mailru` сохранён.
- `yandex` — SmartCaptcha/login-стены; у headless exit нет пути прохождения
  (апстрим #106). Куки на клиенте помогают (`scripts/make_client_cookiestore.py`),
  на exit — нет.
- Критерии возврата и наблюдение за апстримом: `docs/PLAN.md` →
  «Мониторинг апстрима». Не заводить эти транспорты в каналы, пока там
  не сказано обратное.

## Telegram-бот (oflux_bot)

- Код: `src/oflux_bot/`, живёт на VPS (`systemctl status oflux-bot`),
  деплой — `scripts/deploy_bot.sh` (idempotent после каждого push).
- Секреты на VPS: `/root/.config/oflux-bot/` — `bot.yaml` (токен +
  alert_tg_id), `users.json` (реестр — рабочая копия/мастер каналов,
  выпущенных ботом!), `ya-cookies.txt`. Всё 0600, вне git.
- Приглашения: на VPS `.venv/bin/python -m oflux_bot.invite` →
  deep-link `t.me/<bot>?start=<token>`.
- Команды бота — только обёртки над oflux-admin (LocalOps вместо SSH:
  `src/oflux_admin/localops.py`). Ссылки в логах — маскированные
  (`mask_link`), aiogram logger прижат к WARNING.

## Куда смотреть сначала

`docs/PLAN.md` (состояние/план/гонки) → `src/docprovider/README.md`
(live-контракты API Яндекса/Mail.ru и грабли) → этот файл.

Скилл `.claude/skills/refresh-cookies/SKILL.md` — процедура обновления
куки Яндекса (запускать при плановой проверке раз в 1–2 недели или при
`CookiesExpiredError`/passport-ошибках в add-user/rotate/check).
