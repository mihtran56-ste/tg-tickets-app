"""Сетевые настройки Telegram.

В некоторых сетях (например, у российских провайдеров) адрес
api.telegram.org блокируется напрямую. Тогда есть два пути:

1. Прокси/VPN (лучший вариант): в PROXY_URL указывается HTTP-прокси,
   через который бот ходит в Telegram и во внешние API.
2. Привязка к конкретному IP Bot API (TELEGRAM_API_IP): имя хоста и
   проверка сертификата остаются прежними, меняется только адрес,
   на который устанавливается соединение.
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession

log = logging.getLogger("assistant")


class _FixedIPResolver(aiohttp.abc.AbstractResolver):
    """Резолвер, который всегда возвращает один и тот же IP."""

    def __init__(self, ip: str) -> None:
        self._ip = ip

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET):
        return [
            {
                "hostname": host,
                "host": self._ip,
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    async def close(self) -> None:
        pass


class IPPinnedSession(AiohttpSession):
    """Сессия aiogram, привязанная к конкретному IP Bot API."""

    def __init__(self, api_ip: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        # AiohttpSession создаёт TCPConnector(**self._connector_init),
        # поэтому достаточно добавить туда свой резолвер.
        self._connector_init["resolver"] = _FixedIPResolver(api_ip)


def _proxy_reachable(url: str, timeout: float = 1.5) -> bool:
    """Быстрая проверка, что локальный прокси-клиент запущен."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def build_session(proxy: str = "", api_ip: str = "") -> AiohttpSession:
    """Прокси важнее привязки к IP; если ничего не задано — обычная сессия."""
    if proxy and _proxy_reachable(proxy):
        log.info("Telegram и API: использую прокси %s", proxy)
        return AiohttpSession(proxy=proxy)
    if proxy:
        log.warning("Прокси %s недоступен — перехожу на прямой доступ", proxy)
    if api_ip:
        return IPPinnedSession(api_ip)
    return AiohttpSession()
