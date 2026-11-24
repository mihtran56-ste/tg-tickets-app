"""Проверка релея ServiceDesk в Yandex Cloud.

Делает настоящий вход в SD и загружает список заявок через функцию-релей,
печатает время каждого шага. Нужен, чтобы убедиться, что релей работает,
ещё до переключения бота.

Запуск:
    python scripts/relay_check.py https://functions.yandexcloud.net/<id> <секрет>
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config import load_config  # noqa: E402
from sd_client import SDClient  # noqa: E402


async def check(url: str, secret: str) -> int:
    cfg = load_config()
    # Релей главнее локального прокси: sd_proxy_port обнуляем.
    relay_cfg = replace(cfg, sd_proxy_url=url.rstrip("/"), sd_proxy_secret=secret, sd_proxy_port=0)
    client = SDClient(relay_cfg)
    try:
        started = time.monotonic()
        ok, reason = await client.login()
        login_sec = time.monotonic() - started
        print(f"логин через релей: {'OK' if ok else 'ОШИБКА'} за {login_sec:.1f} с ({reason})")
        if not ok:
            return 1

        started = time.monotonic()
        try:
            items, total = await client.fetch_requests(
                view=cfg.sd_view,
                condition=cfg.sd_condition,
                limit=100,
            )
        except Exception as exc:  # noqa: BLE001 — показываем причину как есть
            print(f"загрузка списка: ОШИБКА за {time.monotonic() - started:.1f} с — "
                  f"{type(exc).__name__}: {exc}")
            return 1
        elapsed = time.monotonic() - started
        print(f"загрузка списка: OK за {elapsed:.1f} с — заявок {len(items)} из {total}")
        if client.last_fetch_warnings:
            print(f"предупреждения: {'; '.join(client.last_fetch_warnings)}")
        return 0
    finally:
        await client.close()


def main() -> None:
    if len(sys.argv) < 3:
        print("использование: python scripts/relay_check.py <url> <секрет>")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(check(sys.argv[1], sys.argv[2])))


if __name__ == "__main__":
    main()
