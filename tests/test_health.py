"""Проверка health-эндпоинта, по которому хостинг понимает, что сервис жив.

Отдельно проверяется безопасность: подробный статус мониторинга не должен
отдаваться по публичному адресу без токена.

Запуск:  python tests/test_health.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aiohttp  # noqa: E402

from health import start_health_server  # noqa: E402

TOKEN = "секретный-токен"


def free_port() -> int:
    """Свободный порт, чтобы тест не конфликтовал с другими сервисами."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def get(port: int, path: str) -> tuple[int, dict]:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"http://127.0.0.1:{port}{path}") as response:
            return response.status, await response.json()


def fake_status() -> dict:
    return {"running": True, "seen_count": 25}


async def scenario_minimal() -> None:
    """Без провайдера статуса: 200 и только служебные поля."""
    port = free_port()
    runner = await start_health_server(port)
    try:
        for path in ("/", "/health"):
            status, payload = await get(port, path)
            assert status == 200, f"{path}: ожидали 200, получили {status}"
            assert payload["status"] == "ok", f"{path}: неверный статус {payload}"
            assert "uptime_sec" in payload, f"{path}: нет uptime_sec"
            assert "sd" not in payload, f"{path}: провайдера не было, а поле sd есть"
        print("  OK: / и /health отвечают 200 без лишних полей")
    finally:
        await runner.cleanup()


async def scenario_details_by_token() -> None:
    """Подробности отдаются только с верным токеном."""
    port = free_port()
    runner = await start_health_server(port, fake_status, token=TOKEN)
    try:
        status, payload = await get(port, "/health")
        assert status == 200
        assert "sd" not in payload, f"без токена статус не должен раскрываться: {payload}"
        print("  OK: без токена подробный статус скрыт")

        status, payload = await get(port, f"/health?token={TOKEN}")
        assert status == 200
        assert payload["sd"]["seen_count"] == 25, f"с токеном статус не отдан: {payload}"
        print("  OK: с верным токеном подробный статус отдаётся")

        status, payload = await get(port, "/health?token=неверный")
        assert "sd" not in payload, f"неверный токен не должен раскрывать статус: {payload}"
        print("  OK: неверный токен не раскрывает статус")
    finally:
        await runner.cleanup()


async def scenario_broken_provider() -> None:
    """Провайдер статуса падает — сервер всё равно отвечает 200."""
    port = free_port()

    def broken() -> dict:
        raise RuntimeError("что-то сломалось")

    runner = await start_health_server(port, broken, token=TOKEN)
    try:
        status, payload = await get(port, f"/health?token={TOKEN}")
        assert status == 200, f"падение провайдера не должно ронять сервер, код {status}"
        assert payload["status"] == "ok"
        assert "sd" not in payload, "при ошибке провайдера сами данные не добавляются"
        assert payload.get("sd_error") is True, f"нет отметки об ошибке статуса: {payload}"
        print("  OK: ошибка провайдера не роняет health-сервер")
    finally:
        await runner.cleanup()


async def scenario_host_details() -> None:
    """С токеном видно данные хоста: Telegram, режим, время старта."""
    port = free_port()
    host = {"telegram": "@ServMS_bot", "monitoring": "включён", "started_at": "01.11.2026 12:00:00"}
    runner = await start_health_server(
        port,
        fake_status,
        token=TOKEN,
        extra_provider=lambda: host,
    )
    try:
        status, payload = await get(port, "/health")
        assert "host" not in payload, f"без токена данные хоста не отдаются: {payload}"

        status, payload = await get(port, f"/health?token={TOKEN}")
        assert status == 200
        assert payload["host"]["telegram"] == "@ServMS_bot", f"нет данных Telegram: {payload}"
        assert payload["host"]["monitoring"] == "включён"
        assert payload["sd"]["seen_count"] == 25, "статус SD должен остаться"
        # Счётчик запросов: по нему видно, что сервис действительно будят
        assert payload["host"]["pings"] >= 1, f"счётчик пингов не работает: {payload}"
        assert payload["host"]["last_ping"], "нет времени последнего пинга"
        print("  OK: с токеном видны Telegram, режим работы и счётчик пингов")

        # Второй запрос должен увеличить счётчик
        status, payload2 = await get(port, f"/health?token={TOKEN}")
        assert payload2["host"]["pings"] > payload["host"]["pings"], "счётчик не растёт"
        print("  OK: счётчик пингов увеличивается")
    finally:
        await runner.cleanup()


async def scenario_host_provider_broken() -> None:
    """Падение поставщика данных хоста не ломает эндпоинт."""
    port = free_port()

    def broken() -> dict:
        raise RuntimeError("нет данных")

    runner = await start_health_server(port, fake_status, token=TOKEN, extra_provider=broken)
    try:
        status, payload = await get(port, f"/health?token={TOKEN}")
        assert status == 200
        assert payload["sd"]["seen_count"] == 25, "статус SD должен остаться"
        assert payload.get("host_error") is True, f"нет отметки об ошибке хоста: {payload}"
        print("  OK: ошибка данных хоста не роняет health")
    finally:
        await runner.cleanup()


async def scenario_port_released() -> None:
    """После cleanup порт освобождается — важно при перезапусках на хостинге."""
    port = free_port()
    runner = await start_health_server(port)
    await runner.cleanup()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", port))
        print("  OK: после остановки порт свободен")
    except OSError as exc:
        raise AssertionError(f"порт не освободился: {exc}") from exc


async def main() -> None:
    print("Проверка health-эндпоинта")
    for title, scenario in (
        ("[базовый ответ]", scenario_minimal),
        ("[доступ к деталям]", scenario_details_by_token),
        ("[данные хоста]", scenario_host_details),
        ("[сломанный провайдер]", scenario_broken_provider),
        ("[сломанный хоста]", scenario_host_provider_broken),
        ("[освобождение порта]", scenario_port_released),
    ):
        print(f"\n{title}")
        await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
