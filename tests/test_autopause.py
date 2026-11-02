"""Автопауза SD-мониторинга после серии неудачных проверок.

Проверяем без сети и без Telegram: бот и клиент SD — фейковые.

Запуск:  python tests/test_autopause.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sd_watcher as sd_watcher_module  # noqa: E402
from config import Config  # noqa: E402
from sd_client import SDClient  # noqa: E402
from sd_watcher import SDWatcher, WatchResult  # noqa: E402

AUTH_ERROR = "RuntimeError: Неверный логин или пароль SD (SD_LOGIN/SD_PASSWORD)"
NETWORK_ERROR = (
    "RuntimeError: SD недоступен: таймаут соединения (SD не ответил за 30 с)"
)


class FakeBot:
    """Считает отправленные сообщения вместо реального Telegram."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.kwargs: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append((chat_id, text))
        self.kwargs.append(kwargs)

    @property
    def texts(self) -> list[str]:
        return [text for _chat_id, text in self.sent]


class FakeClient:
    """Фейковый SD-клиент: считает вызовы fetch_requests."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def fetch_requests(self, view: str, condition: str, limit: int = 100):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return [], 0


class FakeState:
    """Минимум состояния, который читает _run_loop."""

    def __init__(self) -> None:
        self.last_check = 0.0


class FakeStateStore:
    """Считает record_error/clear_error — для регресса «ошибки не чистим»."""

    def __init__(self) -> None:
        self.state = FakeState()
        self.errors: list[str] = []
        self.clears = 0

    def record_error(self, message: str) -> None:
        self.errors.append(message)

    def clear_error(self) -> None:
        self.clears += 1


class StopLoop(Exception):
    """Останавливает _run_loop в тесте после заданного числа sleep."""


class FakeAuthResponse:
    """Ответ SD на POST /v1/auth/login."""

    def __init__(self, status: int = 401) -> None:
        self.status = status

    async def __aenter__(self) -> "FakeAuthResponse":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def text(self) -> str:
        return ""


class FakeAuthSession:
    """Считает POST-логины; умеет отвечать 401 или падать по таймауту."""

    def __init__(self, error: Exception | None = None) -> None:
        self.closed = False
        self.error = error
        self.posts = 0

    def post(self, _url: str, **_kwargs: object) -> FakeAuthResponse:
        self.posts += 1
        if self.error is not None:
            raise self.error
        return FakeAuthResponse(401)

    def get(self, _url: str, **_kwargs: object) -> FakeAuthResponse:
        return FakeAuthResponse(500)


def make_watcher(
    bot: FakeBot,
    client: FakeClient | None = None,
    store: FakeStateStore | None = None,
    **overrides: object,
) -> SDWatcher:
    cfg = Config(
        bot_token="1:x",
        allowed_user_ids=frozenset({1}),
        data_dir=ROOT,
        sd_enabled=True,
        sd_check_interval=60,
        sd_view="Default",
        sd_condition="state=1",
        sd_max_failures=2,
        **overrides,  # type: ignore[arg-type]
    )
    return SDWatcher(cfg, bot, store or FakeStateStore(), client or FakeClient())


async def run_loop_briefly(watcher: SDWatcher, stop_after: int) -> None:
    """Запускает _run_loop с подменённым sleep и обрывает после stop_after."""
    original_sleep = sd_watcher_module.asyncio.sleep
    calls = {"n": 0}

    async def fake_sleep(_delay: float) -> None:
        calls["n"] += 1
        if calls["n"] >= stop_after:
            watcher._running = False
            raise StopLoop
        await original_sleep(0)

    sd_watcher_module.asyncio.sleep = fake_sleep  # type: ignore[assignment]
    watcher._running = True
    try:
        await watcher._run_loop()
    except StopLoop:
        pass
    finally:
        sd_watcher_module.asyncio.sleep = original_sleep  # type: ignore[assignment]
        watcher._running = False


async def scenario_two_failures_pause_once() -> None:
    """Две неудачи подряд: автопауза и ровно одно уведомление."""
    bot = FakeBot()
    watcher = make_watcher(bot)

    await watcher._on_failure([AUTH_ERROR])
    assert watcher._paused is False, "после одной неудачи паузы быть не должно"
    assert not bot.sent, "после одной неудачи уведомление не нужно"

    await watcher._on_failure([AUTH_ERROR])

    assert watcher._paused is True, "автопауза не включилась"
    assert watcher._paused_by_failures is True, "пауза не помечена как автопауза"
    assert len(bot.sent) == 1, f"ожидали одно уведомление, получили {len(bot.sent)}"
    print("  OK: 2 неудачи подряд -> автопауза и одно уведомление")


async def scenario_one_failure_no_pause() -> None:
    """Одна неудача: ни паузы, ни сообщений."""
    bot = FakeBot()
    watcher = make_watcher(bot)

    await watcher._on_failure([NETWORK_ERROR])

    assert watcher._paused is False
    assert watcher._paused_by_failures is False
    assert watcher._consecutive_failures == 1
    assert not bot.sent, f"уведомление без автопаузы: {bot.texts}"
    print("  OK: 1 неудача не ставит мониторинг на паузу и молчит")


async def scenario_success_resets_counter() -> None:
    """Успешная проверка обнуляет счётчик неудач."""
    watcher = make_watcher(FakeBot())
    await watcher._on_failure([AUTH_ERROR])
    assert watcher._consecutive_failures == 1

    watcher._on_success()

    assert watcher._consecutive_failures == 0, "счётчик не сброшен"
    print("  OK: успех сбрасывает счётчик неудач")


async def scenario_success_does_not_unpause() -> None:
    """Успех на автопаузе не снимает паузу — только /sd_resume."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    await watcher._on_failure([AUTH_ERROR])
    await watcher._on_failure([AUTH_ERROR])
    assert watcher._paused_by_failures is True

    watcher._on_success()

    assert watcher._paused is True, "успех сам снял автопаузу"
    assert watcher._paused_by_failures is True, "флаг автопаузы сброшен успехом"
    assert watcher._consecutive_failures == 0
    print("  OK: успех не снимает автопаузу, счётчик при этом обнулён")


