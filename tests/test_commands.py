"""Проверка команд управления SD-мониторингом (/sd_*) без Telegram.

Обработчики достаются из Dispatcher и вызываются с фейковым сообщением.

Запуск:  python tests/test_commands.py
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aiogram import Dispatcher  # noqa: E402

import sd_commands as sd_commands_module  # noqa: E402
from access import AccessMiddleware  # noqa: E402
from config import Config  # noqa: E402
from sd_client import SDRequest  # noqa: E402
from sd_commands import pack_blocks, register_sd_commands  # noqa: E402
from sd_sla import resolve_timezone  # noqa: E402


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
        self._paused_by_failures = False
        self._failures = 0
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
        status["paused_by_failures"] = self._paused_by_failures
        status["failures"] = self._failures
        return status

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._paused_by_failures = False
        self._failures = 0

    @property
    def is_paused_by_failures(self) -> bool:
        return self._paused_by_failures

    def get_recent_notifications(self, limit: int = 10) -> list[str]:
        self.last_limit = limit
        return self._recent[-limit:]

    async def check_now(self):
        self.check_calls += 1

        class Result:
            duration_ms = 42
            new_requests: list = []
            errors: list = []
            total_count = 34
            filter_changed = False
            sla_warned = 0
            sla_expired = 0

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

    await handlers["cmd_sd_status"](message)
    assert "(вручную)" in message.last, f"ручная пауза не отмечена: {message.last}"
    print("  OK: /sd_status различает ручную паузу")

    await handlers["cmd_sd_resume"](message)
    assert watcher._paused is False, "мониторинг не возобновился"
    print("  OK: /sd_resume возобновляет мониторинг")


async def scenario_permissions() -> None:
    """Доступ отсекает AccessMiddleware, а не каждый обработчик /sd_*."""
    cfg = make_cfg()
    middleware = AccessMiddleware(cfg.allowed_user_ids)
    called: list[FakeMessage] = []

    async def handler(event: object, _data: dict) -> None:
        called.append(event)  # type: ignore[arg-type]

    stranger = FakeMessage(user_id=555)
    await middleware(handler, stranger, {})  # type: ignore[arg-type]
    assert not called, "посторонний прошёл через AccessMiddleware"
    print("  OK: посторонний не проходит через AccessMiddleware")

    allowed = FakeMessage(user_id=1001)
    await middleware(handler, allowed, {})  # type: ignore[arg-type]
    assert called == [allowed], "разрешённый пользователь не прошёл"
    print("  OK: разрешённый пользователь проходит через AccessMiddleware")


async def scenario_autopause_status_and_resume() -> None:
    """/sd_status показывает автопаузу, /sd_resume её снимает."""
    watcher = FakeWatcher()
    watcher._paused = True
    watcher._paused_by_failures = True
    watcher._failures = 2
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_status"](message)
    assert "Пауза: ✅ да (авто: 2 неудач подряд)" in message.last, (
        f"нет пометки автопаузы: {message.last}"
    )

    await handlers["cmd_sd_resume"](message)
    assert "Автопауза после сбоев снята" in message.last, (
        f"нет сообщения о снятии автопаузы: {message.last}"
    )
    assert watcher._paused is False
    assert watcher._paused_by_failures is False
    print("  OK: /sd_status и /sd_resume знают про автопаузу")


async def scenario_watcher_missing() -> None:
    """Неполные настройки SD: watcher=None, команды не падают."""
    handlers = handlers_map(make_cfg(), None)  # type: ignore[arg-type]
    names = (
        "cmd_sd_status",
        "cmd_sd_test",
        "cmd_sd_last",
        "cmd_sd_pause",
        "cmd_sd_resume",
        "cmd_sd_count",
        "cmd_sd_dump",
        "cmd_sd_today",
    )
    for name in names:
        message = FakeMessage(user_id=1001)
        await handlers[name](message)
        assert "не запущен" in message.last.lower(), (
            f"{name}: нет сообщения о неполных настройках: {message.last}"
        )
    print("  OK: при watcher=None все /sd_* объясняют, что мониторинг не запущен")


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
    assert "Всего по фильтру: 34" in message.last, f"нет размера выборки: {message.last}"
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


async def scenario_dump_empty() -> None:
    """/sd_dump: пустой фильтр."""
    watcher = FakeWatcher()
    watcher.client = FakeClient(items=[], total=0)
    handlers = handlers_map(make_cfg(), watcher)

    message = FakeMessage(user_id=1001)
    await handlers["cmd_sd_dump"](message)
    assert "нет заявок" in message.last.lower(), f"пустой фильтр: {message.last}"
    print("  OK: пустой фильтр обрабатывается")


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


def make_deadline_request(index: int, deadline: datetime | None) -> SDRequest:
    return SDRequest(
        sys_id=f"{index:032x}",
        number=f"REQ{4300000 + index}",
        short_description=f"SUTSPROD-{17350000 + index}",
        assigned_user="Иванов И.И.",
        sys_created_at="01.11.2026 09:00",
        state="В работе",
        multicard_comment_ing="",
        multicard_request_type="Поддержка касс",
        multicard_name_client="produkty",
        multicard_terminal_address="Нижний Новгород, пер Камчатский, 7",
        multicard_deadline=(
            deadline.strftime("%d.%m.%Y %H:%M") if deadline is not None else None
        ),
        deadline_at=deadline,
    ).with_base_url("https://sd.example.ru")


async def scenario_sd_today() -> None:
    """/sd_today: только сегодняшние дедлайны, по порядку, со ссылками."""
    tz = resolve_timezone("Europe/Moscow")
    fixed = datetime(2026, 11, 1, 12, 0, tzinfo=tz)
    original = sd_commands_module.now_in
    sd_commands_module.now_in = lambda _tz: fixed

    try:
        items = [
            make_deadline_request(1, datetime(2026, 11, 1, 18, 0)),
            make_deadline_request(2, datetime(2026, 11, 1, 10, 0)),
            make_deadline_request(3, datetime(2026, 11, 2, 9, 0)),
        ]
        watcher = FakeWatcher()
        watcher.client = FakeClient(items=items, total=3)
        handlers = handlers_map(make_cfg(), watcher)

        message = FakeMessage(user_id=1001)
        await handlers["cmd_sd_today"](message)

        assert any("Смотрю дедлайны" in answer for answer in message.answers), (
            f"нет промежуточного сообщения: {message.answers}"
        )
        text = "\n\n".join(message.answers[1:])
        assert "Сгорят сегодня, 01.11.2026 — 2" in text, f"заголовок:\n{text}"
        assert "уже сгорели: 1" in text, f"нет пометки о сгоревших:\n{text}"
        assert "Дедлайн СУТС: 01.11.2026 18:00" in text
        assert "Дедлайн СУТС: 01.11.2026 10:00" in text
        assert "REQ4300003" not in text, "завтрашняя заявка попала в отчёт"
        assert text.index("10:00") < text.index("18:00"), "сортировка по времени сломана"
        assert "Тип:" not in text and "Поддержка касс" in text, "тип заявки не показан"
        assert "produkty" in text and "пер Камчатский" in text
        assert "record/itsm_request/" in text, "нет ссылок на заявки"
        print("  OK: /sd_today показывает только сегодняшние сроки по порядку")
    finally:
        sd_commands_module.now_in = original

    empty = FakeWatcher()
    empty.client = FakeClient(items=[], total=0)
    handlers = handlers_map(make_cfg(), empty)
    message = FakeMessage(user_id=1001)
    await handlers["cmd_sd_today"](message)
    text = "\n\n".join(message.answers[1:])
    assert "— 0" in text and "ничего не сгорает" in text, f"пустой отчёт:\n{text}"
    print("  OK: пустой /sd_today отвечает понятно")

    disabled = handlers_map(make_cfg(enabled=False), FakeWatcher())
    message = FakeMessage(user_id=1001)
    await disabled["cmd_sd_today"](message)
    assert "отключен" in message.last.lower(), f"выключенный SD: {message.last}"
    print("  OK: при выключенном SD /sd_today сообщает об отключении")


async def scenario_pack_blocks() -> None:
    """Карточки не разрываются при нарезке на сообщения."""
    blocks = ["x" * 100, "y" * 100, "z" * 100]
    parts = pack_blocks(blocks, limit=250)

    assert len(parts) == 2, f"ожидали 2 сообщения, получили {len(parts)}"
    assert all(len(part) <= 250 for part in parts), "лимит нарушен"
    assert parts[0] == "x" * 100 + "\n\n" + "y" * 100, "карточки склеены неверно"
    assert parts[1] == "z" * 100, "блок разорван"
    assert pack_blocks(["короткий"], limit=250) == ["короткий"]
    print("  OK: pack_blocks режет по границам карточек")


async def scenario_status_sla() -> None:
    """В /sd_status видно состояние SLA-контроля."""
    status = {
        "running": True,
        "paused": False,
        "interval_sec": 60,
        "last_check": "01.11.2026 12:00:00",
        "seen_count": 25,
        "total_sent": 25,
        "last_error": "нет",
        "last_error_time": "—",
        "last_result": {
            "checked_at": "01.11.2026 12:00:00",
            "new_count": 0,
            "seeded": 0,
            "errors": [],
            "duration_ms": 42,
            "sla_warned": 0,
            "sla_expired": 0,
            "total": 34,
            "filter_changed": False,
        },
        "sla": {
            "enabled": True,
            "warn_hours": 2.0,
            "initialized": True,
            "tracked": 7,
            "warn_sent": 3,
            "expired_sent": 2,
        },
    }
    watcher = FakeWatcher(status=status)
    handlers = handlers_map(make_cfg(), watcher)
    message = FakeMessage(user_id=1001)

    await handlers["cmd_sd_status"](message)
    text = message.last

    assert "SLA: ✅ вкл" in text, f"нет статуса SLA:\n{text}"
    assert "порог 2 ч" in text
    assert "Предупреждений: 3" in text and "сгорело: 2" in text
    assert "Заявок по фильтру: 34" in text, f"нет размера выборки:\n{text}"
    print("  OK: /sd_status показывает порог, счётчики SLA и размер фильтра")


async def main() -> None:
    print("Проверка команд /sd_*")
    for title, scenario in (
        ("[статус и пауза]", scenario_status_and_pause),
        ("[права]", scenario_permissions),
        ("[автопауза в статусе]", scenario_autopause_status_and_resume),
        ("[неполные настройки SD]", scenario_watcher_missing),
        ("[мониторинг выключен]", scenario_disabled),
        ("[лимит в /sd_last]", scenario_last_limit),
        ("[/sd_test]", scenario_test_command),
        ("[спецсимволы в статусе]", scenario_status_escaping),
        ("[/sd_count без лишнего логина]", scenario_count_reuses_session),
        ("[/sd_dump: все заявки]", scenario_dump_sends_all),
        ("[/sd_dump: пусто]", scenario_dump_empty),
        ("[разрезание сообщений]", scenario_split_message),
        ("[/sd_today]", scenario_sd_today),
        ("[нарезка карточек]", scenario_pack_blocks),
        ("[SLA в /sd_status]", scenario_status_sla),
    ):
        print(f"\n{title}")
        await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
