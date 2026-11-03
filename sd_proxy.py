"""Локальный reverse-proxy для ServiceDesk.

Зачем: SD отдаёт битый ``Content-Security-Policy-Report-Only`` — значение
разорвано переводами строк на куски (``tps:``, ``andex.ru``, ``yandex.ru``).
Строгий парсер aiohttp падает на таком ответе. Прокси ходит в SD через
``http.client`` (он терпит этот заголовок), оставляет только нужные боту
заголовки ответа и отдаёт чистый HTTP. Слушает только 127.0.0.1, наружу
не выставлен.
"""

from __future__ import annotations

import asyncio
import gzip
import http.client
import logging
import ssl
import zlib
from urllib.parse import urlsplit

from aiohttp import web

log = logging.getLogger("sd_proxy")

# Hop-by-hop и заголовки, которые нельзя пробрасывать в http.client:
# Host/Content-Length он выставит сам, сжатие нам не нужно.
_REQUEST_SKIP = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "proxy-connection",
    "upgrade",
    "accept-encoding",
    "keep-alive",
}

# Боту нужны только эти заголовки ответа. CSP и его «продолжения» сюда
# не попадают — именно из-за них падал aiohttp.
_RESPONSE_ALLOW = {
    "content-type",
    "set-cookie",
    "request-id",
    "content-disposition",
}

# Меньше, чем ClientTimeout(total=60, sock_read=45) у бота.
_UPSTREAM_TIMEOUT = 40


class SDProxy:
    """Пересылает запросы в SD и чистит заголовки ответа."""

    def __init__(self, upstream_url: str, verify_ssl: bool = True) -> None:
        parsed = urlsplit(upstream_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"SD_PROXY: неверный upstream URL: {upstream_url!r}")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.prefix = parsed.path.rstrip("/")
        self.verify_ssl = verify_ssl

    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            context = ssl.create_default_context()
            if not self.verify_ssl:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
            return http.client.HTTPSConnection(
                self.host, self.port, timeout=_UPSTREAM_TIMEOUT, context=context
            )
        return http.client.HTTPConnection(
            self.host, self.port, timeout=_UPSTREAM_TIMEOUT
        )

    def forward(
        self,
        method: str,
        path_qs: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        """Синхронный запрос к SD; вызывается через asyncio.to_thread."""
        target = (self.prefix + path_qs) if self.prefix else path_qs
        connection = self._connection()
        try:
            connection.request(method, target, body=body or None, headers=headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, response.getheaders(), data
        finally:
            connection.close()


def request_headers(headers) -> dict[str, str]:
    """Заголовки от бота -> заголовки для SD."""
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in _REQUEST_SKIP:
            continue
        result[name] = value
    result["Accept-Encoding"] = "identity"
    return result


def response_headers(raw: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Оставляет только безопасные заголовки ответа SD."""
    result: list[tuple[str, str]] = []
    for name, value in raw:
        if name.lower() in _RESPONSE_ALLOW:
            result.append((name, value))
    if not any(name.lower() == "content-type" for name, _ in result):
        result.append(("Content-Type", "application/json; charset=UTF-8"))
    return result


def _decode_body(data: bytes, raw_headers: list[tuple[str, str]]) -> bytes:
    """http.client не распаковывает тело: делаем это сами для gzip/deflate."""
    encoding = ""
    for name, value in raw_headers:
        if name.lower() == "content-encoding":
            encoding = value.lower()
            break
    if "gzip" in encoding:
        return gzip.decompress(data)
    if "deflate" in encoding:
        return zlib.decompress(data)
    return data


async def _handle(request: web.Request) -> web.Response:
    body = await request.read()
    proxy: SDProxy = request.app["sd_proxy"]
    headers = request_headers(request.headers)
    try:
        status, raw_headers, data = await asyncio.to_thread(
            proxy.forward,
            request.method,
            request.rel_url.raw_path_qs,
            headers,
            body,
        )
    except (TimeoutError, OSError) as exc:
        log.warning(
            "SD proxy: %s %s -> ошибка транспорта: %s",
            request.method,
            request.rel_url,
            exc,
        )
        return web.Response(status=504, text="SD proxy timeout")
    except Exception:  # noqa: BLE001 — прокси не должен ронять бота
        log.exception(
            "SD proxy: %s %s -> ошибка", request.method, request.rel_url
        )
        return web.Response(status=502, text="SD proxy error")

    try:
        clean_body = _decode_body(data, raw_headers)
    except (OSError, EOFError, zlib.error) as exc:
        log.warning("SD proxy: не удалось распаковать ответ: %s", exc)
        return web.Response(status=502, text="SD proxy decode error")

    log.debug(
        "SD proxy: %s %s -> %s (%d байт)",
        request.method,
        request.rel_url,
        status,
        len(clean_body),
    )
    return web.Response(
        status=status,
        body=clean_body,
        headers=response_headers(raw_headers),
    )


async def start_sd_proxy(
    upstream_url: str, port: int, verify_ssl: bool = True
) -> web.AppRunner:
    """Запускает прокси на 127.0.0.1:port и возвращает runner."""
    app = web.Application()
    app["sd_proxy"] = SDProxy(upstream_url, verify_ssl=verify_ssl)
    app.router.add_route("*", "/{tail:.*}", _handle)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info("SD proxy слушает 127.0.0.1:%s -> %s", port, upstream_url)
    return runner