async def scenario_resume_clears_autopause() -> None:
    """resume() снимает автопаузу и обнуляет счётчик."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    await watcher._on_failure([AUTH_ERROR])
    await watcher._on_failure([AUTH_ERROR])

    watcher.resume()

    assert watcher._paused is False
    assert watcher._paused_by_failures is False
    assert watcher._consecutive_failures == 0
    print("  OK: resume() снимает автопаузу и обнуляет счётчик")


async def scenario_repeated_failures_single_message() -> None:
    """Повторные неудачи на паузе не шлют второе уведомление."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    for _ in range(5):
        await watcher._on_failure([NETWORK_ERROR])

    assert len(bot.sent) == 1, f"уведомлений: {len(bot.sent)}"
    print("  OK: повторные неудачи на паузе не дублируют уведомление")


async def scenario_manual_pause_not_autopause() -> None:
    """Ручная пауза: сбои не превращают её в автопаузу и не копятся."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    watcher.pause()

    await watcher._on_failure([AUTH_ERROR])
    await watcher._on_failure([AUTH_ERROR])

    assert watcher._paused is True
    assert watcher._paused_by_failures is False, "ручная пауза стала автопаузой"
    assert watcher._consecutive_failures == 0
    assert not bot.sent, f"уведомление при ручной паузе: {bot.texts}"
    print("  OK: ручная пауза не превращается в автопаузу")


async def scenario_loop_paused_skips_sd() -> None:
    """На паузе _run_loop не вызывает _check_once (клиент не дёргается)."""
    bot = FakeBot()
    client = FakeClient(error=RuntimeError("SD недоступен"))
    watcher = make_watcher(bot, client)
    watcher.pause()

    await run_loop_briefly(watcher, stop_after=2)

    assert client.calls == 0, f"на паузе было {client.calls} запросов к SD"
    assert watcher._consecutive_failures == 0
    assert watcher._paused_by_failures is False
    print("  OK: на паузе цикл не ходит в SD")


async def scenario_check_now_on_pause() -> None:
    """check_now() на паузе возвращает ошибку и не ходит в SD."""
    bot = FakeBot()
    client = FakeClient()
    watcher = make_watcher(bot, client)
    watcher.pause()

    result = await watcher.check_now()

    assert isinstance(result, WatchResult)
    assert result.errors, "нет сообщения о паузе"
    assert "на паузе" in result.errors[0].lower(), result.errors
    assert client.calls == 0, f"было {client.calls} запросов к SD на паузе"
    print("  OK: check_now() на паузе не ходит в SD и объясняет причину")


async def scenario_clear_error_not_called_on_errors() -> None:
    """Регресс: при errors нельзя вызывать clear_error()."""
    bot = FakeBot()
    store = FakeStateStore()
    watcher = make_watcher(bot, FakeClient(), store)

    async def failing_check() -> WatchResult:
        return WatchResult(
            checked_at=time.time(),
            new_requests=[],
            errors=["RuntimeError: boom"],
            duration_ms=1,
        )

    watcher._check_once = failing_check  # type: ignore[method-assign]
    await run_loop_briefly(watcher, stop_after=4)

    assert store.clears == 0, f"clear_error() вызван {store.clears} раз при ошибках"
    assert store.errors, "record_error() не вызван"
    assert watcher._paused_by_failures is True
    assert len(bot.sent) == 1
    print("  OK: при ошибках ошибка записывается, а не стирается")


async def scenario_two_auth_failures_two_logins() -> None:
    """Неверный пароль: до автопаузы ровно 2 POST /v1/auth/login."""
    bot = FakeBot()
    store = FakeStateStore()
    cfg = Config(
        bot_token="1:x",
        allowed_user_ids=frozenset({1}),
        data_dir=ROOT,
        sd_enabled=True,
        sd_base_url="https://sd.example.ru",
        sd_login="ivanov",
        sd_password="wrong",
        sd_condition="state=1",
        sd_auth_method="simpleone",
        sd_max_failures=2,
    )
    client = SDClient(cfg)
    session = FakeAuthSession()
    client.session = session  # type: ignore[assignment]
    watcher = SDWatcher(cfg, bot, store, client)

    for _ in range(2):
        result = await watcher._check_once()
        assert result.errors, "проверка должна завершиться ошибкой"
        await watcher._on_failure(result.errors)

    assert session.posts == 2, f"логинов до автопаузы: {session.posts}"
    assert watcher._paused_by_failures is True
    assert len(bot.sent) == 1
    assert "Неверный логин или пароль" in bot.texts[0]
    print("  OK: неверный пароль -> 2 логина и одна автопауза")


async def scenario_two_network_failures_two_logins() -> None:
    """SD не отвечает: до автопаузы ровно 2 попытки входа."""
    bot = FakeBot()
    store = FakeStateStore()
    cfg = Config(
        bot_token="1:x",
        allowed_user_ids=frozenset({1}),
        data_dir=ROOT,
        sd_enabled=True,
        sd_base_url="https://sd.example.ru",
        sd_login="ivanov",
        sd_password="secret",
        sd_condition="state=1",
        sd_auth_method="simpleone",
        sd_max_failures=2,
    )
    client = SDClient(cfg)
    session = FakeAuthSession(error=asyncio.TimeoutError())
    client.session = session  # type: ignore[assignment]
    watcher = SDWatcher(cfg, bot, store, client)

    for _ in range(2):
        result = await watcher._check_once()
        assert result.errors, "проверка должна завершиться ошибкой"
        await watcher._on_failure(result.errors)

    assert session.posts == 2, f"попыток входа до автопаузы: {session.posts}"
    assert watcher._paused_by_failures is True
    assert len(bot.sent) == 1
    assert "SD недоступен:" in bot.texts[0]
    assert "Проверь доступность SD (сеть" in bot.texts[0]
    print("  OK: SD не отвечает -> 2 попытки входа и одна автопауза")


async def scenario_notification_text_auth() -> None:
    """Неверный пароль: подсказка про SD_LOGIN/SD_PASSWORD, без сети."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    watcher._consecutive_failures = 2

    await watcher._notify_autopause([AUTH_ERROR])

    text = bot.texts[0]
    assert "SD_LOGIN/SD_PASSWORD" in text, text
    assert "Проверь доступность SD (сеть" not in text, text
    print("  OK: при неверном пароле подсказка про учётные данные")


