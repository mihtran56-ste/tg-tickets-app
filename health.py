"""Мини-HTTP-сервер для хостингов, которым нужен «живой» порт.

Зачем: Render, Hugging Face Spaces и подобные площадки считают сервис живым,
только если он отвечает по HTTP. Боту это не нужно (он работает через
long polling), поэтому сервер отвечает на / и /health коротким JSON.

Безопасность: адрес сервиса публичный, поэтому подробный статус мониторинга
(сколько заявок видели, текст последней ошибки) отдаётся только при совпадении
токена из query-параметра с HEALTH_TOKEN. Без токена — минимальный ответ.

Включается переменной окружения PORT (0 или пусто — выключен).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from aiohttp import web

log = logging.getLogger("health")

StatusProvider = Callable[[], dict[str, Any]]


def _build_app(
    started_at: float,
    status_provider: StatusProvider | None,
    token: str,
    extra_provider: StatusProvider | None = None,
) -> web.Application:
    app = web.Application()

    async def handle(request: web.Request) -> web.Response:
        payload: dict[str, Any] = {
            "status": "ok",
            "uptime_sec": int(time.time() - started_at),
        }

        # Подробности — только по токену, иначе публичный адрес раскрывает
        # внутреннее состояние мониторинга.
        details_allowed = bool(token) and request.query.get("token") == token
        if details_allowed:
            if status_provider is not None:
                try:
                    payload["sd"] = status_provider()
                except Exception:  # noqa: BLE001 — статус не должен ронять сервер
                    log.exception("Не удалось получить статус SD")
                    payload["sd_error"] = True
            if extra_provider is not None:
                try:
                    payload["host"] = extra_provider()
                except Exception:  # noqa: BLE001
                    log.exception("Не удалось получить данные о хосте")
                    payload["host_error"] = True

        return web.json_response(payload)

    app.router.add_get("/", handle)
    app.router.add_get("/health", handle)
    return app


async def start_health_server(
    port: int,
    status_provider: StatusProvider | None = None,
    token: str = "",
    extra_provider: StatusProvider | None = None,
) -> web.AppRunner:
    """Запускает HTTP-сервер и возвращает runner — его нужно закрыть в finally."""
    app = _build_app(time.time(), status_provider, token, extra_provider)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info(
        "Health-сервер слушает порт %s (подробный статус %s)",
        port,
        "по токену" if token else "выключен: HEALTH_TOKEN не задан",
    )
    return runner
