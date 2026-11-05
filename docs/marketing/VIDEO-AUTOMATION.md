# Видео-конвейер: генерация 16:9 + 9:16 и автопубликация (YouTube + TikTok)

Дата: 2026-11-05. Задача: из ОДНОЙ записи делать горизонтальный (банерный)
и вертикальный ролики и публиковать их автоматизированно.

## Схема конвейера

```
запись экрана (OBS, 16:9, вручную)
   │
   ▼  scripts/video/build.py (полностью автоматически)
   ├─ Whisper → субтитры SRT (локально, офлайн)
   ├─ ffmpeg → master 16:9 (интро/аутро-карточки, субтитры)
   ├─ ffmpeg → vertical 9:16 (blur-fill фон + крупные субтитры)
   └─ Pillow → обложка 1280x720 из шаблона
   │
   ▼  scripts/video/publish.py
   ├─ YouTube: Data API v3 (официально, стабильно)
   └─ TikTok: браузерная эмуляция (см. ниже — почему не официальный API)
```

## 1. Генерация видео (никаких браузеров не нужно)

Стек — всё в Python/CLI, ставится раз: `ffmpeg`, `pip install moviepy
openai-whisper pillow` (whisper тянет torch; на Mac всё считается
локально, в облако ничего не уходит).

**Вертикаль из горизонтали** (blur-fill фон + ролик по центру):

```bash
ffmpeg -i master.mp4 -filter_complex \
 "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=24[bg];\
  [0:v]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2" \
 -c:v libx264 -crf 20 vertical.mp4
```

**Субтитры** (Whisper честно понимает русский):

```bash
whisper voice.m4a --model small --language ru --output_format srt
ffmpeg -i vertical.mp4 -vf "subtitles=subs.srt:force_style='FontSize=22,Outline=2,MarginV=60'" \
 vertical_sub.mp4
```

**Интро/аутро-карточки**: статичный PNG (Pillow из шаблона: заголовок
ролика + логотип) → `ffmpeg -loop 1 -t 2 -i card.png ... concat` с
основным роликом.

**Обложка** (thumbnail 1280×720): Pillow-шаблон — крупный текст из 3-5
слов + контрастный фон. Для вертикали обложка не нужна (TikTok берёт
первый кадр).

Для более «дизайнерских» шаблонов позже: **Remotion** (видео из React,
`npx remotion render`, оба формата = просто две композиции) — но это
Node-стек; начинать с ffmpeg/MoviePy.

## 2. YouTube — официальный API (полная автоматизация, браузер не нужен)

- **YouTube Data API v3**, метод `videos.insert`. Квота по умолчанию
  10 000 юнитов/день, загрузка ≈ 1600 юнитов → **до 6 роликов в день**.
- Настройка один раз: Google Cloud project → включить YouTube Data API
  → OAuth-клиент (desktop) → свой аккаунт как test user (верификацию
  приложения проходить не нужно для себя) → сохранить refresh-token.
- Библиотека: `google-api-python-client` (официальная), ~30 строк кода
  на загрузку с title/description/tags/thumbnail.
- **Из РФ YouTube задушен** → загрузку пускать через свой VPN (socks5
  прокси из нашего же клиента) — API-клиент умеет прокси через
  `requests`-транспорт/env `HTTPS_PROXY`.

## 3. TikTok — три пути, честный расклад

**Почему не официальный API**: Content Posting API для не прошедшего
аудит приложения публикует ТОЛЬКО приватные видео (`SELF_ONLY`).
Аудит бесплатный (демо-видео интеграции), но для личного инструмента
его проходят нестабильно. Итог: API сейчас = черновики, не публикация.

**Путь A (рекомендуемый): браузерная эмуляция** — то, о чём ты спросил.
- Библиотека **`tiktok-uploader`** (Selenium + куки) — самая живая;
  альтернатива `easy-uploader` (Playwright+Selenium, мультиплатформа).
- Авторизация через экспортированные куки (`cookies.txt`), публикация
  одной командой:

```python
from tiktok_uploader.upload import upload_tiktok
upload_tiktok("vertical_sub.mp4", description="...", cookies="cookies.txt")
```

- **Грабли**: кнопка «Post» активируется только после обработки
  метаданных (либа ждёт — но если UI поменялся, селекторы ломаются);
  куки протухают (обновлять раз в 2-4 недели); TikTok меняет вёрстку
  часто →-library ломается 1-2 раза в год, чинится обновлением.
- **Критично из РФ**: TikTok сам запрещает ЗАГРУЗКУ новых видео
  РФ-аккаунтам (не РКН — сама компания, с 2022 и в 2026). Нужен
  аккаунт в другом регионе: регистрация/смена региона через VPN
  (наш собственный подходит идеально). Публикация тоже через
  иностранный IP. Попытки обхода региона = риск бана аккаунта:
  аккаунт заводить отдельный «контентный», темп умеренный (1-2 ролика
  в день максимум, без мгновенных серий).

**Путь B: официальный API + аудит** — если однажды захочется
продуктового уровня: пройти аудит Content Posting API (демо-видео
интеграции). Долго и с риском отказа; для своего канала избыточно.

**Путь C (страховка): полуавтомат** — скрипт готовит вертикальный MP4
+ описание с хештегами, публикация руками из приложения (2 тапа).
Держим как запасной на случай бана/поломки селекторов.

## 4. Реализация: `video/` в корне репы (готово, 2026-11-05)

- `video/build.py master.mov --title "..."` → outbox/<slug>/ с master 16:9,
  vertical 9:16, cover.png, meta.json (субтитры: --srt или локальный Whisper)
- `video/publish.py --all` → YouTube (Data API, --proxy для РФ) +
  TikTok (tiktok-uploader по кукам); state.json против дублей,
  опубликованное уезжает в published/
- установка и настройка секретов: `video/README.md`
- депс: `pip install -r video/requirements.txt` + ffmpeg (brew)

## Источники

- [TikTok Content Posting API — official](https://developers.tiktok.com/doc/content-posting-api-get-started)
- [SELF_ONLY до аудита (2026)](https://www.breakreach.com/developers/tiktok-api)
- [tiktok-uploader (PyPI)](https://pypi.org/project/tiktok-uploader)
- [easy-uploader (Playwright/Selenium)](https://github.com/wanghaisheng/easy-uploader)
- [Грабля «Post активен после обработки метаданных»](https://stackoverflow.com/questions/79598400/tiktok-automated-upload-bot-wont-publish-videos)
- [TikTok ограничил загрузку для РФ-аккаунтов, обходы и риски](https://t-j.ru/add-tiktok-russia),
  [обзор 2026](https://t-j.ru/tiktok-come-back)
