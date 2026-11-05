# video/ — конвейер «запись → 16:9 + 9:16 → YouTube + TikTok»

Полный разбор и обоснования: `docs/marketing/VIDEO-AUTOMATION.md`.

```
запись (OBS, 16:9)
   → build.py   → outbox/<slug>/{master_16x9.mp4, vertical_9x16.mp4, cover.png, meta.json}
   → publish.py → YouTube (официальный API) + TikTok (браузерная эмуляция)
                → published/<slug>, state.json помечает что куда ушло

длинное видео (подкаст/стрим/лекция)
   → clipper.py → речь по VAD, нарезка на ~1-мин части, оживлённый баннер,
                  метка «Часть N из M», субтитры → outbox/<slug>-pNN/
                  → дальше тот же publish.py --all
```

## Длинное видео → части (clipper.py)

```bash
.venv/bin/python video/clipper.py long.mov --title "Разбор закона" \
    --part-length 60 --tags "vpn,разбор"
.venv/bin/python video/publish.py --all
```

Что делает автоматически:
- **движок речи**: silero-VAD (`pip install silero-vad`) отличает голос от
  музыки и тишины — мёртвые участки вырезаются; без него fallback
  `--engine silence` вырезает только тишину (музыка пройдёт);
- **нарезка на ~1 мин** по краям речи (рез не по слову, а по паузе),
  `--limit N` — только первые N частей;
- **оживлённый баннер** (`banner.py`, превью: `python3 video/banner.py
  preview`): тёмная плашка с циклящим градиентом, пульсацией и
  эквалайзером + метка «ЧАСТЬ N/M»; отдельно `--brand` меняет вордмарк;
- **«Часть N из M»** вспышкой в первые 2.5 с каждого ролика;
- субтитры: Whisper по каждой части или `--srt-dir` с готовыми
  `pNN.srt`;
- обе части форматов идут в тот же outbox → `publish.py --all`.

Внимание: чужое кино/сериалы = Content ID страйки и смерть канала.
Конвейер для своих длинных записей.

## ffmpeg

Полноценные фильтры (drawtext/subtitles) требуют libass/libfreetype —
brew-бутылка ffmpeg 8+ их НЕ включает. В `video/bin/` лежит статик
ffmpeg/ffprobe с полным набором (evermeet.cx, gitignored); скрипты
предпочитают его PATH. Если чистишь — не удаляй bin/.

## Установка (один раз)

```bash
brew install ffmpeg
.venv/bin/pip install -r video/requirements.txt
# опционально, автосубтитры (тянет torch):
.venv/bin/pip install openai-whisper
```

## Секреты: video/secrets/ (0600, вне git)

### YouTube
1. console.cloud.google.com → проект → включить **YouTube Data API v3**.
2. «Учётные данные» → OAuth client ID → тип **Desktop app** → скачать
   `client_secret.json` → положить в `video/secrets/`.
3. Первый запуск `publish.py` откроет браузер для входа в свой канал;
   refresh-token сохранится в `secrets/token.json` автоматически
   (приложение в test-режиме — для своего аккаунта этого достаточно).
4. Из РФ: `--proxy socks5://127.0.0.1:1080` (наш VPN-клиент в режиме
   socks5).

### TikTok
1. Аккаунт должен быть **не РФ-региона** (TikTok запрещает загрузку
   новых видео РФ-аккаунтам) — регистрация через VPN.
2. Войти в tiktok.com в браузере → экспортировать куки в Netscape
   формате (расширение «Get cookies.txt LOCALLY») →
   `video/secrets/cookies.txt`.
3. Темп: не больше 1–2 роликов в день на аккаунт.

## Рутина

```bash
# 1. собрать оба формата из одной записи:
.venv/bin/python video/build.py ~/Movies/record.mov \
    --title "Своя VPN-ферма за вечер" \
    --desc "OpenFlux + носители: подробный туториал" \
    --tags "vpn,openflux,selfhosted"

# 2. посмотреть, что уйдёт (ничего не публикует):
.venv/bin/python video/publish.py --all --dry

# 3. опубликовать:
.venv/bin/python video/publish.py --all --proxy socks5://127.0.0.1:1080
```

`state.json` помнит, что и куда ушло (повторный запуск дублей не
создаст); когда обе платформы отработали — каталог перезжает в
`published/`. `--platforms youtube` — только одна платформа;
`--privacy unlisted` — сначала скрытая публикация для проверки.

## Субтитры

`--srt file.srt` приоритетнее; без него build вызывает `whisper`
(если установлен) с моделью small, русский. Нет ни того, ни другого —
ролики соберутся без субтитров (предупреждение печатается).
