---
name: deploy-core
description: Обновление ядра OpenFlux на VPS из ветки форка yegor251/OpenFlux — спрашивает у админа актуальную ветку, подтягивает апстрим в форк, ребейзит ветку, собирает бинари и выкатывает на сервер (симлинк-свитч с откатом).
---

# Обновление ядра OpenFlux: форк → VPS

## Контекст (знать до старта)

- Источник ядра — **форк `yegor251/OpenFlux`**; его `main` — зеркало
  апстрима, **из него не собираем**. Рабочий код живёт в ветках
  (пример: `feature/direct-lane-guard` = PR #212).
- Dev-клон: `~/PycharmProjects/OpenFlux-contrib`
  (origin=форк, upstream=p1neappleXpress) — создаётся скриптом сам.
- Вся механика: `scripts/update_openflux.sh sync|build|deploy <branch>`.
- Авто-апдейтер апстрима на VPS выключен (SRV-06, вариант A) — эта
  процедура единственный путь обновления ядра.

## Шаги

1. **Спроси у админа актуальную ветку** (AskUserQuestion). Варианты
   собери заранее: ветки форка из GitHub API
   (`api.github.com/repos/yegor251/OpenFlux/branches`, исключая `main`)
   плюс последняя использованная — из `bin/.core-build-info`
   (`branch=...`).
2. `scripts/update_openflux.sh sync <branch>` — тянет upstream/main,
   ребейзит ветку, force-push в форк (открытый PR обновится сам).
   - Если скрипт упал с `REBASE CONFLICT` — **остановись**: скажи
     админу, что нужно разрешить конфликт руками в
     `~/PycharmProjects/OpenFlux-contrib`
     (`git rebase upstream/main`), предложи помощь; после разрешения
     повтори шаг.
3. `scripts/update_openflux.sh build <branch>` — чекаут ветки, бинари
   `bin/openflux` + `bin/openflux-linux`, пере-форк инсталлятора
   (`deploy/make_fork.py`), штамп `bin/.core-build-info`.
4. Обязательная проверка: `.venv/bin/python -m pytest tests/ -q`
   (должен быть зелёным; ядро на тесты не влияет, но это общий гейт).
5. **Подтверди выкат у админа**: deploy рестартит ВСЕ каналы — боевые
   клиенты (мак/телефон) переподключатся руками; канарки сами. Без
   явного «да» не деплоить.
6. `scripts/update_openflux.sh deploy` — аплоад бинаря, симлинк-свитч
   `openflux → openflux-<sha>`, рестарт юнитов, проверка `active`.
7. Отчитайся: ветка/коммит/дата из `CORE-VERSION` на VPS, список юнитов.
   Напомни админу переподключить клиентов и прогнать smoke-тест
   (socks5-стенд из CLAUDE.md или реальный клиент).

## Откат

Deploy печатает прошлую цель симлинка. Откат:
`ssh root@159.194.227.248 'ln -sfn <prev> /opt/openflux-node/bin/openflux && systemctl restart openflux-node@*'`,
где `<prev>` — прошлая цель (заводской путь — `openflux-node-v1.4.2`,
он же `.openflux-orig-link`).

## Замечания

- Первое ядро, собранное из новой ветки, стоит проверять замером
  goodput (быстрый стенд из CLAUDE.md) до и после выката.
- Если админ называет ветку, которой нет в форке, — не выдумывать:
  показать реальный список и переспросить.
