"""Проверка релея Yandex Cloud: прокси в ServiceDesk.

Сети не требует: HTTPS-соединение подменяется фейком.

Запуск:  python tests/test_relay.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "yandex"))

import sd_relay  # noqa: E402

SECRET = "test-secret"


class FakeResponse:
    status = 200

    def getheaders(self) -> list[tuple[str, str]]:
        return [
            ("Content-Type", "application/json; charset=UTF-8"),
            ("Content-Security-Policy-Report-Only", "default-src 'self'; broken\r\nline"),
            ("Set-Cookie", "auth=one; path=/"),
            ("Set-Cookie", "session=two; path=/"),
            ("request-id", "abc"),
            ("Transfer-Encoding", "chunked"),
        ]

    def read(self) -> bytes:
        return b'{"status":"OK","data":{"items":[]}}'


class FakeConnection:
    """Запоминает, что релей отправил в SD."""

    last: dict = {}

    def __init__(self, host: str, port: int, timeout: float, context: object) -> None:
        FakeConnection.last = {"host": host, "port": port, "timeout": timeout}

    def request(self, method: str, target: str, body=None, headers=None) -> None:
        FakeConnection.last.update(
            {"method": method, "target": target, "body": body, "headers": headers or {}}
        )

    def getresponse(self) -> FakeResponse:
        return FakeResponse()

    def close(self) -> None:
        pass


def scenario_normalize_path() -> None:
    """Префикс функции Yandex отрезается, настоящий путь остаётся."""
    cases = {
        "/v1/list/itsm_request": "/v1/list/itsm_request",
        "/d4e123abc/v1/list/itsm_request": "/v1/list/itsm_request",
        "/d4e123abc/login.do": "/login.do",
        "/": "/",
        "": "/",
    }
    for raw, expected in cases.items():
        result = sd_relay.normalize_path(raw)
        assert result == expected, f"{raw!r} -> {result!r}, ожидали {expected!r}"
    print("  OK: префикс функции отрезается, путь сохраняется")


def scenario_cookie_headers() -> None:
    """CSP выбрасывается, несколько Set-Cookie склеиваются."""
    headers = sd_relay._collapse_cookies(FakeResponse().getheaders())
    names = {name.lower() for name in headers}
    assert "content-security-policy-report-only" not in names, names
    assert "transfer-encoding" not in names, names
    assert headers["Set-Cookie"] == "auth=one; path=/, session=two; path=/", headers
    assert headers["request-id"] == "abc"
    assert headers["Content-Type"].startswith("application/json")
    print("  OK: битый CSP выброшен, cookies склеены")


def scenario_secret_required() -> None:
    """Без секрета релей отвечает 403 и в SD не ходит."""
    original = sd_relay.SECRET
    sd_relay.SECRET = SECRET
    original_conn = sd_relay.http.client.HTTPSConnection
    sd_relay.http.client.HTTPSConnection = FakeConnection  # type: ignore[assignment]
    try:
        FakeConnection.last = {}
        response = sd_relay.handler(
            {"httpMethod": "GET", "path": "/v1/list/itsm_request", "headers": {}},
            None,
        )
        assert response["statusCode"] == 403, response
        assert not FakeConnection.last, "запрос всё-таки ушёл в SD"
        print("  OK: без секрета — 403 и никаких запросов в SD")
    finally:
        sd_relay.SECRET = original
        sd_relay.http.client.HTTPSConnection = original_conn  # type: ignore[assignment]


def scenario_forwards_request() -> None:
    """Запрос уходит в SD с настоящим путём, секрет наружу не отдаётся."""
    original = sd_relay.SECRET
    sd_relay.SECRET = SECRET
    original_conn = sd_relay.http.client.HTTPSConnection
    sd_relay.http.client.HTTPSConnection = FakeConnection  # type: ignore[assignment]
    try:
        response = sd_relay.handler(
            {
                "httpMethod": "GET",
                "path": "/fn123/v1/list/itsm_request",
                "queryStringParameters": {"page": "1", "condition": "state!=closed^x"},
                "headers": {
                    "Host": "functions.yandexcloud.net",
                    "X-Relay-Secret": SECRET,
                    "X-Auth-Key": "key-123",
                    "Cookie": "auth=one",
                    "Accept-Encoding": "gzip",
                },
            },
            None,
        )
        sent = FakeConnection.last
        assert response["statusCode"] == 200, response
        assert sent["method"] == "GET", sent
        assert sent["target"] == "/v1/list/itsm_request?page=1&condition=state%21%3Dclosed%5Ex", sent["target"]
        headers = sent["headers"]
        assert "Host" not in headers, headers
        assert "Accept-Encoding" in headers and headers["Accept-Encoding"] == "identity", headers
        assert "x-relay-secret" not in {k.lower() for k in headers}, headers
        assert headers["X-Auth-Key"] == "key-123", headers
        assert headers["Cookie"] == "auth=one", headers
        assert "Content-Security-Policy-Report-Only" not in response["headers"], response["headers"]
        assert response["body"].startswith('{"status":"OK"'), response["body"]
        print("  OK: путь и query собраны верно, секрет наружу не ушёл, CSP выброшен")
    finally:
        sd_relay.SECRET = original
        sd_relay.http.client.HTTPSConnection = original_conn  # type: ignore[assignment]


def main() -> None:
    print("Проверка релея Yandex Cloud")
    for title, scenario in (
        ("[путь]", scenario_normalize_path),
        ("[заголовки ответа]", scenario_cookie_headers),
        ("[секрет]", scenario_secret_required),
        ("[пересылка запроса]", scenario_forwards_request),
    ):
        print(f"\n{title}")
        scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    main()
