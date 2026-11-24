"""Реверс-прокси ServiceDesk в Yandex Cloud Functions.

Зачем: с адресов Render соединение с ``sd.servionica.ru`` обрывается на этапе
**connect** (24.11.2026: 100% отказов, таймаут 35 с), а из российского облака
полный сброс фильтра занимает ~3 секунды. Функция принимает запрос бота,
пересылает его в SD и возвращает ответ.

Что делает, кроме пересылки:
- убирает битый ``Content-Security-Policy-Report-Only`` (на нём падает строгий
  парсер aiohttp, из-за этого в проекте и появился локальный sd_proxy.py);
- проверяет секрет в заголовке ``X-Relay-Secret``: адрес функции публичный,
  без секрета его мог бы использовать кто угодно;
- пишет в лог метод, путь, код ответа и длительность — по ним видно, где
  вязнет (соединение или ответ ServiceDesk).

Только стандартная библиотека: ни зависимостей, ни requirements.txt.
Точка входа: ``handler``. Переменные окружения: ``RELAY_SECRET`` (обязательно),
``SD_HOST`` (по умолчанию sd.servionica.ru), ``SD_TIMEOUT`` (по умолчанию 30).
"""

from __future__ import annotations

import http.client
import os
import ssl
import time
from urllib.parse import urlencode

UPSTREAM_HOST = os.environ.get("SD_HOST", "sd.servionica.ru")
UPSTREAM_TIMEOUT = float(os.environ.get("SD_TIMEOUT", "30"))
SECRET = os.environ.get("RELAY_SECRET", "")

# Заголовки ответа, которые нужны боту. CSP и его «продолжения» сюда не
# попадают — именно из-за них падал разбор ответа на стороне бота.
RESPONSE_ALLOW = {"content-type", "set-cookie", "request-id", "content-disposition"}

# Что не пробрасываем в SD: Host выставит http.client, сжатие нам не нужно,
# секрет релея наружу не отдаём. Заголовки шлюза (Envoy/трассировка) тоже
# лишние: ServiceDesk они не нужны и только шумят в его логах.
REQUEST_SKIP = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "upgrade",
    "accept-encoding",
    "keep-alive",
    "x-relay-secret",
    "x-api-gateway-function-id",
    "x-envoy-external-address",
    "x-envoy-original-path",
    "x-forwarded-for",
    "x-forwarded-proto",
    "x-real-remote-address",
    "traceparent",
    "tracestate",
    "uber-trace-id",
}

# Где искать настоящий путь запроса. API Gateway подставляет в event["path"]
# шаблон маршрута («/{proxy+}»), а исходный путь отдаёт в заголовке Envoy.
ORIGINAL_PATH_HEADERS = ("x-envoy-original-path", "x-original-uri", "x-rewrite-url")

# Корни путей ServiceDesk. По ним отличаем настоящий путь от пути с префиксом
# функции, который может добавить платформа.
PATH_ROOTS = ("/v1/", "/api/", "/list/", "/login.do", "/auth.do", "/record/")


def normalize_path(raw: str) -> str:
    """Отрезает префикс функции Yandex, если платформа его добавила.

    Настоящий путь начинается с известного корня («/v1/...»). Если он не
    начинается — значит перед ним стоит сегмент с id функции, и его нужно
    отбросить. Простой поиск подстроки тут не годится: «/list/» встречается
    внутри «/v1/list/», и путь обрезался бы неправильно.
    """
    path = raw or "/"
    if path.startswith(PATH_ROOTS):
        return path

    parts = path.split("/", 2)
    if len(parts) == 3 and parts[2]:
        candidate = "/" + parts[2]
        if candidate.startswith(PATH_ROOTS):
            return candidate
    return path


def _reply(status: int, body: str, headers: dict[str, str] | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": headers or {"Content-Type": "text/plain; charset=utf-8"},
        "body": body,
        "isBase64Encoded": False,
    }


def _collapse_cookies(raw: list[tuple[str, str]]) -> dict[str, str]:
    """Собирает заголовки ответа, склеивая несколько Set-Cookie в один.

    Формат ответа функции — объект, поэтому несколько Set-Cookie передать
    нельзя. ServiceDesk отдаёт их немного, а бот авторизуется заголовком
    X-Auth-Key, поэтому склейка через «, » безопасна.
    """
    result: dict[str, str] = {}
    cookies: list[str] = []
    for name, value in raw:
        lowered = name.lower()
        if lowered not in RESPONSE_ALLOW:
            continue
        if lowered == "set-cookie":
            cookies.append(value)
            continue
        result[name] = value
    if cookies:
        result["Set-Cookie"] = ", ".join(cookies)
    if not any(name.lower() == "content-type" for name in result):
        result["Content-Type"] = "application/json; charset=UTF-8"
    return result


def request_path(event: dict, lower_headers: dict[str, str]) -> str:
    """Настоящий путь запроса.

    API Gateway кладёт в ``event["path"]`` шаблон маршрута («/{proxy+}»), а
    исходный путь передаёт в заголовке ``X-Envoy-Original-Path`` — без этого
    все запросы уходили бы в корень ServiceDesk, и логин получал 405.
    """
    path = event.get("path") or "/"
    for name in ORIGINAL_PATH_HEADERS:
        value = lower_headers.get(name)
        if value:
            path = value.split("?", 1)[0]
            break
    return normalize_path(path)


def handler(event: dict, context: object) -> dict:
    started = time.monotonic()
    incoming = {str(key).lower(): value for key, value in (event.get("headers") or {}).items()}

    if SECRET and incoming.get("x-relay-secret") != SECRET:
        print("отказ: неверный или отсутствующий секрет релея")
        return _reply(403, "forbidden")

    method = event.get("httpMethod") or "GET"
    path = request_path(event, incoming)
    query = event.get("queryStringParameters") or {}
    target = path + (("?" + urlencode(query, doseq=True)) if query else "")

    headers = {
        key: value
        for key, value in (event.get("headers") or {}).items()
        if key.lower() not in REQUEST_SKIP
    }
    headers["Accept-Encoding"] = "identity"

    raw_body = event.get("body") or ""
    if event.get("isBase64Encoded") and raw_body:
        import base64

        body: bytes | None = base64.b64decode(raw_body)
    else:
        body = raw_body.encode("utf-8") if raw_body else None

    context_ssl = ssl.create_default_context()
    connection = http.client.HTTPSConnection(
        UPSTREAM_HOST, 443, timeout=UPSTREAM_TIMEOUT, context=context_ssl
    )
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        elapsed = time.monotonic() - started
        print(f"{method} {path} -> {response.status} за {elapsed:.1f} с ({len(payload)} байт)")
        return _reply(
            response.status,
            payload.decode("utf-8", errors="replace"),
            _collapse_cookies(response.getheaders()),
        )
    except (TimeoutError, OSError) as exc:
        print(f"{method} {path} -> таймаут через {time.monotonic() - started:.1f} с: {exc}")
        return _reply(504, "relay timeout")
    except Exception as exc:  # noqa: BLE001 — функция не должна падать молча
        print(f"{method} {path} -> ошибка: {type(exc).__name__}: {exc}")
        return _reply(502, "relay error")
    finally:
        connection.close()
