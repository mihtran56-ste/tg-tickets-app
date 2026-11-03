"""Проверка локального SD-прокси, снимающего битый CSP-заголовок.

Запуск:  python tests/test_sd_proxy.py
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiohttp import ClientSession  # noqa: E402
from sd_proxy import request_headers, response_headers, start_sd_proxy  # noqa: E402


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def scenario_response_headers() -> None:
    """CSP и его «продолжения» не должны попадать боту."""
    raw = [
        ("Content-Type", "application/json; charset=UTF-8"),
        ("Content-Security-Policy-Report-Only", "default-src 'self'; style-src 'self' 'unsafe-inline' ht"),
        ("tps", "//api-maps.yandex.ru https://maps.yastatic.net"),
        ("andex.ru https: //maps.yastatic.net", "connect-src 'self'"),
        ("request-id", "abc123"),
        ("Set-Cookie", "auth=test; path=/"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Length", "999"),
    ]
    filtered = response_headers(raw)
    names = {name.lower() for name, _ in filtered}
    assert "content-type" in names
    assert "request-id" in names
    assert "set-cookie" in names
    assert "content-security-policy-report-only" not in names
    assert "tps" not in names
    assert "transfer-encoding" not in names
    assert "content-length" not in names
    assert all("andex" not in name.lower() for name, _ in filtered)
    print("  OK: CSP и битые заголовки отфильтрованы")


def scenario_request_headers() -> None:
    """В SD не должны уходить Host/Content-Length и запрос сжатия."""
    raw = {
        "Host": "127.0.0.1:8765",
        "Content-Length": "42",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "X-Auth-Key": "key",
        "Authorization": "Bearer key",
        "Connection": "keep-alive",
    }
    headers = request_headers(raw)
    assert "Host" not in headers
    assert "Content-Length" not in headers
    assert "Accept-Encoding" in headers and headers["Accept-Encoding"] == "identity"
    assert headers["X-Auth-Key"] == "key"
    assert headers["Authorization"] == "Bearer key"
    assert "Connection" not in headers
    print("  OK: заголовки запроса приведены к безопасному виду")


async def scenario_proxy_strips_bad_csp() -> None:
    """Прокси отдаёт 200, тело и не отдаёт битый CSP."""
    body = b'{"status":"OK","data":{"auth_key":"test-key"}}'

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Set-Cookie: auth=test; path=/\r\n"
            b"request-id: test-request\r\n"
            b"Content-Security-Policy-Report-Only: default-src 'self'; "
            b"style-src 'self' 'unsafe-inline' ht\r\n"
            b"tps: //api-maps.yandex.ru https://maps.yastatic.net; "
            b"img-src 'self' data: https://s3-sd.servionica.ru\r\n"
            b"andex.ru https: //maps.yastatic.net; connect-src 'self'\r\n"
            b"\r\n" + body
        )
        writer.write(response)
        await writer.drain()
        writer.close()

    upstream_server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    upstream_port = upstream_server.sockets[0].getsockname()[1]
    proxy_port = free_port()
    runner = await start_sd_proxy(
        f"http://127.0.0.1:{upstream_port}", proxy_port
    )
    try:
        async with ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{proxy_port}/v1/list/itsm_request?page=1"
            ) as response:
                assert response.status == 200, f"статус: {response.status}"
                assert "Content-Security-Policy-Report-Only" not in response.headers
                assert response.headers.get("request-id") == "test-request"
                assert response.headers.get("Content-Type", "").startswith("application/json")
                data = await response.json()
                assert data["status"] == "OK"
                assert data["data"]["auth_key"] == "test-key"
    finally:
        await runner.cleanup()
        upstream_server.close()
        await upstream_server.wait_closed()
    print("  OK: прокси вернул 200 и тело без битого CSP")


async def main() -> None:
    print("Проверка SD-прокси")
    print("\n[фильтр заголовков ответа]")
    scenario_response_headers()
    print("\n[заголовки запроса]")
    scenario_request_headers()
    print("\n[интеграция с битым CSP]")
    await scenario_proxy_strips_bad_csp()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
