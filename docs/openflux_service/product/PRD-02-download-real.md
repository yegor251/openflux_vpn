# PRD-02 — `/download`: team-ссылки и зеркало

| | |
|---|---|
| Область | продукт / дистрибуция |
| Приоритет | P1 |
| Статус | open (~30 минут работы) |
| Зависимости | CL-01 (Android-приёмка до смены версии в `/download`) |
| Апстрим | `OpenFluxTeam/OpenFluxAndroid` v3.10.3, `OpenFluxTeam/OpenFluxDesktop` v3.10.3, iOS TestFlight |

## Проблема

`/download` указывает на замороженные репы p1neappleXpress (v3.7.0) и
заглушку iOS. Живые сборки — в организации OpenFluxTeam, iOS —
TestFlight. Huawei/без-GitHub юзеры не могут скачать APK с GitHub.

## Ожидаемый результат

- [ ] `/download` (текст в `src/oflux_bot/handlers.py`, DOWNLOAD_TEXT):
      Android — релиз **v3.10.3** из `OpenFluxTeam/OpenFluxAndroid`
      (arm64-v8a APK); iOS — TestFlight
      `https://testflight.apple.com/join/BwnAcdus` (сборщик @saharev1);
      Desktop — `OpenFluxTeam/OpenFluxDesktop` v3.10.3 (dmg/msi/deb);
- [ ] APK зафиксирован (версия + SHA-256), зеркало на нашем VPS как
      запасной путь (Issues #34/#38: GitHub качается не у всех);
- [ ] правило: версия в `/download` меняется только после приёмки
      (CL-01, 15-минутный регресс);
- [ ] проверено на реальном устройстве: ссылка из бота → рабочий туннель.
