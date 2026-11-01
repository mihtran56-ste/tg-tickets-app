"""Проверка постраничной загрузки заявок.

SimpleOne листает параметром `page` (с 1), а offset/limit игнорирует —
проверено на живом API. Тест подменяет HTTP-сессию и проверяет логику.

Запуск:  python tests/test_pagination.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from sd_client import SDClient  # noqa: E402


def raw_item(number: str) -> dict:
    return {
        "sys_id": f"{abs(hash(number)):032d}",
        "number": {"value": number},
        "short_description": {"value": f"Заявка {number}"},
        "assigned_user": {"value": {"display_value": "Иванов"}},
        "sys_created_at": {"value": "2026-10-31 10:00:00"},
        "state": {"value": {"database_value": "in_progress", "display_value": "В работе"}},
    }


def page_payload(numbers: list[str], total: int) -> dict:
    return {"data": {"items": [raw_item(n) for n in numbers], "pagination": {"total": total}}}


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    async def text(self) -> str:
        return json.dumps(self._payload, ensure_ascii=False)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    """Отдаёт заранее заданные страницы и запоминает параметры запросов."""

    def __init__(self, pages: dict[int, dict]) -> None:
        self.pages = pages
        self.requests: list[dict] = []
        self.closed = False

    def get(self, url: str, params: dict | None = None, headers: dict | None = None):
        params = params or {}
        self.requests.append(dict(params))
        page = int(params.get("page", 1))
        payload = self.pages.get(page, page_payload([], 0))
        return FakeResponse(payload)

    async def close(self) -> None:
        self.closed = True


def make_client() -> SDClient:
    client = SDClient(
        Config(
            bot_token="1:x",
            allowed_user_ids=frozenset({1}),
            data_dir=ROOT,
            sd_base_url="https://sd.example.ru",
            sd_login="ivanov",
            sd_password="secret",
            sd_condition="state=1",
        )
    )
    client._logged_in = True  # логин уже пройден
    client._auth_key = "test-key"
    return client


async def scenario_two_pages() -> None:
    """Первая страница 20, вторая 3 — получаем весь фильтр."""
    first = [f"REQ{i:04d}" for i in range(1, 21)]
    second = ["REQ0021", "REQ0022", "REQ0023"]

    client = make_client()
    client.session = FakeSession({
        1: page_payload(first, 23),
        2: page_payload(second, 23),
    })

    items, total = await client.fetch_requests(view="Default", condition="x", limit=100)

    assert total == 23, f"total: {total}"
    assert len(items) == 23, f"получено {len(items)} вместо 23"
    assert len({i.number for i in items}) == 23, "есть дубликаты между страницами"
    pages = [r.get("page") for r in client.session.requests]
    assert pages == [1, 2], f"запрашивались страницы {pages}"
    assert client._pagination_supported is True
    print("  OK: две страницы склеены, дублей нет")


async def scenario_server_ignores_page() -> None:
    """Сервер отдаёт ту же страницу — не зацикливаемся и запоминаем это."""
    same = [f"REQ{i:04d}" for i in range(1, 21)]

    client = make_client()
    client.session = FakeSession({
        1: page_payload(same, 40),
        2: page_payload(same, 40),  # сервер «забыл» про page
    })

    items, total = await client.fetch_requests(view="Default", condition="x", limit=100)

    assert len(items) == 20, f"ожидали 20 уникальных, получили {len(items)}"
    assert client._pagination_supported is False, "флаг «пагинация не работает» не выставлен"
    first_requests = len(client.session.requests)
    assert first_requests == 2, f"сделано {first_requests} запросов"
    print("  OK: повтор страницы не приводит к дублям и зацикливанию")

    # Вторая проверка не должна снова ходить на вторую страницу
    client.session.requests.clear()
    await client.fetch_requests(view="Default", condition="x", limit=100)
    pages = [r.get("page") for r in client.session.requests]
    assert pages == [1], f"после неудачи пагинация не отключилась: страницы {pages}"
    print("  OK: после неудачи листаем только первую страницу")


async def scenario_single_page() -> None:
    """Заявок меньше страницы — второй запрос не делается."""
    client = make_client()
    client.session = FakeSession({1: page_payload(["REQ1", "REQ2"], 2)})

    items, total = await client.fetch_requests(view="Default", condition="x", limit=100)

    assert len(items) == 2 and total == 2
    assert len(client.session.requests) == 1, f"лишние запросы: {client.session.requests}"
    print("  OK: одна страница — один запрос")


async def scenario_empty() -> None:
    """Пустой фильтр обрабатывается без ошибок."""
    client = make_client()
    client.session = FakeSession({1: page_payload([], 0)})

    items, total = await client.fetch_requests(view="Default", condition="x", limit=100)

    assert items == [] and total == 0
    print("  OK: пустой результат не ломает цикл")


async def main() -> None:
    print("Проверка постраничной загрузки")
    for title, scenario in (
        ("[две страницы]", scenario_two_pages),
        ("[сервер игнорирует page]", scenario_server_ignores_page),
        ("[одна страница]", scenario_single_page),
        ("[пусто]", scenario_empty),
    ):
        print(f"\n{title}")
        await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
