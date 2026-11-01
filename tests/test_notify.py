"""Проверка уведомлений о запуске и остановке бота.

Запуск:  python tests/test_notify.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import Config  # noqa: E402
from notify import notify_lifecycle, targets  # noqa: E402

ALLOWED = 1001
NOTIFY_CHAT = 2002


class FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, **_kwargs: object) -> None:
        if self.fail:
            raise RuntimeError("Telegram недоступен")
        self.sent.append((chat_id, text))


def make_cfg(**overrides: object) -> Config:
    base = {
        "bot_token": "1:x",
        "allowed_user_ids": frozenset({ALLOWED}),
        "data_dir": ROOT,
        "sd_enabled": True,
        "sd_check_interval": 60,
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def scenario_targets() -> None:
    assert targets(make_cfg()) == [ALLOWED], "по умолчанию — ALLOWED_USER_IDS"
    # frozenset не хранит порядок, поэтому сравниваем множества
    preferred = set(targets(make_cfg(notify_chat_ids=frozenset({NOTIFY_CHAT, ALLOWED}))))
    assert preferred == {NOTIFY_CHAT, ALLOWED}, f"NOTIFY_CHAT_IDS имеет приоритет: {preferred}"
    assert NOTIFY_CHAT in targets(
        make_cfg(allowed_user_ids=frozenset({ALLOWED}), notify_chat_ids=frozenset({NOTIFY_CHAT}))
    ), "если задан NOTIFY_CHAT_IDS, ALLOWED_USER_IDS не используется"
    assert targets(make_cfg(allowed_user_ids=frozenset(), notify_chat_ids=frozenset())) == []
    print("  OK: получатели выбираются верно (NOTIFY_CHAT_IDS важнее ALLOWED_USER_IDS)")


async def scenario_start_message() -> None:
    bot = FakeBot()
    sent = await notify_lifecycle(bot, make_cfg(), "start")
    assert sent == 1, f"ожидали одно сообщение, было {sent}"
    assert "запущен" in bot.sent[0][1].lower(), f"нет текста о запуске: {bot.sent[0][1]}"
    assert "включён" in bot.sent[0][1], f"не указано состояние мониторинга: {bot.sent[0][1]}"
    print("  OK: сообщение о запуске уходит с состоянием мониторинга")


async def scenario_stop_message() -> None:
    bot = FakeBot()
    sent = await notify_lifecycle(bot, make_cfg(), "stop")
    assert sent == 1
    assert "остановлен" in bot.sent[0][1].lower(), f"нет текста об остановке: {bot.sent[0][1]}"
    print("  OK: сообщение об остановке уходит")


async def scenario_flags_off() -> None:
    bot = FakeBot()
    assert await notify_lifecycle(bot, make_cfg(notify_on_start=False), "start") == 0
    assert await notify_lifecycle(bot, make_cfg(notify_on_stop=False), "stop") == 0
    assert not bot.sent, "при выключенном флаге сообщений быть не должно"
    print("  OK: при NOTIFY_ON_START/STOP=false сообщения не отправляются")


async def scenario_no_recipients() -> None:
    bot = FakeBot()
    sent = await notify_lifecycle(
        bot,
        make_cfg(allowed_user_ids=frozenset(), notify_chat_ids=frozenset()),
        "start",
    )
    assert sent == 0, "без получателей отправлять некуда"
    assert not bot.sent
    print("  OK: без получателей уведомление не падает")


async def scenario_send_failure() -> None:
    bot = FakeBot(fail=True)
    sent = await notify_lifecycle(bot, make_cfg(), "start")
    assert sent == 0, "при сбое доставки счётчик должен быть нулевым"
    print("  OK: ошибка отправки не роняет запуск бота")


async def main() -> None:
    print("Проверка уведомлений о запуске и остановке")
    for title, scenario in (
        ("[получатели]", None),
        ("[запуск]", scenario_start_message),
        ("[остановка]", scenario_stop_message),
        ("[флаги выключены]", scenario_flags_off),
        ("[нет получателей]", scenario_no_recipients),
        ("[сбой отправки]", scenario_send_failure),
    ):
        print(f"\n{title}")
        if scenario is None:
            scenario_targets()
        else:
            await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
