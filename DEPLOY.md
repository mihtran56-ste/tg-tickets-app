# Деплой TG_Tickets

Документ описывает развёртывание бота на бесплатном хостинге. Актуальный план
и обоснование выбора площадки — в `Второй_мозг/04_Материалы/plan_tg_tickets_hosting.md`.

## Требования, которые накладывает бот

- Постоянный процесс (long polling), входящий HTTP не обязателен.
- ~60–100 МБ ОЗУ, почти нет CPU.
- Запись файлов состояния в `DATA_DIR` (`sd_state.json`, `tickets.json`).
- Исходящий HTTPS: `api.telegram.org`, `sd.example.ru`.

## Переменные окружения

| Переменная | Обязательна | Назначение |
|---|---|---|
| `BOT_TOKEN` | да | токен от @BotFather |
| `ALLOWED_USER_IDS` | да | кому можно пользоваться ботом |
| `ADMIN_USER_IDS` | нет | админы (`/all`, назначение заявок) |
| `SD_ENABLED` | да | `true` — включить мониторинг |
| `SD_BASE_URL` | да | `https://sd.example.ru` |
| `SD_LOGIN` / `SD_PASSWORD` | да | учётка SD |
| `SD_CONDITION` | да | фильтр из ссылки SD (без `ORDER BY`) |
| `SD_VIEW` | нет | `Default` |
| `SD_CHECK_INTERVAL` | нет | секунды между проверками, по умолчанию 60 |
| `SD_NOTIFY_CHAT_IDS` | нет | куда слать; пусто — в `ALLOWED_USER_IDS` |
| `SD_SEED_ON_FIRST_RUN` | нет | `true` — первый запуск без рассылки истории |
| `SD_VERIFY_SSL` | нет | проверка сертификата SD, по умолчанию `true`; отключать только при самоподписанном сертификате |
| `DATA_DIR` | нет | папка состояния (для тома) |
| `PORT` | нет | порт health-эндпоинта, `0` — выключен |
| `OUTAGE_MIN_SECONDS` | нет | простой короче этого падением не считается (10 с) |
| `OUTAGE_COOLDOWN_SECONDS` | нет | не чаще одного отчёта о падении за интервал (600 с) |
| `HEALTH_TOKEN` | нет | токен доступа к подробному статусу `/health?token=...`; без него отдаётся только `{"status":"ok"}` |
| `LLM_API_KEY` | нет | ключ OpenAI-совместимого API; без него диалог с ИИ выключен |
| `LLM_BASE_URL` | нет | адрес API, если не OpenAI (например, OpenRouter) |
| `LLM_MODEL` | нет | модель, по умолчанию `gpt-4o-mini` |
| `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_SYSTEM_PROMPT` | нет | тонкая настройка ответов |
| `PROXY_URL` / `TELEGRAM_API_IP` | нет | только для хостинга в РФ |

## Проверка готовности перед деплоем

```bash
python preflight.py
```

Проверяет настройки, живой вход в ServiceDesk и токен бота (через ту же сеть,
что и бот). В Telegram ничего не отправляет. Код возврата 0 — можно запускать:

```
[OK      ] BOT_TOKEN: похож на токен BotFather
[OK      ] ALLOWED_USER_IDS: 1 ID
[OK      ] Настройки SD: заполнены
[OK      ] ServiceDesk: заявок получено 23 из 23
[OK      ] Telegram: бот @ServMS_bot
```

Запускать на сервере после деплоя и локально перед ним — так видно, что
переменные доехали, а фильтр не пустой.

## Деплой через API Render (без кликов в панели)

Если есть API-ключ Render (Account Settings → API Keys), сервис создаётся скриптом:

```powershell
$env:RENDER_API_KEY = "rnd_..."
.venv\Scripts\python scripts\render_deploy.py --repo-url https://github.com/ЛОГИН/tg-tickets
```

Скрипт идемпотентный: находит workspace, создаёт (или переиспользует) web service
на free-тарифе во Франкфурте, заливает переменные из `.env.server`, запускает
деплой и ждёт статуса `live`. При ошибке печатает ответ Render, по которому видно,
какое поле не приняли.
## Вариант A. Render (free web service)

1. Репозиторий на GitHub (приватный) уже содержит `render.yaml`.
2. Render → New → Blueprint → выбрать репозиторий. Render подхватит `render.yaml`.
3. В дашборде заполнить секреты (`sync: false`): `BOT_TOKEN`, `ALLOWED_USER_IDS`,
   `ADMIN_USER_IDS`, `SD_LOGIN`, `SD_PASSWORD`, `SD_CONDITION`, `SD_NOTIFY_CHAT_IDS`.
4. Дождаться деплоя, открыть Logs — должны появиться строки:
   `Успешная авторизация через /v1/auth/login`, `Эндпоинт ... рабочий`.
5. **Обязательно:** бесплатный сервис засыпает через ~15 минут без HTTP-трафика.
   Завести бесплатный пинг на `https://<имя>.onrender.com/health` каждые 10 минут
   (cron-job.org / UptimeRobot). Без пинга бот будет спать и не увидит заявки.
6. Проверка: в Telegram `/sd_status` — свежий `last_check`, пустой `last_error`,
   `/sd_test` — соединение с SD.

Ограничения бесплатного тарифа: 750 часов в месяц (24/7 ≈ 730 — запас ~2 %),
0,1 CPU, **нет постоянного диска** — состояние теряется при каждом передеплое.
Поэтому и нужен `SD_SEED_ON_FIRST_RUN=true`: после перезапуска бот молча
запомнит текущие заявки и не пришлёт историю заново.

## Вариант B. Docker (VPS, Amvera, HF Spaces)

```bash
docker build -t tg-tickets .
docker run -d --name tg-tickets --restart unless-stopped \
  --env-file .env.server -v tg-tickets-data:/data -e DATA_DIR=/data tg-tickets
```

Важно: для сервера нужен **отдельный** env-файл (`.env.server`), без `PROXY_URL`
и `TELEGRAM_API_IP` — иначе бот будет искать локальный прокси, которого на сервере
нет. В `.dockerignore` эти файлы уже исключены из образа.

На HF Spaces платформа требует порт 7860: задать `PORT=7860` — health-сервер
займёт его и будет отвечать 200.

## Вариант C. VPS без Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить значения
python bot.py          # либо systemd-юнит с Restart=always
```

## Проверка после деплоя

- [ ] `/sd_status` отвечает, `last_check` моложе 2 минут
- [ ] `last_error` пуст
- [ ] новая заявка по фильтру приходит в Telegram
- [ ] перезапуск сервиса не вызывает рассылку старых заявок
- [ ] в репозитории нет `.env`, токен не попал в логи
