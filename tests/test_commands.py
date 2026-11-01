"""Проверка команд управления SD-мониторингом (/sd_*) без Telegram.

Обработчики достаются из Dispatcher и вызываются с фейковым сообщением.

Запуск:  python tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Dispatcher  # noqa: E402

from config import Config  # noqa: E402
from sd_commands import register_sd_commands  # noqa: E402


class FakeUser:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class FakeMessage:
    """Запоминает ответы вместо отправки в Telegram."""

    def __init__(self, text: str = "", user_id: int = 1001) -> None:
        self.text = text
        self.from_user = FakeUser(user_id)
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs: object) -> None:
        self.answers.append(text)

    @property
    def last(self) -> str:
        return self.answers[-1] if self.answers else ""


class FakeWatcher:
    def __init__(self, status: dict | None = None, recent: list[str] | None = None) -> None:
        self._paused = False
        self._status = status or {
            "running": True,
            "paused": False,
            "interval_sec": 60,
            "last_check": "01.11.2026 00:48:34",
            "seen_count": 25,
            "total_sent": 25,
            "last_error": "нет",
            "last_error_time": "—",
            "last_result": None,
        }
        self._recent = recent or []
        self.check_calls = 0
        self.client = FakeClient()

    def get_status(self) -> dict:
        status = dict(self._status)
        status["paused"] = self._paused
        return status

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def get_recent_notifications(self, limit: int = 10) -> list[str]:
        self.last_limit = limit
        return self._recent[-limit:]

    async def check_now(self):
        self.check_calls += 1

        class Result:
            duration_ms = 42
            new_requests: list = []
            errors: list = []

        return Result()


class FakeClient:
    """Считает обращения, чтобы проверить переиспользование сессии."""

    def __init__(self, items: list | None = None, total: int = 23) -> None:
        self.calls = 0
        self._items = items or []
        self._total = total

    async def fetch_requests(self, view: str, condition: str, limit: int = 100):
        self.calls += 1
        return list(self._items), self._total


def make_cfg(enabled: bool = True, allowed: frozenset[int] = frozenset({1001})) -> Config:
    return Config(
        bot_token="1:x",
        allowed_user_ids=allowed,
        data_dir=ROOT,
        sd_enabled=enabled,
        sd_check_interval=60,
    )


def handlers_map(cfg: Config, watcher: FakeWatcher) -> dict[str, object]:
    dp = Dispatcher()
    register_sd_commands(dp, cfg, watcher)
    return {h.callback.__name__: h.callback for h in dp.message.handlers}


async def scenario_status_and_pause() -> None:
    watcher = FakeWatcher()
    handlers = handlers_map(make_cfg(), watcher)

    message = FakeMessage(user_id=1001)
    await handlers["cmd_sd_status"](message)
    text = message.last
    assert "Статус SD-мониторинга" in text, f"нет заголовка статуса: {text}"
    assert "25" in text, f"нет счётчика заявок: {text}"
    print("  OK: /sd_status показывает статус и счётчики")

    await handlers["cmd_sd_pause"](message)
    assert watcher._paused is True, "пауза не включилась"
    print("  OK: /sd_pause ставит мониторинг на паузу")

    await handlers["cmd_sd_resume"](message)
    assert watcher._paused is False, "мониторинг не возобновился"
    print("  OK: /sd_resume возобновляет мониторинг")


async def scenario_permissions() -> None:
    watcher = FakeWatcher()
    handlers = handlers_map(make_cfg(), watcher)
    stranger = FakeMessage(user_id=555)

    await handlers["cmd_sd_pause"](stranger)
    assert "Нет прав" in stranger.last, f"чужой не должен ставить паузу: {stranger.last}"
    assert watcher._paused is False, "чужой всё-таки поставил паузу"
    print("  OK: чужой пользователь не управляет мониторингом")

    await handlers["cmd_sd_count"](stranger)
    assert "Нет прав" in stranger.last, f"чужой не должен запрашивать количество: {stranger.last}"
    print("  OK: чужой пользователь не может запросить количество")


async def scenario_disabled() -> None:
    watcher = FakeWatcher()
    handlers = handlers_map(make_cfg(enabled=False), watcher)
    message = FakeMessage(user_id=1001)

    for name in ("cmd_sd_status", "cmd_sd_pause", "cmd_sd_test", "cmd_sd_last", "cmd_sd_count"):
        await handlers[name](message)
        assert "отключен" in message.last.lower(), f"{name}: нет сообщения об отключении: {message.last}"
    print("  OK: при SD_ENABLED=false все команды сообщают об отключении")


async def scenario_last_limit() -> None:
    watcher = FakeWatcher(recent=["REQ1", "REQ2", "REQ3"])
    handlers = handlers_map(make_cfg(), watcher)

    cases = [("/sd_last 2", 2), ("/sd_last", 10), ("/sd_last abc", 10), ("/sd_last 0", 1), ("/sd_last 99", 50)]
    for text, expected in cases:
        message = FakeMessage(text=text, user_id=1001)
        await handlers["cmd_sd_last"](message)
        assert watcher.last_limit == expected, (
            f"«{text}»: ожидали лимит {expected}, получили {watcher.last_limit}"
        )
    print("  OK: /sd_last корректно разбирает число и ограничивает его")

    empty = handlers_map(make_cfg(), FakeWatcher())
    message = FakeMessage(text="/sd_last", user_id=1001)
    await empty["cmd_sd_last"](message)
    assert "Пока нет уведомлённых" in message.last, f"пустая история: {message.last}"
    print("  OK: пустая история уведомлений обрабатывается")


async def scenario_test_command() -> None:
    watcher = FakeWatcher()
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_test"](message)
    assert watcher.check_calls == 1, "проверка не запустилась"
    assert any("Проверяю SD" in a for a in message.answers), f"нет промежуточного сообщения: {message.answers}"
    assert "Новых заявок нет" in message.last, f"нет итога проверки: {message.last}"
    print("  OK: /sd_test запускает проверку и сообщает результат")


async def scenario_status_escaping() -> None:
    """Текст ошибки в статусе не ломает HTML-разметку сообщения."""
    status = {
        "running": True,
        "paused": False,
        "interval_sec": 60,
        "last_check": "01.11.2026 00:48:34",
        "seen_count": 25,
        "total_sent": 25,
        "last_error": "Ошибка <script>alert(1)</script> & сбой",
        "last_error_time": "01.11 00:49",
        "last_result": {
            "checked_at": "01.11 00:48",
            "new_count": 0,
            "duration_ms": 42,
            "errors": ["сбой <b>сети</b>"],
        },
    }
    watcher = FakeWatcher(status=status)
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_status"](message)
    text = message.last

    assert "&lt;script&gt;" in text, f"тег в тексте ошибки не экранирован: {text}"
    assert "&amp;" in text, f"амперсанд не экранирован: {text}"
    assert "&lt;b&gt;сети&lt;/b&gt;" in text, f"ошибка из last_result не экранирована: {text}"
    assert "<script>" not in text, f"живой тег попал в сообщение: {text}"
    assert "<b>Статус SD-мониторинга</b>" in text, f"пропала своя разметка: {text}"
    print("  OK: /sd_status экранирует тексты ошибок и сохраняет разметку")


async def scenario_count_reuses_session() -> None:
    """/sd_count пользуется сессией монитора, а не логинится заново."""
    watcher = FakeWatcher()
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_count"](message)

    assert watcher.client.calls == 1, f"клиент монитора не использован: {watcher.client.calls}"
    assert "23" in message.last, f"количество не показано: {message.last}"
    print("  OK: /sd_count переиспользует сессию монитора")


async def scenario_dump_sends_all() -> None:
    """/sd_dump присылает все заявки списком, длинный список режется по лимиту."""
    from sd_client import SDRequest
    from sd_commands import MESSAGE_LIMIT

    items = [
        SDRequest(
            sys_id=f"{i:032x}",
            number=f"REQ{i:04d}",
            short_description=f"SUTSPROD-{1000 + i}",
            assigned_user="Иванов И.И.",
            sys_created_at="31.10.2026 14:00",
            state="В работе",
            multicard_comment_ing="",
        ).with_base_url("https://sd.example.ru")
        for i in range(1, 61)
    ]
    watcher = FakeWatcher()
    watcher.client = FakeClient(items=items, total=60)
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_dump"](message)

    parts = message.answers[1:]  # первый ответ — «Загружаю...»
    assert parts, "ничего не отправлено"
    assert len(parts) > 1, f"длинный список не разрезан: {len(parts)} сообщений"
    assert all(len(part) <= 4096 for part in parts), "есть сообщение длиннее лимита Telegram"
    assert all(len(part) <= MESSAGE_LIMIT for part in parts[:-1]), "куски больше заявленного лимита"

    joined = "\n".join(parts)
    assert "SUTSPROD-1001" in joined, "первая заявка потерялась"
    assert "SUTSPROD-1060" in joined, "последняя заявка потерялась"
    assert joined.count("SUTSPROD-") == 60, f"заявок в списке: {joined.count('SUTSPROD-')}"
    assert "record/itsm_request/" in joined, "в списке нет ссылок на заявки"
    print(f"  OK: {len(items)} заявок пришли в {len(parts)} сообщениях, все со ссылками")


async def scenario_dump_empty_and_permissions() -> None:
    """/sd_dump: пустой фильтр и посторонний пользователь."""
    watcher = FakeWatcher()
    watcher.client = FakeClient(items=[], total=0)
    handlers = handlers_map(make_cfg(), watcher)

    message = FakeMessage(user_id=1001)
    await handlers["cmd_sd_dump"](message)
    assert "нет заявок" in message.last.lower(), f"пустой фильтр: {message.last}"
    print("  OK: пустой фильтр обрабатывается")

    stranger = FakeMessage(user_id=555)
    await handlers["cmd_sd_dump"](stranger)
    assert "Нет прав" in stranger.last, f"посторонний не должен получать список: {stranger.last}"
    print("  OK: посторонний не может запросить все заявки")


async def scenario_split_message() -> None:
    """Разрезание длинного текста: ничего не теряется и лимит соблюдается."""
    from sd_commands import split_message

    line = "x" * 100
    text = "\n".join(line for _ in range(100))  # 10 100 символов
    parts = split_message(text, limit=1000)
    assert len(parts) > 1, "текст не разрезан"
    assert all(len(part) <= 1000 for part in parts), "лимит нарушен"
    assert sum(part.count("x") for part in parts) == text.count("x"), "часть текста потерялась"
    assert split_message("короткий текст", limit=1000) == ["короткий текст"]
    print("  OK: длинный текст режется без потерь")


async def main() -> None:
    print("Проверка команд /sd_*")
    for title, scenario in (
        ("[статус и пауза]", scenario_status_and_pause),
        ("[права]", scenario_permissions),
        ("[мониторинг выключен]", scenario_disabled),
        ("[лимит в /sd_last]", scenario_last_limit),
        ("[/sd_test]", scenario_test_command),
        ("[спецсимволы в статусе]", scenario_status_escaping),
        ("[/sd_count без лишнего логина]", scenario_count_reuses_session),
        ("[/sd_dump: все заявки]", scenario_dump_sends_all),
        ("[/sd_dump: пусто и права]", scenario_dump_empty_and_permissions),
        ("[разрезание сообщений]", scenario_split_message),
    ):
        print(f"\n{title}")
        await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