async def scenario_notification_text_network() -> None:
    """Сеть/TLS: подсказка про доступность SD, без совета менять пароль."""
    bot = FakeBot()
    watcher = make_watcher(bot)
    watcher._consecutive_failures = 2

    await watcher._notify_autopause([NETWORK_ERROR])

    text = bot.texts[0]
    assert "Проверь доступность SD (сеть" in text, text
    assert "Если пароль меняли" not in text, text
    print("  OK: при сбое сети подсказка про доступность SD")


async def main() -> None:
    print("Проверка автопаузы SD-мониторинга")
    for title, scenario in (
        ("[две неудачи]", scenario_two_failures_pause_once),
        ("[одна неудача]", scenario_one_failure_no_pause),
        ("[успех сбрасывает счётчик]", scenario_success_resets_counter),
        ("[успех не снимает паузу]", scenario_success_does_not_unpause),
        ("[resume]", scenario_resume_clears_autopause),
        ("[без дублей]", scenario_repeated_failures_single_message),
        ("[ручная пауза]", scenario_manual_pause_not_autopause),
        ("[цикл на паузе]", scenario_loop_paused_skips_sd),
        ("[check_now на паузе]", scenario_check_now_on_pause),
        ("[ошибки не стираются]", scenario_clear_error_not_called_on_errors),
        ("[2 логина: пароль]", scenario_two_auth_failures_two_logins),
        ("[2 логина: сеть]", scenario_two_network_failures_two_logins),
        ("[текст: пароль]", scenario_notification_text_auth),
        ("[текст: сеть]", scenario_notification_text_network),
    ):
        print(f"\n{title}")
        await scenario()
    print("\nВсе проверки пройдены.")


if __name__ == "__main__":
    asyncio.run(main())
