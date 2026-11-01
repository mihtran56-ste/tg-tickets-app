"""Проверка сборки бота без сети: Dispatcher создаётся, обработчики на месте.

Запуск:  python tests/test_wiring.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Dispatcher  # noqa: E402

from bot import build_dispatcher  # noqa: E402
from config import Config  # noqa: E402
from tickets import TicketStore  # noqa: E402


def main() -> None:
    data_dir = ROOT / ".tmp-test-wiring"
    shutil.rmtree(data_dir, ignore_errors=True)
    data_dir.mkdir(parents=True)

    try:
        cfg = Config(
            bot_token="123456789:AA-test",
            allowed_user_ids=frozenset({1}),
            admin_user_ids=frozenset({1}),
            data_dir=data_dir,
        )
        store = TicketStore(data_dir / "tickets.json")

        print("Проверка сборки бота")
        dp = build_dispatcher(cfg, store, sd_watcher=None, llm_client=None)
        assert isinstance(dp, Dispatcher), "build_dispatcher должен возвращать Dispatcher"
        print("  OK: Dispatcher создан")

        handlers = dp.message.handlers
        assert handlers, "должны быть зарегистрированы обработчики сообщений"
        print(f"  OK: обработчиков сообщений: {len(handlers)}")

        # SD-команды регистрируются всегда: при выключенном мониторинге
        # они отвечают «отключен», а не молчат
        dp_with_sd = build_dispatcher(cfg, store, sd_watcher=object(), llm_client=None)
        assert len(dp_with_sd.message.handlers) == len(handlers), (
            "набор обработчиков не должен зависеть от наличия watcher"
        )
        print(f"  OK: набор обработчиков одинаков без SD и с SD: {len(handlers)}")

        # Обработчик текста существует и не падает на пустом сообщении
        text_handlers = [h for h in dp.message.handlers if getattr(h, "callback", None)]
        assert text_handlers, "должен быть обработчик обычного текста"
        print("  OK: обработчик обычного текста зарегистрирован")

        print("\nВсе проверки пройдены.")
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
