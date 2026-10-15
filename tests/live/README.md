# Live-тесты (реальные аккаунты Яндекс и Mail.ru)

Ходят в настоящие сервисы под вашими учётками. Запуск — только вручную:

```bash
export DOCPROVIDER_MAILRU_LOGIN="user@mail.ru"
export DOCPROVIDER_MAILRU_PASSWORD="..."
export DOCPROVIDER_YANDEX_COOKIES="/path/to/cookies.txt"
pytest -m live
```

Без переменных окружения тесты пропускаются. Дефолтный `pytest` их
никогда не запускает (в pyproject стоит `addopts = -m 'not live'`).

Mail.ru-тест после проверки убирает за собой (unpublish). Яндекс-тест
создаёт документ в папке `/disk/openflux` и пытается закрыть доступ
(best-effort); проверьте Диск, если тест падал посередине.

## Удаление после финальной приёмки

    rm -rf tests/live

И всё: юнит-тесты и код пакета от live-части не зависят. Маркер `live`
в pyproject можно оставить или убрать — без этой папки он ни на что не
влияет.
