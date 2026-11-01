"""Проверка обязательных настроек SD перед запуском мониторинга.

Главный сценарий: на хостинге забыли SD_CONDITION — фильтр пустой, и бот
начал бы мониторить весь ServiceDesk. Мониторинг должен не запуститься.

Запуск:  python tests/test_sd_settings.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from notify import notify_lifecycle  # noqa: E402
from sd_settings import describe_missing, missing_sd_settings  # noqa: E402

ALLOWED = 1001


def make_cfg(**overrides: object) -> Config:
    base: dict[str, object] = {
        "bot_token": "1:x",
        "allowed_user_ids": frozenset({ALLOWED}),
        "data_dir": ROOT,
        "sd_enabled": True,
        "sd_check_interval": 60,
        "sd_base_url": "https://sd.example.ru",
        "sd_login": "ivanov",
        "sd_password": "secret",
        "sd_condition": "stateNOT INcancelled@closed",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def scenario_complete() -> None:
    cfg = make_cfg()
    assert missing_sd_settings(cfg) == [], "полные настройки не должны считаться неполными"
    assert describe_missing(cfg) == "", "для полных настроек объяснение пустое"
    print("  OK: полные настройки проходят проверку")


def scenario_missing_condition() -> None:
    cfg = make_cfg(sd_condition="")
    missing = missing_sd_settings(cfg)
    assert missing == ["SD_CONDITION"], f"ожидали только SD_CONDITION, получили {missing}"
    text = describe_missing(cfg)
    assert "SD_CONDITION" in text, f"в тексте нет имени переменной: {text}"
    assert "весь ServiceDesk" in text, f"нет предупреждения про весь SD: {text}"
    print("  OK: пустой фильтр блокирует мониторинг с понятным объяснением")


def scenario_missing_several() -> None:
    cfg = make_cfg(sd_login="", sd_password="", sd_condition="")
    missing = missing_sd_settings(cfg)
    assert missing == ["SD_LOGIN", "SD_PASSWORD", "SD_CONDITION"], f"порядок/состав: {missing}"
    text = describe_missing(cfg)
    assert "логин" in text and "пароль" in text and "фильтр заявок" in text, f"нет пояснений: {text}"
    print("  OK: перечислены все незаполненные настройки с пояснениями")


def scenario_missing_base_url() -> None:
    cfg = make_cfg(sd_base_url="")
    assert "SD_BASE_URL" in missing_sd_settings(cfg)
    print("  OK: пустой адрес SD тоже блокирует мониторинг")


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
        self.sent.append(text)


async def scenario_startup_message_tells_truth() -> None:
    """Сообщение о запуске говорит, что мониторинг не запущен, а не «включён»."""
    bot = FakeBot()
    await notify_lifecycle(bot, make_cfg(sd_condition=""), "start", monitoring="НЕ запущен: неполные настройки")
    text = bot.sent[0]
    assert "НЕ запущен" in text, f"сообщение врёт про состояние мониторинга: {text}"
    assert "включён" not in text, f"нельзя писать «включён»: {text}"
    print("  OK: уведомление о старте показывает реальное состояние мониторинга")


async def main() -> None:
    print("Проверка настроек SD перед запуском")
    for title, scenario in (
        ("[полные настройки]", scenario_complete),
        ("[нет фильтра]", scenario_missing_condition),
        ("[нет логина и пароля]", scenario_missing_several),
        ("[нет адреса]", scenario_missing_base_url),
    ):
        print(f"\n{title}")
        scenario()
    print("\n[честность уведомления]")
    await scenario_startup_message_tells_truth()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
