# Релей ServiceDesk в Yandex Cloud

Прокси между ботом на Render и `sd.servionica.ru`.

## Зачем

24.11.2026 замерено: с адресов Render соединение с ServiceDesk обрывается на
этапе **connect** — 100% отказов, таймаут 35 с, ни одного ответа за 10 минут.
С российского IP тот же полный сброс фильтра (36 заявок, 2 страницы) занимает
**3 секунды**.

Функция принимает запрос бота, пересылает его в SD и возвращает ответ. Выход в
интернет — российская инфраструктура Яндекса. Бот остаётся на Render (Telegram,
команды) и получает данные через релей.

```
Render (бот) ──HTTPS──► API Gateway ──► функция sd-relay ──HTTPS──► sd.servionica.ru
                          ▲ публичный домен     ▲ российский выход
```

## Почему нужен API Gateway, а не прямой адрес функции

Прямой адрес `https://functions.yandexcloud.net/<id>` **не принимает подпути**:
Яндекс разбирает `/v1/auth/login` как продолжение идентификатора функции и
отвечает `invalid functionID`. API Gateway маршрутизирует `/{proxy+}` в функцию
и передаёт настоящий путь в заголовке `X-Envoy-Original-Path` — функция читает
его в `request_path()` (в `event["path"]` шлюз кладёт шаблон `/{proxy+}`).

## Что внутри функции

- `sd_relay.py` — единственный файл, только стандартная библиотека.
- Убирает битый `Content-Security-Policy-Report-Only`, на котором падал разбор
  ответа (из-за него в проекте появился локальный `sd_proxy.py`).
- Проверяет секрет: без правильного `X-Relay-Secret` отвечает 403 и в SD не ходит.
- Не пропускает к SD заголовки шлюза (`X-Envoy-*`, `X-Forwarded-*`, трассировку).
- Пишет в лог метод, путь, код ответа и длительность.

## Переменные окружения функции

| Переменная | Обязательна | Значение |
|---|---|---|
| `RELAY_SECRET` | да | случайная строка; тот же секрет задаётся боту как `SD_PROXY_SECRET` |
| `SD_HOST` | нет | `sd.servionica.ru` |
| `SD_TIMEOUT` | нет | `30` (секунды) |

## Создание через CLI

Нужен сервисный аккаунт с ролью `functions.admin` на каталог и его
**авторизованный ключ** (`key.json`). OAuth-токен Яндекс ID для CLI больше не
годится: токены, выданные после 01.06.2026, не обмениваются на IAM.

```powershell
yc config set service-account-key .\key.json
yc config set cloud-id  <cloud-id>
yc config set folder-id <folder-id>

# 1. Функция
yc serverless function create --name sd-relay

# 2. Версия (в архиве только sd_relay.py)
yc serverless function version create `
  --function-name sd-relay `
  --runtime python312 `
  --entrypoint sd_relay.handler `
  --memory 128m `
  --execution-timeout 40s `
  --source-path .\yandex `
  --environment RELAY_SECRET=<секрет>,SD_HOST=sd.servionica.ru,SD_TIMEOUT=30

# 3. API Gateway (спецификация — yandex/apigw-spec.yaml, подставить свои id)
yc serverless api-gateway create sd-relay-gw --spec apigw-spec.yaml
yc serverless api-gateway get sd-relay-gw --format json   # поле domain
```

В спецификации обязательны **оба** метода — `get` и `post`: бот логинится
POST-ом. Конструкция `x-yc-apigateway-any-method` не сработала (POST отбивался
с 405), а `put`/`delete` шлюз отказывается принимать в спецификации.

## Проверка

```powershell
python scripts/relay_check.py https://<gateway-domain> <секрет>
```

Ожидаемо: `логин через релей: OK за ~2.7 с`, `загрузка списка: OK за ~3.8 с —
заявок 36 из 36`.

## Подключение бота

Переменные Render:

```
SD_PROXY_URL    = https://<gateway-domain>
SD_PROXY_SECRET = <тот же секрет>
SD_PROXY_PORT   = 0        (локальный прокси больше не нужен)
SD_ENABLED      = true
```

Ссылки на заявки по-прежнему строятся от `SD_BASE_URL`, то есть ведут в
настоящий ServiceDesk.

## Стоимость

Free tier Cloud Functions: **1 000 000 вызовов и 10 ГБ×час в месяц**
([документация](https://yandex.cloud/ru/docs/billing/concepts/serverless-free-tier)).

При интервале 60 с и двух запросах на цикл (~86 000 вызовов) выходит
≈ 128 МБ × 2,5 с × 86 000 ≈ **7,7 ГБ×час** — вписывается, но запас небольшой.
После перехода на «каждый цикл только первая страница» станет ≈ 3,8 ГБ×час.
Расход видно в консоли: **Billing** → **Детализация**.

## Откат

Убрать `SD_PROXY_URL` на Render (оставить пустым) и перезапустить сервис — бот
вернётся к прямому доступу. Функцию и шлюз можно остановить в консоли, они
ничего не стоят в покое.
