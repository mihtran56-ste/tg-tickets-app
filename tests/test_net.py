"""Проверка выбора сетевой сессии Telegram (прокси / прибитый IP / напрямую).

Запуск:  python tests/test_net.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram.client.session.aiohttp import AiohttpSession  # noqa: E402

from telegram_net import IPPinnedSession, _proxy_reachable, build_session  # noqa: E402


async def scenario_direct() -> None:
    """Ни прокси, ни IP не заданы — обычная сессия."""
    session = build_session("", "")
    try:
        assert isinstance(session, AiohttpSession)
        assert not isinstance(session, IPPinnedSession), "без настроек не должно быть привязки к IP"
        print("  OK: без настроек — обычная сессия")
    finally:
        await session.close()


async def scenario_pinned_ip() -> None:
    """Задан только IP Bot API — сессия привязывается к нему."""
    session = build_session("", "149.154.167.220")
    try:
        assert isinstance(session, IPPinnedSession), "должна быть сессия с прибитым IP"
        resolver = session._connector_init.get("resolver")
        assert resolver is not None, "резолвер не установлен"
        assert resolver._ip == "149.154.167.220", f"прибит не тот IP: {resolver._ip}"
        answers = await resolver.resolve("api.telegram.org", 443)
        assert answers[0]["host"] == "149.154.167.220", f"резолвер отдаёт {answers}"
        print("  OK: прибитый IP подставляется резолвером")
    finally:
        await session.close()


async def scenario_unreachable_proxy() -> None:
    """Прокси указан, но не отвечает — откат на прямой доступ, без падения."""
    session = build_session("http://127.0.0.1:1", "149.154.167.220")
    try:
        assert isinstance(session, IPPinnedSession), (
            "при недоступном прокси должен включиться резервный путь через IP"
        )
        print("  OK: недоступный прокси не роняет запуск, есть резервный путь")
    finally:
        await session.close()


def scenario_proxy_reachable() -> None:
    """Проверка доступности прокси: слушающий порт — да, закрытый — нет."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert _proxy_reachable(f"http://127.0.0.1:{port}") is True, "открытый порт не распознан"

    assert _proxy_reachable("http://127.0.0.1:1", timeout=0.5) is False, "закрытый порт распознан как живой"
    print("  OK: живость прокси определяется верно")


async def main() -> None:
    print("Проверка сетевых настроек Telegram")
    print("\n[напрямую]")
    await scenario_direct()
    print("\n[прибитый IP]")
    await scenario_pinned_ip()
    print("\n[недоступный прокси]")
    await scenario_unreachable_proxy()
    print("\n[проверка прокси]")
    scenario_proxy_reachable()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
